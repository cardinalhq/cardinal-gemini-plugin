#!/usr/bin/env python3
"""Emit Cardinal agent-session telemetry from Gemini CLI hooks.

Gemini CLI emits per-model-call and per-tool-call hook events directly
(unlike Codex which required transcript-JSONL scraping), so this hook
normalizes each event payload into the existing Cardinal/Lakerunner event
contract and POSTs OTLP/HTTP logs. Failures are best-effort and silent:
telemetry must not break the agent loop.

Monorepo adapter: all shared behavior (OTLP contract, initiative
resolution, bash classification, pricing, spend-limits delivery, session
counters) comes from the vendored `cardinal_core` package; this file keeps
only the Gemini-specific parts — payload parsing, tool-name normalization,
and event dispatch.

Latency contract: Gemini CLI awaits every hook process until it exits AND
its stdio pipes close (packages/core/src/hooks/hookRunner.ts, resolve on
`close`). Each hook therefore does only local-file work synchronously and
hands network work (OTLP posts, `gh` PR lookup, limits verdict refresh) to
a detached background child with /dev/null stdio (`--background <spool>`).

Event dispatch (payload shapes: packages/core/src/hooks/types.ts):

  SessionStart  → convention prompt + budget standing + the session's
                  Investigation (investigation_agent.start: bootstrap,
                  poller, guidance with the labeled links) + storyboard
                  session id and discovery block + connect hint / stray-key
                  warning (additionalContext)
  BeforeAgent   → spend-limits gate + decision prompt + what was staged
                  for this prompt (additionalContext); owner input
                  (owner_input.capture, detached: no network here) with its
                  one-time disclosure as systemMessage; background:
                  cardinal.git_state (+PR) + verdict refresh + the discovery
                  re-look (staged for the next boundary)
  AfterModel    → api_request + cardinal.turn_usage, final chunk only
                  (fires per streamed chunk; non-final chunks exit early)
  BeforeTool    → storyboard context stamping on Cardinal's storyboard
                  tools only (matcher), via hookSpecificOutput.tool_input
  BeforeTool --role checks-gate
                → denies the built-in activate_skill for the checks skill
                  while the Checks preview is off (matcher ^activate_skill$)
  AfterTool     → cardinal.turn_tool + tool_result (per tool call); every
                  tool call (built-in or MCP, succeeded or failed; not
                  Cardinal's own) is also captured in the local evidence
                  spool through the generic pipeline
                  (cardinal_core.evidence_capture) and its [evidence:ev_…]
                  id returned as additionalContext, with the Investigation's
                  advisories (investigation_agent.deliver), recording
                  failures, the evidence token / hero upload on Cardinal's
                  storyboard tools and the storyboards about a file
                  write_file / replace edited (edit lookup)
  AfterTool --role preview
                → the storyboard__preview render (its own 150 s group)
  AfterAgent    → the main agent's turn end ({prompt, prompt_response,
                  stop_hook_active}): investigation_agent.turn_end flushes
                  owner input and STAGES what is deliverable; it never
                  blocks (an AfterAgent block re-enters as user text and
                  re-fires BeforeAgent); subagent-shaped payloads still
                  give cardinal.subagent_usage
  PreCompress   → cardinal.plan_usage (compaction trigger)
  SessionEnd    → no-op
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

START = time.monotonic()

# AfterModel fires once per streamed chunk and the host awaits each hook
# (packages/core/src/core/geminiChat.ts: `await hookSystem.fireAfterModelEvent`
# inside the chunk loop). Only the final chunk — a candidate carrying
# finishReason — is accounted, so exit before importing cardinal_core.
_PRELOADED_STDIN: str | None = None
if __name__ == "__main__" and sys.argv[1:3] == ["--event", "AfterModel"]:
    _PRELOADED_STDIN = sys.stdin.read()
    if '"finishReason"' not in _PRELOADED_STDIN:
        sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _plugin_version  # noqa: E402
from cardinal_core import bashclass, decisions, evidence, initiative, limits, otlp, pricing, session  # noqa: E402
from cardinal_core.paths import AgentPaths  # noqa: E402


PLUGIN_VERSION = _plugin_version.plugin_version()
SCOPE_NAME = "cardinal-gemini-plugin"

PATHS = AgentPaths(home=Path.home() / ".gemini")
DEBUG_PAYLOADS_ENV = "CARDINAL_GEMINI_DEBUG_PAYLOADS"
# Test hook: run background jobs inline so OTLP output is deterministic.
BACKGROUND_INLINE_ENV = "CARDINAL_GEMINI_INLINE_BACKGROUND"

# Decision capture CLI, resolved from this hook's own location so the
# injected instruction names the installed plugin's absolute path.
DECISION_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-decision"
# `gh pr view` bound for the background PR refresh.
PR_REFRESH_TIMEOUT_SEC = 4.0

TARGET_KEYS = {
    "read_file": "file_path",
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


def silent_exit() -> None:
    sys.exit(0)


def session_id_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "sessionID"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    value = os.environ.get("GEMINI_SESSION_ID")
    if value:
        return value
    return None


def connected() -> bool:
    return otlp.connection_from_paths(PATHS) is not None


def emit_records(records: list[dict[str, Any]]) -> None:
    """Synchronous OTLP post — call only from the background child."""
    if not records:
        return
    conn = otlp.connection_from_paths(PATHS)
    if conn is None:
        return
    state = PATHS.read_state()
    resource = otlp.resource_attrs(
        service_name="gemini-cli",
        agent_runtime="gemini",
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"),
        plugin_version=PLUGIN_VERSION,
    )
    otlp.emit_records(
        records, conn, resource,
        scope_name=SCOPE_NAME, scope_version=PLUGIN_VERSION,
    )


# ---------------------------------------------------------------------------
# Background work — detached child so the host never waits on the network
# ---------------------------------------------------------------------------

def spawn_background(job: dict[str, Any]) -> None:
    """Spool `job` (0600) and run it in a detached child. The child gets
    /dev/null for stdin/stdout/stderr and its own session: Gemini CLI waits
    for the hook's stdio pipes to close, so an inherited pipe would hold
    the prompt until the network work finished."""
    try:
        spool_dir = PATHS.runtime_dir / "spool"
        spool_dir.mkdir(parents=True, exist_ok=True)
        path = spool_dir / f"{job.get('kind')}-{os.getpid()}-{time.time_ns()}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(job, fh, default=str)
    except (OSError, TypeError, ValueError):
        return
    if os.environ.get(BACKGROUND_INLINE_ENV) == "1":
        run_background_job(path)
        return
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--background", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def emit_in_background(records: list[dict[str, Any]]) -> None:
    if records and connected():
        spawn_background({"kind": "emit", "records": records})


def run_background_job(path: Path) -> None:
    try:
        job = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if not isinstance(job, dict):
        return
    kind = job.get("kind")
    if kind == "emit":
        records = job.get("records")
        if isinstance(records, list):
            emit_records(records)
    elif kind == "before_agent":
        before_agent_background(job)


def dump_debug_payload(event: str, payload: dict[str, Any]) -> None:
    """Env-gated raw hook-payload dump for shape capture. A no-op unless
    CARDINAL_GEMINI_DEBUG_PAYLOADS=1; best-effort like everything else."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        PATHS.debug_dir.mkdir(parents=True, exist_ok=True)
        path = PATHS.debug_dir / f"{event}-{time.time_ns()}.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# BeforeAgent — closest analogue to Claude's UserPromptSubmit
# ---------------------------------------------------------------------------

def build_decision_context(cli: str, session_id: str, entries: list[dict[str, Any]]) -> str:
    """Decision-recording instructions + this session's ledger. Gemini CLI
    HTML-escapes `<`/`>` in additionalContext, so placeholders use braces."""
    return (
        "Cardinal decision capture is on for this session. When you make a choice that "
        "constrains later work (picking between approaches, settling an open question, or "
        "the user deciding something), record it right away with one run_shell_command call:\n"
        f'python3 "{cli}" record --session {shlex.quote(session_id)} '
        '--choice "{the option chosen, 2-7 words}" '
        '--question "{what had to be settled}" --why "{one sentence}" '
        '[--alt "{rejected option}"]... [--by user] [--anchor {path}[::Symbol]]... '
        "[--follows|--refines|--supersedes {id}]\n"
        "Record choices, not progress, findings, or tool calls. Use --by user when the user "
        "made the call. Anchor the files or symbols the decision governs. Link a decision to "
        "an earlier one when it builds on, narrows, or replaces it.\n"
        "Decisions so far this session:\n"
        f"{decisions.render_ledger(entries)}"
    )


