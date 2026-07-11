# Claude / Codex parity — spec & plan of action

Status: **in progress** · Target plugin version: **0.1.0** · Sources of truth:
`cardinal-claude-plugin` v0.12.x, `cardinal-codex-plugin` v0.5.x.

## Goal

Bring the Gemini CLI plugin to feature equivalence with the Claude Code and
Codex plugins so a Gemini session produces the same Cardinal telemetry
contract, the same initiative classification, and the same spend-limits
behaviour as a Claude Code session — modulo fields that genuinely do not
exist on the Gemini side.

## Verified facts (2026-07-11)

These were checked before writing this plan; they gate feasibility.

1. **Attribute naming is compatible.** Lakerunner's agent-sessions processor
   reads underscore keys (`cardinal_initiative_name`, `cardinal_head_sha`,
   …). The Claude plugin emits dotted keys which the ingest pipeline
   normalizes to underscores; this plugin emits the underscore form
   directly, matching the Codex plugin. No change needed.
2. **Gemini CLI supports the hook surface we need.** Documented events cover
   `SessionStart`, `SessionEnd`, `BeforeTool`/`AfterTool`,
   `BeforeToolSelection`, `BeforeModel`/`AfterModel` (per-model-call — this
   closes the Cursor token-count gap), `BeforeAgent`/`AfterAgent`
   (subagents), `PreCompress` (pre-compact), `Notification`. Hook config
   lives in the same `settings.json` used for MCP servers.
3. **Gemini CLI supports MCP.** `mcpServers` in `~/.gemini/settings.json`
   (user scope) or `.gemini/settings.json` (project scope). Same merge
   pattern as Codex/Cursor.
4. **Gemini CLI has a proper Extensions concept.** Bundle directory at
   `~/.gemini/extensions/<name>/` with a `gemini-extension.json` manifest
   declaring `name`, `version`, `mcpServers`, `contextFileName`, and
   subdirectories for commands, agents, and hooks (`hooks/hooks.json`).
   This is the shipping shape.
5. **Native OTLP.** Gemini CLI already emits OTLP/gRPC or OTLP/HTTP with
   `gemini_cli.token.usage`, `gemini_cli.tool.call.*`,
   `gemini_cli.api.request.*`, `gemini_cli.user_prompt`, plus session/
   config / agent / compression log events. Endpoint set via
   `telemetry.otlpEndpoint` in settings.json. We can point this directly
   at Cardinal ingest with zero code.
6. **Hook command runtime is shell-only.** `"type": "command"` in
   hooks.json entries executes the string via the shell, so
   Python/Node/anything on PATH works. Same runtime story as Claude Code
   hooks; the emitter is Python 3.11, matching Codex/Cursor.

## Emitter strategy: hybrid (native OTLP + hooks)

Two complementary emitters. Both target the same Cardinal ingest endpoint.

### Native OTLP path (Gemini-emitted)

`cardinal-connect` writes the `telemetry` block into `~/.gemini/settings.json`:

```json
"telemetry": {
  "enabled": true,
  "target": "otlp",
  "otlpEndpoint": "https://<cardinal-ingest>/v1/logs",
  "otlpHeaders": {"x-cardinalhq-api-key": "<key>"}
}
```

Gemini CLI itself produces:

| Gemini event | Populates Cardinal contract field |
| --- | --- |
| `gemini_cli.token.usage` | Token counts + latency per model call |
| `gemini_cli.tool.call.count` / `.latency` | Tool call rate / duration |
| `gemini_cli.api.request.count` / `.latency` | API request rate / duration |
| `gemini_cli.user_prompt` log event | Session prompt counter |
| `gemini_cli.session.configured` | `gemini.version` and config surface |

### Hook path (plugin-emitted)

Everything Cardinal-specific that Gemini's native OTel doesn't produce:

