---
name: cardinal-mechanize
description: Compile a completed Gemini CLI session (a past investigation) into a candidate Sentinel DAG plus rationale — a reusable procedure that could later be executed against a similar problem. Use when the user asks to /mechanize, compile a session, or extract a reusable investigation procedure. Compiles, then trial-executes the DAG against captured fixtures and checks it reaches the same conclusion the investigation did before emitting.
---

# mechanize (Gemini CLI) — compile a Gemini CLI session into a Sentinel DAG

**Spike-quality compiler.** Produces a candidate `sentinel.yaml` + `rationale.md` from a past investigation session, then **runs it** — Stage 10 executes the DAG against the tool responses captured from the source session, and Stage 11 checks it reaches the conclusion the investigation reached. A compile that has not executed is not finished, and the skill reports it as such. Does NOT ship — this is exploratory work, and the rationale is where the honesty lives.

This SKILL.md is the **Gemini-CLI-specific** part of the mechanize skill: how to find the session and how to read a Gemini CLI transcript. The shared compilation algorithm — Stages 2 through 12, the Sentinel example, the ratification checklist, the expression language, the capability registry, the rules — lives in `CORE.md`, co-located in this directory.

**You MUST read `CORE.md` in full after finishing the Gemini-specific stages below.**

## Known limitation up front

The Cardinal Gemini plugin was intentionally built on Gemini CLI's **live hook events**, not on scraping a session transcript from disk. (`adapters/gemini/hooks/cardinal-gemini-telemetry.py:4-8` — "Gemini CLI emits per-model-call and per-tool-call hook events directly (unlike Codex which required transcript-JSONL scraping)".) As a result, this SKILL does not have a canonical, plugin-validated path or schema for Gemini CLI's on-disk transcript.

**What this means for you:**
- **Stage 1 requires a manual path argument.** `/mechanize` without an argument cannot auto-resolve the current Gemini session — the mechanism this SKILL would use (encoded-cwd lookup, session-ID rglob) is not documented here.
- If the user provides a path to a Gemini CLI chat/checkpoint file, Stage 1 will read it with a **general-structure inspector** rather than a schema-precise reader. Some fidelity loss is expected.
- A future SKILL revision can codify Gemini CLI's on-disk transcript format once it's characterized against this plugin.

## How this skill is invoked

The user typed `/mechanize`, possibly with a session path as argument.

**Argument parsing:**
- If the user provided an argument that looks like an absolute path → that's `SESSION_PATH`. Gemini CLI writes chat/checkpoint files under `~/.gemini/` (exact layout not documented here); trust the path the user provided.
- If the user provided nothing → **stop and ask**:

  > `Gemini CLI transcripts aren't auto-resolvable from this SKILL yet — the Cardinal Gemini plugin uses live hooks instead of transcript scraping. Please provide an absolute path to the session's chat/checkpoint file (usually under ~/.gemini/tmp/…/chats/ or ~/.gemini/tmp/…/checkpoint-*.json). If you don't have that path handy, run \`find ~/.gemini -name '*.json' -newer /tmp -type f\` to list recent candidates.`

  Do NOT guess.

**Output location default:** `./mechanize-out/<basename-of-session-file>/` under the current working directory. If the CWD is not writable, fall back to `~/mechanize-out/<basename>/`. Tell the user where you're writing.

## Then, before anything else — read the spec

Read `sentinels.md` §§ 8, 9, 10, 11, 12, 13, 14, 14a, 28, 28.1, 29, 32, 37, 47, 52 (co-located in this directory), and `FINDINGS.md` in full. The complete reading list with rationale is at the top of `CORE.md`. Do NOT skip this.

## Stage 1 — Read and segment (general-structure inspector)

The user has provided `SESSION_PATH`. Read the file with the following procedure:

1. **Determine the file's shape.** Is it JSONL (one JSON object per line), a single JSON array, or a single JSON object? Gemini CLI chat files and checkpoint files have historically taken different shapes; do not assume.
2. **Identify the message container.** Look for the field that holds the conversation history — typically named `messages`, `history`, `turns`, or `content`. Each element is a turn.
3. **Classify each turn.** Look for a `role` (or `author`) field with values like `user`, `model`, `assistant`, `function`, `tool`. Map:
   - `user` → user message. `parts` array typically holds a mix of text (`.text`), function calls (`.functionCall` with `.name` and `.args`), and function responses (`.functionResponse` with `.name` and `.response`).
   - `model` or `assistant` → assistant message. Same `parts` shape.
   - `function` or `tool` → tool result (some Gemini variants split function responses into their own turn).
