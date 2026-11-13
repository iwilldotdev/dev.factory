# dev.factory — design and contracts

Ticket in → work around `dev.mcp` (Jira, GitLab, Figma, Confluence) → complete delivery: reviewed MRs whose acceptance criteria, UI included, are **proven by executed evidence**, not by model claims.

Successor of dev.harness. Lessons that shaped it (see the dev.harness effectiveness audit of 2026-11-12):

- The model plans and delegates well on its own. Prose gates add cost, not proof. → **Thin prose, thick mechanisms.**
- PASS without execution was the main escape path. → **Only collectors write evidence; gates read only evidence.**
- Missing runtime environment ended every verify INCOMPLETE. → **Environment is codified and checked at plan time.**
- Work arrives as epics across repos and target branches. → **A run is a set of tickets × repos × targets, not one checkout.**
- Nothing was measured. → **Every run produces metrics.**

## 0. Threat model

The factory defends against two things:

1. **Accidental or lazy false PASS by a cooperative model**: claiming an AC or fix from reading code, a check that cannot fail, a setup failure taken for a reproduction, loosening a check or a cap to go green, a stale review or proof carried past a change.
2. **Workers exceeding their role**: a builder recording reviews, an author touching code, any worker changing run state, replanning, rewinding, deciding for the user or setting `FACTORY_ROLE`.

It does **not** defend against a deliberately adversarial orchestrator. All state (run.json, evidence, reviews, workspace.json, `remote/` readbacks) is local files writable by the same OS user that runs the main session; the hooks and digests stop mistakes and shortcuts, not a session determined to forge them. The main session is the trusted orchestrator: it never records reviews (only the factory-auditor does) and uses `run decide --by user` only for decisions the user actually stated in chat.

Mechanisms within this model: plan lock over the plan files and `workspace.json` (§2.6); reviews bound to the plan lock and current SHAs and re-checked at ship; `run replan`/`run rewind` need a recorded user decision (`--decision <n>`) or count against `autonomy.max_replans` / `autonomy.max_build_loops` and block the run; setup failures (exit 126/127/9009, command/file/module not found) are never a reproduction and `expect_fail_pattern` must name real text; env pids are killed or trusted only when their create time matches; pipeline waivers name the pipeline id and keep the user's words.

**Accepted residual risk** (out of scope by design):

- run.json / event forgery by the orchestrator (e.g. writing an evidence file and appending a matching digest event through Python instead of `factory prove`). Digests prove a file is what the CLI wrote, not who ran the CLI.
- `remote/` readbacks (MR, pipeline, discussions) written by hand instead of saved by a dev.mcp read with `save_to`.
- Untracked files hidden from `git status` by `.git/info/exclude` (or a global excludes file): `dirty` checks see only what git reports.
- `review record` trusts `FACTORY_ROLE=auditor` in its environment; the guard keeps scout, author and builder from mentioning it, and the main session is trusted not to use it. The stored `recorded_by` (Claude session/agent env hint, best effort) is an audit trail, not authentication.

**Future work:** tamper-proof proof needs `prove` (and the gates that read its output) to run in another trust domain, e.g. a GitLab CI job that collects the evidence and stores or signs it where the local session cannot write, with the MR pipeline as the authority.

## 1. Roles

| Layer | What | Where |
|---|---|---|
| Orchestrator | Main Claude session: decides, talks to the user, runs `factory` CLI, does every dev.mcp **write** and every `git push` | `claude/skills/factory/` |
| Workers | `factory-scout` (haiku, read-only lookups), `factory-author` (sonnet, plans/ACs/checks/diagnosis), `factory-builder` (sonnet, code), `factory-auditor` (sonnet/opus, review, never the author) | `claude/agents/` |
| Mechanism | `factory` CLI (Python ≥3.10 stdlib only) + `browser/` (Node ≥20, Playwright, pixelmatch) | `factory/`, `browser/` |
| Guard | PreToolUse hooks: worker role limits; nobody (main session included) writes evidence, reviews or run state by hand; among workers only the auditor may mention `FACTORY_ROLE` (needed by `review record`); no worker runs `run replan|rewind|decide|unblock` | `claude/hooks/` |

The CLI never calls the network except: local env health checks, local API checks against the run's env, and `git` on local repos. dev.mcp is the agent's tool; when a gate needs remote facts (MR state, pipeline, Figma payload), the agent calls the dev.mcp read tool with `save_to=<absolute path>` (the tool writes the raw response; the model never relays it) and the CLI validates that file. A failing gate or command prints the exact call (`FETCH <tool> k=v ...`, `fetch: [{tool, args}]` with `--json`).

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
  env/<ws>/<repo>.pid|.log           # processes started by `factory env up`; .pid = {pid, cwd, head, started, create_time}
  secrets/<ws>.env                   # test credentials (never in chat, never in repos)