def decision_context(session_id: str) -> str | None:
    """None unless decision capture is on (`cardinal-decision on` or
    CARDINAL_DECISIONS=1). Local file reads only — no network."""
    if not decisions.is_enabled(PATHS.runtime_dir, os.environ.get(decisions.ENABLE_ENV)):
        return None
    entries = decisions.read_ledger(PATHS.runtime_dir, session_id)
    return build_decision_context(str(DECISION_CLI), session_id, entries)


def merge_prompt_output(gate_out: dict[str, Any] | None, extra_context: str | None,
                        system_message: str | None = None) -> dict[str, Any] | None:
    """One BeforeAgent stdout JSON object from the spend-limits gate, the
    prompt's additional context and a user-visible systemMessage (the
    owner-input disclosure). A blocked turn carries only the block verdict
    (Gemini shows a block's systemMessage in place of its reason)."""
    if gate_out and gate_out.get("decision") == "block":
        return gate_out
    if not extra_context and not system_message:
        return gate_out
    out = dict(gate_out or {})
    if extra_context:
        hso = dict(out.get("hookSpecificOutput") or {})
        hso["hookEventName"] = "BeforeAgent"
        prior = hso.get("additionalContext")
        hso["additionalContext"] = f"{prior}\n\n{extra_context}" if prior else extra_context
        out["hookSpecificOutput"] = hso
    if system_message:
        prior = out.get("systemMessage")
        out["systemMessage"] = f"{prior}\n{system_message}" if prior else system_message
    return out


# ---------------------------------------------------------------------------
# Owner input (cardinal_core.owner_input.capture: the gate every runtime
# shares). BeforeAgent's `prompt` is partToString(request) (gemini-cli
# 0.50.0 GeminiClient.fireBeforeAgentHookSafe, chunk-W4YMYD53.js:335207):
# in -p mode the SessionStart context is prepended as <hook_context>…
# </hook_context> (gemini-APOZRZEF.js:29098) and an @file is inlined after
# the typed text between "--- Content from referenced files ---" and
# "--- End of content ---" (atCommandProcessor); normalize_prompt("gemini")
# removes both. A custom TOML command is expanded before BeforeAgent
# (interactive submit_prompt, interactiveCli-YUJVNPR2.js:27527; -p without
# hook context), so that prompt is NOT what the owner typed: it is
# recognized against the command files (custom_command_expansion) and never
# captured. When the command files cannot be read confidently
# (CommandFilesUnsure), no prompt is captured.
# ---------------------------------------------------------------------------

MAX_COMMAND_FILES = 200
MAX_COMMAND_BYTES = 64 << 10
MIN_TEMPLATE_LITERAL = 8
MAX_TEMPLATE_PARTS = 9
_TOML_PROMPT_RE = re.compile(r'(?m)^\s*prompt\s*=\s*("""|\'\'\'|"|\')')
_INJECTION_RE = re.compile(r"\{\{args\}\}|[!@]\{[^{}]*\}")


class CommandFilesUnsure(Exception):
    """The command files cannot be read confidently (too many, too large,
    an unparseable `prompt`, an unreadable directory): whether a prompt is a
    custom command's expansion cannot be told, so it is not captured."""


_PROMPT_KEY_RE = re.compile(r'(?m)^\s*["\']?prompt["\']?\s*=')


def _closing(rest: str, quote: str) -> int:
    """Where `quote` closes a TOML string whose body starts `rest`: a basic
    string's (") closing quote is not escaped by a backslash; -1 if none."""
    i = 0
    while True:
        j = rest.find(quote, i)
        if j < 0 or not quote.startswith('"'):
            return j
        backslashes = 0
        k = j - 1
        while k >= 0 and rest[k] == "\\":
            backslashes += 1
            k -= 1
        if backslashes % 2 == 0:
            return j
        i = j + 1