4. **Extract tool calls and results.** Each `.functionCall` part is a tool call — `.name`, `.args` (already an object, unlike Codex's JSON-string). Each `.functionResponse` part is a tool result — `.name`, `.response`. Pair calls to results by ordering and name (Gemini does not always carry an explicit call_id).
5. **Extract attachments.** Look for `.inlineData` parts (base64-encoded inline data with `.mimeType`) or `.fileData` parts (URI references). **Do not decode.** Note only kind, mime type, and size.

**If the file's shape doesn't match this description**, stop and produce a `refusal-report.md` naming the shape you found and asking the user to confirm the file is a Gemini CLI session transcript. Do NOT invent tool calls that aren't there.

Produce a mental model of:
- **Objective**: first substantive user text (skip slash-command entries).
- **Tool calls**: ordered list with their ordinal, name, input, and paired response.
- **Attachments**: any inlineData/fileData parts.
- **Conclusion**: last substantive model/assistant text.

## Stage 1.5 — Recognize spill-to-disk pairs (Gemini-CLI-specific)

**Status: unknown.** Gemini CLI's truncation behavior for large `functionResponse` payloads is not documented in this repo. Scan every `functionResponse.response` for patterns suggestive of truncation-with-spill (e.g. `Output truncated`, `See file:`, or a bare file path where a response was expected). If nothing matches, this stage is a no-op.

If a marker IS found, treat it identically to CORE.md's Stage 1.5 collapsing semantics and note the discovered marker pattern in `rationale.md` under `Unresolved: Gemini spill-to-disk pattern observed but not documented`.

## Stage 2 addendum — shell-shaped tool in Gemini CLI

Gemini CLI's built-in shell tool is typically named `run_shell_command` (or variants like `shell` or `bash`). Apply CORE.md Stage 2's synthetic-capability-ID rule (`bash.<argv[0]>`) to every such call — parse the command argument with a shell tokenizer and take `argv[0]`.

For other function-call names, follow CORE.md's registry-vs-abstract rule: derive an abstract capability ID from what the tool *does*, not from its Gemini-specific name.

## Stage 4.5 addendum — attachment vocabulary in Gemini CLI transcripts

Gemini's attachment vocabulary is `parts[].inlineData` (with `mimeType` + `data`) and `parts[].fileData` (with `mimeType` + `fileUri`). Apply CORE.md Stage 4.5's Q1–Q4 chooser using this recognition rule. Do NOT decode `inlineData.data`; do NOT fetch `fileData.fileUri`.

## Stage 5.5 addendum — cold-subagent mechanism in Gemini CLI

Gemini CLI has a subagent lifecycle observable via the `AfterAgent` hook (which fires when a subagent finishes), but exposes no CLI-level synchronous spawn mechanism to this SKILL. Options:

1. **If the user's Gemini install has a subagent-spawning MCP tool configured**, use it. Pass absolute paths to the fresh `sentinel.yaml` and `rationale.md` plus CORE.md Stage 5.5's checklist. Instruct the subagent to return ONLY the verdict block.
2. **Fallback: inline ratification.** Perform the checklist yourself in a fresh reasoning pass. Flag the degradation loudly in `rationale.md`:

   > `Unresolved: Stage 5.5 ran inline because no cold-subagent mechanism was invoked. The verdict below is weaker than a cold read would produce; a reviewer should treat R1–R6 as PASS-with-caveat rather than PASS.`

Either way, use CORE.md Stage 5.5's checklist and verdict format verbatim.

## Stage 8 addendum — presenting `preview.html` in Gemini CLI

Gemini CLI has no `Artifact`-tool equivalent to this SKILL. After the shared renderer writes `<OUT_DIR>/preview.html`, print the absolute path with an open instruction:

```
Preview rendered: <OUT_DIR>/preview.html
Open with: open <OUT_DIR>/preview.html   (macOS)
           xdg-open <OUT_DIR>/preview.html   (Linux)
```

Mermaid inside the HTML renders as raw source when opened as a plain file — known adapter degradation; the rest of the preview is fully legible.

## Stage 9 addendum — rubric-gen and cold grading in Gemini CLI

**Stage 9a (rubric-gen).** Run `python3 <repo-root>/common/mechanize/review.py rubric-gen-instructions <OUT_DIR>`. If the user's Gemini install has a subagent-spawning MCP tool configured, pass the printed prompt to it and instruct the subagent to write `<OUT_DIR>/rubric.md`. Otherwise, follow the prompt yourself, inline, and flag in `rationale.md` under `Unresolved`:

> `Stage 9a ran inline (no subagent MCP tool available in this Gemini install). Rubric may lean toward the compiler's framing.`

**Stage 9b (cold grading).** Run `python3 <repo-root>/common/mechanize/review.py grade-instructions <OUT_DIR>`. If a subagent MCP tool is available, pass the prompt verbatim (NO extra context) and instruct the subagent to write `<OUT_DIR>/review.md`.

If no MCP subagent tool is available, **skip Stage 9b entirely** and record in `rationale.md`:

> `Stage 9b skipped: no cold-subagent MCP tool available in this Gemini install. Per CORE.md an inline pass by the compiler is nearly worthless. Rubric available at rubric.md for manual review.`

Do NOT run Stage 9b inline.

## Now continue with CORE.md

At this point you should have:
- A user-provided session file path.
- A best-effort segmented mental model (objective, tool calls, attachments, conclusion) built via the general-structure inspector.
- Any spill-to-disk pairs collapsed per Stage 1.5 (usually a no-op).

Continue at **CORE.md Stage 2** and follow through Stage 12 — including Stage 10's trial execution, which is a plain `python3` invocation and needs only this agent's shell-shaped tool. Apply the addenda above where CORE.md references attachments, cold subagents, preview presentation (Stage 8), and rubric-gen / cold grading (Stage 9).

Do NOT skip any of Stages 2 through 12. In particular, do NOT stop after Stage 9 and report success: Stage 10 is what turns a plausible-looking DAG into one that has actually run, and Stage 11 is what turns one that ran into one that ran *correctly*. Do NOT hallucinate rules that aren't in CORE.md.

## Success criterion

See CORE.md's "Success criterion" section. A `sentinel.yaml` + `rationale.md` that a human reader can audit is the bar — nothing less. For Gemini-CLI-compiled Sentinels, the rationale MUST include any `Unresolved:` notes for the transcript-format inference (Stage 1), spill markers (Stage 1.5), attachments, and Stage 5.5 mechanism used, so downstream reviewers know which claims rest on general-structure inference rather than a plugin-validated schema.
