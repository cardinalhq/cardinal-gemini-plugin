---
name: cardinal-optimize-toolkit
description: Mine this engineer's own session telemetry for capability-fit recommendations (extract a new sub-agent, pin/downgrade a model tier, adopt or swap an existing capability, consolidate duplicates) and, on confirmation, write the accepted artifact into the working tree. Only run this when the user explicitly asks to optimize their toolkit, review capability-fit recommendations, or names this skill directly — do not trigger it opportunistically off a loose topical match.
---

# Cardinal Optimize Toolkit

Use this skill when the user explicitly asks Gemini CLI to optimize
their toolkit, mine their own session history for capability-fit
recommendations, or review whether a recurring inline pattern should
become a reusable sub-agent. It is **not** a skill to reach for
opportunistically just because the conversation is adjacent to agents
or tooling — see **What not to do** below for why.

A **per-user, past-informed, future-effective** optimizer — not a
session-local one. It looks at your own last 30 days of sessions,
picks the top few recurring inline-work patterns worth acting on,
authors the artifact itself grounded in your actual toolkit, and — only
on your explicit confirmation — writes the accepted artifact to this
working tree. **Anything written takes effect next session**, not this
one: Gemini CLI loads custom subagent definitions
(`.gemini/agents/*.md`) at startup, the same as `~/.gemini/settings.json`
and the extension directory — there is no live-registration channel,
so accepting a candidate here will not change what happens for the
rest of this conversation. (`/agents reload` reloads the agent
registry without a full restart if the user's Gemini CLI version
supports it — mention it as an option, but the "takes effect next
session" framing below is still the safe default to tell the user.)

This burns your own session tokens to run. The server (conductor's
maestro, via the `cardinal` MCP server) provides evidence — clusters,
model mix, toolkit adoption, tier pricing, priced-savings math. **You
(Gemini CLI) do the authorship** from first principles: pick the
recommendation kind by reasoning about the evidence, then write the
`.gemini/agents/<name>.md` artifact grounded in this user's real
capabilities, real tool signatures, real cluster labels. There is no
server-side artifact template — the artifact is composed here, per
invocation, and passed back to the ledger verbatim on `mark`.

## Before you start

This skill orchestrates six `outcomes__*` tools served by the
`cardinal` MCP server (see the `cardinal-connect` skill — same server,
same consent). They are documented in conductor's
`docs/specs/optimize-toolkit-mcp-tools.md`:

1. `outcomes__my_turn_pattern`
2. `outcomes__my_toolkit_adoption`
3. `outcomes__cluster_spawns`
4. `outcomes__org_offered_tiers`
5. `outcomes__estimate_savings`
6. `outcomes__mark`

`outcomes__my_recent_spawns` also exists on the same server — it's a
raw spawn history for ad-hoc debugging, not part of this flow. Reach
for it only if the user explicitly asks "what did I spawn recently?".

**Check your available tools before doing anything else.** If none of
the `outcomes__*` tools appear in your tool list, say so plainly and
stop:

> "The optimize-toolkit tools aren't wired into this org's Cardinal MCP
> server yet — that's a known rollout gap, not a problem with your
> connection. Nothing to do here yet."

Do not attempt a substitute analysis (no reading transcripts, no
grepping local logs, no falling back to "let me look at your repo
instead"). MCP unreachable or empty candidates → one line, stop, no
retries.

## How you (Gemini CLI) should run this

Budget: usually 5–7 tool calls total, ≤10 in the worst case. You'll
also read a handful of files from the working tree to ground kind
picking and authorship — that's fine, keep it targeted.

### 1. Open — situational-awareness bundle (2 calls)

Call `outcomes__my_turn_pattern` and `outcomes__my_toolkit_adoption`,
both with `window: "30d"`. From the two responses, compose a short
(≤6 line) opening paragraph before mentioning any candidate:

- State the evidence window explicitly ("based on your last 30 days of
  sessions") — never let the opening imply "this session."
- Name the caller's top 1–2 model-mix rows by cost from
  `my_turn_pattern.models`.
- Name the toolkit-adoption headline the first candidate will target
  (from `my_toolkit_adoption.agents` or `.skills`).
- **No usable evidence, stop before any ratio math.** If
  `my_toolkit_adoption.coverage.sessions_scanned == 0`, or
  `my_turn_pattern.turns_total` is 0, there is nothing to compute a
  coverage ratio from — say "no optimization candidates right now,
  not enough session history yet" and stop.
- **Coverage caveat.** Use `my_toolkit_adoption.coverage.sessions_with_tier_attribution
  / coverage.sessions_scanned` as the enrichment-coverage proxy. When
  that ratio is under 0.5, prepend a one-line caveat: "evidence from
  N/M of your sessions in the last 30 days — coverage will climb as
  you keep working on a current plugin version." Also check
  `my_turn_pattern.coverage.plugin_versions_seen` — if it shows a
  stale version for recent turns, mention the plugin-version-drift
  possibility and suggest a Gemini CLI restart.

### 2. Discover (1 call)

Call `outcomes__cluster_spawns` with `window: "30d"` (defaults:
`min_jaccard: 0.4`, `min_cluster_size: 3`). Rank clusters by
`total_tokens` and recurrence; drop anything with `recurrence < 3`,
prefer clusters with a higher `with_description_share` in `coverage`.

Take the top **K = 3** clusters forward. If fewer than 3 clear
clusters exist, present fewer.

### 3. Ground yourself in the user's real toolkit (0–2 file reads per candidate)

Before picking a kind for a cluster, look at what the user actually
has. `my_toolkit_adoption` lists capabilities by name and usage, but
"a name in a usage map" is not the same as "a file on disk that will
still be there next session." For each of the top-K clusters:

- If `my_toolkit_adoption` shows an agent/skill whose name looks like
  it might cover this cluster's `representative_label` +
  `tool_signature`, read the matching file under `.gemini/agents/`
  (or the user-scoped equivalent) to confirm the fit before
  recommending `adopt`.
- If you're considering `extract` (mint new), grep under
  `.gemini/agents/` for the cluster's dominant tools in the
  `tool_signature` — a similar-shape agent may already exist under a
  name your `my_toolkit_adoption` scan didn't surface.
- If you're considering `pin`/`downgrade`, locate the existing agent
  file so you can propose a **minimal edit** (change one `model:`
  line) rather than overwriting the whole file.

Keep this bounded — one or two focused reads per candidate, not a
full-repo sweep.

### 4. Pick the kind, from first principles

Call `outcomes__org_offered_tiers` once. **Never suggest a model the
org isn't offered**; if a tier is `null`, that door is closed.

For each of the top-K clusters, reason about kind from the evidence
in front of you. There is no server-side kind gate — you pick, you
justify:

- **`adopt`** — cluster's `tool_signature` and label overlap an
  existing capability you saw in step 3. Recommend the user reach
  for the existing thing consistently, no new file.
- **`swap`** — cluster overlaps an existing capability, but that
  existing one is wrong shape/model. Recommend replacing it.
- **`pin`** — cluster runs on a mix of tiers with the org's cheap
  tier already carrying meaningful share (say, ≥30% of the cluster's
  `subagent_model` occurrences) and no evidence the reasoning tier
  is load-bearing. Recommend pinning the existing capability.
- **`downgrade`** — cluster runs predominantly on the reasoning
  tier (say, ≥50% share) but the `tool_signature` looks mechanical
  (`jaccard_within` ≥ 0.6, small tool set). Recommend re-tiering
  down.
- **`extract`** — recurring inline pattern with no existing named
  capability to point at. Mint a new sub-agent. This is the one
  kind where you author a full new `.gemini/agents/<name>.md` file
  from scratch.
- **`consolidate`** — two clusters look like the same underlying
  job. No new file; present conversationally.
- **`gap`** (signal only, no artifact) — cluster is real recurring
  work, but you cannot pick a kind honestly. Say so plainly.

Thresholds above are rules of thumb, not gates. Adjust when the
cluster's specifics clearly warrant.

### 5. Estimate savings (1 call per non-`gap` candidate)

For each candidate that isn't `gap`, call
`outcomes__estimate_savings` with the cluster's `total_tokens`,
per-component tokens if you can derive them, `current_model`, and
`target_tier`. Read `estimate.assumptions.placeholder_output_ratio` /
`placeholder_cache_ratio` and `estimate.estimate` on every response —
see **Placeholder savings, honestly** below.

### 6. Author + Present (no tool call; you write the artifact)

Per kind:

**`extract` — new file at `.gemini/agents/<name>.md`.** You author:

- `name`: kebab-case, `^[a-z][a-z0-9-]*$`, derived from the
  cluster's `representative_label`. If a same-name file already
  exists, disambiguate rather than overwrite.
- `description`: one sentence stating what work this handles and
  when Gemini CLI should delegate to it. Ground it in the cluster's
  actual observed work — not a template.
- `tools`: derived from the cluster's `tool_signature`. If the
  tool_signature is empty, omit the `tools:` line.
- `model`: the `target_model_id` from `org_offered_tiers`.
- Body: a short (≤10 line) system prompt describing the role,
  grounded in this cluster's specifics.

**`pin`/`downgrade` — edit an existing file at `.gemini/agents/<name>.md`.**
The meaningful change is one frontmatter line:
`model: <target_model_id>`. Author the dry-run as a one-field edit.

**`adopt` — usually no file.** Author a plain-language
recommendation: "stop spawning this inline pattern; your existing
`<capability>` already handles it — I saw N sessions where it would
have applied."

**`swap` — edit or replace an existing file.** Author the dry-run
as the specific edit you'd propose.

**`consolidate` — no automated file work.** Present the two-candidate
overlap and ask the user which capability should absorb the other.

**`gap` — no artifact.** State the pattern, call `mark` with
`status: "presented"` and `proposed_kind: "gap"`; skip the
write/confirmation loop.

**Present** the authored artifact with:

- The evidence summary in plain language.
- The `matching_sessions` slice from the cluster if present.
- The savings figure, honestly caveated per placeholder rules.
- A plain confirmation question ("write this to
  `.gemini/agents/<name>.md`? yes/no"). One candidate at a time.

### 7. Write (only on explicit confirmation)

**Validate the target-file basename before anything else.** Reject
if the name (a) contains `/` or `\`, (b) contains `..`, or (c)
doesn't match kebab-case `^[a-z][a-z0-9-]*$`. On rejection, surface
the value verbatim to the user with the specific reason and stop.

Only write after an explicit "yes"-shaped answer. No write on
silence, hedging, or topic change. After writing, tell the user this
**takes effect next session** (mention `/agents reload` as a faster
option if the user's Gemini CLI version supports it). Consent,
revert, and distribution are git.

### 8. Mark (1 call per candidate you presented)

**Exactly one `mark` call per candidate.** Pick from:

- `status: "accepted"` — confirmed and written.
- `status: "dismissed"` — **explicit refusal only**. Ask one short
  follow-up and forward the answer verbatim as `reason` (cap ~200
  chars).
- `status: "presented"` — shown, no decision either way. **Never
  auto-dismiss on non-confirmation.**

Use `action: { kind: "cluster", cluster_id, proposed_kind }`. When
the candidate is `extract`/`pin`/`downgrade`/`swap`/`consolidate`
and you authored an artifact, pass `agent_spec_md` (your authored
body, verbatim) and `est_savings_low_usd` /
`est_savings_high_usd` (from `estimate_savings`) so the ledger row
isn't lossy. For `adopt` with no file written, and for `gap`, omit
those fields.

Mark is best-effort: if the call fails, don't error the
conversation over it.

## Failure handling for non-`mark` tools

`mark` is the one tool that follows the silent-log rule — every
other tool is on the hard-stop rule. If any of `my_turn_pattern`,
`my_toolkit_adoption`, `cluster_spawns`, `org_offered_tiers`, or
`estimate_savings` returns `503`, `400`, or an empty result set
where the flow depends on at least one row, **surface the error
verbatim, stop the flow, do not retry**.

## Placeholder savings, honestly

Every `outcomes__estimate_savings` response carries fields that
exist specifically so you don't overstate a number:

- `estimate: "no_cohort_catalog_only"` means catalog pricing math
  only. Say so: "treat it as a ceiling, not a promise."
- `assumptions.placeholder_output_ratio` /
  `placeholder_cache_ratio` set to `true` mean typical-ratio
  fallback. Say "estimated within a wide band" rather than a bare
  point figure.
- When `current_cost_usd` is `null`, don't imply a before/after
  delta — state the projected cost alone.

## What not to do

- Don't spawn a sub-agent to do independent investigation.
- Don't invent a cohort comparison when a tool response says
  there isn't one.
- Don't write anything without an explicit "yes" in this
  conversation.
- Don't auto-invoke yourself opportunistically. Gemini CLI has no
  hard invocation gate the way the claude adapter's
  `disable-model-invocation: true` provides — treat this prose
  rule as load-bearing.
- Don't paper over a bad pick with a plausibly-worded artifact.
  If you find yourself writing template-shaped prose, reclassify
  as `gap` and say so.
- Don't exceed the ~10-call budget on `outcomes__*` tools.
