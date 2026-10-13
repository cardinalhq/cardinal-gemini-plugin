---
name: cardinal-connect
description: Connect Gemini CLI to Cardinal by running the device-code flow and configuring telemetry hooks plus the unified Cardinal MCP server.
---

# Cardinal Connect

Use this skill when the user asks to connect Gemini CLI to Cardinal, enable Cardinal telemetry, enable Cardinal MCP tools, rotate a Cardinal connection, or run Cardinal setup.

Run the repository script:

```bash
python3 "__CARDINAL_PLUGIN_ROOT__/scripts/cardinal-connect"
```

If the user asks for a non-production Cardinal host, pass `--host <url>`. When Cardinal is already connected, the script refreshes the hook registration and the installed extension (hooks, skills, agents) in place (credentials unchanged) and exits 0 if it rewrote anything — tell the user to restart Gemini CLI. If it instead reports that Cardinal is already connected (exit 2), ask whether to rotate or rerun with `--rotate` when the user has already asked to overwrite.

Hooks require Gemini CLI 0.26.0 or newer (on by default there); `cardinal-status` warns about older versions or disabled hooks.

Besides the ingest and MCP keys, the script requests a control-plane token (`maestro:act`) with `dashboards:write`, `alerts:write` and `telemetry:query`, so skills like migrate-from-grafana can write dashboards and alert rules and query telemetry as the user — bounded by their org role; `telemetry:query` is read-only in every org they belong to. Mention that when you show the approval URL. The token is stored 0600 in `cardinal-secrets.json` and `cardinal-disconnect` revokes it. Pass `--minimal-scopes` for users who don't want it. A Cardinal server that doesn't offer those scopes yet makes the script connect without them and say so. Users connected before this get the token with `--rotate`.

The script prints an approval URL. Show that URL to the user and wait for the script to finish. On success, tell the user to restart Gemini CLI so it reloads `~/.gemini/settings.json` and the extension directory, then suggest `cardinal-status`.

The plugin's hooks send Cardinal's events (`cardinal.git_state`, `cardinal.turn_usage`, `cardinal.turn_tool`, spend-limits gate). Gemini CLI's own OpenTelemetry exporter is off unless the user asks for it: then pass `--native-telemetry`, and tell them its limit — Gemini CLI takes the API key header only from `~/.gemini/.env`, which it does not read in a project with its own `.env` or in an untrusted folder, so its own events are lost there (Cardinal's hooks are unaffected). `--no-native-telemetry` turns it off.

If Gemini CLI refuses to start with `Invalid telemetry target: otlp` (a `settings.json` written by plugin 0.22.1 or earlier), the user must run `python3 "__CARDINAL_PLUGIN_ROOT__/scripts/cardinal-connect" --repair` from a shell — Gemini CLI cannot start to run it.
