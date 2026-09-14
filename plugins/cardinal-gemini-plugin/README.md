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
