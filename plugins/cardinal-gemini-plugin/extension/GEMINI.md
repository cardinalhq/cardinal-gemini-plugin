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
