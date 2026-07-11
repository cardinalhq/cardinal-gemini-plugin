"""Smoke tests for the Cardinal Gemini plugin.

Runs the telemetry hook with fabricated Gemini CLI payloads for each event,
verifies non-crash behaviour, and inspects the state written under a
sandboxed ~/.gemini/ (via HOME override) — no network, no real Gemini CLI.

Run:
    python3 -m unittest tests.test_smoke -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "cardinal-gemini-telemetry.py"


def run_hook(event: str, payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(HOOK), "--event", event],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=10,
        env=env,
    )


class HookSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # Simulate "not connected" — hooks must silently no-op without state.
        (self.home / ".gemini").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_all_events_no_crash_without_state(self) -> None:
        payload = {"session_id": "s1", "cwd": str(self.home), "prompt": "hi"}
        for event in (
            "SessionStart", "BeforeAgent", "AfterModel", "AfterTool",
            "AfterAgent", "PreCompress", "SessionEnd",
        ):
            with self.subTest(event=event):
                result = run_hook(event, payload, self.home)
                self.assertEqual(result.returncode, 0, msg=result.stderr.decode())

    def test_session_start_outside_git_repo_emits_nothing(self) -> None:
        payload = {"session_id": "s1", "cwd": str(self.home)}
        result = run_hook("SessionStart", payload, self.home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"", "Should suppress convention prompt outside a git repo")

    def test_after_model_writes_progress_cursor(self) -> None:
        # Even without a connected state (no ingest post), the hook still
        # advances its per-session progress file — required so turn/tool
        # counters remain monotonic across events.
        payload = {
            "session_id": "s-test",
            "model": "gemini-2.0-flash",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_input_tokens": 10,
            },
        }
        result = run_hook("AfterModel", payload, self.home)
        self.assertEqual(result.returncode, 0)
        progress = self.home / ".gemini" / "cardinal" / "telemetry" / "s-test.json"
        self.assertTrue(progress.exists(), "AfterModel must write a progress cursor")
        state = json.loads(progress.read_text())
        self.assertEqual(state["turn_seq"], 1)

    def test_after_tool_bash_classification(self) -> None:
        payload = {
            "session_id": "s-tool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "git status"},
            "success": True,
        }
        result = run_hook("AfterTool", payload, self.home)
        self.assertEqual(result.returncode, 0)
        progress = self.home / ".gemini" / "cardinal" / "telemetry" / "s-tool.json"
        self.assertTrue(progress.exists())

    def test_after_agent_emits_on_identifying_facet_only(self) -> None:
        # No identifying facet → suppressed (avoids stray main-agent AfterAgent).
        # With subagent_type present → emit path taken (returncode 0 is enough:
        # network POST silently fails without connection state).
        for payload, should_progress in (
            ({"session_id": "s-a"}, False),
            ({"session_id": "s-a", "subagent_type": "code-reviewer"}, True),
            ({"session_id": "s-a", "description": "review the diff"}, True),
            ({"session_id": "s-a", "duration_ms": 1200}, True),
        ):
            with self.subTest(payload=payload):
                result = run_hook("AfterAgent", payload, self.home)
                self.assertEqual(result.returncode, 0, msg=result.stderr.decode())

    def test_before_agent_advances_user_turn_seq(self) -> None:
        payload = {"session_id": "s-b", "cwd": str(self.home)}
        run_hook("BeforeAgent", payload, self.home)
        run_hook("BeforeAgent", payload, self.home)
        progress = self.home / ".gemini" / "cardinal" / "telemetry" / "s-b.json"
        self.assertTrue(progress.exists())
        state = json.loads(progress.read_text())
        self.assertEqual(state["user_turn_seq"], 2)
        # turn_seq / tool_seq reset each user turn.
        self.assertEqual(state["turn_seq"], 0)
        self.assertEqual(state["tool_seq"], 0)


class PricingTests(unittest.TestCase):
    """Sanity checks on the Gemini pricing table."""

    def test_price_lookup_exact_and_prefix(self) -> None:
        sys.path.insert(0, str(ROOT / "hooks"))
        try:
            import importlib
            mod = importlib.import_module(HOOK.stem.replace("-", "_"))
        except ImportError:
            # The hook file is `cardinal-gemini-telemetry.py` which is not a
            # valid module name; load it via spec instead.
            import importlib.util
            spec = importlib.util.spec_from_file_location("gtel", HOOK)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader
            spec.loader.exec_module(mod)

        self.assertIsNotNone(mod.price_for_model("gemini-2.0-flash"))
        self.assertIsNotNone(mod.price_for_model("gemini-2.0-pro-2026-03-01"),
                             "longest-prefix fallback should price dated SKUs")
        self.assertIsNone(mod.price_for_model("gpt-5"),
                          "non-gemini model should be unpriced")

    def test_compute_cost_with_thought_tokens(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("gtel", HOOK)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)

        # gemini-2.0-flash: input $0.10 / cached $0.025 / output $0.40 per 1M
        # 1M input (200k cached) + 500k output + 100k thought (bills as output)
        cost = mod.compute_cost_usd("gemini-2.0-flash", {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 200_000,
            "output_tokens": 500_000,
            "thought_tokens": 100_000,
        })
        expected = (800_000 * 0.10 + 200_000 * 0.025 + 600_000 * 0.40) / 1_000_000
        self.assertAlmostEqual(cost, round(expected, 6), places=6)


class BashClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("gtel", HOOK)
        self.mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(self.mod)

    def test_single_verb(self) -> None:
        self.assertEqual(self.mod.classify_bash_command("git status"), ("git-read", False))
        self.assertEqual(self.mod.classify_bash_command("rm -rf foo"), ("file-write", False))
        self.assertEqual(self.mod.classify_bash_command("git checkout -b feat/x"), ("git-write", False))

    def test_write_risk_wins_on_compound(self) -> None:
        # ls (file-read) + rm (file-write) → file-write wins, multi flag set.
        self.assertEqual(
            self.mod.classify_bash_command("ls && rm foo"),
            ("file-write", True),
        )


if __name__ == "__main__":
    unittest.main()