| Cardinal event | Hook | Fields |
| --- | --- | --- |
| `cardinal.git_state` | `SessionStart` + `BeforeAgent` | `session_id`, `cardinal_head_sha`, `cardinal_branch`, `cardinal_repo`, `cardinal_remote_url`, `cardinal_initiative_name`, `cardinal_initiative_type`, `cardinal_command`, plan stamp |
| `api_request` | `AfterModel` | model, token buckets, `cost_usd` (per-provider pricing table) |
| `cardinal.turn_usage` | `AfterModel` | as above + `user_turn_seq`, `turn_seq`, plan stamp |
| `cardinal.turn_tool` | `AfterTool` | `tool_name`, `mcp_server_name`, `mcp_tool_name`, `bash_class`, `target`, seq counters |
| `tool_result` | `AfterTool` | `tool_name`, `success`, `tool_parameters`, `tool_input` |
| `cardinal.subagent_usage` | `AfterAgent` | `subagent_type`, `agent_id`, `subagent_description`, `total_tokens`, plan stamp |
| `cardinal.plan_usage` (compact slice) | `PreCompress` | `context_tokens`, `context_window_size`, `context_usage_percent`, `trigger`, `messages_to_compact`, `is_first_compaction` |

### Session context & spend limits (matching Claude/Codex)

| Concern | Hook | Behaviour |
| --- | --- | --- |
| Initiative-convention prompt | `SessionStart` | `additionalContext` carrying the branch-naming convention prompt |
| Budget standing at session start | `SessionStart` | One synchronous limits fetch (1.5s timeout, fail open) prepended to `additionalContext` |
| Per-turn spend gate | `BeforeAgent` | File-I/O-only read of cached verdict; `block` → `{decision: "block", reason}`; `warn`/`notify` → `additionalContext` + `systemMessage` with band hysteresis. Verdict refresh runs *after* the git_state OTLP post on the same hook (best-effort network) |

## Event mapping: Claude → Gemini payload sources

`BeforeAgent` is the closest Gemini analogue to Claude's `UserPromptSubmit`.
Documented payload keys: `session_id`, `transcript_path`, `cwd`,
`hook_event_name`, `timestamp`, and (per the writing-hooks doc) `prompt` on
turns where a prompt is present. This is where we do the git-state emit +
the spend-limits gate.

`AfterModel` payload includes token usage buckets by prompt/response/thought/
cache/tool; this is where per-model-call `api_request` + `cardinal.turn_usage`
originate. Unlike the Codex plugin, we do NOT need to scrape JSONL transcripts
to reconstruct token counts — the hook payload has them per call.

`AfterTool` payload includes `tool_name`, tool arguments, output, success,
duration. This maps 1:1 to Claude's `PostToolUse`.

`AfterAgent` payload is the subagent stop event. Documented keys include the
subagent's declared type/name and run duration; token totals require Gemini
CLI to expose them in the payload (present per docs, verify in practice).

