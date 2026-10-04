# Cardinal Gemini CLI plugin

Connect Google Gemini CLI to Cardinal telemetry and the unified MCP endpoint
in one browser-approved consent.

This is a Gemini-CLI-native port of the command surface shared by the
[Claude Code plugin](https://github.com/cardinalhq/cardinal-claude-plugin),
[Codex plugin](https://github.com/cardinalhq/cardinal-codex-plugin), and
[Cursor plugin](https://github.com/cardinalhq/cardinal-cursor-plugin):

| Skill / script | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, installs the Cardinal extension bundle under `~/.gemini/extensions/cardinal/`, and wires Gemini CLI's native OTLP exporter to Cardinal ingest. |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes the extension bundle and managed settings.json entries, and deletes local state. |
| `cardinal-decision` | Opt-in decision capture: `on` / `off` / `status`, and `record` (called by the agent) emits one `cardinal.decision` event tagged with repo, branch, head sha, PR, anchors and code clusters. |

## Telemetry scope

Gemini CLI ships a native OpenTelemetry exporter (`gemini_cli.token.usage`,
`gemini_cli.tool.call.*`, `gemini_cli.api.request.*`, `gemini_cli.user_prompt`,
plus session / config / agent / compression log events). This plugin points
that exporter directly at Cardinal ingest, so those events arrive without
any hook code. On top of that, plugin-owned hooks emit the Cardinal-specific
event contract used by the sibling plugins (see `docs/specs/gemini-parity.md`
at the repository root for the full parity map):

- `cardinal.git_state` from the active Git checkout on `BeforeAgent`, with initiative classification from the branch name (worktree-noise stripped), slash-command detection, and the branch's PR (`cardinal_pr_number` / `cardinal_pr_url`) when `gh pr view` resolves one (cached per repo+branch; keys absent otherwise).
- `api_request` + `cardinal.turn_usage` per model call from `AfterModel`. Gemini CLI fires `AfterModel` once per streamed chunk; the hook exits immediately on non-final chunks and accounts the final chunk's `llm_response.usageMetadata` (prompt, candidates, total — thought/tool-use-prompt tokens are derived as the remainder; cached-content tokens are not exposed to hooks, so cost ignores cache discounts). Treat cost as an upper bound: the remainder mixes thought and tool-use-prompt tokens and is priced at the output rate. A stream that errors before its final chunk records no usage for that call.
- `cardinal.turn_tool` + `tool_result` per tool call from `AfterTool`, with `mcp__<server>__<tool>` on `turn_tool` (from `mcp_context`), Bash-verb `bash_class` classification, and success from `tool_response.error`.
- `cardinal.plan_usage` (compaction trigger) from `PreCompress`; Gemini's payload carries only `trigger`. Downstream disambiguates from per-model-call plan_usage on the presence of `plan.compact_trigger`.
- No `cardinal.subagent_usage`: Gemini CLI's `AfterAgent` fires once per main-agent turn (not per subagent), so it is not registered. Subagents run their own chat through the same hooks, so their model usage likely lands in the parent session's `cardinal.turn_usage` without a subagent marker.

Hooks never wait on the network: each one does local-file work, prints its
output, and hands OTLP posts, the `gh` PR lookup and the limits-verdict
refresh to a detached background process. The one exception is
`SessionStart`, which makes a single 1.5s-bounded spend-limits fetch per
session when the backend advertises spend limits. Handoff is best-effort:
a failed send is not retried, and a job file whose background process died
before reading it stays under `~/.gemini/cardinal/spool/`.

Claude subscription-specific plan fields that do not exist in Gemini CLI are
left empty; Gemini plan/rate-limit fields are mapped onto the existing plan
usage columns where possible.

### Payload-shape capture

The hook handlers follow the payload shapes in gemini-cli's
`packages/core/src/hooks/types.ts`, but haven't been checked against a live
Gemini CLI session yet. Non-final `AfterModel` chunks exit before the dump. To help pin them down, set `CARDINAL_GEMINI_DEBUG_PAYLOADS=1`
before starting Gemini CLI — raw hook payloads land under
`~/.gemini/cardinal/telemetry/debug/<Event>-<ts>.json`. Share these with the
plugin maintainers so the parity spec (`docs/specs/gemini-parity.md`) can be
locked to real key names.

## Evidence capture (storyboards)

The `AfterTool` hook records every tool call, succeeded or failed (Gemini's own tools such as `run_shell_command` and `read_file`, any MCP tool identified by `mcp_context`, and any tool Gemini adds later), in the local evidence spool shared with the other adapters: `~/.cardinal/evidence/<session_id>/ev_<id>.json` (directories 0700, files 0600). The shared pipeline (`cardinal_core.evidence_capture`) decides without naming any tool: a call that touches something sensitive (a `.env` or key file, `~/.aws`, `printenv`, `gh auth token`, a credentialed URL or header) is kept only as a *withheld* stub; everything else is scrubbed, capped at 256 KiB and removed after 14 days. Gemini's `<untrusted_context>` wrapper is removed so the spool keeps what the tool returned, and `mcp_context`'s connection details (command, args, url) are never recorded. A failed call is kept with status `error`. The hook returns `[evidence:ev_…]` as `additionalContext`. Entries name the client `gemini/<version>`, read from the installed `@google/gemini-cli` package (`gemini` alone when it cannot be found). Nothing leaves the machine: `scripts/cardinal-evidence promote --storyboard sb_… ev_…` uploads only what a storyboard cites, with the connection's MCP key. Cardinal's own `cardinal` server is skipped. Opt out with `CARDINAL_EVIDENCE_CAPTURE=0` or the flag file `~/.cardinal/evidence/disabled`. Capture is local file work and fails open.

## Storyboard associations

Where a storyboard is written from (repo, branch, PR, HEAD, the files this
session edited) is recorded on each act as its `context`, and storyboards
that may relate to the checkout are shown at session start. Shared logic:
`cardinal_core.storyboard_agent`.

- **SessionStart** (the existing hook): when Cardinal MCP is connected,
  `additionalContext` names this session's id (for `storyboard__create`,
  `storyboard__add_act` and `storyboard__find` `session_id`) and
  `scripts/cardinal-storyboard context --bare --session-id <id>` (the
  `context` object itself; without `--bare` it is wrapped in
  `{"context": {…}}`). It says the plugin fills context only when the
  `BeforeTool` handler is registered (the installed extension's
  `hooks/hooks.json`, or `settings.json`) and not turned off; otherwise it
  names the CLI as the way to pass it. Plus the
  storyboards that may relate to this branch, PR or commit (at most 3, 2 KB,
  framed as data; gh cache only, a 2 s network deadline,
  `X-Cardinal-Client: gemini/<plugin version>`). Off:
  `CARDINAL_STORYBOARD_DISCOVERY=0` (the storyboards) or
  `CARDINAL_STORYBOARD_SESSION_START=0` (all of it).
- **Edited files**: the `AfterTool` run records the file a successful
  `write_file` or `replace` edited (`tool_input.file_path`, else
  `returnDisplay.filePath`) for `context.paths`.
- **Automatic context** (`BeforeTool`, matcher
  `storyboard__(create|add_act|publish|find)$`): on Cardinal's own server
  (`mcp_context.server_name` `cardinal`) it returns
  `hookSpecificOutput.tool_input` with an absent `session_id` and an absent
  or `{}` `context` filled; it never changes what the model set, never
  writes `about`, and stamps `publish` only once the server has advertised
  associations. Gemini's internal tools (`update_topic`) are ignored. Off:
  `CARDINAL_STORYBOARD_CONTEXT=0`.

Evidence (gemini 0.50.0, captured 2026-10-03 with a project-level
`.gemini/settings.json`): `write_file` `{file_path, content}` and `replace`
`{file_path, old_string, new_string, instruction}` with `file_path` as the
model gave it; MCP calls carry `mcp_context {server_name, tool_name}`; a
`BeforeTool` `hookSpecificOutput.tool_input` reached the MCP server
rewritten. Fixtures: `tests/test_gemini_storyboard.py`. Re-run
`cardinal-connect` to register `BeforeTool`.

## Session context & spend limits

Parity features with the Claude, Codex, and Cursor plugins, driven by the
same server-side contract:

- **SessionStart context** — every session in a git repo receives the
  Cardinal initiative branch-naming convention as hook context, plus the
  session's current spend-budget standing when your Cardinal backend has
  agent spend limits enabled.
- **Spend-limits gate** — on every prompt the hook reads the locally
  cached limits verdict (file I/O only, never network on the critical path):
  `notify` adds quiet agent context, `warn` also surfaces a message to you
  (each band surfaces once — no nagging), `block` stops the turn with the
  server-authored reason. Verdicts refresh in the background after each
  prompt's telemetry post. Everything fails open.

- **Decision capture (opt-in)** — `python3 scripts/cardinal-decision on`
  turns it on (`CARDINAL_DECISIONS=1/0` in the environment overrides).
  While on, the `BeforeAgent` hook appends
  `hookSpecificOutput.additionalContext` to each prompt telling the agent
  to record material choices with `python3 <plugin>/scripts/cardinal-decision
  record --session <id> ...` and listing the session's decisions so far.
  Each record emits one `cardinal.decision` event (same contract as the
  Claude plugin, `docs/specs/decision-telemetry.md`).

State lives under `~/.gemini/cardinal/` (telemetry progress cursors, plan
stamp, limits verdicts, decision config + ledgers); `cardinal-disconnect`
removes it.

## Install locally

This repository is a local Gemini CLI plugin directory. Clone it, then run
`cardinal-connect`:

```bash
python3 plugins/cardinal-gemini-plugin/scripts/cardinal-connect
```

The connect script prints a Cardinal approval URL, waits for approval, and
writes:

| File | What gets written |
| --- | --- |
| `~/.gemini/extensions/cardinal/gemini-extension.json` | Extension manifest with concrete `mcpServers.cardinal` entry, tagged `cardinalManaged: true`. |
| `~/.gemini/extensions/cardinal/hooks/hooks.json` | Cardinal hook entries for `SessionStart`, `BeforeAgent`, `AfterModel`, `AfterTool`, `PreCompress`, `SessionEnd`. Each command string embeds the marker `cardinal-gemini-plugin` for disconnect identification. |
| `~/.gemini/extensions/cardinal/GEMINI.md` | Context file loaded into the model context by Gemini CLI. |
| `~/.gemini/settings.json` | Managed `telemetry` block pointing Gemini's native OTLP exporter at Cardinal ingest. |
| `~/.gemini/cardinal.json` | Non-secret state: org/user metadata, endpoint URLs, key ids, key prefixes, and config locations. |
| `~/.gemini/cardinal-secrets.json` | Local plaintext ingest/MCP keys needed by hooks and status probes; written mode `0600`. |

Restart Gemini CLI after connecting so it reloads MCP, hook config, and
extensions.

## Scripts

```bash
python3 scripts/cardinal-connect
python3 scripts/cardinal-connect --host https://app.cardinalhq.io
python3 scripts/cardinal-connect --rotate
python3 scripts/cardinal-connect --telemetry-only
python3 scripts/cardinal-connect --no-extension
python3 scripts/cardinal-connect --dry-run
python3 scripts/cardinal-status
python3 scripts/cardinal-disconnect
python3 scripts/cardinal-disconnect --force
```

## Requirements

- Gemini CLI **0.26.0 or newer** (hooks are on by default from 0.26.0 via
  `hooksConfig.enabled`). On older versions enable hooks yourself in
  `~/.gemini/settings.json`: `"hooks": {"enabled": true}` on 0.24–0.25,
  `"tools": {"enableHooks": true}` on 0.21–0.23. Extension hooks need 0.21+.
  `cardinal-connect` does not set these flags: the key moved between
  releases, and on 0.26+ writing it would override an explicit opt-out.
  `cardinal-status` warns when hooks are disabled or the CLI is too old.
- Python 3.11+.
- A Cardinal account.

### Upgrading from plugin versions before 0.17

Earlier versions registered hooks that Gemini CLI never ran: the extension
`hooks/hooks.json` lacked the top-level `hooks` key the extension loader
requires, and timeouts were written as `5` (Gemini reads milliseconds).
Re-run `python3 scripts/cardinal-connect` — when Cardinal is already
connected it rewrites the hook registration in place (no new credentials)
— then restart Gemini CLI. `cardinal-status` reports a stale registration.

### Sandbox

Gemini CLI's sandbox is off by default. With it on (macOS
`permissive-open` profile), writes under `~/.gemini` are blocked, so
`cardinal-decision` exits with status 4 and a message instead of recording.
Turn the sandbox off for the session or allow writes to
`~/.gemini/cardinal/` to use decision capture.

## License

Apache 2.0. See [LICENSE](./LICENSE).
