---
name: cardinal-disconnect
description: Disconnect Gemini CLI from Cardinal by revoking keys, removing the extension bundle and managed settings entries, and deleting local state.
---

# Cardinal Disconnect

Use this skill when the user asks to disconnect Gemini CLI from Cardinal, remove Cardinal integration, or clean up local Cardinal state.

Run:

```bash
python3 scripts/cardinal-disconnect
```

The script best-effort revokes the recorded ingest and MCP keys server-side, then removes the extension bundle at `~/.gemini/extensions/cardinal/` (only when tagged `cardinalManaged: true`), strips managed `telemetry` / `mcpServers` / `hooks` entries from `~/.gemini/settings.json`, and deletes `~/.gemini/cardinal.json`, `~/.gemini/cardinal-secrets.json`, and `~/.gemini/cardinal/`.

Pass `--force` if `~/.gemini/cardinal.json` is missing but the user still wants to sweep for orphaned Cardinal-managed entries.

Tell the user to restart Gemini CLI after disconnecting so it reloads MCP and hook configuration.
