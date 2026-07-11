#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from Gemini CLI hooks.

Gemini CLI emits per-model-call and per-tool-call hook events directly
(unlike Codex which required transcript-JSONL scraping), so this hook
normalizes each event payload into the existing Cardinal/Lakerunner event
contract and POSTs OTLP/HTTP logs. Failures are best-effort and silent:
telemetry must not break the agent loop.

Event dispatch (see docs/specs/gemini-parity.md for the full mapping):

  SessionStart  → convention prompt + budget standing (additionalContext)
  BeforeAgent   → spend-limits gate + cardinal.git_state + verdict refresh
  AfterModel    → api_request + cardinal.turn_usage (per model call)
  AfterTool     → cardinal.turn_tool + tool_result (per tool call)
  AfterAgent    → cardinal.subagent_usage
  PreCompress   → cardinal.plan_usage (context-window slice)
  SessionEnd    → best-effort progress-file cleanup
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plugin_version  # noqa: E402


PLUGIN_VERSION = _plugin_version.plugin_version()
HOOK_TIMEOUT_SEC = 2.0

# plan_usage cadence (mirrors Claude / Codex 10-min throttle). Gemini has
# per-call rate-limit info surfaced elsewhere; this throttle applies to
# any plan-usage emissions we synthesize from AfterModel payloads.
PLAN_USAGE_TTL_SEC = 10 * 60

GEMINI_DIR = Path.home() / ".gemini"
STATE_PATH = GEMINI_DIR / "cardinal.json"
SECRETS_PATH = GEMINI_DIR / "cardinal-secrets.json"
TELEMETRY_DIR = GEMINI_DIR / "cardinal" / "telemetry"
# Last-seen plan facts (plan_type + rate_limit_tier), global across
# sessions — Gemini analogue of the Codex plan cache. Written whenever
# a hook payload surfaces plan/tier info; read by every handler to stamp
# the two keys onto emitted records.
PLAN_STAMP_PATH = TELEMETRY_DIR / "plan.json"
DEBUG_PAYLOADS_ENV = "CARDINAL_GEMINI_DEBUG_PAYLOADS"
DEBUG_DIR = TELEMETRY_DIR / "debug"

TARGET_KEYS = {
    "read_file": "path",
    "write_file": "file_path",
    "edit": "file_path",
    "replace": "file_path",
    "read_many_files": "path",
    # Claude-style tool names sometimes appear via MCP passthrough:
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
}

REMOTE_URL_RE = re.compile(r"(?:git@|https?://)([^:/]+)[:/]([^/]+)/(.+?)(?:\.git)?/?$")
SESSION_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "trunk"})

# Noise words that appear between `worktree-` and the real name in
# EnterWorktree-style branches. Kept in lockstep with the Claude / Codex
# plugins (docs/specs/gemini-parity.md §Keeping the repos in lockstep).
WORKTREE_NOISE = frozenset({
    "fix", "feat", "bug", "bugfix", "issue", "issues", "pr",
})
NUMERIC_SEGMENT_RE = re.compile(r"^\d+$")
PREFIX_TO_TYPE = {
    "feat": "feature",
    "feature": "feature",
    "perf": "feature",
    "fix": "bugfix",
    "bugfix": "bugfix",
    "refactor": "refactor",
    "cleanup": "refactor",
    "infra": "infra",
    "chore": "infra",
    "test": "infra",
    "tests": "infra",
    "ci": "infra",
    "build": "infra",
    "deps": "infra",
    "docs": "infra",
    "doc": "infra",
    "research": "research",
    "spike": "research",
}

# USD per 1M tokens per Google's public pricing (Vertex AI + AI Studio).
# `thought_tokens` (reasoning tokens) bill as output per Google's pricing
# page. Longest-prefix fallback so dated SKUs (e.g.
# `gemini-2.0-pro-2026-03-01`) still price correctly.
MODEL_PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "gemini-2.0-pro":         {"input": 1.25, "cached_input": 0.3125, "output": 10.00},
    "gemini-2.0-flash":       {"input": 0.10, "cached_input": 0.025,  "output":  0.40},
    "gemini-2.0-flash-lite":  {"input": 0.075,"cached_input": 0.01875,"output":  0.30},
    "gemini-1.5-pro":         {"input": 1.25, "cached_input": 0.3125, "output":  5.00},
    "gemini-1.5-flash":       {"input": 0.075,"cached_input": 0.01875,"output":  0.30},
    "gemini-1.5-flash-8b":    {"input": 0.0375,"cached_input":0.009375,"output":0.15},
}


