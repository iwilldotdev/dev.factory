# Changelog

## 1.0.0-alpha (2026-11-25)

First release. Tag `1.0.0-alpha`, package version `1.0.0a1` (PEP 440 spelling of the same release; `factory --version` prints `1.0.0a1`). Python 3.11 or later, standard library only; the browser runner in `browser/` needs Node.

### After M7: the first real runs (DEV-9160)

- Removed `factory init --import`: a project is configured from its own workspace, never from copied config.
- `factory run delete` also removes the run's clean worktrees and deletes its branches that have no commit beyond their base; it keeps and names a dirty worktree or a branch with commits of its own.
- An env failure names its kind, the last 30 lines of the env log (once the server started) and one likely cause per kind; the recorded reason leaves the log out, so two identical failures still compare equal.
- `factory setup`: `--discover [DIR]` prints JSON facts of every git repo (remotes without credentials, base branches, stacks and their commands, CI steps, env files, ports); without flags it prints a checklist of the project config (done / missing / optional, each with its key and an example) and exits 1 while something required is missing. The skill sets a project up from zero with it.
- `gate ship` stays passed when re-run after a feedback refresh; a `plan amend` lock consumes the decision it rests on, so `gate reproduce` accepts it and it cannot be reused.
- The intake gate points at `factory run repos <name>[,<name>]` (it works after start) and lists the configured repos; `factory next` asks for the repos once the ticket and its media are recorded.
- Run branch slugs fold accents ("árvore" becomes "arvore") and are cut at a word boundary.
- Autonomy in the generated prose: project config that is not product code is the orchestrator's to fix (then `factory check` and a line in the next report); intake is the only phase that asks the user, later phases decide and record the assumption in `notes/design.md`; envs start in the background once intake passes; a worker fixes the config cause of an env failure once before it returns blocked; automated proof is preferred and human checks are collected in one message; the user's language comes from their first message, or the ticket's when they only typed a command.
- Found on the first real run (DEV-9160), fixed in place:
  - A login spec may declare `"done": {"cookie": "<name>"}` or `{"url": "<regex>"}`: an app that redirects to another app after login no longer fails the same-origin rule.
  - On Windows a dev server listening on `0.0.0.0` or `[::]` was never seen as up, so its env timed out; the probe now connects and talks to the address that accepts.
  - A run is cut from and measured against the remote base (`<remote>/<base>`) when the clone has it: a stale local base made the run start behind, count the base's merged commits against `commit_pattern`, and count the base's lint findings as new. Quality baselines move to the base commit last merged in.
  - A clean merge of the base (git recomputes the same tree) keeps an approved review; verify still re-runs every check at the new HEAD.
  - `factory next` warns in build and review when the base moved, so the merge happens before the review; early UAT images are kept for the ship briefing.
  - The change body strips terminal colors and the local worktree path from output excerpts, and a `quality` check shows its count against the base (`N finding(s), M at the base, K new`) instead of the base's raw errors under a pass.
  - A moved base is integrated by rebase by default (`[ship] base_update = "rebase"`, or `"merge"`): `factory next` asks for `git rebase <remote>/<base>` so the change keeps only its own commits; a review stays approved when the rebased commits carry the same patches; a push that rewrites an already pushed branch gets `force_with_lease` (the SHA it replaces) and always asks the user.
  - The change description is a document for its human reviewer: `ship/summary.md`, written in the project's language (what changes, behaviour changed beyond the ticket, how it was tested), refused while it carries check, AC or decision ids, attempts, run paths or factory commands. The AC x check x evidence table, review rounds and findings stay in the run and the Gate S briefing; `ship verify` checks that the description is the planned body.
  - A finding may say `"carries": "<round:id>"` when it re-lists an earlier one; resolving it resolves the chain, so a finding fixed under a later round's id no longer stays open under its first id.
  - A delivery ships finished: `gate review` fails while a minor finding of the delivery is open (fix it; a round of minor fixes spends no loop). A reviewer marks `"scope": "out"` on a finding that is not part of this delivery; it stays in the run, never in the change.
  - `plan amend --resolves <round:id>[,...]`: an amend that only changes checks to close open minor findings of the delivery spends no loop, like a round of minor fixes; it refuses an AC change or a finding that is not open. A budget blocker left by an earlier refused amend closes once an amend went through without a loop.
  - A standing check takes `vars`, environment variables for its command as the CI job sets them: the CI build of the first run needed a larger Node heap than the default, and the same command ran out of memory under verify.
  - UAT in the browser: a change with a screen takes a third review, `--kind uat`, after code and qa and from another fresh reviewer, who uses the screens logged in through exploration scripts (`factory uat run <run>/uat/<script>`, recorded as `uat` evidence with screenshots, never a check result) and cites them in `explored`. A defect the change causes is a finding even outside the ACs; `gate review` needs the uat review whenever the plan has a screen. The reviewer's own exploration runs never count it as a builder. An approved UAT stays valid across commits that touch only tests (the screens it used are unchanged); code and qa still re-review them.
  - `quality` ids ignore line and column (`file:12:5:`, `file(12,5):`): code that only moves no longer turns a base finding into a new one; the same finding repeated in a file is numbered, so one more copy is still new. Recorded baselines are normalized the same way when compared.
  - The user decision that unblocks a spent loop budget (`factory unblock B-n --decision Q-n`, as `factory next` says) pays for the next loop, once; before, the unblock consumed it and the review or amend it was meant for was refused again.
  - Fixes asked by a review, the UAT or feedback are `fixup!` commits of the commit they correct: the commit-pattern check reads the target's header, `ship plan` refuses until they are folded (`git -c sequence.editor=: rebase -i --autosquash <base>`), and a fold that keeps the tree on the same base keeps the reviews. The change ships its feature commits, without "adjustments" commits.
  - Human checks are deferred to the ship briefing: a pending one no longer holds the verify gate or the reviews, so the run stays autonomous; the user confirms every human check once, with the push question, and `gate ship` needs the confirmation at the final commits. `factory confirm CHK-n --rejected --user-said "..."` records a rejection: a finding that spends one loop and sends the run back to the builder.
  - Answers recorded before the first plan lock (the intake answers the plan was built on) are no longer listed as pending decisions by `status` and `next`.
  - `autonomy.max_loops` defaults to 5; each user decision still allows one more round.
  - The Python core cap rises from 8,500 to 10,000 lines (the owner: quality over the cap).
  - Tests: the foreign port owner starts with the base interpreter, since a Windows venv `python.exe` is a launcher whose child holds the port.
  - A `plan amend` that only changes standing checks of `factory.toml` is project upkeep and spends no loop; a failing standing `cmd` check that already fails at the branch point points to `type = "quality"`.


