# Phase checklists

`R` = `FACTORY_HOME/runs/<run-id>`. Every phase ends with `factory gate <run> <phase>`. On FAIL the printed list is the to-do list; missing remote files come with ready `FETCH <tool> ... save_to=... overwrite=true` calls (`fetch[]` with `--json`). Run them as-is.

## intake

- [ ] Author calls the dev.mcp read tools with `save_to=<absolute path>` and `overwrite: true` (never Write, never paste); a failing `gate intake` prints the missing calls. Read saved files with targeted Grep/Read. `jira_get_issue` → `R/remote/issue-<KEY>.json` for every ticket (and the epic); `jira_get_remote_issue_links` → `R/remote/links-<KEY>.json`; children of an epic via `jira_search_jql` → `R/remote/epic-<KEY>-children.json`.
- [ ] Figma links found in the issue: `figma_get_file_meta` with `save_to` → `R/figma/meta.json` (see figma-pack.md). Nodes are packed in plan.
- [ ] Author summarises in `R/notes/intake.md`: tickets, type (feature/bug), repos touched, targets, dependencies (other tickets/MRs), Figma nodes, unknowns.
- [ ] Record dependencies and choices: `factory run decide <run> "<text>"`.
- [ ] Questions only the user can answer: `factory run block <run> question "<text>"` (one per question). Ask them together with the plan-time questions when possible.
  - Blocker kinds (`factory run block <run> <kind> "<text>"`): `env` the run's own env does not work; `access` a credential or permission is missing; `question` only the user can answer; `dependency` another ticket, run or MR must land first; `conflict` a merge or cherry-pick conflict; `decision` a product or scope call for the user; `infra` a service, machine or network outside the run is down; `tooling` the factory or a tool lacks a capability the run needs.
- [ ] `factory gate <run> intake`.

## plan

