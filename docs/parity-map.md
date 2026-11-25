# Parity map: every v0 capability inside v1

Owner decision (M0): the `1.0.0-alpha` loses no v0 capability. Each v0 capability below is redesigned in the v1 architecture (DESIGN.md): project-local config, the `next` protocol, append-only events, evidence bound to spec hash and SHA, and adapters instead of profiles.

A row is **dropped** only when the v1 design makes it unnecessary. The replacement is then named, and the lesson is `by-design`.

**Sources:** the v0 command tree (`factory/commands/*.py`, every subcommand), README "Pick a mode" and "Follow a run", DESIGN.md, and SKILL.md with its references.

**Columns:**

| Column | Meaning |
|---|---|
| Py | estimated Python lines added beyond the base modules of DESIGN §14 |
| Node | runner lines |
| M | milestone (section 3) |
| `=` in Py | the capability is in the base budget |

## 1. Inventory and redesign

### A. Project, install, providers

| # | v0 capability (commands) | v1 redesign | Lessons | Py | Node | M |
|---|---|---|---|---|---|---|
| 1 | `workspace init/check/list/show` | `factory init`, `factory check`, `factory status --config` on `factory.toml` (repo or workspace); secrets redacted | L-129, L-141 | = | | M1 |
| 2 | `workspace import` (legacy project file) | **dropped** (owner decision, after M7): `factory init --import` was removed; a new project is set up with `factory setup` (discovery and checklist), never by copying old config | — | 0 | | — |
| 3 | Provider profiles, `profile new/test/check/approve`, mapping DSL | **dropped**: user adapters plus `factory ingest` validation replace them | L-120, L-124, L-125 | 0 | | — |
| 4 | `migrate apply` (contracts after profile edits) | **dropped**: no profiles, so nothing to migrate; v0 runs do not migrate (transition note) | L-125 | 0 | | — |
| 5 | `install.py` (skill, agents, hooks, instructions block, shims) | installed as a Python entry point; `factory init --harness <id>` writes adapters and, with `--hooks`, merges optional hook entries into host settings idempotently, with a backup | L-130, L-149, L-160, L-161 | 80 | | M7 |
| 6 | Secrets per workspace, per-repo user/pass keys, ticket-specific user | `[secrets] keys`; env vars or the user secrets file; per-user login keys are `<KEY>_<slug>` (row 33) | L-80, L-81, L-129 | = | | M1 |
| 7 | Risk level (high → strongest reviewers, both review rounds) | `plan.json` `risk: low, medium or high`. High needs both review kinds from two recorded reviewer hints, and `next` tells the harness to use its strongest reviewer | real need (risk rules) | 30 | | M5 |
| 8 | Worker roles (scout, author, builder, auditor), worker reply and infra contracts | roles in adapter prose; infra repetition is mechanical in `next` | L-168, L-169, L-170 | = | | M7 |
| 9 | Guard hooks (write protect, tool guard) | **dropped**: evidence binding replaces write guards; optional hook in the adapter | L-109 to L-119 | 0 | | — |
| 10 | `--version` with build identity | same | L-150 | = | | M1 |

### B. Run lifecycle and steering

