# dev.factory — design and contracts

Ticket in → work around `dev.mcp` (Jira, GitLab, Figma, Confluence) → complete delivery: reviewed MRs whose acceptance criteria, UI included, are **proven by executed evidence**, not by model claims.

Successor of dev.harness. Lessons that shaped it (see dev.harness `docs/audits/2026-11-12-eficacia-construcode.md`):

- The model plans and delegates well on its own. Prose gates add cost, not proof. → **Thin prose, thick mechanisms.**
- PASS without execution was the main escape path. → **Only collectors write evidence; gates read only evidence.**
- Missing runtime environment ended every verify INCOMPLETE. → **Environment is codified and checked at plan time.**
- Work arrives as epics across repos and target branches. → **A run is a set of tickets × repos × targets, not one checkout.**
- Nothing was measured. → **Every run produces metrics.**

## 1. Roles

| Layer | What | Where |
|---|---|---|
| Orchestrator | Main Claude session: decides, talks to the user, runs `factory` CLI, does every dev.mcp **write** and every `git push` | `claude/skills/factory/` |
| Workers | `factory-scout` (haiku, read-only lookups), `factory-author` (sonnet, plans/ACs/checks/diagnosis), `factory-builder` (sonnet, code), `factory-auditor` (sonnet/opus, review, never the author) | `claude/agents/` |
| Mechanism | `factory` CLI (Python ≥3.10 stdlib only) + `browser/` (Node ≥20, Playwright, pixelmatch) | `factory/`, `browser/` |
| Guard | PreToolUse hooks: worker role limits; nobody (main session included) writes evidence, reviews or run state by hand | `claude/hooks/` |

The CLI never calls the network except: local env health checks, local API checks against the run's env, and `git` on local repos. dev.mcp is the agent's tool; when a gate needs remote facts (MR state, pipeline, Figma payload), the agent saves the raw dev.mcp response to a file and the CLI validates that file.

## 2. Files and layout

`FACTORY_HOME` (env var; default `~/.factory`):

```
FACTORY_HOME/
  workspaces/<ws>/workspace.json     # per product (e.g. one per company/product)
  runs/<run-id>/                     # one per ticket or epic
    run.json                         # state; CLI-written only
    acs.json                         # acceptance criteria + checks; author-written, CLI-validated
    figma/<nodeId-dashed>.json|.png  # Figma pack (design-context payload + screenshot), agent-saved from dev.mcp
    figma/meta.json                  # {fileKey, lastModified, version} per file
    checks/                          # browser specs, style maps, api specs written by author/builder
    evidence/<check-id>.json         # collector-written only
    evidence/<check-id>/...          # screenshots, diffs, logs
    baseline/<repo>.json             # quality-command failures on merge-base, computed once
    reviews/<kind>-<n>.json          # recorded via CLI by auditor
    remote/                          # raw dev.mcp responses saved by the agent (mr-*.json, pipeline-*.json, issue-*.json)
    ship-plan.json                   # CLI-generated
    metrics.json                     # CLI-generated
  env/<ws>/<repo>.pid|.log           # processes started by `factory env up`
  secrets/<ws>.env                   # test credentials (never in chat, never in repos)
```

Run id: `<ws>-<ticket-key-lowercase>` (epic: the epic key), e.g. `construcode-dev-9454`.

### 2.1 workspace.json (schemaVersion 1)

