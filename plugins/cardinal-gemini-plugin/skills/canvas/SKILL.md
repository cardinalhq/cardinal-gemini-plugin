---
name: canvas
description: Draw and preview the Canvas of a Cardinal Investigation Storyboard scene — free-form HTML/CSS/SVG/d3/Plot in Cardinal's sandboxed frame, with the five cv operations (cv.data, cv.mark, cv.embed, cv.reveal, cv.highlight) and the in-frame prefabs (timeline, trace-waterfall, world-graph, compare, diff) — then read the PNGs the plugin renders locally with Chromium after every storyboard__preview, and critique every reveal step. Use whenever you write or revise a storyboard surface (storyboard__define_surface), pick a prefab for a scene, style or animate a scene, or need to see what a storyboard scene actually looks like before storyboard__publish. Pairs with the storyboard skill, which owns the argument; this one owns the visual.
---

# canvas — draw the scene, then look at it

Follow [CORE.md](CORE.md), co-located in this directory, for the Canvas workflow: fetch the
`canvas` guide first, the exemplars, and the preview-then-critique loop. This file adds what
is specific to Gemini CLI.

## The hook renders for you

After `storyboard__preview` returns, the Cardinal extension's AfterTool hook (its own
`--role preview` hook, allowed 150 s) passes the result to the storyboard skill's renderer,
which renders every scene and reveal step in your local Chromium. The hook then reports in
your context the PNG directory (`~/.gemini/cardinal/storyboards/<storyboard_id>/r<revision>/`),
each scene's files (`<scene>-<step>.png`, step 0 first) and any per-scene render error, then
the cover and the link-preview mock when the preview carried `card`. You do not build the
renderer's input. Read every PNG it names with `read_file`, as CORE.md says. `read_file` reads
only the workspace: if it refuses a PNG as outside the workspace, ask the user to restart
Gemini CLI with `--include-directories ~/.gemini/cardinal/storyboards` (or add that directory
to `context.includeDirectories` in settings), and do not claim the scene was reviewed.

## Rendering by hand (fallback)

Run the renderer yourself when the hook timed out or left scenes unrendered, for the dark
theme, or to re-render a subset. Use this plugin's copy, never a file with the same name in
the working directory (the renderer handles your Cardinal key). Keep the `-I`: without it
Python imports modules from the working directory first.

```bash
python3 -I "__CARDINAL_PLUGIN_ROOT__/skills/storyboard/scripts/render_preview.py" \
  --from-json <preview.json> [--scene <id>]… [--theme dark] --runtime gemini
```

Give it the preview result (CORE.md, "The renderer's output"), saved to a file. `--runtime
gemini` uses this Gemini CLI's Cardinal connection (`~/.gemini/cardinal.json`) and writes to
`~/.gemini/cardinal/storyboards/`. The run stops itself at 9 minutes; give the shell call a
long timeout. With Gemini CLI's sandbox on, the renderer cannot write under `~/.gemini` or
reach Cardinal: say the preview could not be verified.
