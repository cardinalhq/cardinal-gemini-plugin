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

  SessionStart  → convention prompt + budget standing (additionalContext)
  BeforeAgent   → spend-limits gate + decision prompt (additionalContext);
                  background: cardinal.git_state (+PR) + verdict refresh
  AfterModel    → api_request + cardinal.turn_usage, final chunk only
                  (fires per streamed chunk; non-final chunks exit early)
  AfterTool     → cardinal.turn_tool + tool_result (per tool call)
  AfterAgent    → cardinal.subagent_usage for subagent-shaped payloads only;
                  not registered by cardinal-connect (Gemini fires it per
                  main-agent turn with {prompt, prompt_response})
  PreCompress   → cardinal.plan_usage (compaction trigger)
  SessionEnd    → no-op
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
from cardinal_core import bashclass, decisions, initiative, limits, otlp, pricing, session  # noqa: E402
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


def merge_prompt_output(gate_out: dict[str, Any] | None, extra_context: str | None) -> dict[str, Any] | None:
    """One BeforeAgent stdout JSON object from the spend-limits gate and the
    decision prompt. A blocked turn carries only the block verdict."""
    if not extra_context or (gate_out and gate_out.get("decision") == "block"):
        return gate_out
    out = dict(gate_out or {})
    hso = dict(out.get("hookSpecificOutput") or {})
    hso["hookEventName"] = "BeforeAgent"
    prior = hso.get("additionalContext")
    hso["additionalContext"] = f"{prior}\n\n{extra_context}" if prior else extra_context
    out["hookSpecificOutput"] = hso
    return out


def handle_before_agent(payload: dict[str, Any]) -> None:
    dump_debug_payload("BeforeAgent", payload)
    session_id = session_id_from_payload(payload)
    if not session_id:
        return
    cwd = str(payload.get("cwd") or os.getcwd())

    # Sync half: local files only. The gate verdict and the decision prompt
    # share one stdout JSON object (Gemini parses a single document).
    gate_out = None
    try:
        gate_out = limits.gate_output(PATHS, session_id, hook_event_name="BeforeAgent")
    except Exception:
        pass
    extra_context = None
    try:
        extra_context = decision_context(session_id)
    except Exception:
        pass
    prompt_out = merge_prompt_output(gate_out, extra_context)
    if prompt_out:
        sys.stdout.write(json.dumps(prompt_out))
        sys.stdout.flush()

    # Turn boundary: user_turn_seq increments; per-turn counters reset.
    state = session.load_progress(PATHS, session_id)
    session.begin_user_turn(state)
    session.save_progress(PATHS, session_id, state)

    # Network half (git_state + PR refresh + verdict refresh) runs detached.
    if connected():
        spawn_background({
            "kind": "before_agent",
            "session_id": session_id,
            "cwd": cwd,
            "command": initiative.detect_command(payload.get("prompt")),
            "ts_ns": time.time_ns(),
        })


def before_agent_background(job: dict[str, Any]) -> None:
    session_id = str(job.get("session_id") or "")
    cwd = str(job.get("cwd") or "")
    if not session_id or not cwd:
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
# AfterAgent — subagent stop (not registered; see module docstring)
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
    # Gemini CLI's real AfterAgent is the main agent's turn end
    # (core/client.ts, outermost call) with {prompt, prompt_response,
    # stop_hook_active} — not a subagent stop. Never report it as one.
    if "prompt_response" in payload or "stop_hook_active" in payload:
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

def handle_session_start(payload: dict[str, Any]) -> None:
    dump_debug_payload("SessionStart", payload)
    cwd = str(payload.get("cwd") or os.getcwd())
    if not initiative.is_git_repo(cwd):
        return
    context = session.convention_prompt("Gemini CLI")
    try:
        # One bounded (1.5s) limits fetch per session, only when the backend
        # advertises spend limits; its result is this hook's output.
        standing = session.budget_standing(PATHS, session_id_from_payload(payload), cwd)
        if standing:
            context = f"{context}\n\n{standing}"
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
    # Retention: leave the per-session progress + verdict files behind.
    # cardinal-disconnect removes ~/.gemini/cardinal/ wholesale.
    return


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

HANDLERS = {
    "SessionStart": handle_session_start,
    "BeforeAgent": handle_before_agent,
    "AfterModel": handle_after_model,
    "AfterTool": handle_after_tool,
    "AfterAgent": handle_after_agent,
    "PreCompress": handle_pre_compress,
    "SessionEnd": handle_session_end,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event")
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

    handler = HANDLERS.get(args.event or "")
    try:
        if handler:
            handler(payload)
    except Exception:
        pass
    silent_exit()


if __name__ == "__main__":
    main()