```json
{
  "schemaVersion": 1,
  "name": "construcode",
  "worktree_root": "C:/Users/me/Code/wt",
  "repos": [
    {
      "alias": "React", "name": "construcodereact", "order": 2,
      "gitlab_project": "group/construcodereact",
      "path": "C:/Users/me/Code/construcodereact",
      "target_paths": {"homolog": "C:/Users/me/Code/construcodereact-homolog"},
      "quality": {
        "typecheck": "npx tsc --noEmit",
        "lint": "npx eslint . --format json",
        "test": "npx vitest run --reporter=json"
      },
      "env": {
        "start": ["npm", "run", "dev"],
        "cwd": ".",
        "health": "http://localhost:5173/",
        "ports": [5173],
        "base_url": "http://localhost:5173",
        "env_vars": {},
        "seed": null,
        "login": {"url": "/login", "user_field": "#email", "pass_field": "#password", "submit": "button[type=submit]", "success_url_contains": "/app"},
        "startup_timeout_s": 120,
        "depends_on": ["API"]
      }
    }
  ],
  "ship": {
    "targets": ["pack", "homolog"],
    "mirror_targets": "homolog after pack",
    "draft_default": true,
    "mr_title_format": "[{area} | {target_upper}] - {summary}",
    "assignee": null,
    "reviewer": {"account_id": "…", "name": "…"},
    "jira_comment": "mr-links",
    "jira_transition": "Code Review"
  },
  "commit": {"header_regex": "^\\[[^\\]]+\\] - .+", "forbidden_trailers": []},
  "autonomy": {
    "ship": "ask",
    "max_build_loops": 3,
    "max_review_rounds": 2
  },
  "rules": [
    {"id": "i18n", "text": "User-facing strings go through the i18n layer", "check": {"type": "test", "repo": "React", "cmd": "npm run lint:i18n"}}
  ],
  "epics": [{"key": "DEV-8943", "roadmap": "C:/…/roadmap.md", "ship_comment": "C:/…/jira-ship-comment.md", "jira_transition": "Code Review"}]
}
```

`autonomy.ship`: `"ask"` (Gate S in chat for every remote write) or `"draft-auto"` (push + create/update **draft** MRs without asking; un-draft, Jira comment and transition still ask). `factory workspace import --from <repo>/.dev/project.json` converts a dev.harness profile; missing `path`/`quality`/`env` are left for the user.

`rules[]` are repo conventions with an executable check; every rule check runs in `prove` for each touched repo.

### 2.2 run.json (CLI-written only)

```json
{
  "schemaVersion": 1, "id": "construcode-dev-9454", "workspace": "construcode",
  "tickets": [{"key": "DEV-9454", "type": "feature|bug", "parent": "DEV-7853", "depends_on": ["DEV-9451"]}],
  "repos": {"React": {"branch": "feature/DEV-9454-x", "base": "pack", "worktree": "C:/…/wt/dev-9454-react", "head": "<sha>"}},
  "targets": ["pack", "homolog"],
  "phase": "intake|plan|build|prove|review|ship|feedback|done|blocked",
  "risk": "normal|high",
  "loops": {"build": 0, "review": 0},
  "decisions": [{"at": "…", "phase": "plan", "text": "…", "by": "agent|user"}],
  "blockers": [{"at": "…", "kind": "env|access|question|dependency", "text": "…", "resolved": false}],
  "events": [{"at": "…", "kind": "phase|gate_fail|gate_pass|evidence|review|ship|human|loop", "data": {}}],
  "sessions": ["<claude session id or transcript path>"],
  "created": "…", "updated": "…"
}
```

### 2.3 acs.json (author-written, CLI-validated)

```json
{
  "schemaVersion": 1,
  "acs": [
    {"id": "AC-1", "ticket": "DEV-9454", "text": "…", "kind": "ui|api|logic|data|bug", "source": "jira:DEV-9454#description",
     "checks": ["CHK-1", "CHK-2"]}
  ],
  "checks": [
    {"id": "CHK-1", "type": "test", "repo": "React", "cmd": "npx vitest run src/x.test.tsx", "expect": "exit0"},
    {"id": "CHK-2", "type": "browser", "repo": "React", "spec": "checks/browser/ac-1.spec.ts"},
    {"id": "CHK-3", "type": "visual", "repo": "React", "node": "3544:5710", "path": "/obras/1/tarefas", "selector": "[data-testid=drawer]", "viewport": [1440, 900], "max_diff_ratio": 0.02, "masks": []},
    {"id": "CHK-4", "type": "style", "repo": "React", "node": "3544:5710", "path": "/obras/1/tarefas", "map": "checks/style/drawer.json"},
    {"id": "CHK-5", "type": "api", "repo": "API", "method": "POST", "path": "/v2/campos", "body": {}, "expect": {"status": 201, "json": {"$.id": "exists"}}},
    {"id": "CHK-6", "type": "red-green", "repo": "API", "cmd": "dotnet test --filter X", "fix_commit": "<sha or HEAD>"},
    {"id": "CHK-7", "type": "human", "text": "Native screen picker cannot be automated", "reason": "…"}
  ]
}
```

