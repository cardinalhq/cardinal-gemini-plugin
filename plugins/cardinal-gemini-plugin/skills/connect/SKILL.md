---
name: cardinal-connect
description: Connect Gemini CLI to Cardinal by running the device-code flow and configuring telemetry hooks plus the unified Cardinal MCP server.
---

# Cardinal Connect

Use this skill when the user asks to connect Gemini CLI to Cardinal, enable Cardinal telemetry, enable Cardinal MCP tools, rotate a Cardinal connection, or run Cardinal setup.

Run the repository script:

```bash
python3 scripts/cardinal-connect
```

If the user asks for a non-production Cardinal host, pass `--host <url>`. If the script reports that Cardinal is already connected, ask whether to rotate or rerun with `--rotate` when the user has already asked to overwrite.

The script prints an approval URL. Show that URL to the user and wait for the script to finish. On success, tell the user to restart Gemini CLI so it reloads `~/.gemini/settings.json` and the extension directory, then suggest `cardinal-status`.

Both Gemini CLI's native OpenTelemetry exporter (pointed at Cardinal ingest) and the plugin's hook-based emitter run in parallel — the hooks fill Cardinal-specific columns (`cardinal.git_state`, `cardinal.turn_tool`, `cardinal.subagent_usage`, spend-limits gate) that the native exporter does not.
