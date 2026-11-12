# Epic orchestration

An epic is a **parent run** (`<ws>-<epic-key>`, `kind: epic`) that coordinates **one child run per ticket** (`<ws>-<ticket-key>`). Each child has its own phases, gates, evidence, worktrees, and loops, and is driven with the normal per-ticket checklists (phases.md). `P` = parent run dir.

## Start and plan (parent)

1. `factory epic start <ws> <EPIC> --tickets K1 K2 …`. Children come from the epic's children in Jira (scout: `jira_search_jql` `parent = EPIC`; the raw response is saved by the author at intake). Print `factory-run: <parent-id>` once.
2. Intake for every child in parallel (author per child or one author for all raw saves): `remote/issue-<KEY>.json` in each child dir, plus the epic issue in the parent. `factory gate <child> intake` for each, then `factory gate <parent> intake`.
3. Author writes `P/epic.json`: `{"schemaVersion": 1, "tickets": [{"key", "repos", "depends_on", "summary", "area"}]}`. Dependencies come from Jira links, the roadmap, and code facts. `factory epic plan <parent>` validates it and computes `waves`.
4. Plan every child (acs.json, checks, Figma pack, `run branch`). A child that depends on another child **in the same repo** branches from it: `factory run branch <child> <repo> --base <target> --from-run <dep-child>` (stacked). One worktree per ticket.
5. **One consolidated question round for the whole epic:** env gaps, credential keys for `secrets/<ws>.env`, targets, and unknowns across every child, numbered with defaults. Record answers in each affected child (`run human`, `run unblock`, `run decide --by user`).
6. `factory gate <child> plan` for each child, then `factory gate <parent> plan`.

## Build in waves

- `factory epic status <parent>` lists the **ready** children: every dependency in phase ship or done. Run all ready children of the wave **in parallel**. Each child gets fresh workers (builder → prove → two fresh auditors). Keep one writer per file set: two children touching the same files in the same repo run one after the other, never concurrently.
- Each child goes build → prove → review with its own gates and loop caps, exactly as a single ticket does.
- **A blocked child does not stop independent ones.** Record its blocker (`factory run block <child> …`), keep the other ready children moving, and batch the questions of all blocked children into one message when no unblocked work is left or at a natural pause.
- After each child passes review, re-run `factory epic status <parent>`. Newly ready children form the next wave.
- `factory gate <parent> build` passes once every child has passed review or is done.

## Ship (parent)

1. `git fetch` in every child worktree.
2. `factory ship plan <child>` for each child (freshness per child), then `factory epic ship plan <parent>`. It aggregates one ordered action list (waves, then repo order, then targets), stacked `Depends on !N` across children, and one epic Jira comment (the workspace epic `ship_comment` when set).
3. Gate S once for the whole epic (see ship.md): every action with `needs_user`, listed per child.
4. Execute the actions in order; readbacks go to each child's `remote/mr-<repo>-<target>.json`. Fill `Depends on !N` from earlier readbacks.
5. `factory ship verify <child>` and `factory gate <child> ship` for each child; `factory epic ship verify <parent>`; `factory gate <parent> ship`.

## Feedback and finish

- Feedback runs per child (feedback.md). A child sent back to build does not reopen the others unless they depend on it **and** its fix changes their base branch. In that case, rebase the dependents through a builder and re-prove them.
- Progress to the user is the `factory epic status <parent>` matrix, condensed to a few lines (children per phase, blocked children and why, MRs shipped). Do not narrate per worker.
- Finish: `factory metrics <parent>` (and per child when asked), plus a short epic report.