Plan-gate rules (`factory gate <run> plan`):

- Every AC has ≥1 check; every check is referenced by ≥1 AC; ids unique.
- `kind: ui` ACs need ≥1 `visual` **and** ≥1 `style` check; every `node` has a pack entry (`figma/<node>.json` + `.png` decodable) and `figma/meta.json` names its file revision.
- `kind: bug` ACs need a `red-green` check.
- `browser`/`visual`/`style`/`api` checks need the repo's `env` block and `factory env check` capability (start command + health); missing env → gate fails with one consolidated list of what the user must provide.
- `human` checks are allowed but counted; the ship briefing lists them; they never satisfy a `ui` or `bug` AC on their own.
- Style maps: every expected value must appear in the node's pack JSON (as number or hex), so expectations come from Figma, not from the model.

### 2.4 Evidence record (collector-written only)

```json
{
  "schemaVersion": 1, "check_id": "CHK-3", "type": "visual", "status": "pass|fail|error",
  "repo": "React", "git_sha": "<HEAD of repo worktree at collection>", "dirty": false,
  "collector": "factory/0.1.0 visual", "started": "…", "duration_s": 12.3,
  "summary": "diffRatio 0.004 ≤ 0.02",
  "artifacts": ["evidence/CHK-3/app.png", "evidence/CHK-3/figma.png", "evidence/CHK-3/diff.png"],
  "details": {}
}
```

Collected with a dirty worktree → `status` may be pass but the prove gate rejects it.

### 2.5 Review record (via `factory review record`)

```json
{"schemaVersion": 1, "kind": "code|qa", "round": 1, "verdict": "APPROVE|CHANGES", "reviewer": "factory-auditor", "repos": {"React": "<sha>"},
 "findings": [{"id": "F1", "severity": "blocker|major|minor", "file": "src/x.tsx:42", "text": "…", "ac": "AC-1"}],
 "completeness": {"done": 0, "gap": 0, "not_checked": 0}}
```

QA review must state, for every AC, whether its checks actually exercise the AC (a check that cannot fail is a finding).

## 3. CLI contract

`python -m factory <command> …` (installed shim: `factory`). Exit 0 = OK, 1 = gate/check failed, 2 = usage/config error. Every command prints a short human summary; `--json` prints machine output.

| Command | Owner module | Effect |
|---|---|---|
| `workspace init <ws>` / `workspace import <ws> --from <project.json>` / `workspace check <ws>` | `commands/workspace.py` | create/convert/validate workspace.json |
| `run start <ws> <TICKET...> [--epic KEY] [--risk high]` | `commands/run.py` | create run dir + run.json (phase intake) |
| `run status <run>` / `run list` | `commands/run.py` | phase, loops, blockers, next required action |
| `run branch <run> <repo> --base <target> [--name …]` | `commands/run.py` | create worktree + branch under worktree_root, record in run.json |
| `run decide <run> "<text>" [--by user]` / `run block <run> <kind> "<text>"` / `run unblock <run> <n>` | `commands/run.py` | append decision/blocker |
| `run human <run> "<note>"` | `commands/run.py` | record a human intervention (orchestrator calls it for every user message that steers the run) |
| `gate <run> <phase>` | `commands/gate.py` | check exit criteria of phase; on pass, advance phase; records gate_pass/gate_fail |
| `env up|down|check|logs <ws> <repo> [--run <run>]` | `commands/env.py` | start/stop processes from workspace env (worktree of the run when `--run`), health wait, port-owner diagnosis, pid tracking, tree-kill on Windows |
| `env login <ws> <repo>` | `commands/env.py` → browser | produce Playwright storage state from `secrets/<ws>.env` |
| `figma check <run>` / `figma expect <run> <node> [--selector …]` | `commands/figma.py` | pack completeness; extract expected style values from a node payload into a style-map skeleton |
| `baseline <run> [--repo R]` | `commands/prove.py` | run quality commands on merge-base once; store failures |
| `prove <run> [--check CHK-n] [--repo R]` | `commands/prove.py` | run collectors for checks + quality delta vs baseline + `rules[]`; write evidence |
| `review record <run> --kind code|qa --file <review.json>` | `commands/review.py` | validate and store review bound to current SHAs |
| `ship plan <run>` | `commands/ship.py` | freshness (behind-count vs origin/<target>, needs prior `git fetch`), dependency MR states from `remote/`, then ship-plan.json: per repo×target branch, title, draft, description (ACs × evidence table, Depends on !N, human checks), Jira ADF comment, ordered remote actions |
| `ship verify <run>` | `commands/ship.py` | compare `remote/mr-<repo>-<target>.json` readbacks with the plan (draft, target, source, title); record ship event |
| `feedback ingest <run>` | `commands/feedback.py` | read `remote/mr-*-discussions.json` and `remote/pipeline-*.json`; unresolved human threads and failed jobs become findings; moves phase back to build |
| `metrics <run>` / `metrics report [--since DATE]` | `commands/metrics.py` | from run.json + Claude transcripts (session ids in run.json, or transcripts mentioning `factory-run: <id>`): wall time, human messages, subagents by type/model, tokens, loops, gate failures, escaped findings (review/feedback findings after a prove pass) |

