---
name: cardinal-status
description: Report the current Cardinal Gemini CLI connection state and probe the configured ingest and MCP endpoints.
---

# Cardinal Status

Use this skill when the user asks whether Cardinal is connected, what workspace they're pointed at, or to probe ingest/MCP reachability.

Run:

```bash
python3 scripts/cardinal-status
```

The script prints the recorded workspace, key prefixes, and file paths, then probes the ingest and MCP endpoints. If it exits non-zero, one of those probes failed — suggest `cardinal-connect --rotate` if a key appears invalid, or check network reachability to the recorded endpoints.