| # | v0 capability (commands) | v1 redesign | Lessons | Py | Node | M |
|---|---|---|---|---|---|---|
| 11 | `run start` (`--bug`, `--hotfix`), `run status`, `run list` | `run start <id> [--kind feature, bug, hotfix, epic or review]`; `status`; `factory runs` | L-31 | = | | M1 |
| 12 | `run log` (timeline) | `factory log [run] [--tail N]` renders `events.jsonl` | L-15 | 40 | | M1 |
| 13 | `run close`, `run delete` (zip archive) | `run close --reason`; `run delete <run> --yes` removes the run directory, its clean worktrees and its branches with no commit of their own, and names what it kept (no zip, no parent/child protocol) | L-103, L-105 | 30 | | M1 |
| 14 | `run retype` | the kind change re-runs `gate intake`, and nothing is archived | L-32 | = | | M1 |
| 15 | `run decide`, `block`, `unblock`, `human --type` | `factory answer`, `decide`, `block`, `unblock`; typed human events | L-126, L-140, L-152, L-177 | = | | M1 |
| 16 | `run replan`, `run amend-check` | `plan amend --reason [--decision]` | L-133, L-134 | = | | M2 |
| 17 | `run rewind build or prove` | **dropped**: no phase pointer. A new commit invalidates evidence and reviews by SHA, and `next` returns to verify | L-08, L-135 | 0 | | — |
| 18 | `run session`, `run claim` (`--takeover`) | `factory claim [run] --session <hint> [--takeover]` appends an owner event; `next` from another session refuses without `--takeover` | L-164, L-184 | 50 | | M6 |
| 19 | `wait --file/--cmd/--url` | `factory wait` with the same three probes, a 600 s cap, and redirects not followed | L-84, L-168 | 40 | | M4 |
| 20 | Single question round, `FETCH` lines | `ask`/`answer`; `next` prints absolute paths and adapter commands | L-126, L-158 | = | | M1 |
| 21 | Ticket media manifest (seen/unseen) | `factory media seen/unseen`; `gate intake` | L-28, L-29, L-30 | = | | M1 |
| 22 | `ingest` (+ `--check` for stale raws) | `factory fetch`, `factory ingest`; every gate re-checks raw hashes, and `ingest --check` lists stale ones | L-121, L-122 | = | | M1 |
| 23 | Design note `notes/design.md` for risk medium/high | `gate plan` requires `notes/design.md` with `## Approach`, `## Complexity`, `## Reuse`, `## Architecture risks` when `risk` is medium or high | L-176 | 30 | | M5 |
| 24 | Early UAT checkpoint (UI runs) | after the first `verify` pass of a UI run, `next` proposes a non-blocking UAT action showing `screenshot` and `visual` artifacts; answers are typed human events | L-175 | 30 | | M4 |
| 25 | Steering recorded during a run | `factory decide`/`human`; `next` lists unconsumed decisions | L-177 | = | | M1 |

### C. Checks and proof

| # | v0 capability (commands) | v1 redesign | Lessons | Py | Node | M |
|---|---|---|---|---|---|---|
| 26 | `test` checks (`exit0`, `expect_fail_pattern`) | `cmd` | L-02 to L-05 | = | | M2 |
| 27 | `api` checks (status, JSON path) | `http` (status, JSON subset) | L-82, L-83 | = | | M4 |
| 28 | `browser` spec checks | `browser`, on the bundled runner or the project's own | L-14, L-59 | = | | M4 |
| 29 | `visual`, `style`, UI state adapter actions (`goto`, `click`, `fill`, `wait`), `wait_for`, `settle_ms`, `fit`, masks | DESIGN §9. A `state_spec` or an `actions` list (`goto` same-origin, `click`, `fill`, `wait`), plus `wait_for`, `settle_ms`, `masks`, `fit`. `fit` is kept (owner decision 19, used by a real run): a capture whose size differs from the frame is scaled to the frame before the diff when the check declares it; without `fit` a size mismatch fails | L-51, L-54, L-55, L-186 to L-188 | = | = | M4 |
| 30 | `design add/check/expect`, approx frames, design-review decision | `factory design add/check/expect/review/refresh` | L-186, L-187 | = | | M4 |
| 31 | `human` checks, `run verify-human` | `human` check type. `factory confirm <check> --user-said "<words>"` records a `human` evidence record bound to the current SHAs; a new commit asks again. It never alone satisfies a `bug` or `design` AC, and open human checks are listed in the ship body. Pending human checks do not count as review loops | L-24, L-138 | 60 | | M5 |
| 32 | `red-green` (`cmd` or `of: CHK-n`) | `red_green` on a `cmd` check (the `of` form is the same check) | L-34, L-35 | = | | M2 |
| 33 | `env login`, `--user`, `run login-user` (ticket-specific user) | `[repo.env] login_spec`; plan `login_user = "<slug>"` selects secret keys `<KEY>_<slug>` and a state file per user. The runner `login` gets the base URL of the served instance | L-79, L-80, L-81 | 60 | 20 | M4 |
| 34 | Authenticated `api` checks | an `http` check with `session = "<user>"` sends the auth headers that `[repo.env] login_http` (a command printing headers JSON) produced; headers are kept in memory only | L-52, L-80 | 50 | | M4 |
| 35 | `screenshot` checks (visual evidence on request: BEFORE at base, AFTER at HEAD) | `screenshot` check type (artifact only, never satisfies an AC). `reproduce` captures before images on base and `verify` captures after images at HEAD. `gate plan` requires one per touched UI repo when the plan says `evidence.screenshots = true` (set from an answered question); the ship body lists them for upload | L-56, L-190 | 80 | 20 | M4 |
| 36 | Browser batching (one runner process per repo) | runner `batch --manifest` runs a repo's `browser`/`visual`/`style` checks in one browser, with per-entry timeout and result; `[checks.browser] batch = true` by default | L-57 | 60 | 80 | M4 |
| 37 | `quality` per repo plus `baseline` and quality delta | `quality` check type: `cmd` plus a `parse` mode (lines, a JSON path, or a failure-id regex). `factory baseline` runs it at `branch_point` up to 3 times and keeps only stable failure ids (flaky ones are quarantined). `verify` passes when there are no new ids. Ids are normalized (worktree paths stripped) | L-60, L-61, L-62 | 250 | | M5 |
| 38 | Scoped quality (`quality_fast`) | `quality` `scoped` template with `{changed_files}` from `branch_point..HEAD`; the full command still runs before `gate review` | L-60 | 40 | | M5 |
| 39 | Workspace `rules[]` (forbidden imports; guideline text) | rules are standing `cmd` checks; `kind = "guideline"` entries have no check, and their text goes into the `code` review input, which must cite each rule | L-63 | 20 | | M5 |
| 40 | `prove` (`--check`, `--repo`, `--all`, `--no-retry`, `--bail`, `--final`) | `verify` (`--check`, `--repo`, `--fresh`, `--no-retry`); bail is the fail-fast rule; every verify is final | L-10, L-11, L-46, L-47 | 10 | | M2 |
| 41 | `prove --fast` (weaker evidence mode) | **dropped**: reuse, risk ordering and fail-fast give the speed, and no evidence mode is weaker | L-13 | 0 | | — |
| 42 | `prove --estimate` | `verify --estimate`: per check `reuse` or `run` with the median duration from past events | L-189 | 50 | | M5 |
| 43 | `prove --repro`, `--assume-env` | `factory reproduce` on a throwaway base worktree; `--assume-env` is dropped because the env is always served from the base worktree | L-36, L-37 | = | | M2 |
| 44 | Evidence history, flaky detection | evidence events; `flaky` result | L-14, L-15, L-139 | = | | M2 |
| 45 | `ENV-FAIL` and `PROVE-FAIL` lines | exit codes plus `--json` `kind` | L-48 | = | | M2 |
| 46 | Integrity digests | **dropped**: evidence binding plus append-only events | L-16 | 0 | | — |
| 47 | Commit header regex, inherited-commit rules, merge edge cases | `commit_pattern` over `branch_point..HEAD`; merges that integrate the base skip the header, never the trailer check | L-89, L-90 | 40 | | M2 |

