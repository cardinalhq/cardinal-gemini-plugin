---
name: cardinal-decision
description: Show Cardinal decision-capture status for Gemini CLI and tell the user how to turn it on or off.
---

# Cardinal Decision Capture

Use this skill when the user asks to turn Cardinal decision capture on or off, or asks whether it is on.

Run:

```bash
python3 "__CARDINAL_PLUGIN_ROOT__/scripts/cardinal-decision" status
```

Surface the output. Do not run `on` or `off` yourself: they write `~/.gemini/cardinal`, which Gemini CLI's sandbox blocks when it is on. Instead give the user the exact command to run in their own terminal:

```bash
python3 "__CARDINAL_PLUGIN_ROOT__/scripts/cardinal-decision" on    # or: off
```

`CARDINAL_DECISIONS=1` / `0` in the environment Gemini CLI starts with overrides the setting.

When capture is on, the Cardinal BeforeAgent hook adds instructions to each prompt with the exact `record` command (including `--session`) to run whenever a material decision is made. Follow those instructions.
