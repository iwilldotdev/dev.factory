# Changelog

## Unreleased

- Removed `factory init --import`: a project is configured from its own workspace, never from copied config.
- `factory run delete` also removes the run's clean worktrees and deletes its branches that have no commit beyond their base; it keeps and names a dirty worktree or a branch with commits of its own.
- An env failure names its kind, the last 30 lines of the env log (once the server started) and one likely cause per kind; the recorded reason leaves the log out, so two identical failures still compare equal.
- `factory setup`: `--discover [DIR]` prints JSON facts of every git repo (remotes without credentials, base branches, stacks and their commands, CI steps, env files, ports); without flags it prints a checklist of the project config (done / missing / optional, each with its key and an example) and exits 1 while something required is missing. The skill sets a project up from zero with it.

## 1.0.0-alpha

First release. Tag `1.0.0-alpha`, package version `1.0.0a1` (PEP 440 spelling of the same release; `factory --version` prints `1.0.0a1`). Python 3.11 or later, standard library only; the browser runner in `browser/` needs Node.

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