### D. Environment

| # | v0 capability (commands) | v1 redesign | Lessons | Py | Node | M |
|---|---|---|---|---|---|---|
| 48 | `env up`, `down`, `check` (`--auto-restart`), `logs` | `env up/down/health/logs`; stale means one automatic restart | L-66 to L-73, L-85 | = | | M4 |
| 49 | `env.hot_reload` | judged by the recorded start SHA | L-85 | = | | M4 |
| 50 | `files_from_main` | same, with link-safe copy | L-64, L-65 | = | | M4 |
| 51 | `env_only` repos (`run env-only`) | `depends_on`, never gated for commits | L-87 | = | | M4 |
| 52 | `env seed`, `reset-data` (`FACTORY_DATA_PREFIX`) | `[repo.env] seed`, `reset`; `factory env seed or reset [repo]`. `verify` resets once before the runtime batch. The prefix env var is derived from the run id | L-50 | 60 | | M4 |
| 53 | `env.preflight` (data freshness) | `[repo.env] preflight` is a list of commands run after health; failure is env kind `data` | L-50 | 30 | | M4 |
| 54 | `env.warmup` | `[repo.env] warmup` is a list of local paths requested once after `up`; failure is env kind `warmup` | L-40 | 30 | | M4 |
| 55 | Body classification (`kind=db`) | `[repo.env] env_patterns`: regexes over `http`/`browser` failure output that turn a failure into kind `env` | L-49 | 40 | | M4 |
| 56 | `env lease acquire/release/status` (FIFO, `--wait`, `shareable`) | per-env (per-slot) lock; `env up --wait <s>` queues for the lock; `env status` shows holders. `shareable = true` lets runs that only read share a running env | L-75, L-76, L-77 | 60 | | M6 |
| 57 | Slots and port stride | `env up --slot n` applies `local.toml` `[repo.<name>.slots] port_offset` (`{port}` and `{base_url}` derived); a lock and login state per slot; evidence records the slot | L-78, L-79 | 80 | | M6 |
| 58 | Port diagnosis (system-owned port) | when the port is taken by something the factory did not start, `env up` names the owning pid and process (including system pids) and never kills it | L-74 | 60 | | M4 |