def price_for_model(model: str | None) -> dict[str, float] | None:
    if not model:
        return None
    if model in MODEL_PRICING_USD_PER_M:
        return MODEL_PRICING_USD_PER_M[model]
    match = ""
    for key in MODEL_PRICING_USD_PER_M:
        if model.startswith(key) and len(key) > len(match):
            match = key
    return MODEL_PRICING_USD_PER_M.get(match) if match else None


def compute_cost_usd(model: str | None, usage: dict[str, Any]) -> float | None:
    """Return the USD cost for one Gemini api_request or None if the model
    isn't priced. Follows Google's billing semantics: `input_tokens` is the
    total input count, `cached_input_tokens` is a subset that bills at
    the cached rate, `thought_tokens` (reasoning) bill as output. Returning
    None (vs 0.0) skips the attribute so unpriced models don't accumulate
    misleading zero rows in lakerunner."""
    price = price_for_model(model)
    if price is None:
        return None
    input_total = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    thought = int(usage.get("thought_tokens") or 0)
    non_cached_input = max(0, input_total - cached)
    cost = (
        non_cached_input * price["input"]
        + cached          * price["cached_input"]
        + (output + thought) * price["output"]
    ) / 1_000_000.0
    return round(cost, 6)


def silent_exit() -> None:
    sys.exit(0)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def safe_session(session_id: str) -> str:
    return SESSION_SAFE_RE.sub("_", session_id)[:128]


def progress_path(session_id: str) -> Path:
    return TELEMETRY_DIR / f"{safe_session(session_id)}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def kv(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def parse_ts_ns(raw: Any, fallback_ns: int) -> int:
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            return fallback_ns
    if isinstance(raw, (int, float)) and raw > 0:
        # Gemini sometimes emits epoch millis
        return int(raw * 1_000_000) if raw < 1e13 else int(raw)
    return fallback_ns


def session_id_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "sessionID"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    value = os.environ.get("GEMINI_SESSION_ID")
    if value:
        return value
    return None


def load_connection() -> tuple[dict[str, Any], dict[str, Any]] | None:
    state = read_json(STATE_PATH)
    secrets = read_json(SECRETS_PATH)
    endpoint = state.get("ingest_endpoint")
    api_key = secrets.get("ingest_api_key")
    if not endpoint or not api_key:
        return None
    return state, secrets


def resource_attrs(state: dict[str, Any]) -> dict[str, str]:
    return {
        "service.name": "gemini-cli",
        "agent.runtime": "gemini",
        "deployment.environment": str(state.get("deployment_environment") or "unknown"),
        "user.email": str(state.get("user_email") or "unknown"),
        "cardinal.org": str(state.get("org_slug") or state.get("org_id") or "unknown"),
        "cardinal.plugin_version": PLUGIN_VERSION,
    }


def emit_records(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    conn = load_connection()
    if not conn:
        return
    state, secrets = conn
    endpoint = str(state.get("ingest_endpoint")).rstrip("/")
    api_header = str(secrets.get("ingest_api_header") or "x-cardinalhq-api-key")
    api_key = str(secrets.get("ingest_api_key"))

    body = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [kv(k, v) for k, v in resource_attrs(state).items()],
                },
                "scopeLogs": [
                    {
                        "scope": {
                            "name": "cardinal-gemini-plugin",
                            "version": PLUGIN_VERSION,
                        },
                        "logRecords": records,
                    }
                ],
            }
        ]
    }
    req = urllib.request.Request(
        endpoint + "/v1/logs",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            api_header: api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HOOK_TIMEOUT_SEC):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def log_record(event_name: str, attrs: dict[str, Any], ts_ns: int) -> dict[str, Any]:
    all_attrs = {"event_name": event_name, **attrs}
    return {
        "timeUnixNano": str(ts_ns),
        "observedTimeUnixNano": str(ts_ns),
        "severityNumber": 9,
        "severityText": "INFO",
        "body": {"stringValue": event_name},
        "attributes": [kv(k, v) for k, v in all_attrs.items() if v is not None and v != ""],
    }


