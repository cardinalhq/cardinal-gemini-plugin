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

## Telemetry scope

Gemini CLI ships a native OpenTelemetry exporter (`gemini_cli.token.usage`,
`gemini_cli.tool.call.*`, `gemini_cli.api.request.*`, `gemini_cli.user_prompt`,
plus session / config / agent / compression log events). This plugin points
that exporter directly at Cardinal ingest, so those events arrive without
any hook code. On top of that, plugin-owned hooks emit the Cardinal-specific
event contract used by the sibling plugins (see `docs/specs/gemini-parity.md`
at the repository root for the full parity map):

- `cardinal.git_state` from the active Git checkout on `BeforeAgent`, with initiative classification from the branch name (worktree-noise stripped) and slash-command detection.
- `api_request` + `cardinal.turn_usage` per model call from `AfterModel` — Gemini CLI surfaces per-call token buckets in the hook payload directly, so no transcript scraping is needed.
- `cardinal.turn_tool` + `tool_result` per tool call from `AfterTool`, with MCP-qualified `tool_name` on `turn_tool` and Bash-verb `bash_class` classification.
- `cardinal.subagent_usage` from `AfterAgent` payload keys (`subagent_type`, `agent_id`, `subagent_description`, `total_tokens`, `duration_ms`).
- `cardinal.plan_usage` (context-window slice) from `PreCompress` — `context_tokens`, `context_window_size`, `context_usage_percent`, `trigger`, `messages_to_compact`, `is_first_compaction`. Downstream disambiguates from per-model-call plan_usage on the presence of `plan.compact_trigger`.

Claude subscription-specific plan fields that do not exist in Gemini CLI are
left empty; Gemini plan/rate-limit fields are mapped onto the existing plan
usage columns where possible.

### Payload-shape capture

Gemini CLI's hook payload key names for a few surfaces (notably `AfterAgent`
token totals and `PreCompress` context slice) haven't been observed in the
wild yet, so the emitter probes several key spellings and falls back
gracefully. To help pin them down, set `CARDINAL_GEMINI_DEBUG_PAYLOADS=1`
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

State lives under `~/.gemini/cardinal/` (telemetry progress cursors, plan
stamp, limits verdicts); `cardinal-disconnect` removes it.

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
| `~/.gemini/extensions/cardinal/hooks/hooks.json` | Cardinal hook entries for `SessionStart`, `BeforeAgent`, `AfterModel`, `AfterTool`, `AfterAgent`, `PreCompress`, `SessionEnd`. Each command string embeds the marker `cardinal-gemini-plugin` for disconnect identification. |
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

- Gemini CLI with hooks + MCP server + extensions support.
- Python 3.11+.
- A Cardinal account.

## License

Apache 2.0. See [LICENSE](./LICENSE).