### E. Review, ship and feedback

| # | v0 capability (commands) | v1 redesign | Lessons | Py | Node | M |
|---|---|---|---|---|---|---|
| 59 | `review record` (`code`/`qa`, `ac_coverage`) | same, with `exercises` and `changed_values` | L-22, L-23, L-39, L-107 | = | | M3 |
| 60 | Follow-ups from minors across rounds | same | L-146, L-147 | = | | M3 |
| 61 | `ship plan` (intents `push`, `change_request`, `set_draft`, `readback`, `pipeline_watch`, `issue_comment`, `issue_transition`; `--allow-behind`; `--ticket none, comment, transition, both`) | the same neutral intents; `--allow-behind --decision Q-n` | L-108, L-142 to L-145 | 30 | | M3 |
| 62 | `ship update-description` | `ship plan --update` re-plans the body for existing changes (an `update` intent) | L-146, L-153 | 20 | | M3 |
| 63 | `ship verify`, Gate S briefing | `ship verify`; `next` prints the Gate S briefing (changes, AC × evidence, human checks, approx frames, screenshots, ticket intents) | L-151, L-153, L-154 | = | | M3 |
| 64 | Feedback loop: `feedback ingest` (unresolved threads, failed jobs → findings → build), `gate feedback`, pipeline waiver | phase `feedback` after ship. `factory feedback` reads the refreshed `change` (with `threads`) and `pipeline` contracts. Unresolved threads and failed jobs become findings in a `review` event of kind `feedback`, so `next` returns to build ⇄ verify, then review, then ship (same changes, `update` intents). `gate feedback` needs zero unresolved threads and a green latest pipeline on the head, or a waiver naming the pipeline | L-19, L-20, L-155 | 200 | | M5 |

### F. Composite runs and orchestration

| # | v0 capability (commands) | v1 redesign | Lessons | Py | Node | M |
|---|---|---|---|---|---|---|
| 65 | Hotfix: `run mirror` (cherry-pick, conflict blocker), `--target` flags, `target_paths`, `run target-ticket` | base plus mirror (row 66). The run builds, verifies and reviews the base only. After `gate review`, `factory mirror <repo> --to <target>` creates `{kind}/{key}-{target}` worktrees and cherry-picks the fix commits. A conflict is a `tooling` blocker for the builder. Then `verify --mirror` runs every check flagged `mirror: true` (runtime included), served from the mirror worktree. A per-target ticket key (`--ticket`) titles each mirror change | L-41, L-93, L-98, L-99, L-100, L-102 | 200 | | M6 |
| 66 | `run mirror-diff` and the same-diff gate (`--allow-mirror-divergence`) | `factory mirror diff` compares per-file normalized patches (EOL, trailing whitespace, hunk relocation, import-specifier moves listed separately). `gate ship` needs equal, or a user decision naming the divergent files | L-95, L-96, L-97 | 150 | | M6 |
| 67 | Dependency without a run worktree served from `target_paths` | **dropped**: `depends_on` serves from the run worktree or the main path with a warning | L-71, L-101 | 0 | | — |
| 68 | Epics: `epic start/plan/status`, waves, stacked branches (`run branch --from-run`), epic ship plan/verify, one epic ticket comment | run kind `epic` with child runs (`.factory/runs/<epic>/children` lists them). `epic.json` gives `depends_on`, and `factory epic plan` computes waves and rejects cycles. A same-repo dependent branches from its dependency's head (`branch_point` = that head). `factory epic status` lists ready children. `epic ship plan/verify` aggregates ordered intents with `Depends on` links and one ticket comment | L-88, L-91, L-92, L-185 | 350 | | M6 |
| 69 | `batch status` (epic or review batch matrix) | `factory status <parent>` shows children by phase, blockers, open human checks, owner and lock holder | L-185 | 40 | | M6 |
| 70 | Review runs: `review queue`, `review start` (single or batch), `run branch --change`, `gate report`, `review report` (`ok`, `not-ok`, `incomplete`; local `remote_actions`), `review fix` | run kind `review`. `factory review queue` reads a `change_list` contract (drafts, own and already-approved changes dropped). `review start <change-id>...` creates one run per change (a parent for several) with a detached worktree at the change head. Phases: intake, plan, verify, review, report. `factory review report` writes verdicts and neutral `change_comment`/`change_approve`/`issue_comment` intents that are never auto-run. `review fix` turns the run into a ticket run on the change branch, keeping ACs and evidence | L-185 | 300 | | M6 |
| 71 | Parallel sessions (parent/child, `orchestration.sessions`, `max_parallel`) | `[orchestration] max_parallel`; `next <parent>` lists up to that many ready children; children are claimed (row 18). A parent answers for a child with `factory answer --run <child>`; appends are safe under concurrency | L-164, L-165, L-166, L-184 | 50 | | M6 |
| 72 | Exploration in the app's browser (findings and specs, never PASS) | adapter prose; findings enter as review findings or new checks through `plan amend` | L-01 | 0 | | M7 |