`PreCompress` fires before context compression with the pre-compact context
window state — direct source for `plan_usage` compact slice (this is what
Cursor's `preCompact` plugin already reads and is the same shape).

## Divergences vs Claude / Codex

### 1 — No dedicated `UserPromptSubmit` event

Gemini CLI conflates prompt submission into `BeforeAgent`. The `prompt`
field on `BeforeAgent` carries the user's typed text; slash-command
detection reads it the same way `git-state.py` does. Behavioural parity:
identical — the hook runs on every user turn, git_state fires, limits gate
runs.

### 2 — Per-model-call token counts come from the hook, not transcripts

Better than the Codex plugin: no `handle_stop` transcript scraping. Every
`AfterModel` invocation emits its own `api_request` + `cardinal.turn_usage`
pair. Turn-boundary logic (`user_turn_seq` increments on `BeforeAgent`;
`turn_seq` increments per `AfterModel`; `tool_seq` increments per
`AfterTool` and resets on user turn boundary) lives in a small per-session
progress file at `~/.gemini/cardinal/telemetry/<session>.json` — exactly
the shape the Codex plugin uses for its Stop-time cursor.

### 3 — Native OTLP overlap

Gemini's built-in exporter emits `gemini_cli.token.usage` and
`gemini_cli.tool.call.count` alongside our hook-emitted `api_request` and
`tool_result`. This is intentional overlap: lakerunner processes the
Cardinal contract names; the native events populate secondary Gemini-side
dashboards. **Non-goal**: suppressing either. Cost is one extra OTLP
record per model call, both to the same ingest.

### 4 — Model set + pricing

Gemini models: `gemini-2.0-pro`, `gemini-2.0-flash`, `gemini-2.0-flash-lite`,
`gemini-1.5-pro`, `gemini-1.5-flash`, plus dated SKUs. Pricing table lives
in `cardinal-gemini-telemetry.py` alongside the Codex OpenAI table. Same
longest-prefix fallback for dated SKUs. `input`, `cached_input`, `output`
per 1M tokens; `thought_tokens` bills as output (Gemini reasoning is
output-billed per Google's pricing page).

### 5 — MCP config format

Codex uses TOML (`~/.codex/config.toml`); Gemini uses JSON
(`~/.gemini/settings.json`). Simpler write path: single JSON merge into
`settings.mcpServers.cardinal`. No TOML block-marker regex.

### 6 — Extension bundle vs raw settings.json

`cardinal-connect` supports both delivery paths:

- **Extension bundle** (recommended): writes
  `~/.gemini/extensions/cardinal/{gemini-extension.json, hooks/hooks.json,
  GEMINI.md}`. The extension manifest declares `mcpServers` and
  `contextFileName: "GEMINI.md"`. Restart Gemini, extension loads.
- **Settings.json fallback** (`--no-extension`): merges managed
  `mcpServers.cardinal` block and `hooks.*` entries directly into
  `~/.gemini/settings.json` behind BEGIN/END-marker equivalents (JSON, so
  we tag entries with a `cardinalManaged: true` sibling key instead of
  comment markers).

## Accepted asymmetries (non-goals)

- **OAuth plan cache** (Anthropic-subscription concepts: `organization_type`,
  `billing_type`, `seven_day_sonnet` windows) — no Gemini equivalent.
  Gemini plan facts come from Google Cloud API quotas / Vertex AI billing,
  which the plugin does not surface. Documented in README; unchanged.
- **`cost_usd`** — plugin-computed from a pricing table; Claude Code emits
  cost natively. Same posture as Codex.
- **Native Gemini events** we do not synthesize (chat compression detail
  logs, extension lifecycle logs) — out of scope beyond the OTLP passthrough.

## Plan of action

- **P1 — Extension bundle + connect/disconnect scripts.** Write the
  extension manifest and hooks.json; connect writes to
  `~/.gemini/extensions/cardinal/` and adds the `telemetry` block to
  `~/.gemini/settings.json`.
- **P2 — Telemetry hook (`cardinal-gemini-telemetry.py`).** Port from the
  Codex telemetry hook with these substitutions:
  - Event dispatch on `--event {SessionStart, BeforeAgent, AfterTool,
    AfterModel, AfterAgent, PreCompress, SessionEnd}` instead of
    `{SessionStart, UserPromptSubmit, Stop, SubagentStop}`.
  - `AfterModel` emits `api_request` + `cardinal.turn_usage` directly from
    payload (no JSONL scraping).
  - `AfterTool` emits `cardinal.turn_tool` + `tool_result` directly from
    payload.
  - `BeforeAgent` behaves like Codex `UserPromptSubmit` (gate + git_state
    + verdict refresh).
  - Per-session cursor file just tracks
    `(user_turn_seq, turn_seq, tool_seq, plan_state_sig,
    plan_usage_emitted_at, plan_stamp)`; no `last_line` since there's no
    transcript to resume.
  - `PreCompress` handler emits `cardinal.plan_usage` with the compact
    slice (matches Cursor's preCompact handler).
- **P3 — Spend-limits shared helper.** Copy `_limits_common.py` from Codex
  verbatim; substitute `~/.codex/` → `~/.gemini/` throughout.
- **P4 — Bash-class classifier port.** Same table as Codex; no changes.
- **P5 — Native OTLP wiring in connect.** Verify Gemini accepts the
  Cardinal ingest URL + custom header on `telemetry.otlpEndpoint` /
  `telemetry.otlpHeaders`. If the header slot isn't supported by the
  running Gemini CLI version, fall back to hook-only emission (documented
  in README; plugin still ships full coverage).
- **P6 — Tests.** Port `tests/test_cardinal_plugin.py` from Codex; adjust
  paths + config format expectations to JSON.

## Keeping the repos in lockstep

The three plugins share almost the entire OTLP contract (attribute names,
event names, Bash classifier tables, initiative resolution). When any of
those change:

1. Update the Claude plugin's source of truth files.
2. Port the same change to Codex and Gemini within the same release cycle.
3. Bump the parity spec's "Source of truth" version numbers here.
4. Cross-link the commit in each plugin's release notes.
