# Phase checklists

`R` = `FACTORY_HOME/runs/<run-id>`. Every phase ends with `factory gate <run> <phase>`. On FAIL the printed list is the to-do list; missing remote files come with ready `FETCH <tool> ... save_to=... overwrite=true` calls (`fetch[]` with `--json`). Run them as-is.

## intake

- [ ] Author calls the dev.mcp read tools with `save_to=<absolute path>` and `overwrite: true` (never Write, never paste); a failing `gate intake` prints the missing calls. Read saved files with targeted Grep/Read. `jira_get_issue` → `R/remote/issue-<KEY>.json` for every ticket (and the epic); `jira_get_remote_issue_links` → `R/remote/links-<KEY>.json`; children of an epic via `jira_search_jql` → `R/remote/epic-<KEY>-children.json`.
- [ ] Figma links found in the issue: `figma_get_file_meta` with `save_to` → `R/figma/meta.json` (see figma-pack.md). Nodes are packed in plan.
- [ ] Author summarises in `R/notes/intake.md`: tickets, type (feature/bug), repos touched, targets, dependencies (other tickets/MRs), Figma nodes, unknowns.
- [ ] Record dependencies and choices: `factory run decide <run> "<text>"`.
- [ ] Questions only the user can answer: `factory run block <run> question "<text>"` (one per question). Ask them together with the plan-time questions when possible.
- [ ] `factory gate <run> intake`.

## plan

- [ ] `factory run branch <run> <repo> --base <target> [--name …]` for every touched repo (epic: per repo; the branch is shared by the epic's tickets unless the plan says otherwise).
- [ ] `factory env check <ws> <repo>` for every repo with runtime checks; `factory env up <ws> <repo> --run <run>` now, not at prove. Read failures with `factory env logs`. If login is needed: `factory env login <ws> <repo>` (reads `secrets/<ws>.env`). If the ticket names a user e-mail for the scenario: `factory run login-user <run> <email>` and `factory env login <ws> <repo> --user <email>`; else the workspace default user.
- [ ] Author writes `R/acs.json` + `R/checks/**`: every AC ≥1 check that can fail; UI → `visual` + `style` (maps from `factory figma expect`); bug → `red-green`; api → `api`; `human` only with a reason.
- [ ] Figma pack complete: `factory figma check <run>`.
- [ ] `factory baseline <run>` (quality failures on merge-base, computed once).
- [ ] **Question round (one message):** env gaps from the gate/env check, credential keys to add to `secrets/<ws>.env` (names only), targets (which branches), product unknowns, and anything blocked at intake. Number the questions; give a default for each so the user can answer "defaults ok".
- [ ] After answers: `factory run human`, `factory run unblock`, `factory run decide … --by user --user-said "<gist>"` (only what the user stated in chat).
- [ ] `factory gate <run> plan`. It locks `acs.json`, `checks/**`, `figma/**` (and `diagnosis.md` if present): later gates, `ship plan` and `ship verify` refuse any change. `workspace.json` is locked too. To change them later: `factory run replan <run> "<reason>" [--decision <n>]` (without a user decision it counts against `autonomy.max_replans`), edit, pass `gate plan` again, then prove and review again.

## reproduce (bug runs only)

- [ ] Repro checks were written in plan (test/api/browser, runnable on the base; `test` checks with `expect_fail_pattern` = the failing test name or assertion), plus a `red-green` check (`cmd` or `of: CHK-n` of a test check; optional for UI bugs on the runtime path). Author writes `R/diagnosis.md` now with non-empty `## Root cause`, `## Evidence`, `## Ruled out` (the only plan file that may change in reproduce).
- [ ] `factory prove <run> --repro` (api/browser: `factory env up <ws> <repo> --run <run>` on the unmodified worktree, add `--assume-env`). Every check must print REPRODUCED. NOT-REPRODUCED or ERROR ("could not reproduce: setup failure" = it failed for another reason) means the check, its setup (`quality.setup`) or the diagnosis is wrong: `factory run replan`, author fixes the check, `gate plan`, repro again. No builder.
- [ ] `factory gate <run> reproduce` (also lists the consolidated env gaps for runtime repro checks).

## build

- [ ] Builder per repo/batch with `acs.json`, check ids, commit convention, rules, worktree path.
- [ ] Bugs: builder reads `R/diagnosis.md` and fixes the cause it names. `fix_commit` of the `red-green` check is the commit that contains the fix (default `HEAD`: the fix is the last commit). The test goes in that same commit, or in an earlier commit with `test_files` set on the check; the red side runs at `fix_commit~1` with those test files and must fail on `expect_fail_pattern`.
- [ ] Worktrees clean and ahead of base; commit headers match the regex.
- [ ] `factory gate <run> build`.

## prove

- [ ] Envs up for runtime checks from the run worktree at the current HEAD: `factory env up <ws> <repo> --run <run>` (after new commits: `env down`, then `env up` again). Evidence from an env started elsewhere or at another HEAD is `error`.
- [ ] `factory prove <run>` (all checks, quality delta vs baseline, rules).
- [ ] FAIL → builder with the failing check ids and `R/evidence/<CHK>.json` paths. After a fix, re-run the full `factory prove <run>` (SHAs changed). Each round trip is a build loop; respect `autonomy.max_build_loops`.
- [ ] A wrong check (cannot pass, or tests the wrong thing) → `factory run replan <run> "<why>"`, author fixes the check, `gate plan`, build/prove again. Never loosen a check just to go green (workspace `limits` cap visual/style thresholds).
- [ ] `factory gate <run> prove`.

## review

- [ ] Two fresh auditors in parallel: `kind: code` and `kind: qa`, round n, repos with current SHAs, temp review file paths. High risk → `model: opus`.
- [ ] Each records via `FACTORY_ROLE=auditor factory review record <run> --kind … --file …` (exit code in its reply). Only the auditor role can record; APPROVE needs zero blocker/major findings and completeness `gap` = `not_checked` = 0.
- [ ] `factory gate <run> review`. FAIL with CHANGES → phase goes back to build (loop counted): builder fixes findings → prove → new fresh auditors (round n+1). A finding is resolved only by a later round. Reaching `max_review_rounds` with CHANGES, or exceeding `max_build_loops`, blocks the run; after the user decides, `run unblock` resumes at build.
- [ ] FAIL because the proof is stale (a commit after prove): `factory run rewind <run> prove "<reason>"`, prove, then review again.
- [ ] Small low-risk run: round 2 may be skipped when round 1 approves. High risk: never.

## ship

See ship.md. Ends with `factory ship verify <run>` and `factory gate <run> ship`.

## feedback

See feedback.md. `factory feedback ingest <run>`; findings → build loop; none → `factory gate <run> feedback` → done.

## finish

- [ ] `factory env down <ws> <repo>` for envs you started.
- [ ] `factory metrics <run>`; short report.