- [ ] `factory run branch <run> <repo> --base <target> [--name …]` for every touched repo (epic: per repo; the branch is shared by the epic's tickets unless the plan says otherwise). A repo the env must serve from a run worktree but that gets no code (e.g. API/Legado on homolog for a React-only fix): add `--env-only`, or mark an already registered one with `factory run env-only <run> <repo>` (`--off` undoes; no replan). Build, prove, review, mirror and ship skip env_only repos; its worktree may stay dirty (local config).
- [ ] `factory env check <ws> <repo> --run <run>` (preflight; exit 0 healthy, 1 unhealthy, 3 stale, 4 port/lease, 2 usage; `--auto-restart` for a stale env you started) for every repo with runtime checks; `factory env up <ws> <repo> --run <run>` now, not at prove. Read failures with `factory env logs`. If login is needed: `factory env login <ws> <repo>` (reads `secrets/<ws>.env`). If the ticket names a user e-mail for the scenario: `factory run login-user <run> <email>` and `factory env login <ws> <repo> --user <email>`; else the workspace default user.
- [ ] Author writes `R/acs.json` + `R/checks/**`: every AC ≥1 check that can fail; UI → `visual` + `style` (maps from `factory figma expect`); bug → `red-green`; api → `api`; `human` only with a reason.
- [ ] **Live dry-run of each new `browser`/`api` check** (when the env is up): the author runs `factory prove <run> --check CHK-n` against it before `gate plan`, so bad selectors, seeds, 307s and timeouts surface at plan, not at prove (2-7 replans per run came from this). A dry-run failing with `kind=assert|setup` is fixed in the check; `kind=env|timeout` is an infra signal (blocked contract). The evidence is a dry-run record, not the final proof: prove runs everything again after build. Env not up: say so in the question round; do not skip silently.
- [ ] Risk medium/high or >~5 product files expected: author writes `R/notes/design.md` (≤40 lines: `## Approach`, `## Complexity`, `## Reuse`, `## Architecture risks`) from the code, not from the ticket. The builder follows it and flags deviations.
- [ ] Figma pack complete: `factory figma check <run>`.
- [ ] `factory baseline <run>` (quality failures on merge-base, computed once).
- [ ] **Question round (one message):** env gaps from the gate/env check, credential keys to add to `secrets/<ws>.env` (names only), targets (which branches), product unknowns, and anything blocked at intake. When the run touches UI (the plan gate says so): the visual evidence question (bug: before/after screenshots of the fix; feature: screenshots of the implemented screens; default yes). Number the questions; give a default for each so the user can answer "defaults ok".
- [ ] After answers: `factory run human`, `factory run unblock`, `factory run decide … --by user --user-said "<gist>"` (only what the user stated in chat). Visual evidence answer: add `--visual yes|no`.
- [ ] Visual yes: author adds one `screenshot` check per touched UI repo (`path` + `wait_for`, or `spec` = Playwright steps that end on the changed state; `title` names the screen), referenced by the AC it illustrates.
- [ ] `factory gate <run> plan`. It locks `acs.json`, `checks/**`, `figma/**` (and `diagnosis.md` if present): later gates, `ship plan` and `ship verify` refuse any change. `workspace.json` is locked too. To change them later: `factory run replan <run> "<reason>" [--decision <n>]` (without a user decision it counts against `autonomy.max_replans`), edit, pass `gate plan` again, then prove and review again.

## reproduce (bug runs only)

- [ ] Repro checks were written in plan (test/api/browser, runnable on the base; `test` checks with `expect_fail_pattern` = the failing test name or assertion), plus a `red-green` check (`cmd` or `of: CHK-n` of a test check; optional for UI bugs on the runtime path). Author writes `R/diagnosis.md` now with non-empty `## Root cause`, `## Evidence`, `## Ruled out` (the only plan file that may change in reproduce).
- [ ] `factory prove <run> --repro` (api/browser/screenshot: `factory env up <ws> <repo> --run <run>` on the unmodified worktree, add `--assume-env`). Every check must print REPRODUCED; `BEFORE-<check>` screenshots print PASS (the unfixed screen, kept for the MR). NOT-REPRODUCED or ERROR ("could not reproduce: setup failure" = it failed for another reason) means the check, its setup (`quality.setup`) or the diagnosis is wrong: `factory run replan`, author fixes the check, `gate plan`, repro again. No builder.
- [ ] `factory gate <run> reproduce` (also lists the consolidated env gaps for runtime repro checks).

## build

- [ ] Builder per repo/batch with `acs.json`, check ids, commit convention, rules, worktree path.
- [ ] Bugs: builder reads `R/diagnosis.md` and fixes the cause it names. `fix_commit` of the `red-green` check is the commit that contains the fix (default `HEAD`: the fix is the last commit). The test goes in that same commit, or in an earlier commit with `test_files` set on the check; the red side runs at `fix_commit~1` with those test files and must fail on `expect_fail_pattern`.
- [ ] Worktrees clean and ahead of base; commit headers match the regex.
- [ ] `factory gate <run> build`.
- [ ] **Early UAT checkpoint (UI runs, first build gate only):** run just the `screenshot` checks (`factory prove <run> --check CHK-n` each), show the screenshots with the list of decisions assumed so far (from `factory run log` / `run.json` decisions, never from memory), and ask the user to confirm before the full prove and review. No answer does not block autonomous mode. Record answers with `factory run decide <run> "<text>" --by user --user-said "<gist>"`. A product change from the answer is a replan or fix before the review loop, not after it.

## prove

- [ ] Envs up for runtime checks from the run worktree at the current HEAD: `factory env up <ws> <repo> --run <run>` (after new commits: `env down`, then `env up` again). Evidence from an env started elsewhere or at another HEAD is `error`.
- [ ] `factory prove <run>` (all checks, quality delta vs baseline, rules). Build/fix loops: `factory prove <run> --fast` (bails on the first failure, reuses passing evidence at the same sha/digest). Once before `gate prove`: `factory prove <run> --final` (everything, no reuse). Runtime evidence is reused only if every repo of the run is unchanged (`run_heads`). A browser assert that passes on an immediate retry is recorded as a fail `kind=flaky` (never a pass; fix the check; `--no-retry` disables). Repos with `env.seed.reset` get their test data reset once before the runtime batch (`ENV-FAIL kind=seed` = exit 3); `factory env seed|reset-data <ws> <repo> --run <run>` run it by hand, and `env check` runs `env.preflight` (`kind=preflight`). `prove` and `run status` list a `flaky` check (failed earlier at the same sha and digest, passes now; history in `evidence/history/`; no gate effect), and a body with "SSL Authentication Error" is `ENV-FAIL kind=db`. A repo with `env.files_from_main` gets those gitignored files (`.env`) copied into new worktrees; `ENV-FAIL kind=files` means one is still missing in the served path. `quality_fast` (with `{changed}`) scopes quality in `--fast` only.
- [ ] "How long will this take?" (prove, a loop, the whole run): answer from `factory prove <run> --estimate` (no execution; per check REUSED or RUN with expected seconds, plus the `--fast` and `--final` totals; add `--json` before `prove` for a machine-readable form), never from a guess. Browser checks of one repo run in one Playwright process (`details.batched`; workspace `limits.browser_batch: false` turns it off).
- [ ] FAIL → builder with the failing check ids and `R/evidence/<CHK>.json` paths. After a fix, re-run the full `factory prove <run>` (SHAs changed). Each round trip is a build loop; respect `autonomy.max_build_loops`.
- [ ] A check that fails only on timing or a selector (the AC and assertion are right): `factory run amend-check <run> CHK-n --kind timing|selector --reason "<why>"` instead of a full replan. It is refused (replan instead) when the spec is also read by another check or `workspace.json` changed since the plan lock.
- [ ] A wrong check (cannot pass, or tests the wrong thing) → `factory run replan <run> "<why>"`, author fixes the check, `gate plan`, build/prove again. Never loosen a check just to go green (workspace `limits` cap visual/style thresholds).
- [ ] `human` checks the user confirms in chat: `factory run verify-human <run> <CHK-id> --note "<what the user confirmed>" --by user --user-said "<gist of the user's words>"` (you, only after the user confirmed it in chat; never a worker).
- [ ] `factory gate <run> prove`.

## review

- [ ] Two fresh auditors in parallel: `kind: code` and `kind: qa`, round n, repos with current SHAs, temp review file paths, `notes/design.md` when present, and workspace `rules[]` including `kind: guideline` ones. High risk → `model: opus`. Code reviews cover engineering quality (complexity, readability, architecture, reuse, test quality, design conformance); findings carry `category`. Unresolved minors go to the MR description under `## Follow-ups` at ship.
- [ ] Each records via `FACTORY_ROLE=auditor factory review record <run> --kind … --file …` (exit code in its reply). Only the auditor role can record; APPROVE needs zero blocker/major findings and completeness `gap` = `not_checked` = 0.
- [ ] `factory gate <run> review`. FAIL with CHANGES → phase goes back to build (loop counted): builder fixes findings → prove → new fresh auditors (round n+1). A finding is resolved only by a later round. Reaching `max_review_rounds` with CHANGES, or exceeding `max_build_loops`, blocks the run; after the user decides, `run unblock` resumes at build.
- [ ] FAIL because the proof is stale (a commit after prove): `factory run rewind <run> prove "<reason>"`, prove, then review again.
- [ ] Small low-risk run: round 2 may be skipped when round 1 approves. High risk: never.

## ship

See ship.md. Ends with `factory ship verify <run>` and `factory gate <run> ship`.

## feedback

See feedback.md. `factory feedback ingest <run>`; findings → build loop; none → `factory gate <run> feedback` → done.

## abandoned runs

`factory run close <run> --reason "<why>"` for a run that will not continue (ticket cancelled, superseded): it releases the lease and env and keeps the artifacts. A wrongly typed run: `factory run retype <run> --bug|--hotfix`.

## finish

- [ ] `factory env down <ws> <repo>` for envs you started.
- [ ] `factory metrics <run>`; short report.