```

Run id: `<ws>-<ticket-key-lowercase>` (epic: the epic key), e.g. `acme-dev-9454`.

### 2.1 workspace.json (schemaVersion 1)

```json
{
  "schemaVersion": 1,
  "name": "acme",
  "worktree_root": "C:/Users/me/Code/wt",
  "repos": [
    {
      "alias": "React", "name": "webapp", "order": 2,
      "gitlab_project": "group/webapp",
      "path": "C:/Users/me/Code/webapp",
      "target_paths": {"homolog": "C:/Users/me/Code/webapp-homolog"},
      "quality": {
        "setup": "npm ci",
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
    "max_review_rounds": 2,
    "max_replans": 2
  },
  "limits": {"max_diff_ratio": 0.03, "max_tolerance_px": 2},
  "rules": [
    {"id": "i18n", "text": "User-facing strings go through the i18n layer", "check": {"type": "test", "repo": "React", "cmd": "npm run lint:i18n"}}
  ],
  "epics": [{"key": "DEV-8943", "roadmap": "C:/…/roadmap.md", "ship_comment": "C:/…/jira-ship-comment.md", "jira_transition": "Code Review"}]
}
```

`autonomy.ship`: `"ask"` (Gate S in chat for every remote write) or `"draft-auto"` (push + create/update **draft** MRs without asking; un-draft, Jira comment and transition still ask). `factory workspace import --from <repo>/.dev/project.json` converts a dev.harness profile; missing `path`/`quality`/`env` are left for the user.

`rules[]` are repo conventions with an executable check; every rule check runs in `prove` for each touched repo.

`quality.setup` (optional) is not a quality command: it is the restore command every worktree gets before checks run, identically for the run worktree (`run branch`/`run mirror`/`--mr`), the baseline and REPRO temp worktrees, and both red-green worktrees. Without it, worktrees get a link to the main repo's `node_modules` (with it, no link: a restore through a junction would rewrite the main repo). A failing setup is an `error`, never a reproduction.

`limits` caps check thresholds (§2.3): a plan cannot loosen a visual or style check past them.

### 2.2 run.json (CLI-written only)

```json
{
  "schemaVersion": 1, "id": "acme-dev-9454", "workspace": "acme", "kind": "ticket|epic",
  "parent": "<epic run id, child runs only>", "children": ["<child run ids, epic runs only>"],
  "tickets": [{"key": "DEV-9454", "type": "feature|bug", "parent": "DEV-7853", "depends_on": ["DEV-9451"]}],
  "repos": {"React": {"branch": "feature/DEV-9454-x", "base": "pack", "worktree": "C:/…/wt/dev-9454-react", "head": "<sha>",
                      "branches": {"homolog": "feature/DEV-9454-x-homolog"}, "base_branch": "<stacked epic child branch>", "base_run": "<child id>"}},
  "targets": ["pack", "homolog"],
  "phase": "intake|plan|build|prove|review|ship|feedback|done|blocked",
  "risk": "normal|high",
  "loops": {"build": 0, "review": 0, "replan": 0},
  "plan_lock": {"at": "…", "seq": 12, "digest": "<sha256>", "files": {"acs.json": "<sha256>", "checks/…": "<sha256>"}},
  "proved": {"React": {"HEAD": "<sha>"}},
  "decisions": [{"at": "…", "phase": "plan", "text": "…", "by": "agent|user"}],
  "blockers": [{"at": "…", "kind": "env|access|question|dependency|conflict", "text": "…", "resolved": false}],
  "events": [{"at": "…", "kind": "phase|gate_fail|gate_pass|evidence|review|ship|human|loop|plan_lock|replan|rewind", "data": {}}],
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
    {"id": "CHK-3", "type": "visual", "repo": "React", "node": "3544:5710", "path": "/obras/1/tarefas", "selector": "[data-testid=drawer]", "viewport": [1440, 900], "max_diff_ratio": 0.02, "masks": [], "wait_for": "[data-testid=drawer]", "settle_ms": 300, "fit": false},
    {"id": "CHK-4", "type": "style", "repo": "React", "node": "3544:5710", "path": "/obras/1/tarefas", "map": "checks/style/drawer.json", "wait_for": "[data-testid=drawer]"},
    {"id": "CHK-5", "type": "api", "repo": "API", "method": "POST", "path": "/v2/campos", "body": {}, "expect": {"status": 201, "json": {"$.id": "exists"}}},
    {"id": "CHK-6", "type": "red-green", "repo": "API", "cmd": "dotnet test --filter X", "fix_commit": "<sha or HEAD>"},
    {"id": "CHK-7", "type": "human", "text": "Native screen picker cannot be automated", "reason": "…"}
  ]
}
```

Optional UI check keys: `wait_for` (selector awaited before capture/probe; visual and style), `settle_ms` (extra wait after load; visual), `fit` (visual: pad both images to the larger width and height, no scaling, before comparing instead of failing on a size mismatch; the padded area counts as mismatched pixels).

`expect_fail_pattern` (regex): the output a check shows when it fails for the right reason (the test name or the assertion text). Required on every `test` check of a `kind: bug` AC and on every `red-green` check (on the check itself, or on its `of` test check). A non-zero exit whose output lacks the pattern is status `error` ("could not reproduce: setup failure"), never `fail`/`reproduced`. Optional on `browser`/`api` checks (matched against the failed test errors / response mismatch).

Plan-gate rules (`factory gate <run> plan`):

- Every AC has ≥1 check; every check is referenced by ≥1 AC; ids unique.
- `kind: ui` ACs need ≥1 `visual` **and** ≥1 `style` check; every `node` has a pack entry (`figma/<node>.json` + `.png` decodable) and `figma/meta.json` names its file revision.
- `kind: bug` ACs need a `red-green` check, **or** a runtime check (`browser`/`api`/`visual`/`style`) that has REPRO evidence `reproduced` at the merge-base and passes at HEAD (enforced at `gate prove`; §8).
- `browser`/`visual`/`style`/`api` checks need the repo's `env` block and `factory env check` capability (start command + health); missing env → gate fails with one consolidated list of what the user must provide.
- `human` checks are allowed but counted; the ship briefing lists them; they never satisfy a `ui` or `bug` AC on their own.
- Style maps: every expected value must match a value of the **same property family** in the node's pack JSON, so expectations come from Figma, not from the model: `fontSize` ↔ `fontSize` (also `style.fontSize`); `lineHeight` ↔ `lineHeightPx`/`lineHeight`; `fontWeight`, `letterSpacing`, `opacity`, `fontFamily` ↔ the same key; `borderWidth` ↔ `strokeWeight`; `text` ↔ `characters`; sizes (`width`, `height`, `padding*`, `gap`, `borderRadius`) ↔ `absoluteBoundingBox.width/height`, `width`, `height`, `padding*`, `itemSpacing`, `cornerRadius`; colors (`color`, `backgroundColor`, `borderColor`) ↔ paints under `fills`/`strokes`/`background`. A key with no family is rejected.
- Threshold ceilings (workspace `limits`, defaults `{"max_diff_ratio": 0.03, "max_tolerance_px": 2}`): a visual `max_diff_ratio` or a style `tolerance_px` (map entry or check) above them is rejected.
- Bug ACs: every `test` check needs `expect_fail_pattern`; every `red-green` check needs one (own or of its `of` check).
- On pass the gate writes the **plan lock** (§2.6).

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

Collected with a dirty worktree → `status` may be pass but the prove gate rejects it. Dirty means `git status --porcelain --untracked-files=normal`: untracked files count (`.gitignore` is respected; the `node_modules` link `run branch` creates does not count).

Runtime checks (`browser`, `visual`, `style`, `api`) are bound to their env: `prove` writes status `error` unless the repo's env was started by `factory env up` from the check's worktree (`env.cwd` under it) at the current HEAD (REPRO: at the merge-base) and that process is still the one recorded (pid + create time).

Evidence ids: one record per check id; the quality-delta record of a repo is `QUALITY-<alias>` (type `quality`), and each workspace rule check is `RULE-<rule-id>-<alias>` for every touched repo it applies to. The prove gate requires all of them to be `pass`.

### 2.5 Review record (via `factory review record`)

```json
{"schemaVersion": 1, "kind": "code|qa", "round": 1, "verdict": "APPROVE|CHANGES", "reviewer": "factory-auditor", "repos": {"React": "<sha>"},
 "findings": [{"id": "F1", "severity": "blocker|major|minor", "file": "src/x.tsx:42", "text": "…", "ac": "AC-1"}],
 "completeness": {"done": 0, "gap": 0, "not_checked": 0}}
```

QA review must state, for every AC, whether its checks actually exercise the AC (a check that cannot fail is a finding). The record carries `"ac_coverage": [{"ac": "AC-1", "exercises": true, "note": "…"}]` with one entry per AC in `acs.json`; `review record --kind qa` rejects a missing entry, and `exercises: false` cannot be combined with `APPROVE`.

`review record` runs only with `FACTORY_ROLE=auditor` in its environment (the factory-auditor agent sets it in the command; `worker-guard.js` denies any mention of `FACTORY_ROLE` to scout, author and builder; `protect.js` does not check it, because settings hooks also run inside subagents and would block the auditor; the orchestrator never records reviews, §0). The stored review gets `plan_lock` (the plan-lock digest it was recorded against) and, when the host exports one, `recorded_by` (`CLAUDE_AGENT_ID`, `CLAUDE_SESSION_ID`, ...; best effort, omitted otherwise). `completeness` (integers `done`, `gap`, `not_checked`) is required; `APPROVE` is invalid with `gap > 0`, `not_checked > 0`, or any `blocker`/`major` finding. A `resolved` flag on a finding is ignored by `record`, the review gate and the review report: a finding is resolved only by a later round that no longer lists it. `human` checks are not the auditor's `not_checked`: they are listed in the ship briefing. The CLI adds `recorded` (ISO time) to every stored review; `metrics` uses it to count findings that appear after the prove gate (escaped). The `review` event in run.json is `{review_kind, round, verdict, findings, digest, plan_lock[, recorded_by]}`; `gate_pass`/`gate_fail` events carry `phase`.

### 2.6 Proof integrity

- **Plan lock.** When `gate plan` passes, run.json gets `plan_lock = {at, seq, digest, files: {<path>: sha256}, workspace}` over `acs.json`, every file under `checks/` and `figma/`, and `diagnosis.md` when present; `workspace` is the sha256 of `workspace.json` as canonical JSON (key order and whitespace ignored; no volatile keys today). Any workspace change after the plan (caps, limits, quality commands) fails every later gate, before any loop/cap logic runs, with "workspace changed after plan; replan". `gate reproduce` refuses changes except `diagnosis.md` (written in reproduce) and then re-locks with it. `gate prove`, `gate review`, `ship plan`, `ship verify` and `gate ship` re-hash and fail listing the changed files. The one legit way to change them: `factory run replan <run> "<reason>" [--decision <n>]` (phase → plan, lock cleared, `replan` event, `loops.replan += 1`); the plan gate must pass again (re-running every acs.json validation), evidence written before the new lock (`seq` = event index) no longer counts, and reviews recorded against the old lock no longer count.
- **Replan/rewind authority.** `--decision <n>` names a decision recorded with `run decide … --by user --user-said "<gist>"` in the current phase; it is consumed (`used`) and the replan/rewind does not count against a cap (`loops.replan_approved += 1` for replans). Without it, a replan counts against `autonomy.max_replans` (default 2) and a rewind counts into `loops.build`; past `max_replans` / `max_build_loops` the command refuses, the run is blocked (question blocker, resumes in the phase it was in) and the user decides.
- **Proof at HEAD.** The review gate, `ship plan`, `ship verify` and `gate ship` re-run the prove-gate criteria (evidence pass, not dirty, `git_sha` == current HEAD, quality/rules, plan lock) without collecting anything: a commit after prove fails them. `gate prove` records `proved = {alias: {target|"HEAD": sha}}`. `ship plan` records each MR's `head_sha` (tip of its source branch); `ship verify` requires the branch still there and the readback `sha` (or `diff_refs.head_sha`) equal to it. After a rebase/merge or late fix: `factory run rewind <run> build|prove "<reason>" [--decision <n>]` (`rewind` event; `loops.build += 1` without a user decision). `ship verify` and `gate ship` also re-check that the latest `code` and `qa` reviews are `APPROVE`, digest-intact, bound to the current SHAs and the current plan lock.
- **Reproduction is an assertion.** A failing side (repro on base, red side of red-green) is `error`, never `reproduced`/red, when the exit code is 126, 127 or 9009, when the output shows a setup marker (`command not found`, `is not recognized as an internal or external command`, `ENOENT`, `No such file or directory`, `Cannot find module`, `could not find a part of the path`, `MSBUILD : error MSB1009`, `ModuleNotFoundError`; on Windows also cmd.exe's localized `'<cmd>' …` line), or when `expect_fail_pattern` is not in the output. The plan gate (and repro) reject an `expect_fail_pattern` shorter than 6 characters or with fewer than 4 literal word characters (`.`, `.*`, `\w+`, `[a-z]{6}`).
- **Env identity.** `env down`, `env up` (stale restart) and evidence binding act on a recorded pid only when its stored create time equals the live one. When either cannot be read (legacy record, OS refusal), the CLI neither kills nor binds: it reports and asks the user.
- **Digests.** Every evidence file `prove` writes gets an `evidence` event `{check, status, sha, digest}` (sha256 of the file); every review file `review record` (and `feedback ingest`) writes gets a `review` event with `digest`. Gates and the review report refuse an evidence/review file whose bytes do not match its latest event, or that has no event (hand-written even when `protect.js` is bypassed).

## 3. CLI contract

`python -m factory <command> …` (installed shim: `factory`). Exit 0 = OK, 1 = gate/check failed, 2 = usage/config error. Every command prints a short human summary; `--json` prints machine output.

| Command | Owner module | Effect |
|---|---|---|
| `workspace init <ws>` / `workspace import <ws> --from <project.json>` / `workspace check <ws>` | `commands/workspace.py` | create/convert/validate workspace.json |
| `run start <ws> <TICKET...> [--epic KEY] [--risk high] [--bug KEY]... [--login-user EMAIL]` | `commands/run.py` | create run dir + run.json (phase intake) |
| `run status <run>` / `run list` | `commands/run.py` | phase, loops, blockers, next required action |
| `run branch <run> <repo> --base <target> [--name …] [--from-run <child-id>]` | `commands/run.py` | create worktree + branch under worktree_root, record in run.json; `--from-run` stacks on that run's branch of the same repo (records `base_branch`, `base_run`) |
| `run start … --hotfix` / `run mirror <run> <repo> --to <target>` | `commands/run.py` | hotfix run and cherry-pick mirror worktree (§9) |
| `run branch <run> <repo> --base <ref> --for-target <target> [--name …]` | `commands/run.py` | for a repo already branched: create a second branch (default `<branch>-<target>`) from `<ref>` and record it in `repos[alias].branches[target]`; `ship plan` uses it as that target's MR source |
| `run decide <run> "<text>" [--by user --user-said "<gist>"]` / `run block <run> <kind> "<text>"` / `run unblock <run> <n>` | `commands/run.py` | append decision/blocker; `--by user` only for a decision the user stated in chat, and it requires `--user-said` (the gist of that message) |
| `run login-user <run> EMAIL` | `commands/run.py` | set run.json `login_user` (event `login_user`); refused once `gate prove` has passed |
| `run human <run> "<note>"` | `commands/run.py` | record a human intervention (orchestrator calls it for every user message that steers the run) |
| `run replan <run> "<reason>" [--decision <n>]` | `commands/run.py` | back to plan to change acs.json/checks/figma/diagnosis: clears the plan lock, `replan` event, `loops.replan += 1`; without a user decision counts against `autonomy.max_replans` (default 2) and blocks past it (§2.6) |
| `run rewind <run> build|prove "<reason>" [--decision <n>]` | `commands/run.py` | back to an earlier phase after the worktree moved past the proof (rebase/merge, late fix): `rewind` event; without a user decision `loops.build += 1` and blocks past `autonomy.max_build_loops` |
| `gate <run> <phase>` | `commands/gate.py` | check exit criteria of phase; on pass, advance phase; records gate_pass/gate_fail |
| `env up|down|check|logs <ws> <repo> [--run <run>]` | `commands/env.py` | start/stop processes from workspace env (worktree of the run when `--run`), health wait, port-owner diagnosis, pid tracking, tree-kill on Windows. `up` refuses when the env is already up for another cwd or HEAD (down first) and, without `ports`, when the health URL already answers before anything was started ("unknown server"). The pid record stores the process create time; `down` never kills a pid whose create time differs (reused pid) or cannot be read (reports and asks the user) |
| `env login <ws> <repo> [--user EMAIL]` | `commands/env.py` → browser | produce Playwright storage state from `secrets/<ws>.env`; credentials reach the Node CLI as `FACTORY_LOGIN_USER`/`FACTORY_LOGIN_PASS` env vars, never argv |
| `figma check <run>` / `figma expect <run> <node> [--selector …]` | `commands/figma.py` | pack completeness; extract expected style values from a node payload into a style-map skeleton |
| `baseline <run> [--repo R]` | `commands/prove.py` | run quality commands on merge-base once; store failures |
| `prove <run> [--check CHK-n] [--repo R]` | `commands/prove.py` | run collectors for checks + quality delta vs baseline + `rules[]`; write evidence |
| `review record <run> --kind code|qa --file <review.json>` | `commands/review.py` | validate and store review bound to current SHAs (only with `FACTORY_ROLE=auditor`; §2.5) |
| `ship plan <run>` | `commands/ship.py` | freshness (behind-count vs origin/<target>, needs prior `git fetch`), dependency MR states from `remote/`, then ship-plan.json: per repo×target branch, title, draft, description (ACs × evidence table, Depends on !N, human checks), Jira ADF comment, ordered remote actions |
| `ship verify <run>` | `commands/ship.py` | compare `remote/mr-<repo>-<target>.json` readbacks with the plan (draft, target, source, title, head sha == proven HEAD); plan lock and proof at HEAD (§2.6); record ship event |
| `feedback ingest <run>` | `commands/feedback.py` | read `remote/mr-*-discussions.json` and `remote/pipeline-*.json`; unresolved human threads and failed jobs become findings; moves phase back to build |
| `metrics <run>` / `metrics report [--since DATE]` | `commands/metrics.py` | from run.json + Claude transcripts (session ids in run.json, or transcripts mentioning `factory-run: <id>`): wall time, human messages, subagents by type/model, tokens, loops, gate failures, escaped findings (review/feedback findings after a prove pass) |

Source branch of an MR for target `T` (`ship plan`): `repos[alias].branches[T]` when recorded (`run branch --for-target`), else `branch` when `T` is the run's `base` for that repo (or the run ships to a single target), else `<branch>-<T>`.

Ship plan actions: per repo×target `push`, `mr`, `readback`; in `draft-auto` mode push and a draft `mr` need no confirmation, an `mr` that is not a draft does, and a `readback` carries the `needs_user` of its `mr` action. Jira comment and transition always ask.

Runtime resolution for runtime collectors: `base_url` comes from `env.base_url`, else the origin of `env.health` (`factory.commands.env.base_url`); the Playwright storage state is `factory.commands.env.storage_state_path(ws, alias[, user])` and is passed only when the file exists. Per-run login user: the workspace has one default test user (`USER`/`PASS` or `<ALIAS>_USER`/`<ALIAS>_PASS`); when a ticket names another e-mail for a scenario, `run.json login_user` is set (`run start --login-user` / `run login-user`). `env login --user EMAIL` writes `<alias>.<slug>.storage.json` (slug = e-mail lowercased, non-alphanumerics to `_`; the default user keeps `<alias>.storage.json`) and takes the password from `<ALIAS>_PASS_<slug>` / `PASS_<slug>` when present, else the default password; the password is never printed. The `browser`/`visual`/`style` collectors use the `login_user` state when set, and return an evidence `error` naming the exact `factory env login <ws> <repo> --user <email>` command when that file is missing. `prove` puts the resolved `base_url` into `ctx.env`.

Phase exit criteria (`gate`):

| Leaving | Requires |
|---|---|
| intake | ≥1 ticket; every ticket has `remote/issue-<KEY>.json`; open blockers of kind question are empty |
| plan | `acs.json` valid per §2.3; `figma check` OK; branches/worktrees created for every touched repo; env capability for every runtime check (bug runs then go to `reproduce`, not `build`; §8); writes the plan lock |
| reproduce | bug runs only (§8): every `kind: bug` AC has a `reproduced` REPRO evidence bound to the merge-base; `diagnosis.md` has non-empty `## Root cause`, `## Evidence`, `## Ruled out`; env capability for runtime repro checks; plan files unchanged except `diagnosis.md` (re-locked) |
| build | every touched repo worktree clean and ahead of base; commits match `commit.header_regex`; no forbidden trailers |
| prove | plan lock intact; every check has evidence `pass`, not dirty, `git_sha` == current HEAD of its repo, digest matching its event, written after the plan lock; quality delta vs baseline has no new failures; every rule check passes |
| review | the prove criteria still hold at the current HEADs (else rewind to prove); latest `code` and `qa` reviews `APPROVE`, digests matching, bound to current SHAs and the current plan lock, zero blocker/major findings (`resolved` ignored); else phase → build and `loops.build += 1`. Blocked (question blocker; `run unblock` resumes at **build**) when `loops.build > autonomy.max_build_loops` or the review round count reaches `autonomy.max_review_rounds` with CHANGES |
| ship | the prove criteria still hold; latest reviews still `APPROVE` and bound (as in review); `ship verify` OK |
| feedback | no open blocker; for every MR of `ship-plan.json`: `remote/mr-<repo>-<target>-discussions.json` with zero unresolved human threads, and `remote/pipeline-<repo>-<target>.json` = the latest pipeline, status `success`, `sha` == the MR head (or a user decision in phase feedback naming the pipeline by id: `run decide <run> "pipeline #<id>: <why>" --by user --user-said "<gist>"`; the id is the latest pipeline's, else the pipeline file's) |

## 4. Collectors

`factory/collectors/<type>.py` exposes `collect(check: dict, ctx: Ctx) -> dict` returning the evidence record body (`status`, `summary`, `artifacts`, `details`); `prove` fills common fields and writes the file through `core.write_evidence`.

| type | Implementation |
|---|---|
| `test` | run `cmd` in repo worktree (shell, timeout), exit code, tail of output |
| `red-green` | temp worktree at `fix_commit~1` with the test files from `fix_commit` → must fail; at `fix_commit` → must pass. `cmd`, or `of: <check-id>` to run another `test` check in those worktrees (§8) |
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

## 6. Epics

An epic is a **parent run** that coordinates **one child run per ticket**. Children have their own phase, gates, evidence, worktrees and loops; a blocked child never blocks children that do not depend on it.

- Parent id: `<ws>-<epic-key>`; child ids: `<ws>-<ticket-key>`. Parent `run.json` adds `"kind": "epic"` and `"children": ["<child-id>", …]`; child adds `"parent": "<parent-id>"`. Plain single-ticket runs have `"kind": "ticket"`.
- `epic.json` in the parent dir (author-written, CLI-validated): `{"schemaVersion": 1, "tickets": [{"key": "DEV-9451", "repos": ["API", "React"], "depends_on": ["DEV-9450"], "summary": "…", "area": "…"}]}`.

| Command | Effect |
|---|---|
| `epic start <ws> <EPIC> --tickets K1 K2 …` | parent run (phase intake) + child runs (phase intake), cross-linked |
| `epic plan <parent>` | validate epic.json (unknown keys, cycles, repos unknown to workspace, every child present), copy depends_on/summary/area into children, compute `waves` (topological levels) into parent run.json |
| `epic status <parent> [--json]` | matrix child × phase × loops × open blockers × MR readbacks; prints the next **ready** children (all deps in phase ship/done) |
| `epic ship plan <parent>` / `epic ship verify <parent>` | aggregate children's ship plans into one ordered action list (waves order, then repos order, then targets), Depends-on links across children, one epic Jira comment (workspace epic ship_comment when set); verify = every child ship verify OK |
| `gate <parent> <phase>` | parent phases: intake → plan → build (children running) → ship → done; leaving build requires every child in phase ship-ready (review passed) or done; leaving plan requires `epic plan` OK and each child's plan gate passed |

`epic plan` also writes `wave`, `planned_repos` and `stack_from` (`{repo: child-id}`, the dependency child to pass to `run branch --from-run`) into each child, and `waves`/`epic_plan` into the parent. A child is **ready** when it is not in ship/feedback/done/blocked and every `depends_on` child is in ship, feedback or done. `epic ship plan` refuses while any child is before ship; it builds each child's own plan (writing the child `ship-plan.json`, which `ship verify` reads), then writes the parent `ship-plan.json` with `mrs[]` (each with `child` and cross-child `depends_on`; descriptions gain `Depends on {mr_url:<child>:<repo>:<target>}`) and `actions[]` (push/mr/readback per MR, one epic Jira comment, child transitions). Parent gates: intake needs children; plan needs epic.json valid and applied and every child past its plan gate; build needs every child in ship/feedback/done; ship needs every child `ship verify` OK; then `done`.

Branches: a child that depends on another child in the same repo branches from that child's branch (stacked), recorded as `base_branch` in the child repo record; `run branch` accepts `--from-run <child-id>`.

## 7. Review runs (reviewing work that already exists)

For MRs the factory did not build: someone else's MR, the user's earlier work, tickets already in code review. Same machinery, different entry and exit; **read-only by default, nothing posted remotely unless the user approves**.

- `run.json` `"kind": "review"`; `"mrs": [{"repo": "React", "iid": 669, "source_branch": "…", "target_branch": "pack", "head_sha": "…", "ticket": "DEV-9431"}]`. Several MRs → a parent review run with one child per MR (reuse §6 parent/children; `"kind": "review-batch"`).
- Ticket ACs, not the diff, are the reference: the author writes `acs.json` from the ticket (and Figma), then checks; the diff is reviewed against them.

| Command | Effect |
|---|---|
| `review queue <ws> --mrs-json <file> [--me <username>]` | the agent saves `gitlab_list_merge_requests` responses (reviewer = me, state opened) to a file; CLI filters out drafts, MRs the user already approved (when approvals are present in the payload) and MRs authored by the user, maps each to its ticket key (title/branch/description regex from the workspace or `[A-Z]+-\d+`), prints the queue |
| `review start <ws> --mr <alias>!<iid> … [--ticket KEY]` | review run (or review-batch parent + children); records MR facts from `remote/mr-<alias>-<iid>.json` the agent saved |
| `run branch <run> <repo> --mr <iid>` | detached worktree at the MR head (`git fetch origin <source_branch>` done by the orchestrator first); builders are not used; `dirty` is checked as usual |
| `review report <run>` | verdict per MR: `ok` / `not-ok` / `incomplete`, with findings (severity, file:line, AC), ACs × evidence, `human` checks, freshness (behind-count vs target, unmerged deps), and what could not be verified; writes `review-report.json` + `.md` |
| `review fix <run>` | converts the run into `kind: ticket` on the MR source branch (worktree re-created as a normal branch worktree), keeps acs/evidence/findings, phase → build |

Implementation notes: `review start` takes the saved payloads with `--from <folder-or-file>` (the run folder does not exist yet; payloads are copied to `remote/mr-<alias>-<iid>.json`), `--mr alias!iid[=TICKET]`; run ids are `<ws>-<ticket>-review` and batch parents `<ws>-review-<stamp>`; the batch parent has phases intake → report → done (`gate report` needs every child done; `review report <parent>` aggregates). `gate <run> review` for a review run only needs both reviews recorded and bound to the MR head (a `CHANGES` verdict is a report result, not a loop to build). `review fix` converts in place with `git checkout` in the same worktree (no worktree removal) and refuses a dirty worktree or a local branch that is not at the MR head.

Phases for `kind: review`: intake → plan → prove → review → report → done. No build, no ship. `gate <run> report` requires `review report` written. Remote actions after report (MR comment with the findings, approval, Jira comment) are listed in the report with `needs_user: true` and are never pre-authorised by `autonomy`.

## 8. Bugs

A run is a bug run when a ticket is `type: bug` (`run start --bug KEY`). Phases: intake → plan → **reproduce** → build → prove → review → ship → feedback → done. The plan gate is unchanged except that it advances to `reproduce`; `gate build` in `reproduce` is refused (wrong phase). Feature runs have no `reproduce`.

- **Author** writes, in plan: reproduction checks (`test`, `api` or `browser` checks that FAIL on the unfixed code, referenced by the `kind: bug` AC), a `red-green` check for the fix (`cmd`, or `of: CHK-n` pointing at the reproduction `test` check), and `diagnosis.md` (run dir root) with `## Root cause`, `## Evidence`, `## Ruled out`.
- **`factory prove <run> --repro [--check CHK-n] [--assume-env]`** runs every non-`red-green`, non-`human` check referenced by a `kind: bug` AC against the base: the merge-base of the run branch with `origin/<base>` (falls back to the local base), in a temp detached worktree (the same helper as `baseline`, prepared with `quality.setup` like every worktree). It writes `evidence/REPRO-<check-id>.json` (CLI only; `protect.js` already denies `evidence/**`) with `status`: `reproduced` (check FAILED on base **on its assertion**: `test` output matches `expect_fail_pattern`; a `browser` spec has a failed test whose error is an assertion (`expect(`…) or matches the pattern; an `api` check got a response that mismatches), `not-reproduced` (it passed), `error` (could not run, or failed for another reason: "could not reproduce: setup failure"). `git_sha` and `merge_base` are the merge-base sha. Exit 0 only when all are `reproduced`.
- Checks must be runnable on the base without the fix (an existing test, or a script that does not need branch changes). `test` checks run in the temp worktree. `api`/`browser` checks need a running env, which serves the run worktree, not the temp one: they run only with `--assume-env`, and only if the run worktree is clean and still at the merge-base (before any fix commit), otherwise status `error`. `visual`/`style` cannot be reproduced.
- **Gate `reproduce`:** each bug AC has a check with REPRO status `reproduced` and `git_sha` equal to the current merge-base; `diagnosis.md` exists with the three sections non-empty (structure only; the code auditor judges the content); runtime repro checks need the repo env (the same consolidated env message as plan). No builder touches code before this gate passes.
- **Build:** the builder fixes the cause named in `diagnosis.md`. The code auditor must state whether the fix addresses the root cause and files a finding if it only masks the symptom.
- **UI bugs (no red-green):** a `kind: bug` AC may use a runtime check instead of `red-green` (`of` only supports `test`). The author writes a `browser` spec (or `api` check) that fails on the unfixed code. Order matters: the orchestrator brings the env up from the clean run worktree **before any build** (`factory env up --run`) and runs `factory prove <run> --repro --assume-env` (`reproduced`), then the builder fixes; `prove` later must pass the same check at HEAD. `gate prove` enforces for bug ACs without `red-green`: ≥1 runtime check with `REPRO-<id>` `reproduced` and bound to the merge-base, plus the normal passing evidence at HEAD. `visual`/`style` cannot be reproduced on base, so they cannot satisfy this alone.
- **`red-green`:** `fix_commit` (default `HEAD`) is the commit that contains the fix. The test may be in the same commit (its test files are found in that commit's diff) or in an earlier commit (then set `test_files`; the red worktree at `fix_commit~1` already has it). Both worktrees get the same preparation (`quality.setup` or the `node_modules` link). The red side must fail with `expect_fail_pattern` in its output, else `error`. `red-green` is optional for a bug AC that uses the repro + runtime path above.
- **`red-green` with `of`:** `of` references a `test` check of the same repo (validated at plan; `cmd` and `of` are exclusive). The collector runs the inner check in the `fix_commit~1` worktree (with the test files from `fix_commit`; must fail) and in the `fix_commit` worktree (must pass), through the `test` collector with `ctx.repo_path` set to the temp worktree. `browser`/`api` inner checks are not supported: the env would have to be started from the temp worktree, so plan rejects them. Use a `test` check for red-green (a `browser`/`api` check can still be the reproduction). Evidence details carry `of`.

## 9. Hotfix

Only when the user asks for a hotfix (homolog + pack in one go). One run, bug semantics (reproduce phase, §8), two targets, homolog first.

- **Workspace:** `ship.hotfix = {"base": "homolog", "mirror": ["pack"]}` (default when absent but a target named `homolog` exists; `workspace check` validates that base and mirrors are in `ship.targets` and the base is not a mirror). `ship.package` (optional, e.g. `"REL_9"`). Top-level `branch_format = {"feature": "…", "bug": "…", "hotfix": "…"}` with placeholders `{key} {slug} {target} {target_upper} {package}` (`slug` = summary slug, else repo alias slug; `target` = the `--base` / mirror target). `run branch` uses it when `--name` is absent (hotfix runs use `hotfix`, falling back to `bug`; no format → the legacy `bugfix|feature/KEY-alias`).
- **`run start <ws> KEY --hotfix [--feature]`:** tickets are bugs unless `--feature`; run.json `hotfix: {base, mirror}` and `targets = [base, *mirror]` in that order. Branch the base target normally: `run branch <run> <repo> --base homolog`.
- **`run mirror <run> <repo> --to <target> [--name …]`:** requires `gate build` passed and a clean base worktree. Creates a worktree `<run>-<repo>-<target>` from `origin/<target>` (local fallback) on the branch from `branch_format`, cherry-picks `merge-base..branch` of the base target in order, records `repos[alias].branches[target]` and `worktrees[target]`. On conflict the worktree stays mid-cherry-pick, a blocker of kind `conflict` (with `files`) is recorded and the command exits 1: a builder resolves, `git add`, `git cherry-pick --continue`, then `run unblock`. Never `--abort`/reset.
- **Evidence per target:** `prove` runs every check in each target worktree; ids are `CHK-n@<target>` and `REPRO-CHK-n@<target>` (repro on each target's merge-base; mirror merge-base = the target tip, so mirror repro is `test` checks only, runtime checks are reproduced on the base target). `gate reproduce` and `gate prove` require every target (a missing mirror worktree is a failure). Quality delta and rule checks stay on the base target worktree. Non-hotfix runs keep plain ids. Runtime checks on a mirror need the env started from the mirror worktree (not automated).
- **Ship plan:** MRs ordered base target first, then mirrors; no inter-MR `Depends on`; each description has a `Hotfix mirror` section: `Mirrored to {mr_url:<repo>:<mirror>}` on the base MR, `Mirror of {mr_url:<repo>:<base>}` on mirrors (the orchestrator replaces them with `!N` after creating the MRs); one Jira comment lists all MRs.

## 10. Non-goals (v0.1)

Queue pulling from Jira boards, autonomy levels beyond `ask`/`draft-auto`, CI auto-fix without the orchestrator, non-Claude hosts.

## 11. Sessions and env lease

Batches (epics, review batches) can run each ticket in its **own visible host session**: the user opens, reads and steers each one, instead of watching a single orchestrator wait on hidden workers.

- **Workspace:**
  - `orchestration = {"sessions": true, "per": "ticket"|"run"}` (default off; `per` defaults to `ticket`, one session covering every run of that ticket);
  - `env = {"lease": true, "lease_ttl_s": 900}` (default off).

  `workspace check` validates both blocks.
- **Parent session:**
  - does the batch intake and the single consolidated question round;
  - then, with `orchestration.sessions` and 2+ tickets, starts one session per ticket with the host's session tool, passing a brief with the run ids, worktrees, recorded user decisions and `/factory session <run-ids>`;
  - after that it only coordinates: `factory batch status <parent>` after each child report, the parent gate and report, and every dev.mcp write, only with the user's ok.

  Without a session tool (plain CLI) or when the host refuses, it falls back to worker subagents.
- **Child session:**
  - `factory run claim <run…>` records `{session, claimed_at, pid}` in `run.json.sessions` (the current owner is the last entry; the session id comes from `CLAUDE_CODE_SESSION_ID`, falling back to the other host hints);
  - commands that advance a run (`gate`, `prove`, `env up|login --run`) refuse when another live session owns it, unless `--takeover` (recorded as an event);
  - a process with no session id, such as a plain terminal, is not checked;
  - children never write the parent run or dev.mcp.
- **Run writes:**
  - `run.json` carries a `rev` counter;
  - `run_save` takes a per-run lock file (`run.json.lock`, `O_EXCL`), re-reads the disk copy and refuses with "changed on disk by another process" when `rev` moved since load, so a concurrent write fails loudly instead of being lost;
  - `run_update(run_id, fn)` (load, mutate, save under the lock) is used by the short mutators (`run human|block|unblock|decide|login-user|claim`);
  - `write_json` uses a unique temp name and retries `os.replace` on Windows sharing errors.
- **Env lease** (one env per workspace, because ports are fixed):
  - **State:** `env lease acquire|release|status <ws> --run <id> [--wait] [--timeout S]` keeps `FACTORY_HOME/env/<ws>/lease.json` (holder run, session, pid, acquired_at, heartbeat, FIFO queue) under an `O_EXCL` lock file. A lease is stale when its heartbeat is older than `lease_ttl_s` or its run is `done`/`blocked`; a stale lease can be reclaimed.
  - **Enforcement:** with `env.lease`, `env up --run` and `env login --run` require the lease, and the read-record → spawn → pid-write sequence of `up` runs under the lock. `env down` refuses when another run holds the lease unless `--force` is passed. `prove` and `env check` renew the heartbeat.
  - **Without `env.lease`:** behaviour is unchanged.
- **Visibility:**
  - `batch status <parent> [--json]` covers epic and review-batch parents. Per child it shows ticket, phase, next gate, open blockers, open `human` checks, owner session and the lease holder. `epic status` uses the same rows.
  - `run log <run> [--tail N] [--since ISO] [--json]` prints `run.json` events as a timeline.
  - `metrics` attributes child-session transcripts (marker `factory-run: <child>`) to the parent too.
- **Exploration is not proof.** In the UI phase a child may drive the host's built-in browser against the local env and the Figma frames, to find gaps and write Playwright specs with real selectors. Only collectors write evidence; nothing seen in that browser is a PASS.

