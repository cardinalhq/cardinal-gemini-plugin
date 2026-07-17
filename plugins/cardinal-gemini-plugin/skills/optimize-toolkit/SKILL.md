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

### 2. Discover — pull raw spawns (1 call)

Call `outcomes__cluster_spawns` with `window: "30d"`, **`min_jaccard: 0.99`**,
**`min_cluster_size: 1`**. Those thresholds effectively disable the server's
built-in token-Jaccard clustering — each spawn returns as its own single-
member "cluster", giving you the raw spawn population (label + session_id +
tool_signature + tokens + model). You do the actual clustering client-side
in the next step, because the server's token-Jaccard is too coarse for
semantically-similar-but-token-diverse labels ("Trace polly cwd flow" and
"Map maestro sites/org API" are both investigation-shape but share zero
content tokens).

Note the `coverage.with_description_share` — if it's under 0.5, mention it
in the caveat you already added in step 1 (older sessions from pre-enrichment
plugin versions don't emit `subagent_description`; it's legacy drift, not
a per-agent gap).

### 3. Reduce + semantically cluster (Bash + LLM pass)

**Stage 3a — mechanical reduction (Bash).** Save the cluster_spawns
response to a temp file and pipe it through the reducer that ships
alongside this SKILL.md. The reducer collapses N-identical labels,
groups by first content-word (verb), sub-groups by dominant-tool
signature, and collapses same-session bursts. It turns 100+ raw spawn
records into ~15–30 sub-clusters — a small enough set for the semantic
pass in 3b to reason over without paging in the whole raw population.

```bash
# Find the reducer that shipped with this skill. Portable across
# install locations.
REDUCER=$(find ~/.gemini/plugins ~/.gemini/skills ~/.gemini/extensions . \
  -name reduce_spawns.py -path '*optimize-toolkit*' 2>/dev/null | head -1)
[ -z "$REDUCER" ] && {{ echo "reduce_spawns.py not found"; exit 1; }}

# Feed cluster_spawns response as JSON on stdin; get verb-bucketed JSON out.
python3 "$REDUCER" < /tmp/spawns_raw.json > /tmp/spawns_reduced.json
cat /tmp/spawns_reduced.json
```

Output shape: `{{ input_spawns, input_verb_buckets, reduced_rows: [{{verb,
spawn_count, unique_labels, top_labels[:8], tokens_total, session_count,
burst_count, tool_shape, sample_top_by_tokens}}] }}`. Rows come
sorted by `tokens_total` descending.

**Stage 3b — semantic cluster (your reasoning, in-context).** Read the
reducer's output and group verb buckets into meta-clusters by intent, not
by surface tokens. Verb buckets like `code`/`silent-failure`/`silent`/
`test`/`comment`/`type`/`review`/`independent`/`fresh-eyes`/`general` all
fold into one **Code Review** meta-cluster; `trace`/`research`/`investigate`
/`find`/`explore`/`map`/`inventory`/`reconcile` fold into one **Research
& Investigation** meta-cluster. Expected meta-clusters for a typical
engineer include: **Code Review**, **Research & Investigation**,
**Migration / Implementation**, **Testing & Validation**, **Planning /
Organization**. Not every meta-cluster surfaces for every user; only
name the ones that clear a real token/recurrence floor from the data
in front of you.

The semantic pass is **your judgment** — no server-side taxonomy. Use
the reducer's rich per-bucket evidence (top_labels, session_count,
tool_shape) to justify each grouping. A verb bucket that
doesn't cleanly fit any meta-cluster stays as its own single-bucket
meta-cluster; don't force-fit for symmetry.

Take the top **K = 3** meta-clusters by `tokens_total`. If fewer than 3
clear meta-clusters exist, present fewer — do not pad.

**Known coverage gap.** `cluster_spawns` only covers Task-tool subagent
spawns. Slash commands (visible in `my_toolkit_adoption.commands`)
never show up here — a heavy user of a release-command won't see a
"Releases" cluster no matter how the reducer runs. If the user asks
about command patterns, say so plainly and point at
`my_toolkit_adoption.commands` for the raw counts; don't fabricate a
cluster from spawn data alone.

### 4. Ground yourself in the user's real toolkit (0–2 file reads per meta-cluster)

Before picking a kind for a meta-cluster, look at what the user actually
has. `my_toolkit_adoption` lists capabilities by name and usage, but
"a name in a usage map" is not the same as "a file on disk that will
still be there next session." For each of the top-K meta-clusters:

- If `my_toolkit_adoption` shows an agent/skill whose name looks like
  it might cover the meta-cluster's dominant verb buckets +
  `tool_shape`, read the matching file under
  `.gemini/agents/` (or the user-scoped equivalent) to confirm the fit
  before recommending `adopt`. Bad `adopt` recommendations happen when
  the name matches but the actual scope doesn't.
- If you're considering `extract` (mint new), grep under
  `.gemini/agents/` for the meta-cluster's `tool_shape` — a
  similar-shape agent may already exist under a name your
  `my_toolkit_adoption` scan didn't surface.
- If you're considering `pin`/`downgrade`, locate the existing agent
  file so you can propose a **minimal edit** (change one `model` line)
  rather than overwriting the whole file.

Keep this bounded — one or two focused reads per meta-cluster, not a
full-repo sweep. If the file isn't obviously there, that's information
("adopt target's name matched but the file isn't under `.gemini/agents/` —
treat as evidence to soften the adopt pitch, or reframe as `gap`").

### 5. Pick the kind, from first principles

Call `outcomes__org_offered_tiers` once — this tells you the org's
actual `cheap` and `reasoning` model tiers. **Never suggest a model
the org isn't offered**; if a tier is `null`, that door is closed for
this org.

**Drill into sub-clusters — meta-clusters are framing, not the
recommendation unit.** The meta-cluster tells you the organizing shape
of an engineer's work (Research is heavy, Code Review is heavy); the
actionable play lives one level deeper, at the sub-cluster level: the
individual verb-bucket from the reducer with its specific
`top_labels`, `tool_shape`, `sample_top_by_tokens[].model`,
`session_count`, and `burst_count`. Pitching at the meta-cluster level
alone produces truisms ("mechanical work should run on Haiku") — the
non-obvious plays only appear when you compare a sub-cluster's shape
against `my_toolkit_adoption`.

**Strongest pattern (empirically): toolkit-consistency adopt.** When a
sub-cluster's verb + `tool_shape` matches an existing agent
in `my_toolkit_adoption` (e.g. code-review-shaped labels + Bash+Read +
no Agent/Skill call in the tool_signature → `pr-review-toolkit:code-reviewer`
covers this), the play is `adopt` — the user has the tool, they're
inconsistently skipping it. Contrast evidence sharpens the pitch: if
another spawn in the same window DID show `Agent` or `Skill` in its
tool_signature for the same verb, cite that ("you already use this
tool sometimes — 3 of your same-shape spawns bypassed it").

**Tie-break — adopt beats downgrade when both fire.** If a sub-cluster
is both `adopt`-covered (an existing agent handles this shape) AND
downgrade-shaped (running on reasoning tier with a mechanical
tool_signature), pitch `adopt` alone. The existing agent's own model
config is a separate concern; routing the work to the agent captures
the primary win. Do not double-recommend.

For each of the top-K meta-clusters, reason about kind from the
evidence in front of you (the semantic cluster label, its member verb
buckets, its dominant tool shapes, its token magnitude, and the
`my_toolkit_adoption` match you just grounded). There is no server-side kind gate — you pick, you
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

### 6. Estimate savings (1 call per non-`gap` candidate)

For each candidate that isn't `gap`, call
`outcomes__estimate_savings` with the cluster's `total_tokens`,
per-component tokens if you can derive them, `current_model`, and
`target_tier`. Read `estimate.assumptions.placeholder_output_ratio` /
`placeholder_cache_ratio` and `estimate.estimate` on every response —
see **Placeholder savings, honestly** below.

### 7. Author + Present (no tool call; you write the artifact)

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

### 8. Write (only on explicit confirmation)

**Validate the target-file basename before anything else.** Reject
if the name (a) contains `/` or `\`, (b) contains `..`, or (c)
doesn't match kebab-case `^[a-z][a-z0-9-]*$`. On rejection, surface
the value verbatim to the user with the specific reason and stop.

Only write after an explicit "yes"-shaped answer. No write on
silence, hedging, or topic change. After writing, tell the user this
**takes effect next session** (mention `/agents reload` as a faster
option if the user's Gemini CLI version supports it). Consent,
revert, and distribution are git.

### 9. Mark (1 call per candidate you presented)

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
- **`current_cost_usd: null` — dominant model not priced in this
  org's catalog.** If the sub-cluster's dominant model (e.g.,
  `claude-opus-4-8`) isn't in `org_offered_tiers.all`,
  `estimate_savings` returns `current_cost_usd: null` and
  `savings_high/low_usd: 0`. Do not quote a $ figure. Pitch the play
  on consistency or shape grounds ("mechanical tool-use pattern, better
  routed through <existing agent>") — the savings surface just isn't
  usable in this org for this model.

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
