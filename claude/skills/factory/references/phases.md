# Phase checklists

`R` = `FACTORY_HOME/runs/<run-id>`. Every phase ends with `factory gate <run> <phase>`. On FAIL the printed list is the to-do list.

## intake

- [ ] Author saves raw dev.mcp responses, unedited: `jira_get_issue` → `R/remote/issue-<KEY>.json` for every ticket (and the epic); `jira_get_remote_issue_links` → `R/remote/links-<KEY>.json`; children of an epic via `jira_search_jql` → `R/remote/epic-<KEY>-children.json`.
- [ ] Figma links found in the issue: `figma_get_file_meta` → `R/figma/meta.json` (see figma-pack.md). Nodes are packed in plan.
- [ ] Author summarises in `R/notes/intake.md`: tickets, type (feature/bug), repos touched, targets, dependencies (other tickets/MRs), Figma nodes, unknowns.
- [ ] Record dependencies and choices: `factory run decide <run> "<text>"`.
- [ ] Questions only the user can answer: `factory run block <run> question "<text>"` (one per question). Ask them together with the plan-time questions when possible.
- [ ] `factory gate <run> intake`.

## plan

- [ ] `factory run branch <run> <repo> --base <target> [--name …]` for every touched repo (epic: per repo; the branch is shared by the epic's tickets unless the plan says otherwise).
- [ ] `factory env check <ws> <repo>` for every repo with runtime checks; `factory env up <ws> <repo> --run <run>` now, not at prove. Read failures with `factory env logs`. If login is needed: `factory env login <ws> <repo>` (reads `secrets/<ws>.env`).
- [ ] Author writes `R/acs.json` + `R/checks/**`: every AC ≥1 check that can fail; UI → `visual` + `style` (maps from `factory figma expect`); bug → `red-green`; api → `api`; `human` only with a reason.
- [ ] Figma pack complete: `factory figma check <run>`.
- [ ] `factory baseline <run>` (quality failures on merge-base, computed once).
- [ ] **Question round (one message):** env gaps from the gate/env check, credential keys to add to `secrets/<ws>.env` (names only), targets (which branches), product unknowns, and anything blocked at intake. Number the questions; give a default for each so the user can answer "defaults ok".
- [ ] After answers: `factory run human`, `factory run unblock`, `factory run decide … --by user`.
- [ ] `factory gate <run> plan`.

## build

- [ ] Builder per repo/batch with `acs.json`, check ids, commit convention, rules, worktree path.
- [ ] Bugs: builder writes the red test, commits it, then the fix (the `red-green` check's `fix_commit`).
- [ ] Worktrees clean and ahead of base; commit headers match the regex.
- [ ] `factory gate <run> build`.

## prove

- [ ] Envs up for runtime checks (`factory env check`).
- [ ] `factory prove <run>` (all checks, quality delta vs baseline, rules).
- [ ] FAIL → builder with the failing check ids and `R/evidence/<CHK>.json` paths. After a fix, re-run the full `factory prove <run>` (SHAs changed). Each round trip is a build loop; respect `autonomy.max_build_loops`.
- [ ] A wrong check (cannot pass, or tests the wrong thing) → author fixes the check, record `factory run decide` with why. Never loosen a check just to go green.
- [ ] `factory gate <run> prove`.

## review

- [ ] Two fresh auditors in parallel: `kind: code` and `kind: qa`, round n, repos with current SHAs, temp review file paths. High risk → `model: opus`.
- [ ] Each records via `factory review record <run> --kind … --file …` (exit code in its reply).
- [ ] `factory gate <run> review`. FAIL with CHANGES → phase goes back to build (loop counted): builder fixes findings → prove → new fresh auditors (round n+1).
- [ ] Small low-risk run: round 2 may be skipped when round 1 approves. High risk: never.

## ship

See ship.md. Ends with `factory ship verify <run>` and `factory gate <run> ship`.

## feedback

See feedback.md. `factory feedback ingest <run>`; findings → build loop; none → `factory gate <run> feedback` → done.

## finish

- [ ] `factory env down <ws> <repo>` for envs you started.
- [ ] `factory metrics <run>`; short report.