def git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def canonical_repo(remote_url: str | None) -> str | None:
    if not remote_url:
        return None
    m = REMOTE_URL_RE.match(remote_url.strip())
    if not m:
        return None
    name = re.sub(r"\.git$", "", m.group(3))
    return f"{m.group(2)}/{name}" if m.group(2) and name else None


def strip_worktree_noise(name: str) -> str:
    """worktree-fix-1018-github-app-repo-picker → github-app-repo-picker.
    Non-worktree names pass through verbatim; if nothing real remains after
    the head, keep the original."""
    if not name.startswith("worktree-"):
        return name
    segs = name.split("-")
    i = 1
    while i < len(segs) and (
        segs[i] in WORKTREE_NOISE or NUMERIC_SEGMENT_RE.match(segs[i])
    ):
        i += 1
    if i < len(segs):
        return "-".join(segs[i:])
    return name


def resolve_initiative(branch: str | None) -> tuple[str | None, str]:
    if not branch or branch == "HEAD":
        return None, "research"
    if branch in PROTECTED_BRANCHES:
        return None, "research"
    if "/" in branch:
        prefix, _, rest = branch.partition("/")
        mapped = PREFIX_TO_TYPE.get(prefix.lower())
        if mapped and rest:
            return strip_worktree_noise(rest), mapped
    return strip_worktree_noise(branch), "feature"


COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9][\w:-]*)")
COMMAND_TAG_RE = re.compile(r"<command-name>\s*/?([\w:-]+)\s*</command-name>")


def detect_command(prompt: Any) -> str | None:
    """'/code-review --fix' → 'code-review'. Accepts the raw typed form
    (anchored at start) and the expanded <command-name> tag form."""
    if not isinstance(prompt, str):
        return None
    m = COMMAND_RE.match(prompt)
    if m:
        return m.group(1)
    m = COMMAND_TAG_RE.search(prompt)
    if m:
        return m.group(1)
    return None


def read_plan_stamp() -> dict[str, Any]:
    blob = read_json(PLAN_STAMP_PATH)
    out: dict[str, Any] = {}
    for key in ("plan_type", "rate_limit_tier"):
        v = blob.get(key)
        if isinstance(v, str) and v:
            out[key] = v
    return out


