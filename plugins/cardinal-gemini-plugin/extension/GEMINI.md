# Cardinal for Gemini CLI

You are running inside a Cardinal-instrumented Gemini CLI session.

Cardinal attributes agent spend to "initiatives" — one branch = one
initiative. When you create a new branch for work in this session,
follow the convention:

```
<type-prefix>/<kebab-name>

type-prefix ∈ {feat, fix, refactor, infra, chore, research, spike}
kebab-name  = lowercase, 1–4 dash-separated segments
```

Examples:

- `feat/outcomes-observability` → name "outcomes-observability", type "feature"
- `fix/login-crash` → name "login-crash", type "bugfix"
- `refactor/auth-token-rotation` → name "auth-token-rotation", type "refactor"
- `research/data-pipeline-spike` → name "data-pipeline-spike", type "research"

Prefix aliases: `feature` = `feat`, `bugfix` = `fix`, `chore` = `infra`,
`spike` = `research`. Other conventional prefixes are also recognized:
`perf` → feature; `cleanup` → refactor; `test`, `tests`, `ci`, `build`,
`deps`, `docs`, `doc` → infra. Sessions on `main` / `master` / `develop` /
`trunk` are treated as research/scoping work — when intent crystallises
into a deliverable, cut a typed branch using this convention.
Off-convention branches get a stable name but default to type "feature",
so the convention is the way to ensure correct classification.

The `cardinal` MCP server is available for observability queries against
your Cardinal workspace.

## Citing tool results in a Cardinal storyboard

Every tool result in this session is kept on this machine by the Cardinal
plugin and gets an id, shown next to the result as `[evidence:ev_…]`. Any of
them can be cited in an Investigation Storyboard: shell commands (tests,
`git`, `make`), file reads and edits, searches, web fetches, other MCP
servers, any tool. Nothing is uploaded until you promote it. Promote only
what a scene cites, as you write that scene:
`cardinal-evidence promote --storyboard <sb_id> ev_… [ev_…]` prints `ev_… -> rcpt_…`;
bind that receipt. An entry promoted before prints its existing receipt
(`already promoted`): reuse that id. `cardinal-evidence find <text>` looks up an id you lost;
`cardinal-evidence show ev_…` prints exactly what you would cite.
(`cardinal-evidence` is the plugin's `scripts/cardinal-evidence`; the first
`[evidence:…]` line of a session gives its full path.)

- `[evidence:ev_… withheld: …]` means the call touched something sensitive
  (a `.env`, a key, a credential command). Nothing was kept and it cannot be
  cited: say so plainly instead of paraphrasing its result.
- Redacted is not withheld: a captured result showing `[redacted]` was kept
  with its secrets masked. Cite it and call it redacted, never withheld; claim
  nothing about the masked values.
- Never claim more than the output shows. A captured result is the client's
  record, labeled "reported by <client>". Exit 0 shows a command succeeded,
  not that the feature works; bind the exact field that carries the claim.