def _toml_prompt(text: str) -> str | None:
    """The `prompt` string of a Gemini custom command file (basic and
    literal strings, single- or multi-line); None when the file has none (or
    one that is not a string: Gemini skips such a file). Raises
    CommandFilesUnsure when there is a `prompt` that cannot be parsed."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            value = tomllib.loads(text).get("prompt")
        except Exception as e:
            raise CommandFilesUnsure("unparseable command file") from e
        return value if isinstance(value, str) else None
    m = _TOML_PROMPT_RE.search(text)
    if not m:
        if _PROMPT_KEY_RE.search(text):
            raise CommandFilesUnsure("a prompt key this parser cannot read")
        return None
    quote = m.group(1)
    rest = text[m.end():]
    end = _closing(rest, quote)
    if end < 0:
        raise CommandFilesUnsure("an unclosed prompt string")
    value = rest[:end]
    if len(quote) == 3 and value.startswith("\n"):
        value = value[1:]
    if quote.startswith('"'):
        if len(quote) == 3:
            # A line-ending backslash joins the next non-blank text.
            value = re.sub(r"\\[ \t]*\n\s*", "", value)
        try:
            value = json.loads('"' + value.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r") + '"')
        except ValueError as e:
            raise CommandFilesUnsure("a prompt string with escapes this parser cannot read") from e
    return value


def _extension_command_dir(ext: Path) -> Path:
    """An installed extension's commands/: a linked one (`gemini extensions
    link`) loads from its source (.gemini-extension-install.json
    {"type": "link", "source"}). Raises CommandFilesUnsure when the install
    metadata is there but cannot be read."""
    meta = ext / ".gemini-extension-install.json"
    if not meta.exists():
        return ext / "commands"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CommandFilesUnsure("unreadable extension install metadata") from e
    if isinstance(data, dict) and data.get("type") == "link":
        source = data.get("source")
        if not isinstance(source, str) or not source:
            raise CommandFilesUnsure("a linked extension without a source")
        return Path(source) / "commands"
    return ext / "commands"


def command_dirs(cwd: str) -> list[Path]:
    """Where Gemini loads custom commands (FileCommandLoader
    getCommandDirectories): ~/.gemini/commands, <project>/.gemini/commands
    and every installed extension's commands/ (a linked extension's in its
    source). Raises CommandFilesUnsure when the extensions cannot be
    listed."""
    dirs = [PATHS.home / "commands", Path(cwd) / ".gemini" / "commands"]
    extensions = PATHS.home / "extensions"
    if extensions.exists():
        try:
            exts = sorted(p for p in extensions.iterdir() if p.is_dir())
        except OSError as e:
            raise CommandFilesUnsure("unreadable extensions directory") from e
        dirs += [_extension_command_dir(p) for p in exts]
    return dirs


def _toml_files(d: Path) -> list[Path]:
    """Every *.toml under `d` (sorted); none when `d` is not a directory.
    Raises CommandFilesUnsure when `d` or a directory under it cannot be
    read (a permission error, an unreachable linked source)."""
    def unsure(err: OSError) -> None:
        raise CommandFilesUnsure("unreadable commands directory") from err

    try:
        st = os.stat(d)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        unsure(e)
    if not stat.S_ISDIR(st.st_mode):
        return []
    if not os.access(d, os.R_OK | os.X_OK):
        raise CommandFilesUnsure("unreadable commands directory")
    found = []
    for root, _dirs, names in os.walk(d, onerror=unsure):
        found += [Path(root) / n for n in names if n.endswith(".toml")]
        if len(found) > MAX_COMMAND_FILES:
            raise CommandFilesUnsure("too many command files")
    return sorted(found)


def _in_order(parts: list[str], typed: str) -> bool:
    """Every literal part of a template appears in `typed`, in order."""
    i = 0
    for part in parts:
        j = typed.find(part.strip(), i)
        if j < 0:
            return False
        i = j + len(part.strip())
    return True


def custom_command_expansion(prompt: Any, cwd: str) -> bool:
    """Whether `prompt` (hook context and @file sections removed) is a
    custom command's expanded template: its literal text with {{args}},
    !{…} and @{…} standing for anything, and Gemini's appended raw
    invocation when the template has no {{args}}. Local file reads only,
    bounded; False when no command file matches. Raises CommandFilesUnsure
    when it cannot be told: more than MAX_COMMAND_FILES files, one larger
    than MAX_COMMAND_BYTES, an unparseable `prompt`, an unreadable command
    or extension directory, a template with too many placeholders to match
    whose literal text the prompt contains."""
    from cardinal_core import owner_input

    typed = owner_input.normalize_prompt("gemini", prompt)
    if typed is None:
        return False
    seen = 0
    for d in command_dirs(cwd):
        for f in _toml_files(d):
            seen += 1
            if seen > MAX_COMMAND_FILES:
                raise CommandFilesUnsure("too many command files")
            try:
                if f.stat().st_size > MAX_COMMAND_BYTES:
                    raise CommandFilesUnsure("a command file too large to read")
                template = _toml_prompt(f.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as e:
                raise CommandFilesUnsure("an unreadable command file") from e
            if not template or not template.strip():
                continue
            parts = _INJECTION_RE.split(template.strip())
            # A template that is (almost) only placeholders would match every
            # prompt (what it submits is what was typed as its args).
            if len("".join(parts).strip()) < MIN_TEMPLATE_LITERAL:
                continue
            # One with many is not worth the backtracking: unsure when the
            # prompt holds its literal text in order.
            if len(parts) > MAX_TEMPLATE_PARTS:
                if _in_order(parts, typed):
                    raise CommandFilesUnsure("a template with too many placeholders")
                continue
            pattern = ".*?".join(re.escape(part) for part in parts)
            if re.fullmatch(r"\s*" + pattern + r"(?:\s*/[\s\S]*)?\s*", typed, re.DOTALL):
                return True
    return False


def owner_input_log(status: str) -> None:
    """The capture status (never the prompt) in the debug dir, when
    CARDINAL_GEMINI_DEBUG_PAYLOADS=1."""
    if os.environ.get(DEBUG_PAYLOADS_ENV) != "1":
        return
    try:
        PATHS.debug_dir.mkdir(parents=True, exist_ok=True)
        with open(PATHS.debug_dir / "owner-input.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f} {status}\n")
    except OSError:
        pass


def capture_owner_input(payload: dict[str, Any], session_id: str, cwd: str) -> str | None:
    """Record what the owner typed as owner input of this session's
    Investigation (owner_input.capture, detached: the turn is taken and the
    prompt queued under the session lock, a detached process posts it).
    Returns the one-time disclosure to show the user now, else None; it is
    recorded as shown (mark_disclosure_shown) only after it was flushed. Only a
    bound session imports anything; never raises."""
    status = "unbound"
    try:
        from cardinal_core import investigation_events as ie

        if not ie.valid_session(session_id) or not ie.binding_path(Path.home(), session_id).exists():
            return None
        from cardinal_core import owner_input

        binding = ie.read_binding(Path.home(), session_id)
        prompt = payload.get("prompt")
        if owner_input.enabled(binding) and just_after_agent(session_id):
            # Another hook's AfterAgent block re-fired BeforeAgent with its
            # reason as the prompt: not typed (AFTER_AGENT_REFIRE_S).
            status = "not_typed"
            return None
        if owner_input.enabled(binding):
            try:
                expansion = custom_command_expansion(prompt, cwd)
            except CommandFilesUnsure:
                status = "custom_command_unsure"   # cannot tell: never captured
                return None
            if expansion:
                status = "custom_command"
                return None
        wiring = storyboard_wiring()
        agent_id = payload.get("agent_id")
        res = owner_input.capture(Path.home(), session_id, prompt, runtime="gemini", conn=wiring.connection(),
                                  client=wiring.client, environ=os.environ,
                                  agent_id=agent_id if isinstance(agent_id, str) else None,
                                  deadline=START + owner_input.BUDGET, detached=True, cwd=cwd,
                                  mark_disclosure=False)
        status = res.status
        return res.disclosure
    except Exception:
        status = "error"
        return None
    finally:
        owner_input_log(status)


def mark_disclosure_shown(session_id: str) -> None:
    """The owner-input disclosure was flushed to Gemini as a systemMessage:
    record it (owner_input.mark_disclosed), so capture starts with the next
    prompt. Never raises."""
    try:
        from cardinal_core import owner_input

        owner_input.mark_disclosed(Path.home(), session_id)
    except Exception:
        pass


def staged_for_prompt(session_id: str) -> str | None:
    """What was staged for this prompt (turn-end advisories, the discovery
    re-look) as model context (investigation_agent.prompt; no network)."""
    try:
        from cardinal_core import investigation_agent

        wiring = storyboard_wiring()
        investigation_agent.spawn_poller(wiring, session_id, str(INVESTIGATION_POLLER))
        items = investigation_agent.prompt(wiring, session_id).agent_context
        return "\n\n".join(items) if items else None
    except Exception:
        return None


def handle_before_agent(payload: dict[str, Any]) -> None:
    dump_debug_payload("BeforeAgent", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())

    # Sync half: local files only. The gate verdict, the prompt's context
    # and the disclosure share one stdout JSON object (Gemini parses a
    # single document).
    gate_out = None
    try:
        gate_out = limits.gate_output(PATHS, session_id, hook_event_name="BeforeAgent")
    except Exception:
        pass
    blocked = bool(gate_out and gate_out.get("decision") == "block")
    parts: list[str] = []
    try:
        decision = decision_context(session_id)
        if decision:
            parts.append(decision)
    except Exception:
        pass
    disclosure = None
    if not blocked:
        # A blocked prompt never reaches the model, and a block cannot show
        # the disclosure: nothing is captured or delivered for it.
        disclosure = capture_owner_input(payload, session_id, cwd)
        staged = staged_for_prompt(session_id)
        if staged:
            parts.append(staged)
    prompt_out = merge_prompt_output(gate_out, "\n\n".join(parts) or None, disclosure)
    if prompt_out:
        sys.stdout.write(json.dumps(prompt_out))
        sys.stdout.flush()
        if disclosure and disclosure in str(prompt_out.get("systemMessage") or ""):
            # R3: told only once the message reached Gemini (a failed write
            # raised above): the next prompt discloses again otherwise.
            mark_disclosure_shown(session_id)

    # Turn boundary: user_turn_seq increments; per-turn counters reset.
    state = session.load_progress(PATHS, session_id)
    session.begin_user_turn(state)
    session.save_progress(PATHS, session_id, state)

    # Network half (git_state + PR refresh + verdict refresh + the discovery
    # re-look) runs detached.
    relook = not blocked and relook_wanted(session_id)
    if connected() or relook:
        spawn_background({
            "kind": "before_agent",
            "session_id": session_id,
            "cwd": cwd,
            "command": initiative.detect_command(payload.get("prompt")),
            "ts_ns": time.time_ns(),
            "relook": relook,
        })


# The discovery re-look's network deadline in the detached child.
RELOOK_BUDGET_S = 4.0


def relook_wanted(session_id: str) -> bool:
    """Whether the detached child should re-look for related storyboards:
    discovery on, and a session bound to an Investigation (its staged items
    are delivered at the next AfterTool / BeforeAgent). Local only."""
    try:
        from cardinal_core import investigation_events as ie
        from cardinal_core import storyboard_authoring as sa

        return (not sa.discovery_disabled(PATHS, "gemini")
                and ie.valid_session(session_id)
                and ie.binding_path(Path.home(), session_id).exists())
    except Exception:
        return False


def relook_background(session_id: str, cwd: str) -> None:
    """The discovery re-look (storyboard_authoring.relook_if_moved: only
    when the branch or HEAD moved since the last look), its block staged
    for the session's next boundary (investigation_turn.stage)."""
    try:
        from cardinal_core import investigation_turn
        from cardinal_core import storyboard_authoring as sa

        wiring = storyboard_wiring()
        block = sa.relook_if_moved(PATHS, "gemini", session_id, cwd, connection=wiring.connection,
                                   client=wiring.client, deadline=time.monotonic() + RELOOK_BUDGET_S)
        if block:
            investigation_turn.stage(Path.home(), session_id, block)
    except Exception:
        pass


def before_agent_background(job: dict[str, Any]) -> None:
    session_id = str(job.get("session_id") or "")
    cwd = str(job.get("cwd") or "")
    if not session_id or not cwd:
        return
    if job.get("relook"):
        relook_background(session_id, cwd)
    if not connected():
        return
    ts_ns = job.get("ts_ns") if isinstance(job.get("ts_ns"), int) else time.time_ns()

    branch = None
    repo = None
    head_sha = initiative.git(["rev-parse", "HEAD"], cwd)
    if head_sha:
        branch = initiative.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        remote_url = initiative.git(["remote", "get-url", "origin"], cwd)
        repo = initiative.canonical_repo(remote_url)
        initiative_name, initiative_type = initiative.resolve_initiative(branch)
        # The branch's PR via `gh` (cached per repo+branch). Unresolved →
        # keys absent (log_record drops None); never fails the record.
        pr_number = pr_url = None
        try:
            pr_number, pr_url = decisions.resolve_pr(
                cwd, repo, branch, decisions.cache_dir(PATHS.runtime_dir),
                timeout=PR_REFRESH_TIMEOUT_SEC,
            )
        except Exception:
            pass
        attrs: dict[str, Any] = {
            "session_id": session_id,
            "cardinal_cwd": cwd,
            "cardinal_head_sha": head_sha,
            "cardinal_branch": branch,
            "cardinal_repo": repo,
            "cardinal_remote_url": remote_url,
            "cardinal_initiative_name": initiative_name,
            "cardinal_initiative_type": initiative_type,
            "cardinal_command": job.get("command"),
            "cardinal_pr_number": pr_number,
            "cardinal_pr_url": pr_url,
            **session.read_plan_stamp(PATHS),
        }
        emit_records([otlp.log_record("cardinal.git_state", attrs, ts_ns)])

    try:
        limits.maybe_refresh_verdict(PATHS, session_id=session_id, repo=repo, branch=branch)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AfterModel — per-model-call api_request + cardinal.turn_usage
# ---------------------------------------------------------------------------

def normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Map Gemini usage keys onto the Cardinal contract's bucket names.

    The hook's LLMResponse.usageMetadata carries only promptTokenCount,
    candidatesTokenCount and totalTokenCount (hookTranslator.ts
    toHookLLMResponse); thought + tool-use-prompt tokens are recovered as
    total - prompt - candidates. Cached-content tokens are not exposed.
    """
    def _int(*keys: str) -> int:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    usage = {
        "input_tokens": _int("input_tokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": _int("output_tokens", "response_tokens", "candidatesTokenCount"),
        "thought_tokens": _int("thought_tokens", "thoughtsTokenCount"),
        "cached_input_tokens": _int(
            "cached_input_tokens", "cache_read_tokens",
            "cached_content_token_count", "cachedContentTokenCount",
        ),
        "tool_use_tokens": _int("tool_use_tokens", "toolUsePromptTokenCount"),
    }
    total = _int("total_tokens", "totalTokenCount")
    if total and not usage["thought_tokens"]:
        remainder = total - usage["input_tokens"] - usage["output_tokens"] - usage["tool_use_tokens"]
        if remainder > 0:
            usage["thought_tokens"] = remainder
    return usage


def usage_attrs(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thought_tokens": usage.get("thought_tokens"),
        "cache_read_tokens": usage.get("cached_input_tokens"),
        "cache_read_input_tokens": usage.get("cached_input_tokens"),
    }


def final_chunk_usage(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    """(usageMetadata, model) for the stream's final chunk, else (None, None).
    AfterModelInput = {llm_request: {model, ...}, llm_response: {candidates:
    [{finishReason?}], usageMetadata?}} (hooks/types.ts, hookTranslator.ts)."""
    response = payload.get("llm_response")
    if not isinstance(response, dict):
        return None, None
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not any(
        isinstance(c, dict) and c.get("finishReason") for c in candidates
    ):
        return None, None
    meta = response.get("usageMetadata")
    if not isinstance(meta, dict):
        return None, None
    request = payload.get("llm_request")
    model = request.get("model") if isinstance(request, dict) else None
    return meta, model


def handle_after_model(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterModel", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return

    raw_usage, model = final_chunk_usage(payload)
    if raw_usage is None:
        return
    usage = normalize_usage(raw_usage)
    if not any(usage.values()):
        return

    state = session.load_progress(PATHS, session_id)
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
            session.write_plan_stamp(PATHS, stamp)

    plan_stamp = state.get("plan_stamp") if isinstance(state.get("plan_stamp"), dict) else {}

    state_conn = PATHS.read_state()
    base = {
        "session_id": session_id,
        "user_email": state_conn.get("user_email"),
        "agent_runtime": "gemini",
        "model": str(model) if model else None,
        **usage_attrs(usage),
    }
    cost_usd = pricing.compute_cost_usd(
        str(model) if model else None, usage, pricing.GEMINI_PRICING_USD_PER_M
    )
    if cost_usd is not None:
        base["cost_usd"] = cost_usd

    records: list[dict[str, Any]] = [
        otlp.log_record("api_request", base, ts_ns),
        otlp.log_record("cardinal.turn_usage", {
            **base,
            "ts": ts_ns,
            "user_turn_seq": state["user_turn_seq"],
            "turn_seq": state["turn_seq"],
            **plan_stamp,
        }, ts_ns + 1),
    ]

    # plan_state: once per session; re-emit on value change.
    plan_sig = f"{plan_stamp.get('plan_type') or ''}|{plan_stamp.get('rate_limit_tier') or ''}"
    if plan_sig != "|" and plan_sig != state.get("plan_state_sig"):
        records.append(otlp.log_record("cardinal.plan_state", {
            "session_id": session_id,
            "agent_runtime": "gemini",
            "ts": ts_ns,
            "plan_type": plan_stamp.get("plan_type"),
            "rate_limit_tier": plan_stamp.get("rate_limit_tier"),
        }, ts_ns + 2))
        state["plan_state_sig"] = plan_sig

    emit_in_background(records)
    session.end_model_call(state)
    session.save_progress(PATHS, session_id, state)


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


def tool_success(payload: dict[str, Any]) -> str:
    """AfterToolInput.tool_response = {llmContent, returnDisplay, error?}
    (core/coreToolHookTriggers.ts); a present `error` means failure."""
    success = payload.get("success")
    tool_response = payload.get("tool_response")
    if success is None and isinstance(tool_response, dict):
        success = not tool_response.get("error")
    if success is None:
        exit_code = payload.get("exit_code") or payload.get("exitCode")
        if isinstance(exit_code, (int, float)):
            success = "true" if int(exit_code) == 0 else "false"
        else:
            status = payload.get("status")
            if isinstance(status, str):
                success = "true" if status.lower() in {"ok", "success", "completed"} else "false"
    if isinstance(success, bool):
        return "true" if success else "false"
    if isinstance(success, str):
        return success.lower()
    return "true"


# ---------------------------------------------------------------------------
# AfterTool — evidence capture (the Gemini side of the generic evidence
# pipeline, cardinal_core.evidence_capture): every tool call, built-in or
# MCP, succeeded or failed
# ---------------------------------------------------------------------------

# Cardinal's own MCP server, as the extension manifest and cardinal-connect
# name it (mcpServers.cardinal). Its gateway mints a *witnessed* receipt for
# every call, so evidence capture skips it (the only tool it skips).
CARDINAL_MCP_SERVERS = ("cardinal",)
# The evidence CLI a capture's context line points the agent at.
EVIDENCE_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-evidence"

# Gemini CLI wraps each MCP text block it hands the model (tools/mcp-tool.ts
# wrapUntrusted, gemini-cli 0.50): "<untrusted_context>\n" + text with its
# closing tag escaped + "\n</untrusted_context>". Unwrapped before spooling,
# so the spool keeps what the MCP server returned.
_UNTRUSTED_OPEN = "<untrusted_context>\n"
_UNTRUSTED_CLOSE = "\n</untrusted_context>"
_UNTRUSTED_CLOSE_ESCAPED = "&lt;/untrusted_context&gt;"
# The label Gemini puts ahead of an image/audio/blob part
# (transformImageAudioBlock / transformResourceBlock); not tool output.
_MEDIA_LABEL_RE = re.compile(r"^\[Tool '.*' provided the following .+ with mime-type: .*\]$", re.DOTALL)
# What Gemini substitutes when an MCP result has no content array.
_UNPARSEABLE_RESULT = "[Error: Could not parse tool response]"

_GEMINI_VERSION: list[str | None] = []


def _unwrap_untrusted(text: str) -> str:
    if (len(text) >= len(_UNTRUSTED_OPEN) + len(_UNTRUSTED_CLOSE)
            and text.startswith(_UNTRUSTED_OPEN) and text.endswith(_UNTRUSTED_CLOSE)):
        inner = text[len(_UNTRUSTED_OPEN):len(text) - len(_UNTRUSTED_CLOSE)]
        return inner.replace(_UNTRUSTED_CLOSE_ESCAPED, "</untrusted_context>")
    return text


def gemini_tool_response(tool_response: Any) -> dict[str, Any] | None:
    """AfterTool `tool_response` = {llmContent, returnDisplay, error?}
    (coreToolHookTriggers.ts) for an MCP tool -> an MCP-shaped
    {content: [blocks], structuredContent?} that cardinal_core.evidence
    normalizes like any other client's result. llmContent is a string, one
    Part or a Part list: {text} parts become text blocks (unwrapped),
    {functionResponse: {response: {content, structuredContent?}}} parts
    (older Gemini CLI) contribute their raw MCP content, anything else
    (inlineData) counts as a non-text block. None for a failed call (a
    present `error`) or a result Gemini could not parse: only a successful
    result is evidence."""
    if not isinstance(tool_response, dict) or tool_response.get("error"):
        return None
    llm = tool_response.get("llmContent")
    if isinstance(llm, list):
        parts: list[Any] = llm
    elif isinstance(llm, (str, dict)):
        parts = [llm]
    else:
        parts = []
    blocks: list[Any] = []
    out: dict[str, Any] = {}
    for i, part in enumerate(parts):
        if isinstance(part, str):
            blocks.append({"type": "text", "text": _unwrap_untrusted(part)})
            continue
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            nxt = parts[i + 1] if i + 1 < len(parts) else None
            if isinstance(nxt, dict) and "inlineData" in nxt and _MEDIA_LABEL_RE.match(text):
                continue
            blocks.append({"type": "text", "text": _unwrap_untrusted(text)})
            continue
        fr = part.get("functionResponse")
        response = fr.get("response") if isinstance(fr, dict) else None
        if isinstance(response, dict) and isinstance(response.get("content"), list):
            blocks.extend(response["content"])
            if response.get("structuredContent") is not None:
                out["structuredContent"] = response["structuredContent"]
            continue
        blocks.append({"type": "inlineData" if "inlineData" in part else "other"})
    if not blocks:
        display = tool_response.get("returnDisplay")
        if isinstance(display, str) and display:
            blocks.append({"type": "text", "text": display})
    if len(blocks) == 1 and blocks[0] == {"type": "text", "text": _UNPARSEABLE_RESULT}:
        return None
    out["content"] = blocks
    return out


def gemini_version() -> str | None:
    """Best-effort version of the installed Gemini CLI: Gemini's hook
    payload and environment carry none, so resolve `gemini` on PATH to its
    package.json (@google/gemini-cli) and read `version`. File reads only,
    at most once per process; None when it cannot be found."""
    if _GEMINI_VERSION:
        return _GEMINI_VERSION[0]
    version = None
    try:
        exe = shutil.which("gemini")
        if exe:
            d = Path(os.path.realpath(exe)).parent
            for _ in range(6):
                pkg = d / "package.json"
                if pkg.is_file() and pkg.stat().st_size < 1 << 20:
                    data = json.loads(pkg.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("name") == "@google/gemini-cli":
                        v = data.get("version")
                        version = v if isinstance(v, str) else None
                        break
                if d.parent == d:
                    break
                d = d.parent
    except (OSError, ValueError):
        version = None
    _GEMINI_VERSION.append(version)
    return version


def mcp_server_and_tool(payload: dict[str, Any], raw_name: str) -> tuple[str, str] | None:
    """(server, tool) for an MCP call: from mcp_context {server_name,
    tool_name} (hooks/types.ts McpToolContext), else from an
    `mcp__<server>__<tool>` name. Gemini's own `mcp_<server>_<tool>` name is
    ambiguous (either part may hold `_`), so it alone is not enough."""
    mcp_context = payload.get("mcp_context")
    if isinstance(mcp_context, dict):
        server = mcp_context.get("server_name")
        tool = mcp_context.get("tool_name") or raw_name
        if isinstance(server, str) and server and isinstance(tool, str) and tool:
            return server, tool
    return evidence.split_mcp_tool(raw_name)


def gemini_error_text(tool_response: Any) -> str | None:
    """The failure text of an AfterTool call that failed (a present
    `error`): what the model saw (llmContent text), else the error's
    message. None for a call that did not fail."""
    if not isinstance(tool_response, dict) or not tool_response.get("error"):
        return None
    llm = tool_response.get("llmContent")
    if isinstance(llm, str) and llm.strip():
        return _unwrap_untrusted(llm)
    if isinstance(llm, dict) and isinstance(llm.get("text"), str) and llm["text"].strip():
        return _unwrap_untrusted(llm["text"])
    err = tool_response.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg
    if isinstance(err, str) and err.strip():
        return err
    display = tool_response.get("returnDisplay")
    if isinstance(display, str) and display.strip():
        return display
    return "error"


def evidence_call(payload: dict[str, Any]):
    """A ToolCall for any AfterTool payload (built-in tool or MCP), or None
    when there is nothing to record (no tool name, Gemini's own "could not
    parse" placeholder)."""
    from cardinal_core import evidence_capture as cap

    raw_name = payload.get("tool_name") or payload.get("toolName")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    parts = mcp_server_and_tool(payload, raw_name)
    if parts is not None:
        server, tool = parts
        tool_name = f"mcp__{server}__{tool}"
        kind = "cardinal" if server in CARDINAL_MCP_SERVERS else "mcp"
        source = {"kind": kind, "server": server, "runtime": "gemini"}
    else:
        tool, tool_name = raw_name, raw_name
        source = cap.builtin_source("gemini")
    tool_response = payload.get("tool_response")
    if tool_response is None:
        tool_response = payload.get("toolResponse")
    error = gemini_error_text(tool_response)
    response = None
    if error is None:
        if isinstance(tool_response, dict) and ("llmContent" in tool_response or "returnDisplay" in tool_response):
            response = gemini_tool_response(tool_response)
            if response is None:
                return None
        else:
            # Any other shape (a future Gemini CLI, another tool API) is kept
            # as it came, through the generic normalizer: an unfamiliar
            # result is never dropped.
            response = tool_response
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = payload.get("toolInput")
    tuid = payload.get("tool_call_id") or payload.get("toolCallId") or payload.get("tool_use_id")
    cwd = payload.get("cwd")
    return cap.ToolCall(
        runtime="gemini",
        tool_name=tool_name,
        source=source,
        tool=tool,
        tool_input=tool_input,
        response=response,
        error=error,
        session_id=session_id_from_payload(payload),
        tool_use_id=tuid if isinstance(tuid, str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
        client=evidence.client_string("gemini", gemini_version()),
    )


def capture_evidence(payload: dict[str, Any]) -> str | None:
    """Record one tool call (any tool, succeeded or failed) in the local
    evidence spool through the generic pipeline and return the context
    line (`[evidence:ev_...]`, or `[evidence:ev_... withheld: ...]`); None
    for Cardinal's own tools, when the user opted out
    (CARDINAL_EVIDENCE_CAPTURE=0 or ~/.cardinal/evidence/disabled), with
    CARDINAL_EVIDENCE_CONTEXT=0, or on any failure. Local file work only;
    never raises."""
    try:
        from cardinal_core import evidence_capture as cap

        call = evidence_call(payload)
        if call is None:
            return None
        # Never silent: a call the pipeline cannot finish in time is kept as a
        # withheld stub (capture_call_guarded).
        got = cap.capture_call_guarded(call, Path.home(), promote_cmd=shlex.quote(str(EVIDENCE_CLI)))
        return got.line if got is not None else None
    except BaseException:
        return None


# ---------------------------------------------------------------------------
# Storyboard associations (cardinal_core.storyboard_agent): the session id +
# discovery block at SessionStart, the files write_file / replace edited
# (recorded in the AfterTool run: no new hook process), and context stamping
# on Cardinal's storyboard tools (BeforeTool: Gemini applies a returned
# hookSpecificOutput.tool_input to the call). Payload shapes captured from
# gemini 0.50.0 (tests/test_gemini_storyboard.py fixtures).
# ---------------------------------------------------------------------------

STORYBOARD_CLI = Path(__file__).resolve().parent.parent / "scripts" / "cardinal-storyboard"
INVESTIGATION_POLLER = Path(__file__).resolve().parent / "investigation-poller.py"
# The plugin root the Checks preview template (checks/skill/) ships in.
CHECKS_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STORYBOARD_SERVER = "cardinal"
# The four tools stamped_input fills (link's about refs are the model's);
# the same set as the BeforeTool matcher.
STORYBOARD_TOOL_RE = re.compile(r"^storyboard__(create|add_act|publish|find)$")
STORYBOARD_FLAT_RE = re.compile(r"^mcp_cardinal_storyboard__(create|add_act|publish|find)$")
# The BeforeTool handler cardinal-connect registers (extension hooks.json,
# or settings.json without the extension).
BEFORE_TOOL_HOOK_NEEDLE = f"--event BeforeTool # {SCOPE_NAME}"
EDIT_TOOLS = ("write_file", "replace", "edit")


def storyboard_wiring():
    from cardinal_core import storyboard_agent

    return storyboard_agent.Wiring("gemini", PATHS, PLUGIN_VERSION, cli=str(STORYBOARD_CLI))


def storyboard_tool(payload: dict[str, Any]) -> str | None:
    """The storyboard tool's short name (create, add_act, publish, find)
    of a call to Cardinal's MCP server, else None. mcp_context
    {server_name, tool_name} first (the flattened mcp_<server>_<tool> name
    is ambiguous); the flattened name only without it."""
    mcp_context = payload.get("mcp_context")
    if isinstance(mcp_context, dict) and mcp_context.get("server_name"):
        if mcp_context.get("server_name") != STORYBOARD_SERVER:
            return None
        m = STORYBOARD_TOOL_RE.match(str(mcp_context.get("tool_name") or ""))
        return m.group(1) if m else None
    m = STORYBOARD_FLAT_RE.match(str(payload.get("tool_name") or ""))
    return m.group(1) if m else None


def edited_file(payload: dict[str, Any]) -> str | None:
    """The file a successful write_file / replace call edited: tool_input
    file_path (as the model gave it, relative to cwd or absolute), else
    returnDisplay.filePath (absolute). None for another tool or a failed
    call (a present `error`)."""
    if payload.get("tool_name") not in EDIT_TOOLS:
        return None
    response = payload.get("tool_response")
    if not isinstance(response, dict) or gemini_error_text(response) is not None:
        return None
    tool_input = payload.get("tool_input")
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not (isinstance(path, str) and path):
        display = response.get("returnDisplay")
        path = display.get("filePath") if isinstance(display, dict) else None
    return path if isinstance(path, str) and path else None


def record_edit(payload: dict[str, Any]) -> None:
    try:
        path = edited_file(payload)
        if path is None:
            return
        from cardinal_core import storyboard_agent

        cwd = payload.get("cwd")
        storyboard_agent.record_edits(storyboard_wiring(), session_id_from_payload(payload), [path],
                                      cwd if isinstance(cwd, str) else None)
    except Exception:
        pass


def stamping_registered() -> bool:
    """Whether a Cardinal BeforeTool handler is registered: in the installed
    extension's hooks/hooks.json, or ~/.gemini/settings.json `hooks` (a
    no-extension install). An upgraded plugin runs this code before
    cardinal-connect has re-registered hooks (the old extension copy has
    no BeforeTool)."""
    from cardinal_core import storyboard_agent

    gemini = Path.home() / ".gemini"
    return any(storyboard_agent.hooks_file_registers(path, "BeforeTool", BEFORE_TOOL_HOOK_NEEDLE)
               for path in (gemini / "extensions" / "cardinal" / "hooks" / "hooks.json",
                            gemini / "settings.json"))


def auto_context(wiring) -> bool:
    """Whether BeforeTool will fill an absent session_id / context: not
    turned off with CARDINAL_STORYBOARD_CONTEXT=0, and registered."""
    return not wiring.context_disabled() and stamping_registered()


def handle_before_tool(payload: dict[str, Any]) -> None:
    """BeforeTool on Cardinal's storyboard tools: fill an absent session_id /
    context (storyboard_agent.stamped_input) by returning the stamped
    tool_input. Prints nothing (the call runs unchanged) when nothing was
    added or on any failure; CARDINAL_STORYBOARD_CONTEXT=0 opts out."""
    dump_debug_payload("BeforeTool", payload)
    tool = storyboard_tool(payload)
    if tool is None:
        return
    from cardinal_core import storyboard_agent

    out = storyboard_agent.stamped_input(storyboard_wiring(), tool, payload.get("tool_input"),
                                         session_id_from_payload(payload), payload.get("cwd"))
    if out is None:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "BeforeTool",
            "tool_input": out,
        }
    }))
    sys.stdout.flush()


def checks_gate_decision(payload: dict[str, Any]) -> dict | None:
    """The deny for Gemini CLI's built-in activate_skill naming the checks
    skill (tool_input.name) while the Checks preview is off, else None.
    gemini-cli 0.63.0 runs BeforeTool for activate_skill and blocks the call
    on stdout {"decision": "deny", "reason"} with exit 0. A session that
    loaded the skill before `cardinal-checks off` keeps listing it until the
    user's /skills reload; this keeps it from being used meanwhile."""
    if payload.get("tool_name") not in (None, "activate_skill"):
        return None
    tool_input = payload.get("tool_input")
    name = tool_input.get("name") if isinstance(tool_input, dict) else None
    from cardinal_core import checks_install

    if not isinstance(name, str) or name.strip() != checks_install.SKILL_NAME:
        return None
    if checks_install.is_enabled("gemini"):
        return None
    return {"decision": "deny", "reason": checks_install.DENY_REASON}


def handle_before_tool_checks_gate(payload: dict[str, Any]) -> None:
    """BeforeTool --role checks-gate (its own hook group, matcher
    ^activate_skill$: Gemini dedupes an event's hooks by name:command)."""
    out = checks_gate_decision(payload)
    if out is not None:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# AfterTool — the Investigation and storyboard authoring
# (cardinal_core.investigation_agent, storyboard_authoring)
# ---------------------------------------------------------------------------

# Gemini's run_shell_command result ends with "Exit Code: N" when the
# command failed (gemini-cli 0.50.0 shell tool, chunk-W4YMYD53.js:314500).
_SHELL_EXIT_RE = re.compile(r"(?m)^Exit Code: (-?\d+)\s*$")
SHELL_TOOL = "run_shell_command"


def mcp_server(payload: dict[str, Any]) -> str | None:
    """mcp_context.server_name of an MCP call (the flattened
    mcp_<server>_<tool> name alone is ambiguous), else None."""
    ctx = payload.get("mcp_context")
    server = ctx.get("server_name") if isinstance(ctx, dict) else None
    return server if isinstance(server, str) and server else None


def mcp_result(payload: dict[str, Any]) -> Any:
    """The tool result in MCP shape for cardinal_core (storyboard_authoring
    takes {content, structuredContent}): Gemini's {llmContent,
    returnDisplay} converted (gemini_tool_response, <untrusted_context>
    removed); None for a failed call."""
    response = payload.get("tool_response")
    if isinstance(response, dict) and ("llmContent" in response or "returnDisplay" in response):
        return gemini_tool_response(response)
    return response


def shell_failure(payload: dict[str, Any], session_id: str | None) -> str | None:
    """investigation_failures.observe for a run_shell_command call: a
    recording command (cardinal-storyboard investigation …, cardinal-evidence
    promote …) that could not reach Cardinal says so once. The exit code is
    read from Gemini's "Exit Code: N" line (none: it succeeded). Local only."""
    try:
        if payload.get("tool_name") != SHELL_TOOL:
            return None
        from cardinal_core import investigation_failures

        response = payload.get("tool_response")
        text = gemini_error_text(response)
        failed = text is not None
        if text is None and isinstance(response, dict):
            llm = response.get("llmContent")
            text = llm if isinstance(llm, str) else None
        m = _SHELL_EXIT_RE.search(text or "")
        code = int(m.group(1)) if m else (None if failed else 0)
        result = {"exit_code": code, "output": text or ""} if code is not None else {"output": text or ""}
        return investigation_failures.observe(Path.home(), session_id, payload.get("tool_input"), result,
                                              text if failed else None, runtime="gemini", tool_name=SHELL_TOOL,
                                              succeeded=not failed)
    except Exception:
        return None


def authoring_lines(payload: dict[str, Any], session_id: str | None) -> list[str]:
    """Storyboard authoring around Cardinal's tools: the evidence token
    (storyboard__create / add_act / preview), the evidence hold after a
    successful storyboard__checkpoint, the hero upload after
    storyboard__publish (network, UPLOAD_TIMEOUT_S), and the storyboards
    about a file write_file / replace just edited (edit lookup, network,
    ending by START + EDIT_BUDGET_S). Never raises."""
    lines: list[str] = []
    try:
        from cardinal_core import storyboard_authoring as sa

        name = payload.get("tool_name")
        server = mcp_server(payload)
        if sa.is_cardinal_tool("gemini", name, server=server):
            result = mcp_result(payload)
            sa.observe_cardinal_result(PATHS, "gemini", name, result, session_id=session_id, server=server)
            if sa.is_cardinal_tool("gemini", name, "storyboard__publish", server=server):
                wiring = storyboard_wiring()
                text = sa.on_publish(PATHS, "gemini", result, connection=wiring.connection, client=wiring.client)
                if text:
                    lines.append(text)
            return lines
        path = edited_file(payload)
        if path and session_id:
            wiring = storyboard_wiring()
            block = sa.edit_lookup(PATHS, "gemini", session_id, path, payload.get("cwd"),
                                   connection=wiring.connection, client=wiring.client,
                                   deadline=START + sa.EDIT_BUDGET_S)
            if block:
                lines.append(block)
    except Exception:
        pass
    return lines


def advisories(session_id: str | None) -> list[str]:
    """The Investigation's deliverable items at this tool boundary
    (investigation_agent.deliver: staged items, the poller's inbox, an owed
    challenge or directive; no network). Gemini names no subagent on a tool
    call (AfterTool carries no agent id), so every call counts as the main
    thread's."""
    lines: list[str] = []
    try:
        from cardinal_core import investigation_agent

        wiring = storyboard_wiring()
        investigation_agent.spawn_poller(wiring, session_id, str(INVESTIGATION_POLLER))
        investigation_agent.deliver(wiring, session_id, lines.append)
    except Exception:
        pass
    return lines


def handle_after_tool_preview(payload: dict[str, Any]) -> None:
    """AfterTool --role preview (its own hook group, matcher
    storyboard__preview$, 150 s): render Cardinal's storyboard__preview
    result locally (storyboard_authoring.on_preview) and name the PNGs."""
    from cardinal_core import storyboard_authoring as sa

    if not sa.is_cardinal_tool("gemini", payload.get("tool_name"), "storyboard__preview",
                               server=mcp_server(payload)):
        return
    tool_input = payload.get("tool_input")
    ctx = sa.on_preview(PATHS, "gemini", mcp_result(payload), session_id=session_id_from_payload(payload),
                        tool_input=tool_input if isinstance(tool_input, dict) else None, started=START)
    if not ctx:
        return
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "AfterTool", "additionalContext": ctx}}))
    sys.stdout.flush()


def handle_after_tool(payload: dict[str, Any]) -> None:
    dump_debug_payload("AfterTool", payload)
    session_id = session_id_from_payload(payload)
    evidence_line = capture_evidence(payload)
    record_edit(payload)
    lines = [line for line in (shell_failure(payload, session_id), evidence_line) if line]
    lines += authoring_lines(payload, session_id)
    lines += advisories(session_id)
    if lines:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "AfterTool",
                "additionalContext": "\n\n".join(lines),
            }
        }))
        sys.stdout.flush()
    if not session_id:
        return
    raw_name = str(payload.get("tool_name") or payload.get("toolName") or "")
    if not raw_name:
        return
    args = parse_args_json(payload.get("tool_input") or payload.get("toolInput") or payload.get("arguments"))
    tool_name, params, target = normalize_tool(raw_name, args)
    qualified_name = raw_name
    # MCP tools carry mcp_context {server_name, tool_name} (hooks/types.ts
    # McpToolContext); Gemini's own tool_name is not `mcp__`-qualified.
    mcp_context = payload.get("mcp_context")
    if isinstance(mcp_context, dict) and mcp_context.get("server_name"):
        server = str(mcp_context["server_name"])
        tool = str(mcp_context.get("tool_name") or raw_name)
        tool_name = "mcp_tool"
        params = {"mcp_server_name": server, "mcp_tool_name": tool}
        qualified_name = f"mcp__{server}__{tool}"
    if target is None:
        key = TARGET_KEYS.get(tool_name) or TARGET_KEYS.get(raw_name)
        if key:
            v = args.get(key)
            if isinstance(v, str) and v:
                target = v

    state = session.load_progress(PATHS, session_id)
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
        # turn_tool carries the qualified MCP name (harvester's clustering
        # signal); tool_result keeps the normalized form.
        attrs["tool_name"] = qualified_name
        attrs["mcp_server_name"] = params.get("mcp_server_name")
        attrs["mcp_tool_name"] = params.get("mcp_tool_name")
    elif tool_name == "Bash":
        classified = bashclass.classify_bash_command(str(params.get("full_command") or ""))
        if classified is not None:
            bash_class, bash_multi = classified
            attrs["bash_class"] = bash_class
            if bash_multi:
                attrs["bash_multi"] = True

    result_attrs: dict[str, Any] = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "tool_name": tool_name,
        "success": tool_success(payload),
        "tool_parameters": json.dumps(params, separators=(",", ":")) if params else None,
        "tool_input": json.dumps(args, separators=(",", ":")) if args else None,
    }

    emit_in_background([
        otlp.log_record("cardinal.turn_tool", attrs, ts_ns),
        otlp.log_record("tool_result", result_attrs, ts_ns + 1),
    ])
    state["tool_seq"] += 1
    session.save_progress(PATHS, session_id, state)


# ---------------------------------------------------------------------------
# AfterAgent — the main agent's turn end; never a block
# ---------------------------------------------------------------------------

# The turn end's network work (one bounded events check when the poller is
# stale, the owner-input outbox flush) ends by START + this; the hook has
# HOOK_TIMEOUT_MS (10 s).
AFTER_AGENT_BUDGET_S = 2.6

# When ANOTHER hook's AfterAgent blocks, gemini-cli re-fires BeforeAgent with
# the block reason as `prompt` (GeminiClient.sendMessageStream): that text is
# not what the owner typed. Every AfterAgent turn end records when it ran
# (wall clock, in a small per-session file under ~/.gemini/cardinal/, not the
# binding, so no session lock is contended), and a BeforeAgent within
# AFTER_AGENT_REFIRE_S of it is not captured (not_typed).
AFTER_AGENT_REFIRE_S = 2.0
_SAFE_SID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def after_agent_marker(session_id: str) -> Path | None:
    """~/.gemini/cardinal/after-agent/<sid>.json, or None for an id that is
    not a safe file name."""
    if not isinstance(session_id, str) or not _SAFE_SID_RE.fullmatch(session_id) or session_id in (".", ".."):
        return None
    return PATHS.runtime_dir / "after-agent" / f"{session_id}.json"


def record_after_agent(session_id: str | None) -> None:
    """Note that this session's AfterAgent ran now ({last_after_agent_at}).
    Never raises."""
    try:
        path = after_agent_marker(session_id or "")
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"last_after_agent_at": time.time()}), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except Exception:
        pass


def just_after_agent(session_id: str, now: float | None = None) -> bool:
    """Whether this session's AfterAgent ran within AFTER_AGENT_REFIRE_S of
    `now` (wall clock): BeforeAgent is then most likely the re-fire of
    another hook's AfterAgent block. The wall clock may step: a recorded time
    up to the window AHEAD of now counts too (never capture when unsure).
    Local file read only; never raises."""
    try:
        path = after_agent_marker(session_id)
        if path is None or not path.exists():
            return False
        at = json.loads(path.read_text(encoding="utf-8")).get("last_after_agent_at")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            return False
        now = time.time() if now is None else now
        return abs(now - at) < AFTER_AGENT_REFIRE_S
    except Exception:
        return False


def turn_end(payload: dict[str, Any], session_id: str | None) -> None:
    """investigation_agent.turn_end on runtime "gemini": what is deliverable
    (the inbox, a sync check, an open directive, decision reminders) is
    STAGED for the next BeforeAgent / AfterTool, the owner-input outbox is
    flushed, and the owner-input turn boundary is marked. Its Delivery is
    never printed: on Gemini an AfterAgent block re-enters the model as a
    plain user-role message and re-fires BeforeAgent with it as the prompt
    (gemini-cli 0.50.0 GeminiClient.sendMessageStream,
    chunk-W4YMYD53.js:335735-335757), so advisory text would become owner
    input (R1; investigation_turn.TURN_END_BLOCK["gemini"] is False).
    The time it ran is recorded (record_after_agent) at its start and again
    at its end, since a re-fired BeforeAgent follows the last AfterAgent hook
    to finish. Never raises."""
    record_after_agent(session_id)
    try:
        from cardinal_core import investigation_agent

        cwd = payload.get("cwd")
        investigation_agent.turn_end(storyboard_wiring(), session_id,
                                     stop_hook_active=payload.get("stop_hook_active") is True,
                                     deadline=START + AFTER_AGENT_BUDGET_S,
                                     cwd=cwd if isinstance(cwd, str) else None)
    except Exception:
        pass
    record_after_agent(session_id)

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
    # Gemini CLI's real AfterAgent is the main agent's turn end
    # (core/client.ts, outermost call) with {prompt, prompt_response,
    # stop_hook_active} — not a subagent stop. Never report it as one.
    if "prompt_response" in payload or "stop_hook_active" in payload:
        turn_end(payload, session_id)
        return

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
        "model": (
            payload.get("model")
            or payload.get("modelName")
            or payload.get("model_name")
            or (usage_block.get("model") if isinstance(usage_block, dict) else None)
        ),
        "total_tokens": total_tokens,
        "duration_ms": payload.get("duration_ms") or payload.get("durationMs"),
        "status": payload.get("status"),
        **session.read_plan_stamp(PATHS),
    }
    identifying = any(
        attrs[k] is not None
        for k in ("subagent_type", "agent_id", "subagent_description",
                  "total_tokens", "duration_ms")
    )
    if not identifying:
        return
    emit_in_background([otlp.log_record("cardinal.subagent_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# PreCompress — context-window compaction slice
# ---------------------------------------------------------------------------

def handle_pre_compress(payload: dict[str, Any]) -> None:
    dump_debug_payload("PreCompress", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    # PreCompressInput carries only `trigger` (manual|auto); the context
    # slice keys are probed in case a future host adds them.
    attrs = {
        "session_id": session_id,
        "agent_runtime": "gemini",
        "context_tokens": payload.get("context_tokens") or payload.get("contextTokens"),
        "context_window_size": payload.get("context_window_size") or payload.get("contextWindowSize"),
        "context_usage_percent": payload.get("context_usage_percent") or payload.get("contextUsagePercent"),
        "trigger": payload.get("trigger"),
        "messages_to_compact": payload.get("messages_to_compact") or payload.get("messagesToCompact"),
        "is_first_compaction": payload.get("is_first_compaction") or payload.get("isFirstCompaction"),
        **session.read_plan_stamp(PATHS),
    }
    # Presence of `plan.compact_trigger` distinguishes this downstream from
    # per-model-call plan_usage.
    if payload.get("trigger"):
        attrs["plan.compact_trigger"] = payload.get("trigger")
    emit_in_background([otlp.log_record("cardinal.plan_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# SessionStart — convention prompt + budget standing
# ---------------------------------------------------------------------------

SESSION_START_DISCOVERY_BY_S = 7.0


def handle_session_start(payload: dict[str, Any]) -> None:
    dump_debug_payload("SessionStart", payload)
    cwd = str(payload.get("cwd") or os.getcwd())
    parts: list[str] = []
    if initiative.is_git_repo(cwd):
        context = session.convention_prompt("Gemini CLI")
        try:
            # One bounded (1.5s) limits fetch per session, only when the backend
            # advertises spend limits; its result is this hook's output.
            standing = session.budget_standing(PATHS, session_id_from_payload(payload), cwd)
            if standing:
                context = f"{context}\n\n{standing}"
        except Exception:
            pass
        parts.append(context)
    sid = session_id_from_payload(payload)
    try:
        # The session's Investigation (bootstrap bounded by its own 3 s
        # request; the guidance with the labeled links), its background
        # poller, then the session id line (unless the guidance has it) +
        # the storyboards that may relate to this checkout (a 2 s network
        # deadline, never past START + 7 s; the hook has 10 s).
        from cardinal_core import investigation_agent, storyboard_agent

        wiring = storyboard_wiring()
        live = investigation_agent.start(wiring, sid, payload.get("source"), cwd=cwd)
        investigation_agent.spawn_poller(wiring, sid, str(INVESTIGATION_POLLER))
        if live:
            parts.append(live)
        storyboard = storyboard_agent.session_start_text(
            wiring, cwd, sid, include_session_line=not bool(live), auto_context=auto_context(wiring),
            deadline=min(START + SESSION_START_DISCOVERY_BY_S, time.monotonic() + 2.0))
        if storyboard:
            parts.append(storyboard)
    except Exception:
        pass
    try:
        # Local only: how to connect (unconnected), or an MCP key without a URL.
        from cardinal_core import storyboard_authoring as sa

        source = payload.get("source")
        for line in (sa.connect_hint(PATHS, "gemini", source=source),
                     sa.stray_key_warning(PATHS, "gemini", sid=sid, source=source)):
            if line:
                parts.append(line)
    except Exception:
        pass
    try:
        # Checks preview (opt-in, `scripts/cardinal-checks on|off`): make the
        # installed skill match the setting (it takes effect at the next
        # session: Gemini CLI has no skill reload or skill-tool deny) and, while on
        # and connected, the routing rule. Local only. The skill lives in
        # Cardinal's extension (~/.gemini/extensions/cardinal/skills/checks).
        from cardinal_core import checks_install
        from cardinal_core import storyboard_authoring as sa

        _, rule = checks_install.session_start(
            "gemini", CHECKS_PLUGIN_ROOT, connected=sa.is_connected(PATHS, "gemini"))
        if rule:
            parts.append(rule)
    except Exception:
        pass
    if not parts:
        return
    context = "\n\n".join(parts)
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
    # Retention: leave the per-session progress + verdict files behind.
    # cardinal-disconnect removes ~/.gemini/cardinal/ wholesale.
    return


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

HANDLERS = {
    "SessionStart": handle_session_start,
    "BeforeAgent": handle_before_agent,
    "BeforeTool": handle_before_tool,
    "AfterModel": handle_after_model,
    "AfterTool": handle_after_tool,
    "AfterAgent": handle_after_agent,
    "PreCompress": handle_pre_compress,
    "SessionEnd": handle_session_end,
}
# (event, --role) -> handler: the extra hook groups cardinal-connect registers.
ROLE_HANDLERS = {
    ("AfterTool", "preview"): handle_after_tool_preview,
    ("BeforeTool", "checks-gate"): handle_before_tool_checks_gate,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event")
    # A second hook group on the same event needs its own command (Gemini
    # dedupes by name:command): AfterTool --role preview, BeforeTool --role
    # checks-gate.
    parser.add_argument("--role", default="")
    parser.add_argument("--background", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.background:
        try:
            run_background_job(Path(args.background))
        except Exception:
            pass
        silent_exit()

    raw = _PRELOADED_STDIN if _PRELOADED_STDIN is not None else sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    handler = ROLE_HANDLERS.get((args.event or "", args.role)) if args.role else HANDLERS.get(args.event or "")
    # R1: AfterAgent never blocks. Whatever a handler might print there is
    # discarded, so no decision, reason or context can leave this process.
    real_stdout = sys.stdout
    if args.event == "AfterAgent":
        sys.stdout = io.StringIO()
    try:
        if handler:
            handler(payload)
    except Exception:
        pass
    finally:
        sys.stdout = real_stdout
    silent_exit()


if __name__ == "__main__":
    main()