### M1: project, config, run, contracts, intake
- `factory.toml` (repo, workspace and `local.toml` levels), schema validation, secrets named by key only; `factory init`, `check`, `status`, `runs`, `log`.
- A run is an append-only `events.jsonl` under `.factory/runs/<id>/`; contracts (ticket, change, pipeline, design) enter through adapters (`fetch`) or agent-written files (`ingest`); the intake gate; media seen or unseen.
- `factory next` prints the one required action. The `agents-md` adapter. Structural tests: provider-name ban, size ceilings, git safety, no network.

### M2: plan, lock, verify
- `plan.json` with ACs and checks, the plan lock and `plan amend` with a loop budget, per-run worktrees, `cmd` checks, `verify` (evidence bound to commit and spec hash, reuse, fail-fast), bug runs with `reproduce` and red-green, commit-pattern check, `metrics`.

### M3: review and ship
- `review record` with independence from the builder, the review gate and loop budget, `ship plan` (neutral intents; the CLI never pushes), `ship verify` against a readback, `confirm` and `waive`, the ship gate. Criterion met: a real ticket in one repo reaches a draft change.

### M4: runtime and design
- Workspace runs, `env up|down|health|logs|seed|reset` with locks, login users and test data, `wait`, `http`, `browser`, `visual`, `style` and `screenshot` checks, the bundled browser runner (`browser/`), design frames (exact and approximate), early UAT.

### M5: proof depth and feedback
- Feedback phase after ship (unresolved threads and failed jobs become findings and the run returns to build), `human` checks, `quality` checks with a baseline and scoped runs, `guideline` rules, risk and `notes/design.md`, `verify --estimate`, metrics report and tokens.

### M6: composite runs
- Hotfix with mirror worktrees, `verify --mirror` and the same-diff gate; epics with waves and stacked branches; review runs (`review queue|start|report|fix`); `claim` and `next --session/--takeover` for parallel sessions; env slots and `shareable`; `feedback --dismiss` and `ignore_authors`.

### M7: harness adapters, installation
- `factory init --harness <id>` for the chat harness writes a skill, one worker agent with scout, author, builder and reviewer sections, and a short managed block in the host instructions file. `--harness agents-md` writes the full equivalent prose into an `AGENTS.md` block. Every generated file has at most 100 lines; a harness has at most 3.
- `--scope user` installs into the user's host home (no project created); `--hooks` merges one entry, `factory hook pre-tool`, into the host settings, which refuses hand edits of run events and evidence. Both are idempotent, keep a numbered backup of anything they change and leave files they did not write alone.
- `init` in a directory that already has a `factory.toml` writes only the adapter (refresh after an upgrade).
- `[[repo]] remote` (default `origin`) names the remote of the `push` intents of `ship plan`.
- Adapter prose ships as package data (`dev_factory/adapters/*.md`).
- Docs: `docs/install.md`, `docs/m7-notes.md`.