### G. Metrics

| # | v0 capability (commands) | v1 redesign | Lessons | Py | Node | M |
|---|---|---|---|---|---|---|
| 73 | `metrics <run>` (wall time, human messages, gate failures, loops, escaped findings, time per phase) | same, derived from events | L-177 to L-181 | = | | M2 |
| 74 | `metrics report --since` | `factory metrics --all [--since]` aggregates over the project's runs | L-179 | 40 | | M5 |
| 75 | Tokens from transcripts | optional `[metrics] tokens` adapter command (`{run}`, `{session}`) prints `{session, input, output}`. Stored as `tokens` events per session and run, never overwritten by empty values. The core never scans transcripts | L-182, L-183 | 60 | | M5 |
| 76 | Language and progress narration rules | adapter prose (one line each) | L-170 | 0 | | M7 |

## 2. Budgets with parity

| Part | Lines |
|---|---|
| Base core (DESIGN §14 without design) | 4,750 |
| Design pillar (`design`, `checks/visual`, `checks/style`, glue) | 700 |
| Parity rows above (sum of Py) | 2,970 |
| **Python core total** | **8,420** |
| Node runner: base (`login`, `visual`, `style`, `spec`) | 700 |
| Node runner: parity (batch 80, screenshot 20, per-user login 20) | 120 |
| **Node runner total** | **820** |

New modules, each 400 lines or fewer:

| Module | Lines |
|---|---|
| `quality` | 290 |
| `feedback` | 200 |
| `mirror` | 350 |
| `epic` | 390 |
| `review_run` | 300 |
| `envdata` (seed, reset, preflight, warm-up, env patterns, port diagnosis) | 220 |
| `envlock` (slots, wait, shareable) | 140 |
| `human` / `screenshot` | 140 |

The rest grows existing modules within their caps. Proposed caps are in `m0-decisions.md` decision 16.

## 3. Milestones (alpha = full parity)

| M | Content | Stop for William |
|---|---|---|
| M1 | Project, config, run, events, contracts, adapters, intake (media), `log`, `delete`, `close`, `init --import`, minimal `agents-md` adapter | |
| M2 | Plan, lock, amend, worktrees, `cmd` checks, `verify` (reuse, order, fail-fast), reproduce, red-green, commit pattern, metrics | |
| M3 | Review, ship, `ship verify`, update intents. **Criterion: a real ticket in a single repo reaches a draft change** | yes |
| M4 | Workspace, env (data, warm-up, port diagnosis, login users), `http` (sessions), `browser` (batching), design (`visual`, `style`, approx), screenshots, UAT, `wait` (delivered; decisions in `docs/m4-notes.md`) | |
| M5 | Feedback loop, human checks, quality baseline, delta and scoped, rules, risk and design note, estimates, metrics report and tokens | |
| M6 | Composite runs: hotfix and mirror with the same-diff gate, epics, review runs, claim, parallel children, env slots and shareable (delivered; decisions in `docs/m6-notes.md`) | yes (parity review) |
| M7 | Harness adapters with optional hooks, installation, transition note, `1.0.0-alpha` = full parity (delivered; decisions in `docs/m7-notes.md`) | yes (independent review, then the owner tags) |

**Rationale for the order:**

- Value arrives at M3 with the smallest closed loop, as in the pivot.
- M4 comes next because most real tickets touch UI.
- M5 closes the loop after the change request exists.
- M6 composes runs that are already stable.
- Full adapters come last, so they describe a frozen CLI. A minimal adapter ships in M1 so M3 can run on a real ticket.
