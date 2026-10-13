# Cardinal Gemini CLI plugin

Connect Google Gemini CLI to Cardinal telemetry and the unified MCP endpoint
in one browser-approved consent.

This is a Gemini-CLI-native port of the command surface shared by the
[Claude Code plugin](https://github.com/cardinalhq/cardinal-claude-plugin),
[Codex plugin](https://github.com/cardinalhq/cardinal-codex-plugin), and
[Cursor plugin](https://github.com/cardinalhq/cardinal-cursor-plugin):

| Skill / script | What it does |
| --- | --- |
| `cardinal-connect` | Runs Cardinal's device-code flow, mints ingest and MCP keys, and installs the Cardinal extension bundle under `~/.gemini/extensions/cardinal/`. `--native-telemetry` also points Gemini CLI's own OTLP exporter at Cardinal ingest (opt-in, see "Gemini CLI's own exporter"). `--repair` fixes a `settings.json` an earlier plugin version broke. |
| `cardinal-status` | Shows the recorded Cardinal workspace and probes the configured ingest and MCP endpoints. |
| `cardinal-disconnect` | Best-effort revokes Cardinal keys, removes the extension bundle and managed settings.json entries, and deletes local state. |
| `cardinal-decision` | Opt-in decision capture: `on` / `off` / `status`, and `record` (called by the agent) emits one `cardinal.decision` event tagged with repo, branch, head sha, PR, anchors and code clusters. The `cardinal-decision` skill reports status only and gives you the `on` / `off` command to run yourself. |
| `canvas` skill | Draw and preview a storyboard scene's Canvas, then read the PNGs the preview hook renders. |
| `brainstorm` agent | Self-contained divergent idea generation (N options with tradeoffs and a recommendation). |

## Telemetry scope

Plugin-owned hooks emit the Cardinal event contract used by the sibling
plugins (see `docs/specs/adapter-parity.md` at the repository root for the
parity map). Gemini CLI's own OpenTelemetry exporter (`gemini_cli.token.usage`,
`gemini_cli.tool.call.*`, `gemini_cli.api.request.*`, `gemini_cli.user_prompt`,
plus session / config / agent / compression log events) is **off unless you
pass `--native-telemetry`**; see "Gemini CLI's own exporter" for why. The hooks
send:

- `cardinal.git_state` from the active Git checkout on `BeforeAgent`, with initiative classification from the branch name (worktree-noise stripped), slash-command detection, and the branch's PR (`cardinal_pr_number` / `cardinal_pr_url`) when `gh pr view` resolves one (cached per repo+branch; keys absent otherwise).
- `api_request` + `cardinal.turn_usage` per model call from `AfterModel`. Gemini CLI fires `AfterModel` once per streamed chunk; the hook exits immediately on non-final chunks and accounts the final chunk's `llm_response.usageMetadata` (prompt, candidates, total — thought/tool-use-prompt tokens are derived as the remainder; cached-content tokens are not exposed to hooks, so cost ignores cache discounts). Treat cost as an upper bound: the remainder mixes thought and tool-use-prompt tokens and is priced at the output rate. A stream that errors before its final chunk records no usage for that call.
- `cardinal.turn_tool` + `tool_result` per tool call from `AfterTool`, with `mcp__<server>__<tool>` on `turn_tool` (from `mcp_context`), Bash-verb `bash_class` classification, and success from `tool_response.error`.
- `cardinal.plan_usage` (compaction trigger) from `PreCompress`; Gemini's payload carries only `trigger`. Downstream disambiguates from per-model-call plan_usage on the presence of `plan.compact_trigger`.
- No `cardinal.subagent_usage`: Gemini CLI's `AfterAgent` fires once per main-agent turn (not per subagent); it is registered as the turn end (see "Investigations, owner input and turn ends"), not as a subagent stop. Subagents run their own chat through the same hooks, so their model usage likely lands in the parent session's `cardinal.turn_usage` without a subagent marker.

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

### Gemini CLI's own exporter (opt-in)

`cardinal-connect --native-telemetry` writes this `telemetry` block into
`~/.gemini/settings.json`:

```json
{"enabled": true, "target": "local", "otlpEndpoint": "<ingest endpoint>",
 "otlpProtocol": "http", "logPrompts": false}
```

Gemini CLI then exports in process over OTLP/http to `<endpoint>/v1/logs`,
`/v1/traces` and `/v1/metrics` (it appends the paths itself from 0.34.0;
on an older CLI connect writes `<endpoint>/v1/logs`). `target` must be
`local` or `gcp`: any other value stops Gemini CLI from starting.

Gemini CLI has no settings key for OTLP headers: the exporters read the
API key header only from the environment variable
`OTEL_EXPORTER_OTLP_HEADERS`. Connect puts it in a marked block in
`~/.gemini/.env` (mode 0600), which Gemini CLI loads at start. That works
only where Gemini CLI actually reads that file:

- Gemini CLI loads **one** `.env` per start: walking up from the working
  directory, `<dir>/.gemini/.env` then `<dir>/.env`, and only past the
  filesystem root `~/.gemini/.env`, then `~/.env`. In a project that has its
  own `.env` (or one in a parent directory), `~/.gemini/.env` is not read,
  and Gemini's exporter sends without the key there (ingest answers 401).
- In a folder Gemini CLI does not trust (folder trust is on by default),
  it skips `~/.gemini/.env`, with the same effect.
- A variable already set in your shell wins: your own
  `OTEL_EXPORTER_OTLP_HEADERS`, or `OTEL_EXPORTER_OTLP_ENDPOINT` /
  `GEMINI_TELEMETRY_OTLP_ENDPOINT` (which override the settings endpoint and
  would receive Cardinal's header).
- Gemini CLI passes its environment to the commands and MCP servers it
  starts, so OpenTelemetry-instrumented programs you run from Gemini CLI
  inherit the header (and may send it to their own collector).

Where the header is missing only Gemini's own events are lost: Cardinal's
hooks read the key from `~/.gemini/cardinal-secrets.json` and keep working.
`cardinal-status` shows whether the exporter is on and warns when the
current directory hides `~/.gemini/.env`. `--no-native-telemetry` turns it
off; without either flag, connect keeps the earlier choice.

Nothing Cardinal writes into `settings.json` carries the key or a marker
key: Gemini CLI rejects unknown keys in `telemetry` and in `mcpServers`
entries. What Cardinal wrote there is recorded in `~/.gemini/cardinal.json`
(`settings_owned`), and `cardinal-disconnect` removes exactly that, plus
the `.env` block.

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

## Investigations, owner input and turn ends

Every connected session is bound to a Cardinal Investigation (shared logic:
`cardinal_core.investigation_agent`, `investigation_turn`, `owner_input`,
`storyboard_authoring`; the same contract as the Claude, Codex and Cursor
plugins). What each hook does:

| Hook | What it does |
| --- | --- |
| `SessionStart` | Binds the session (`investigation_agent.start`: one 3 s request), starts the background poller (`hooks/investigation-poller.py`, one per session, stops with Gemini CLI) and puts the session guidance in context: the labeled links (`Storyboard (private draft until published)`, `Investigation timeline (shows the live record before any publish)`), how to acknowledge and post, and the checkpoint command. Unconnected: the connect hint (told once per machine). An MCP key without a URL: the stray-key warning (once per session). |
| `BeforeAgent` | Owner input (below), then what was staged for this prompt (advisories that arrived after the last tool call, the discovery re-look) as `additionalContext`. The re-look itself (only when the branch or HEAD moved) runs in the detached background child and is staged. |
| `AfterTool` | The Investigation's advisories at this tool boundary (`authority: ADVISORY`), a recording command that could not reach Cardinal (`run_shell_command`), the evidence token after Cardinal's `storyboard__create` / `add_act` / `preview`, the hero upload after `storyboard__publish`, and the storyboards about a file `write_file` / `replace` edited (edit lookup, once per directory, 1.5 s). Cardinal's MCP results arrive as Gemini's `{llmContent, returnDisplay}` and are converted to MCP shape first; the server comes from `mcp_context`. |
| `AfterTool --role preview` | Its own hook group (matcher `storyboard__preview$`, 150 s: Gemini dedupes an event's hooks by `name:command`, so it needs a distinct command): renders Cardinal's `storyboard__preview` result in your local Chromium and names the PNGs under `~/.gemini/cardinal/storyboards/<sb>/r<rev>/`. |
| `AfterAgent` | The turn's end: what is deliverable (the poller's inbox, one bounded check when the poller is stale, an open directive, decision reminders) is **staged** for the next `BeforeAgent` / `AfterTool`, and the owner-input outbox is flushed (2.6 s budget). **It never blocks and prints nothing**: on Gemini CLI an AfterAgent block re-enters the model as a plain user-role message and re-fires `BeforeAgent` with the reason as its `prompt` (gemini-cli 0.50.0 `GeminiClient.sendMessageStream`, `chunk-W4YMYD53.js:335735-335757`), so advisory text would become owner input. |

Re-run `cardinal-connect` after upgrading the plugin: on an existing
connection it rebuilds the installed extension (hooks, skills, agents, and
its `mcpServers`, adding `cardinal-behavior` to a connection made before the
Checks preview) in place, credentials unchanged.

### Owner input

When the Cardinal server advertises `capabilities.owner_input.enabled` and
this session authors its Investigation, `BeforeAgent` records what you typed
as owner input of that Investigation (`owner_input.capture`):

- **Disclosure first.** The first prompt is not recorded: the hook shows
  `Cardinal: This session's prompts are recorded to Investigation <inv> as
  owner input …` as a `systemMessage` (an info line in the interactive UI; in
  `-p` mode Gemini prints it to stderr as `Hook system message: …`), and
  capture starts with the next prompt.
- **Exactly what you typed.** Gemini hands `BeforeAgent` the request text:
  in `-p` mode the SessionStart context is prepended as
  `<hook_context>…</hook_context>`, and `@file` references append the
  files' contents after what you typed, starting at `--- Content from
  referenced files ---`; both are removed before anything is sent (the
  referenced-files section up to the end of the prompt, so a file that itself
  contains Gemini's markers cannot leak). A custom TOML command
  (`~/.gemini/commands`, `<project>/.gemini/commands`, an extension's
  `commands/`) reaches `BeforeAgent` already expanded, so a prompt that is a
  command's template is recognized and never recorded (a linked extension's
  commands are read from its source). When the command files cannot be read
  confidently (more than 200, one over 64 KiB, a `prompt` that does not
  parse, an unreadable commands directory), no prompt is recorded. Gemini's
  loop-recovery request, `/init`'s `GEMINI.md` template, an MCP server's
  prompt (submitted as a JSON string literal, so a prompt that is exactly one
  quoted JSON string is skipped), a prompt that names a subagent
  (`agent_id`) and the same prompt again within one turn (and within 30 s)
  are never recorded.
  `/plan <args>` submits exactly the args you typed, and they are recorded.
- **No waiting.** The turn is taken and the prompt queued under the session
  lock; a detached process posts it (`AfterAgent` flushes what is left).
  `BeforeAgent` never waits on the network.
- **Kill switch.** `CARDINAL_OWNER_INPUT=0` (or `false` / `off` / `no`) in the
  environment Gemini CLI starts with, or `{"gemini": "off"}` in
  `~/.cardinal/owner-input.json`: nothing is recorded, a queued prompt is
  deleted, and turning it back on shows the disclosure again first.
- Credentials are scrubbed and the text is capped at 32 KiB; only you and the
  grantees you authorize can read it. A prompt the spend-limits gate blocks is
  not recorded.

### Limits

What Gemini CLI can and can't do next to the Claude, Codex and Cursor plugins,
feature by feature, with a citation for every limit: [`docs/parity.md`](../../docs/parity.md).

- **Checks preview: a hook cannot reload skills.** Gemini CLI loads an
  extension's `skills/` when it loads the extension
  (`ExtensionManager.loadExtension`), and a SessionStart hook's output
  (`additionalContext`, `systemMessage`) cannot make it load them again. So
  `cardinal-checks on` / `off` apply to sessions started afterwards, or to a
  running session once you type `/skills reload` (gemini-cli 0.63.0,
  `chunk-ED56GFWH.js:101678`); no hook can trigger that reload, which is the
  only remaining limit. While the preview is off, a session that still lists
  the skill cannot use it: a second BeforeTool group (`--role checks-gate`,
  matcher `^activate_skill$`; gemini-cli 0.63.0 runs BeforeTool for the
  built-in `activate_skill`, `bundle/chunk-RWBXO4OP.js:342886, :348609`)
  denies `activate_skill` for `cardinal-checks` (`tool_input.name`) with
  `{"decision": "deny", "reason": ...}`. Re-run `cardinal-connect` after an
  upgrade to register it.
- **No subagent events.** Gemini CLI has no SubagentStart / SubagentStop hook
  event (gemini-cli 0.50.0 `HookEventName`, `chunk-W4YMYD53.js:324340-324352`),
  so a subagent gets no discovery block at its start, and `AfterTool` carries
  no agent id: Gemini's subagents run tools through the same hooks, so an
  advisory can be delivered at a subagent's tool boundary.
- **Advisories after the turn's last tool call** cannot be delivered in that
  turn (`AfterAgent` never blocks, above): they are delivered at the next
  `BeforeAgent`, or at the next tool call.
- **A prompt right after a turn ends is not recorded.** When another
  extension's or settings hook's `AfterAgent` blocks, Gemini CLI re-fires
  `BeforeAgent` with that hook's reason as the `prompt`, which nothing marks
  as not typed. Every `AfterAgent` notes when it ran
  (`~/.gemini/cardinal/after-agent/<session>.json`), and a `BeforeAgent`
  within 2 s of it is not captured. A prompt you really submit that fast (a
  message queued while the agent was still working) is not recorded either.
- **Rendered PNGs are outside the workspace.** Gemini's `read_file` reads only
  the workspace and the project temp directory, so the PNGs the preview hook
  names under `~/.gemini/cardinal/storyboards/` need
  `--include-directories ~/.gemini/cardinal/storyboards` (or the same in
  `context.includeDirectories`) to be read.

## Checks preview (opt-in)

Checks are reusable, evidence-backed decisions over telemetry (a contract the
user approves, approved teaching examples, per-trace results with evidence),
built on the `cardinal-behavior` MCP server. The preview is **off by
default**; turn it on or off in your own terminal:

```bash
python3 scripts/cardinal-checks on       # install the checks skill; start a new Gemini CLI session
python3 scripts/cardinal-checks off      # remove it
python3 scripts/cardinal-checks status   # the setting, the installed skill, accepted checks
```

- **`cardinal-behavior` MCP server.** `cardinal-connect` registers it
  in the Cardinal extension's `mcpServers` (merged: other servers stay; it goes with the `cardinal` entry when there is no MCP credential), or as a `cardinalManaged` entry in `~/.gemini/settings.json` with `--no-extension`, launched as `scripts/cardinal-behavior --runtime gemini`;
  `cardinal-disconnect` removes it. A connection made before the Checks
  preview has none: re-running `python3 scripts/cardinal-connect` (no
  `--rotate`) adds it without a new key, and `cardinal-checks on` / `status`
  print that command while it is missing. It is a local stdio server that Gemini CLI
  starts outside its sandbox, so it can write the Checks ledger and read
  range runs that a sandboxed agent cannot. It reads this runtime's
  connection (`~/.gemini/cardinal.json` `mcp_url` + `cardinal-secrets.json`
  `mcp_api_key`); `CARDINAL_MCP_URL` with `CARDINAL_MCP_API_KEY` in its
  environment overrides them (a URL without its key does not). No key is
  written into the MCP registration. Its draft ledger and `enabled.json` are
  private (0600 files in 0700 directories).
- **The skill** (`cardinal-checks`, from `checks/skill/`: this plugin's
  SKILL.md over the shared `common/checks/CORE.md`) is installed into
  Cardinal's extension, `~/.gemini/extensions/cardinal/skills/checks/` (a rebuilt extension keeps it; with `--no-extension` there is no extension to load it from, so `on` refuses) by `on` and removed by `off`. The SessionStart hook
  reconciles that copy with the setting and, while the preview is on and
  Cardinal is connected, adds the routing rule (answer with the query tools
  first; a check only for per-trace judgments). Off: no text at all.
- **Labels and reuse** go through MCP tools (`set_check_example`,
  `approve_check_example`, `remove_check_example`, `get_check_draft`,
  `lock_check_examples`, `list_accepted_checks`), never shell commands. The
  ledger is `~/.gemini/cardinal/checks/drafts/`; disconnect removes it.
- **Range runs** (`execute_behavior_range`) run in a detached worker; the
  agent follows one by polling `get_behavior_range` (each call waits up to
  30 s for new events, and a completed run's summary comes back in it).
  Claude Code listens with its Monitor tool instead; Gemini CLI has no
  equivalent, so it polls.

Gemini CLI names these tools `mcp_cardinal-behavior_<tool>`. A completed
range's summary comes back in `get_behavior_range` because `read_file`
cannot read `~/.cardinal/behavior-executions/` without
`--include-directories`.

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
| `~/.gemini/extensions/cardinal/gemini-extension.json` | Extension manifest with a concrete `mcpServers.cardinal` entry, tagged `cardinalManaged: true`. Other `mcpServers` entries already in the installed manifest are kept (merged, never replaced). |
| `~/.gemini/extensions/cardinal/hooks/hooks.json` | Cardinal hook groups for `SessionStart`, `BeforeAgent`, `BeforeTool` (storyboard tools), `AfterModel`, `AfterTool` (and `AfterTool --role preview`, 150 s), `AfterAgent`, `PreCompress`, `SessionEnd`. Each command string embeds the marker `cardinal-gemini-plugin` for disconnect identification. |
| `~/.gemini/extensions/cardinal/skills/` | The plugin's skills (connect, status, disconnect, decision, storyboard, canvas, install-site, migrate-from-grafana, optimize-toolkit), with each script path rewritten to this plugin's absolute path. Gemini CLI loads an extension's `skills/*/SKILL.md`. |
| `~/.gemini/extensions/cardinal/agents/` | The `brainstorm` subagent (Gemini's local-agent Markdown format, read-only tools). |
| `~/.gemini/extensions/cardinal/GEMINI.md` | Context file loaded into the model context by Gemini CLI. |
| `~/.gemini/settings.json` | Nothing by default. With `--native-telemetry`, the `telemetry` block above (no key). With `--no-extension`, the `cardinal` / `cardinal-behavior` MCP servers and the hook groups. |
| `~/.gemini/.env` | Only with `--native-telemetry`: a marked block setting `OTEL_EXPORTER_OTLP_HEADERS` to the ingest key header (file mode 0600). Your own lines are kept. |
| `~/.gemini/cardinal.json` | Non-secret state: org/user metadata, endpoint URLs, key ids, key prefixes, config locations, and what Cardinal wrote into `settings.json` / `.env` (`settings_owned`). |
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
python3 scripts/cardinal-connect --native-telemetry     # also Gemini CLI's own exporter
python3 scripts/cardinal-connect --repair               # fix settings.json, no new keys
python3 scripts/cardinal-status
python3 scripts/cardinal-disconnect
python3 scripts/cardinal-disconnect --force
python3 scripts/cardinal-checks on|off|status
python3 scripts/cardinal-wake enable|disable|status|run --dry-run
```

## Owner-woken sessions (notify-only for now)

`python3 scripts/cardinal-wake enable` (macOS; a per-user launchd agent)
watches for owner decisions you post on an Investigation page addressed to
a stopped session on this machine. Only a decision posted from your
signed-in Cardinal web session counts. The waker reads it from Cardinal
with this plugin's key, never from local files. It recognizes a Gemini CLI
session by its chat file (`~/.gemini/tmp/*/chats/session-*-<first 8 of the
id>.json[l]` naming the session id). `disable` removes the agent;
`status` shows recent wakes and approved roots, never decision text.

**Today a Gemini CLI session always gets a notification**, naming the
session, the Investigation and the state, and is never resumed. An
unattended Gemini launch is built but ships off (`GEMINI_LAUNCH_PROVEN`
in `cardinal_core.session_wake`) until its live proofs pass
([`docs/specs/resume-and-wake.md`](../../docs/specs/resume-and-wake.md)
§5.3 Gemini, §6 C-9 to C-16). When it is on, a decision resumes the
session only if:

- you approved its directory: `cardinal-wake enable --launch --root PATH`
  (repeatable; never your home directory or above it). The session's
  directory, from Gemini's own project record, must be inside an approved
  root and be a git work tree;
- every turn of the session ran inside Gemini's sandbox (`gemini -s`);
- you sign in with a Gemini API key, in `~/.gemini/.env`
  (`GEMINI_API_KEY=…`). The sandbox can read neither the macOS keychain nor
  any `.env` file, so the waker passes the key to the woken process in its
  environment; it never logs or stores it. "Sign in with Google" is
  notify-only, because the sandbox can't read the OAuth credentials;
- nothing in the repository or your settings changes the sandbox. That
  rules out any of:
  - a custom `.gemini/sandbox-macos-*.sb`, `sandbox.Dockerfile` or
    `sandbox.bashrc`;
  - a `.gemini/settings.json` with `tools.sandbox*`, `security.*`,
    `context.includeDirectories`, MCP servers, hooks or tool-discovery
    commands;
  - any `.env` that Gemini may load setting `SANDBOX`, `GEMINI_SANDBOX*`,
    `SEATBELT_*`, `GEMINI_CLI_HOME`, proxy, `NODE_OPTIONS` or `DEBUG`
    variables;
  - a symlinked or unreadable `.gemini`;
- no other Gemini wake is running and port 8877 is free.

It then starts the waker's allow-list proxy, which runs
`sandbox-exec -f <the waker's copy of Gemini's restrictive-proxied profile>
gemini --resume <id> --approval-mode yolo --skip-trust -p "<wake prompt>"`:
- **Sandbox, from the first instruction:** the waker applies the seatbelt
  itself, around the whole Gemini process. The agent can write only the
  workspace, its own temp dir (under `~/.cardinal/wake/tmp/`, with a
  private cache inside it, both removed after the run), and
  `~/.cache/cardinal/sandboxed` (this plugin's hook state). The waker's copy
  of the profile drops Gemini's `~/.npm` and `~/.cache` writes, and passes
  the private cache instead of your shared user cache directory: npm and
  other tools later read those outside any sandbox.
  - Gemini's own `-s` is not passed: a seatbelt can't be nested. Instead
    `SANDBOX=sandbox-exec` is set, which tells Gemini it is already
    sandboxed (and tells this plugin's hooks where to keep their state).
- **Network:** the profile allows only `localhost:8877`. The proxy
  (`allowlist_proxy.py`, copied into `~/.cardinal/wake/lib`) owns that port
  exclusively before Gemini starts. It tunnels HTTPS to the Gemini API and
  your Cardinal hosts and refuses everything else. Each verdict goes to the
  wake's log. Gemini CLI's own OTLP exporter (above) reaches the ingest host
  through it but has no key there: the profile denies reading `.env` files,
  where `cardinal-connect` keeps the exporter's header.
- **Acknowledgment:** as for Codex, the wake prompt asks the agent to write
  its answer to `$TMPDIR/cardinal-wake-ack.json` (its private temp dir);
  after the run has exited the waker checks it and posts that one
  acknowledgment for the session, unless the session already acknowledged
  the decision itself.
- **Runtime check:** within 20 s the waker must also see `sandbox-exec` with
  that profile in the process tree. If it doesn't, it kills the process tree
  and turns Gemini launch off until you run `cardinal-wake reenable gemini`.
  A process that has already exited is judged after the run instead.
- **Yolo:** it acts inside the sandbox. The sandbox and the proxy are the
  boundary.
- **The daemon:** like the Codex launch, it runs with a vetted Python
  (`-I`), a vetted `gemini` (and `node`), and a system-only PATH, all
  checked again before every launch.

**State inside the sandbox.** Gemini's sandbox also wraps this plugin's
hooks, and they cannot write `~/.cardinal` (nor `~/.gemini`) there.
Inside it (`SANDBOX=sandbox-exec`), the hooks keep their Investigation
state and their evidence spool under
`~/.cache/cardinal/sandboxed/.cardinal`. That is a separate spool, written
by the sandboxed agent's own process: evidence captured there is what that
(agent-writable) spool recorded, and `cardinal-evidence` run outside the
sandbox reads `~/.cardinal/evidence`, not this spool. Telemetry and
decision state under `~/.gemini/cardinal` cannot be written inside the
sandbox and are skipped there. The prompt hook also records whether each
turn ran sandboxed. That record is agent-writable too, so it is a weaker
assurance than the rollout Codex keeps. What bounds a Gemini wake is the
approved roots, the proxy and the runtime sandbox check. The waker's own
state (`~/.cardinal/wake`) is never written by hooks.

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

### Gemini CLI refuses to start after `cardinal-connect` (plugin 0.22.1 and earlier)

Plugin versions up to 0.22.1 wrote a `telemetry` block Gemini CLI rejects,
so it exits at once with:

```
Invalid configuration in ~/.gemini/settings.json:
Error in: telemetry
    Unrecognized key(s) in object: 'otlpHeaders', 'cardinalManaged'
Invalid telemetry configuration: Invalid telemetry target: otlp. Valid values are: local, gcp.
```

That block (`target: "otlp"`) was invalid on every Gemini CLI from 0.26.0
on, and it held the ingest key in plain text. A Cardinal hook cannot fix it
because Gemini CLI stops before any hook runs. Update the plugin, then run
from a shell:

```bash
python3 <plugin>/scripts/cardinal-connect --repair
```

(`<plugin>` is this plugin's directory; the path is in
`~/.gemini/extensions/cardinal/hooks/hooks.json`. Re-running
`cardinal-connect` with no flags does the same when Cardinal is connected.)
The repair removes Cardinal's block, or rewrites it to the valid shape
with `--native-telemetry`; removes `cardinalManaged` from Cardinal's
`mcpServers` entries; blanks the key in `settings.json.bak.*` backups
earlier connects wrote; and changes no credentials. A `telemetry` block
that isn't Cardinal's but uses `target: "otlp"` or `otlpHeaders` is made
valid (`target` set to `local`, `otlpHeaders` dropped) rather than removed.
`cardinal-status` reports the broken block. Without the plugin at hand,
delete the `"telemetry": {...}` entry from `~/.gemini/settings.json` by
hand.

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

## Repository associations across a session

Repository provenance is shared across Claude Code, Codex, Cursor and Gemini.
Starting in a parent workspace does not prevent later repository attachment:
explicit tool working directories, file arguments and successful edits add
repository labels locally. The background poller submits them to Cardinal.
The original checkout context remains unchanged; later repos are additive
`written_from` provenance, never automatically asserted as the storyboard's
subject. Labels contain canonical repository names, never local paths.

To backfill an existing session from its saved edited-file ledger, run:

```sh
cardinal-storyboard investigation repos --session SESSION_ID
```

Supply `--cwd /path/to/checkout` (repeatable) to attach additional checkouts.
This command uses the session's existing investigation and storyboard. It does
not publish or share anything. Automatic collection respects
`CARDINAL_STORYBOARD_CONTEXT=0`, and only an author's own session may sync.
At most 32 repositories are retained. Shell command text is not parsed: use
explicit tool working directories or the CLI for shell-only checkout changes.
Historical repair requires a retained local edit ledger or explicit checkouts.

Multiple labels and browser grouping require Cardinal's
`repository_provenance` capability (the companion Conductor migration/server
change). Older servers continue to accept the existing context request, and
the CLI reports when full multi-repo support is unavailable. Every adapter's
next patch release includes this same shared implementation; the parity matrix
and packaged-artifact tests guard against adapter drift.