Phase exit criteria (`gate`):

| Leaving | Requires |
|---|---|
| intake | ≥1 ticket; every ticket has `remote/issue-<KEY>.json`; open blockers of kind question are empty |
| plan | `acs.json` valid per §2.3; `figma check` OK; branches/worktrees created for every touched repo; env capability for every runtime check |
| build | every touched repo worktree clean and ahead of base; commits match `commit.header_regex`; no forbidden trailers |
| prove | every check has evidence `pass`, not dirty, `git_sha` == current HEAD of its repo; quality delta vs baseline has no new failures; every rule check passes |
| review | latest `code` and `qa` reviews `APPROVE`, bound to current SHAs, zero open blocker/major findings; else phase → build and `loops.build += 1` (stop at `autonomy.max_build_loops`) |
| ship | `ship verify` OK |

## 4. Collectors

`factory/collectors/<type>.py` exposes `collect(check: dict, ctx: Ctx) -> dict` returning the evidence record body (`status`, `summary`, `artifacts`, `details`); `prove` fills common fields and writes the file through `core.write_evidence`.

| type | Implementation |
|---|---|
| `test` | run `cmd` in repo worktree (shell, timeout), exit code, tail of output |
| `red-green` | temp worktree at `fix_commit~1` with the test files from `fix_commit` → must fail; at `fix_commit` → must pass |
| `api` | env must be up; urllib request to `base_url + path`; status + simple JSONPath (`$.a.b`, `exists`/literal) |
| `browser` | `node browser/dist/cli.js run-spec --spec … --base-url … --storage-state … --out …` (Playwright test runner, JSON reporter) |
| `visual` | `node browser/dist/cli.js capture …` then `compare --figma figma/<node>.png …` (pixelmatch; copy check; masks) |
| `style` | `node browser/dist/cli.js style-probe --url … --map … --out …`: getComputedStyle + bounding box per selector vs expected values with tolerance (px ±1 default, colors exact after normalisation) |

The Python wrappers for `browser|visual|style` live in `factory/collectors/` and shell out to Node; they never decide pass/fail themselves beyond reading the Node result file.

## 5. Host integration (Claude Code)

- Skill `factory` (model-invocable): entry for any ticket/epic URL or key; the runbook of phases, which worker does what, when to call which CLI command, and the stop rules. Target ≤150 lines; details in `references/`.
- Agents `factory-scout|author|builder|auditor` with role guard hooks.
- Hook `protect.js` (installed in the profile's settings for **all** sessions): deny Write/Edit/NotebookEdit and shell redirection into `FACTORY_HOME/runs/*/{run.json,evidence/**,reviews/**,baseline/**,metrics.json,ship-plan.json}`.
- `install.py --profile <dir> [--remove-harness] [--dry-run]`: copies/links skill, agents, hooks into the profile, merges hook entries into `settings.json`, writes the delegation snippet into the profile's `CLAUDE.md` between markers, creates `FACTORY_HOME`. `--remove-harness` removes `dev-*` skills/agents/hooks and the harness block from `CLAUDE.md` (backup first).

## 6. Non-goals (v0.1)

Queue pulling from Jira boards, autonomy levels beyond `ask`/`draft-auto`, CI auto-fix without the orchestrator, non-Claude hosts.
