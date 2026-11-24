# M6 notes: what was decided where the docs left room

M6 delivers the composite runs: hotfix with mirror and the same-diff gate, epics, review runs, claim and parallel children, env slots and `shareable`, and the feedback additions (`--dismiss`, `[feedback] ignore_authors`). Parity rows 18, 56, 57, 65, 66, 68 to 71, and the lessons L-41, L-78, L-88, L-91 to L-93, L-95 to L-100, L-184 and L-185. L-161 (`init --harness --hooks`) stays in M7.

Composite runs compose: they are runs with their own events and use the same gates, `next`, `verify` and `ship`. A parent run (epic or review batch) holds an `epic_planned` event; each child is a normal run whose `run_started` names its `parent`.

Hotfix and mirror:

- **The base is the run.** A hotfix is a `bug` run (`--kind hotfix`; the ticket type stays `bug`). It builds, verifies and reviews the base only. `factory mirror <repo> --to <target>` needs the review gate, creates `{kind}/{key}-{target}` (`--ticket` gives that target its own key, which titles its change), cherry-picks `branch_point..HEAD` of the base worktree (no merges) and appends a `mirror` event. The mirror worktree is not a `worktree` event, so the run's own worktree never changes. A branch that exists already, or the base branch name, refuses before `git worktree add`.
- **Conflict.** The cherry-pick stays open in the mirror worktree, a `tooling` blocker names the files, and the exit is 1. After `git cherry-pick --continue` the same `factory mirror` call records `applied`. A failed pick without unmerged files (for example no git identity) is an error, not a conflict.
- **Targets.** `[[repo.mirror]]` of a repo plus the `[[hotfix.targets]]` of the project. Every configured target is required at `gate ship`. A hotfix without targets is a plain bug run.
- **`verify --mirror`.** `git.overlay` makes `git.current` answer with the mirror tree, so the existing `verify` and the env binding serve and check that tree. One batch per target, so a failure on one target never cancels the other. Evidence records `target`. Red-green is skipped. An env that another run or a hand start owns is never stopped (the lock failure is exit 3).
- **Same-diff gate.** `mirror diff` compares the base fix with the mirror fix per file. A line removed in one place and added in another cancels, and lines are compared without line ending and trailing whitespace, so relocated hunks, CRLF and trailing spaces are equal. A file whose only differences are import lines is class `imports`, every other difference is `content` (a file on one side only included). `mirror diff <repo> --to <t> --decision D-n` records the user's decision; it has to name every divergent file and covers those two heads only.
- **Ship.** One `push` and one `mirror_change_request` intent per mirror. The ship gate does not read the mirror changes back; their readbacks stay with the harness (open question 1).

Epics and review runs:

- **`epic.json`** (in the epic run directory): `children` with `ticket`, `repos`, `kind`, `depends_on`. `epic plan` computes the waves and starts the children; a cycle or an unknown dependency starts nothing.
- **Ready.** A child is ready when it is not finished and every dependency passed its review gate (its head is stable). `next <epic>` lists at most `max_parallel` ready children, counting the children another session claimed; a claimed child is not listed for another session.
- **Stacked branch.** `run branch` of a child starts at the head of its same-repo dependency's worktree (`--from-run <run>` names one), and that head is the `branch_point`, so commit checks span only the child's own commits.
- **Epic ship.** `epic ship plan` writes `ship/plan.json` in the epic run: the children's intents in wave order with `depends_on_changes` on each change request, without their ticket intents, and one `issue_comment` on the epic ticket (needs the user). Children ship through their own `ship verify`; `epic ship verify` passes when every child passed its ship gate.
- **Review batch.** `review start a b c` makes one parent (an epic with `batch: true`) and one review run per change. A batch ends when every child has reported.
- **Review run.** The run ticket is the linked ticket (`--ticket`) or `review-<change id>`. Intake needs a `change` contract (`fetch change` or `ingest change`) instead of a ticket. `run branch` creates a detached worktree at the change head (the CLI never fetches: an unknown head says to fetch the branch) with the merge base as `branch_point`. Phases: intake, plan, verify, review, report. The review phase ends when both reviews cover the current commits; a CHANGES verdict is the outcome of a review run, not a loop. `review report` needs a verdict that agrees with the reviews and writes `report/plan.json`: `change_comment`, `change_approve` (only for `ok`) and `issue_comment`, each `needs_user` and `auto: false`. `review fix` checks out the change branch in the worktree and retypes the run to its plan's kind; ACs, lock and evidence stay and intake runs again.
- **`review queue`** runs `[providers.change_list]` (`{id}` is `queue`), or reads `--file`; `draft`, `own` and `approved` are computed by the adapter.

Claim and slots:

- **Claim.** `claim --session <hint>` appends a `claim`; `next` from another session (or none) refuses without `--takeover`, which appends a `claim` with `takeover`. `FACTORY_SESSION` can replace the flag. A parent answers for a child with `answer --run <child>`.
- **Slots.** `env up --slot n` appends a `slot` event, and every later command of the run derives the slot's ports (`slots.apply`). Choose the slot before the plan locks: `{port}` in `up` changes the config hash. Slot 0 needs no local configuration.
- **Shareable.** With `shareable = true` a run whose commit equals the live env's commit uses that env instead of failing on its lock; the binding accepts it. The owner still alone stops it.

Feedback additions:

- `feedback --dismiss <F1|thread:id> --decision Q-n` stops one unresolved thread from counting. It needs a recorded, unconsumed decision (consumed once) and only a thread; a red pipeline stays with `waive`. A dismissal holds across commits.
- `[feedback] ignore_authors` skips threads by those authors (default empty).

Open questions:

1. Should the ship gate read the mirror changes back like the base change (contract per mirror, compared with its intent)? M6 plans them and leaves the readback to the harness.
2. Should a hotfix run be allowed to skip a configured target with a user decision? Today every configured target is required.
3. `epic ship verify` trusts each child's own ship gate; it does not compare the epic plan with the changes again.