def dump_debug_payload(event: str, payload: dict[str, Any]) -> None:
    """Env-gated raw hook-payload dump for shape capture. A no-op unless
    CARDINAL_GEMINI_DEBUG_PAYLOADS=1; best-effort like everything else."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = DEBUG_DIR / f"{event}-{time.time_ns()}.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _limits():
    try:
        import _limits_common as lc
        return lc
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Per-session progress cursor (turn/tool sequence counters)
# ---------------------------------------------------------------------------

def load_progress(session_id: str) -> dict[str, Any]:
    p = read_json(progress_path(session_id))
    return {
        "user_turn_seq": int(p.get("user_turn_seq") or 0),
        "turn_seq": int(p.get("turn_seq") or 0),
        "tool_seq": int(p.get("tool_seq") or 0),
        "plan_state_sig": p.get("plan_state_sig"),
        "plan_usage_emitted_at": p.get("plan_usage_emitted_at"),
        "plan_stamp": p.get("plan_stamp") if isinstance(p.get("plan_stamp"), dict) else read_plan_stamp(),
    }


def save_progress(session_id: str, state: dict[str, Any]) -> None:
    atomic_write_json(progress_path(session_id), {
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "plan_state_sig": state.get("plan_state_sig"),
        "plan_usage_emitted_at": state.get("plan_usage_emitted_at"),
        "plan_stamp": state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Bash-class classifier (identical to Claude/Codex — do not diverge)
# ---------------------------------------------------------------------------

BASH_CLASS_RANK = (
    "file-write",
    "git-write",
    "pkg",
    "network",
    "build",
    "test",
    "git-read",
    "file-read",
    "other",
)

BASH_CMD_CLASS = {
    "pytest": "test", "tox": "test", "jest": "test", "vitest": "test",
    "rspec": "test", "phpunit": "test",
    "make": "build", "cmake": "build", "tsc": "build", "gradle": "build",
    "mvn": "build", "gcc": "build", "clang": "build", "webpack": "build",
    "pip": "pkg", "pip3": "pkg", "brew": "pkg", "gem": "pkg",
    "apt": "pkg", "apt-get": "pkg", "yum": "pkg", "dnf": "pkg",
    "apk": "pkg", "poetry": "pkg", "uv": "pkg",
    "ls": "file-read", "cat": "file-read", "find": "file-read",
    "grep": "file-read", "rg": "file-read", "head": "file-read",
    "tail": "file-read", "wc": "file-read", "du": "file-read",
    "df": "file-read", "stat": "file-read", "file": "file-read",
    "tree": "file-read", "which": "file-read", "pwd": "file-read",
    "less": "file-read", "more": "file-read", "diff": "file-read",
    "awk": "file-read", "echo": "file-read", "sort": "file-read",
    "uniq": "file-read", "cut": "file-read", "jq": "file-read",
    "rm": "file-write", "mv": "file-write", "cp": "file-write",
    "mkdir": "file-write", "rmdir": "file-write", "chmod": "file-write",
    "chown": "file-write", "touch": "file-write", "ln": "file-write",
    "sed": "file-write", "tee": "file-write", "truncate": "file-write",
    "dd": "file-write", "tar": "file-write", "unzip": "file-write",
    "zip": "file-write",
    "curl": "network", "wget": "network", "gh": "network",
    "ssh": "network", "scp": "network", "rsync": "network",
    "nc": "network", "ping": "network", "dig": "network",
    "host": "network", "nslookup": "network",
}

GIT_READ_SUBS = {
    "status", "log", "diff", "show", "blame", "shortlog", "reflog",
    "describe", "rev-parse", "ls-files", "ls-remote", "ls-tree",
    "cat-file", "grep",
}
BASH_MULTIPLEX_CLASS = {
    "git": ({s: "git-read" for s in GIT_READ_SUBS}, "git-write"),
    "go": (
        {"test": "test", "vet": "test",
         "build": "build", "run": "build", "generate": "build",
         "get": "pkg", "install": "pkg", "mod": "pkg"},
        "other",
    ),
    "cargo": (
        {"test": "test", "bench": "test",
         "build": "build", "check": "build", "run": "build",
         "clippy": "build",
         "add": "pkg", "install": "pkg", "update": "pkg",
         "remove": "pkg"},
        "other",
    ),
    "npm": (
        {"test": "test", "run": "build", "exec": "build"},
        "pkg",
    ),
    "pnpm": (
        {"test": "test", "run": "build", "exec": "build"},
        "pkg",
    ),
    "yarn": (
        {"test": "test", "run": "build"},
        "pkg",
    ),
    "bun": (
        {"test": "test", "run": "build", "build": "build"},
        "pkg",
    ),
}


def classify_bash_command(command: str) -> tuple[str, bool] | None:
    for sep in ("&&", "||", ";", "|", "\n"):
        command = command.replace(sep, "\x00")
    classes: set[str] = set()
    for segment in command.split("\x00"):
        words = segment.split()
        while words and ("=" in words[0] or words[0] == "sudo"):
            words.pop(0)
        if not words:
            continue
        cmd = words[0].rsplit("/", 1)[-1]
        mux = BASH_MULTIPLEX_CLASS.get(cmd)
        if mux is not None:
            sub_map, default = mux
            sub = words[1] if len(words) > 1 else ""
            classes.add(sub_map.get(sub, default))
        else:
            classes.add(BASH_CMD_CLASS.get(cmd, "other"))
    if not classes:
        return None
    winner = min(classes, key=BASH_CLASS_RANK.index)
    return winner, len(classes) > 1


# ---------------------------------------------------------------------------
# Spend-limits gate (BeforeAgent)
# ---------------------------------------------------------------------------

def limits_gate_output(session_id: str) -> dict[str, Any] | None:
    """Port of Codex's limits gate. File I/O only — never touches the
    network. Returns hook JSON to print, or None (fail open)."""
    lc = _limits()
    if lc is None:
        return None
    verdict = lc.read_verdict(session_id)
    if not verdict:
        return None

    decision = verdict.get("decision")
    try:
        band = int(verdict.get("band") or 0)
    except (TypeError, ValueError):
        band = 0
    fetched_at = verdict.get("fetched_at")
    age = (
        time.time() - fetched_at
        if isinstance(fetched_at, (int, float))
        else float("inf")
    )

    if decision == "block" and age <= lc.BLOCK_MAX_AGE_SEC:
        if not lc.override_path(session_id).exists():
            reason = (
                verdict.get("block_reason")
                or verdict.get("user_message")
                or "A Cardinal spend limit for this work has been reached."
            )
            return {"decision": "block", "reason": reason}
        decision = "warn"

    if band <= 0 or age > lc.WARN_MAX_AGE_SEC:
        return None

    ack = lc._read_json_file(lc.ack_path(session_id))
    try:
        last_band = int(ack.get("band") or 0)
    except (TypeError, ValueError):
        last_band = 0
    if band <= last_band:
        return None

    out: dict[str, Any] = {}
    agent_context = verdict.get("agent_context")
    if isinstance(agent_context, str) and agent_context:
        out["hookSpecificOutput"] = {
            "hookEventName": "BeforeAgent",
            "additionalContext": agent_context,
        }
    user_message = verdict.get("user_message")
    if decision == "warn" and isinstance(user_message, str) and user_message:
        out["systemMessage"] = user_message
    if not out:
        return None
    lc.atomic_write_json(
        lc.ack_path(session_id), {"band": band, "surfaced_at": time.time()}
    )
    return out


# ---------------------------------------------------------------------------
# BeforeAgent — closest analogue to Claude's UserPromptSubmit
# ---------------------------------------------------------------------------

def handle_before_agent(payload: dict[str, Any]) -> None:
    dump_debug_payload("BeforeAgent", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())

    # Sync gate FIRST — its stdout is the hook's verdict channel and must
    # not wait on any network call below.
    try:
        gate_out = limits_gate_output(session_id)
        if gate_out:
            sys.stdout.write(json.dumps(gate_out))
            sys.stdout.flush()
    except Exception:
        pass

    # Turn boundary: user_turn_seq increments; per-turn counters reset.
    state = load_progress(session_id)
    state["user_turn_seq"] += 1
    state["turn_seq"] = 0
    state["tool_seq"] = 0
    save_progress(session_id, state)

    branch = None
    repo = None
    head_sha = git(["rev-parse", "HEAD"], cwd)
    if head_sha:
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        remote_url = git(["remote", "get-url", "origin"], cwd)
        repo = canonical_repo(remote_url)
        initiative_name, initiative_type = resolve_initiative(branch)
        attrs: dict[str, Any] = {
            "session_id": session_id,
            "cardinal_cwd": cwd,
            "cardinal_head_sha": head_sha,
            "cardinal_branch": branch,
            "cardinal_repo": repo,
            "cardinal_remote_url": remote_url,
            "cardinal_initiative_name": initiative_name,
            "cardinal_initiative_type": initiative_type,
            "cardinal_command": detect_command(payload.get("prompt")),
            **read_plan_stamp(),
        }
        emit_records([log_record("cardinal.git_state", attrs, time.time_ns())])

    # Async half of the gate — refresh after the OTLP post, best-effort.
    try:
        lc = _limits()
        if lc is not None:
            lc.maybe_refresh_verdict(session_id=session_id, repo=repo, branch=branch)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AfterModel — per-model-call api_request + cardinal.turn_usage
# ---------------------------------------------------------------------------

def normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Map Gemini's payload key spellings onto the Cardinal contract's
    canonical bucket names. Gemini variants observed / documented include
    `promptTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount`,
    `cachedContentTokenCount`, `toolUsePromptTokenCount`.
    """
    def _int(*keys: str) -> int:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return {
        "input_tokens": _int("input_tokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": _int("output_tokens", "response_tokens", "candidatesTokenCount"),
        "thought_tokens": _int("thought_tokens", "thoughtsTokenCount"),
        "cached_input_tokens": _int(
            "cached_input_tokens", "cache_read_tokens",
            "cached_content_token_count", "cachedContentTokenCount",
        ),
        "tool_use_tokens": _int("tool_use_tokens", "toolUsePromptTokenCount"),
    }


def usage_attrs(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thought_tokens": usage.get("thought_tokens"),
        "cache_read_tokens": usage.get("cached_input_tokens"),
        "cache_read_input_tokens": usage.get("cached_input_tokens"),
    }


def handle_after_model(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterModel", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return

    # Gemini's AfterModel payload nests token usage under `usage` or
    # `usageMetadata` depending on version — probe both.
    raw_usage = payload.get("usage") or payload.get("usageMetadata") or {}
    if not isinstance(raw_usage, dict):
        return
    usage = normalize_usage(raw_usage)
    if not any(usage.values()):
        return

    model = payload.get("model") or payload.get("modelId") or payload.get("model_id")
    state = load_progress(session_id)
    ts_ns = time.time_ns()

    # Update plan stamp if the payload surfaces plan/tier info.
    plan_type = payload.get("plan_type") or payload.get("planType")
    limit_tier = payload.get("rate_limit_tier") or payload.get("rateLimitTier")
    if isinstance(plan_type, str) or isinstance(limit_tier, str):
        stamp: dict[str, Any] = {}
        if isinstance(plan_type, str) and plan_type:
            stamp["plan_type"] = plan_type
        if isinstance(limit_tier, str) and limit_tier:
            stamp["rate_limit_tier"] = limit_tier
        if stamp:
            state["plan_stamp"] = stamp
            atomic_write_json(PLAN_STAMP_PATH, {
                **stamp,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    plan_stamp = state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else {}

    state_conn = read_json(STATE_PATH)
    base = {
        "session_id": session_id,
        "user_email": state_conn.get("user_email"),
        "agent_runtime": "gemini",
        "model": str(model) if model else None,
        **usage_attrs(usage),
    }
    cost_usd = compute_cost_usd(str(model) if model else None, usage)
    if cost_usd is not None:
        base["cost_usd"] = cost_usd

    records: list[dict[str, Any]] = []
    records.append(log_record("api_request", base, ts_ns))
    records.append(log_record("cardinal.turn_usage", {
        **base,
        "ts": ts_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        **plan_stamp,
    }, ts_ns + 1))

    # plan_state: once per session; re-emit on value change.
    plan_sig = f"{plan_stamp.get('plan_type') or ''}|{plan_stamp.get('rate_limit_tier') or ''}"
    if plan_sig != "|" and plan_sig != state.get("plan_state_sig"):
        records.append(log_record("cardinal.plan_state", {
            "session_id": session_id,
            "agent_runtime": "gemini",
            "ts": ts_ns,
            "plan_type": plan_stamp.get("plan_type"),
            "rate_limit_tier": plan_stamp.get("rate_limit_tier"),
        }, ts_ns + 2))
        state["plan_state_sig"] = plan_sig

    emit_records(records)
    state["turn_seq"] += 1
    state["tool_seq"] = 0
    save_progress(session_id, state)


# ---------------------------------------------------------------------------
# AfterTool — cardinal.turn_tool + tool_result
# ---------------------------------------------------------------------------

def parse_args_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def normalize_tool(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Return (canonical tool_name, extra params, target)."""
    if name in {"run_shell_command", "shell", "bash"}:
        cmd = str(args.get("command") or args.get("cmd") or "")
        return "Bash", {"full_command": cmd, "bash_command": cmd.split(" ", 1)[0] if cmd else ""}, None
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else ""
        tool = parts[2] if len(parts) > 2 else name
        return "mcp_tool", {"mcp_server_name": server, "mcp_tool_name": tool}, None
    return name, {}, None


def handle_after_tool(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterTool", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    raw_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if not raw_name:
        return
    args = parse_args_json(payload.get("tool_input") or payload.get("toolInput") or payload.get("arguments"))
    tool_name, params, target = normalize_tool(raw_name, args)
    if target is None:
        key = TARGET_KEYS.get(tool_name) or TARGET_KEYS.get(raw_name)
        if key:
            v = args.get(key)
            if isinstance(v, str) and v:
                target = v

    state = load_progress(session_id)
    plan_stamp = state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else {}
    ts_ns = time.time_ns()

    attrs: dict[str, Any] = {
        "session_id": session_id,
        "ts": ts_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        "tool_name": tool_name,
        "target": target,
        **plan_stamp,
    }
    if tool_name == "mcp_tool":
        # turn_tool carries the raw qualified MCP name (harvester's clustering
        # signal); tool_result keeps the normalized form.
        attrs["tool_name"] = raw_name
        attrs["mcp_server_name"] = params.get("mcp_server_name")
        attrs["mcp_tool_name"] = params.get("mcp_tool_name")
    elif tool_name == "Bash":
        classified = classify_bash_command(str(params.get("full_command") or ""))
        if classified is not None:
            bash_class, bash_multi = classified
            attrs["bash_class"] = bash_class
            if bash_multi:
                attrs["bash_multi"] = True

    success = payload.get("success")
    if success is None:
        # Fall back to exit_code / status if present.
        exit_code = payload.get("exit_code") or payload.get("exitCode")
        if isinstance(exit_code, (int, float)):
            success = "true" if int(exit_code) == 0 else "false"
        else:
            status = payload.get("status")
            if isinstance(status, str):
                success = "true" if status.lower() in {"ok", "success", "completed"} else "false"
    if isinstance(success, bool):
        success_str = "true" if success else "false"
    elif isinstance(success, str):
        success_str = success.lower()
    else:
        success_str = "true"

    result_attrs: dict[str, Any] = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "tool_name": tool_name,
        "success": success_str,
        "tool_parameters": json.dumps(params, separators=(",", ":")) if params else None,
        "tool_input": json.dumps(args, separators=(",", ":")) if args else None,
    }

    records = [
        log_record("cardinal.turn_tool", attrs, ts_ns),
        log_record("tool_result", result_attrs, ts_ns + 1),
    ]
    emit_records(records)
    state["tool_seq"] += 1
    save_progress(session_id, state)


# ---------------------------------------------------------------------------
# AfterAgent — subagent stop
# ---------------------------------------------------------------------------

def subagent_description_from_payload(payload: dict[str, Any]) -> str | None:
    """Best-effort extraction of the subagent's short task label. Task
    label only — free-text boundary widening capped at 160 chars, matching
    Claude v0.12.1's `subagent_description`."""
    candidates: list[Any] = [
        payload.get("description"),
        payload.get("task_description"),
        payload.get("taskDescription"),
        payload.get("prompt"),
        payload.get("label"),
    ]
    for input_key in ("tool_input", "toolInput"):
        tool_input = payload.get(input_key)
        if isinstance(tool_input, dict):
            candidates.append(tool_input.get("description"))
            candidates.append(tool_input.get("prompt"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:160]
    return None


def handle_after_agent(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterAgent", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return

    # Gemini's AfterAgent payload usage may nest under several keys.
    usage_block = (
        payload.get("usage")
        or payload.get("usageMetadata")
        or payload.get("tokens")
        or {}
    )
    if isinstance(usage_block, dict):
        total_tokens = (
            usage_block.get("total_tokens")
            or usage_block.get("totalTokenCount")
            or usage_block.get("total_token_count")
        )
    else:
        total_tokens = None
    if total_tokens is None:
        total_tokens = payload.get("total_tokens") or payload.get("totalTokens")

    attrs = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "subagent_type": (
            payload.get("subagent_type")
            or payload.get("subagentType")
            or payload.get("agent_type")
            or payload.get("agentType")
            or payload.get("matcher")
        ),
        "agent_id": payload.get("agent_id") or payload.get("agentId"),
        "subagent_description": subagent_description_from_payload(payload),
        "total_tokens": total_tokens,
        "duration_ms": payload.get("duration_ms") or payload.get("durationMs"),
        "status": payload.get("status"),
        **read_plan_stamp(),
    }
    # Emit when we have ANY identifying facet — an untyped call with no
    # description, id, tokens, or duration is almost certainly a stray
    # payload (Gemini fires AfterAgent for the main agent too on some
    # versions); skipping those keeps the subagent_usage stream honest.
    identifying = any(
        attrs[k] is not None
        for k in ("subagent_type", "agent_id", "subagent_description",
                  "total_tokens", "duration_ms")
    )
    if not identifying:
        return
    emit_records([log_record("cardinal.subagent_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# PreCompress — context-window compaction slice
# ---------------------------------------------------------------------------

def handle_pre_compress(payload: dict[str, Any]) -> None:
    dump_debug_payload("PreCompress", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    attrs = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "context_tokens": payload.get("context_tokens") or payload.get("contextTokens"),
        "context_window_size": payload.get("context_window_size") or payload.get("contextWindowSize"),
        "context_usage_percent": payload.get("context_usage_percent") or payload.get("contextUsagePercent"),
        "trigger": payload.get("trigger"),
        "messages_to_compact": payload.get("messages_to_compact") or payload.get("messagesToCompact"),
        "is_first_compaction": payload.get("is_first_compaction") or payload.get("isFirstCompaction"),
        "plan": {"compact_trigger": payload.get("trigger")} if payload.get("trigger") else None,
        **read_plan_stamp(),
    }
    # Flag disambiguation downstream: presence of `plan.compact_trigger`
    # distinguishes this from per-model-call plan_usage.
    if attrs["plan"]:
        attrs["plan.compact_trigger"] = attrs["plan"]["compact_trigger"]
    attrs.pop("plan", None)
    emit_records([log_record("cardinal.plan_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# SessionStart — convention prompt + budget standing
# ---------------------------------------------------------------------------

CONVENTION_PROMPT = (
    "You are running inside a Cardinal-instrumented Gemini CLI session. "
    "Cardinal attributes agent spend to 'initiatives' — "
    "one branch = one initiative. When you create a new branch for "
    "work in this session, follow the convention:\n\n"
    "  <type-prefix>/<kebab-name>\n\n"
    "  type-prefix  ∈ {feat, fix, refactor, infra, chore, research, spike}\n"
    "  kebab-name   = lowercase, 1–4 dash-separated segments\n\n"
    "Examples:\n"
    "  feat/outcomes-observability    → name 'outcomes-observability', type 'feature'\n"
    "  fix/login-crash                → name 'login-crash',            type 'bugfix'\n"
    "  refactor/auth-token-rotation   → name 'auth-token-rotation',    type 'refactor'\n"
    "  research/data-pipeline-spike   → name 'data-pipeline-spike',    type 'research'\n\n"
    "Prefix aliases: 'feature' = 'feat', 'bugfix' = 'fix', 'chore' = "
    "'infra', 'spike' = 'research'. Other conventional prefixes are "
    "also recognized: 'perf' → feature; 'cleanup' → refactor; 'test', "
    "'tests', 'ci', 'build', 'deps', 'docs', 'doc' → infra. Sessions "
    "on main/master/develop/trunk are treated as research/scoping work — "
    "when intent crystallises into a deliverable, cut a typed branch "
    "using this convention. Off-convention branches get a stable name "
    "but default to type 'feature', so the convention is the way to "
    "ensure correct classification."
)


def _is_git_repo(cwd: str) -> bool:
    return git(["rev-parse", "--is-inside-work-tree"], cwd) == "true"


def _budget_standing(session_id: str | None, cwd: str) -> str | None:
    if not session_id:
        return None
    lc = _limits()
    if lc is None or not lc.limits_config():
        return None
    repo, branch = lc.git_facts(cwd)
    verdict = lc.maybe_refresh_verdict(
        session_id=session_id, repo=repo, branch=branch, force=True, timeout=1.5
    )
    if not verdict:
        return None
    lines = lc.standing_lines(verdict)
    if not lines:
        return None
    parts = ["Cardinal spend budgets apply to this session:"]
    parts.extend(lines)
    user_message = verdict.get("user_message")
    if isinstance(user_message, str) and user_message:
        parts.append(user_message)
    parts.append(
        "Work economically as budgets tighten; budget standing updates "
        "arrive automatically as the session proceeds."
    )
    return "\n".join(parts)


def handle_session_start(payload: dict[str, Any]) -> None:
    dump_debug_payload("SessionStart", payload)
    cwd = str(payload.get("cwd") or os.getcwd())
    if not _is_git_repo(cwd):
        return
    context = CONVENTION_PROMPT
    try:
        standing = _budget_standing(session_id_from_payload(payload), cwd)
        if standing:
            context = f"{CONVENTION_PROMPT}\n\n{standing}"
    except Exception:
        pass
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# SessionEnd — best-effort cleanup
# ---------------------------------------------------------------------------

def handle_session_end(payload: dict[str, Any]) -> None:
    dump_debug_payload("SessionEnd", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    # Retention: leave the per-session progress + verdict files behind.
    # cardinal-disconnect removes ~/.gemini/cardinal/ wholesale.
    return


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    args = parser.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        if args.event == "SessionStart":
            handle_session_start(payload)
        elif args.event == "BeforeAgent":
            handle_before_agent(payload)
        elif args.event == "AfterModel":
            handle_after_model(payload)
        elif args.event == "AfterTool":
            handle_after_tool(payload)
        elif args.event == "AfterAgent":
            handle_after_agent(payload)
        elif args.event == "PreCompress":
            handle_pre_compress(payload)
        elif args.event == "SessionEnd":
            handle_session_end(payload)
    except Exception:
        pass
    silent_exit()


if __name__ == "__main__":
    main()
