# M7 notes: what was decided where the docs left room

M7 delivers the harness adapters, the optional hook, user scope, the remote of the push intents, the install and transition docs and the changelog. Parity rows 5, 8, 72, 76 and the lessons L-130, L-149, L-160, L-161, L-168 to L-170 and L-01 (the last two only through prose; their tests are older).

## Files and caps (decision 16)

- **Three files for the chat harness.** `skills/factory/SKILL.md` (96 lines), `agents/factory-worker.md` (59) and the managed block of the host instructions file (12). The block counts as one of the three, because the CLI writes it into a file of the user's. `agents-md` writes one block of 69 lines.
- **One worker agent, four roles.** Scout, author, builder and reviewer are sections of `factory-worker.md`; the request names the role. Four agent files would break the 3-file cap, and the roles share the whole reply and infrastructure contract (L-168, L-169). The cost: the agent's tool list is one list, so a role that must not write (scout, reviewer) is held by prose and by the CLI (`review record` refuses a reviewer that built; evidence binds to commits), not by a tool restriction.
- **Tool list without provider names.** `Read, Grep, Glob, Edit, Write, Bash, mcp__dev_mcp__*`. The v0 agents denied the tracker's write tools by name; names of providers are banned in v1, so tracker and change writes are denied by prose only ("remote services are read-only for you"). Open question 1.
- **The prose is package data**, `dev_factory/adapters/*.md`, loaded by `harness.text()` with the version substituted. It is not Python, so it does not count against the 8,500-line core cap; the 100-line cap is tested for every file. `pyproject.toml` lists `adapters/*.md` as package data. Not verified by a wheel build here (no build backend offline); the first install from the tag proves it. Open question 2.
- **The host block is short on purpose.** `agents-md` has no skill, so its block holds the whole protocol (loop, roles, narration, reviewer independence, exploration, every command group including the M6 ones). The chat harness keeps the long text in the skill, which loads on `/factory`, and keeps always-loaded instructions to twelve lines.

## Prose rules (rows 8, 72, 76; L-168 to L-170)

- **Reviewer independence** is stated in the skill, the worker and the `agents-md` block: a fresh worker that wrote nothing in the run; `--actor reviewer-<n>` and `--actor builder-<n>`; the main session never reviews or records a review; a resumed reviewer never re-judges a fix; a green pipeline or a report is not evidence; two different reviewers at high risk. The mechanical part is old: `review record` rejects a reviewer or actor in `builders` (events carry the actor of `verify` and `run branch`).
- **Exploration** in the app browser produces a review finding or a new check through `plan amend`, never a pass (row 72).
- **Language and narration** (row 76, L-170): the user's language in every message, one line at each phase start and each worker launch, two-line summaries of worker reports, long commands announced with an ETA.
- The skill covers the M6 commands: `mirror` and `mirror diff`, `epic plan|ship`, `review queue|start|report|fix`, `claim`, `next --session/--takeover`, `feedback --dismiss`, `env up --slot`, and `/factory session <run-ids>`. A test checks that every `factory <command>` the three prose files name exists (`tests/test_project.py`).
- Human checks go through `factory confirm CHK-n --user-said`; the skill says it only after the user's answer in chat.

## Scope, hooks, init

- **`--scope user`** writes the three files into the host home (the host's config-dir variable or its default folder in the home) and creates no project: running it in a stray directory must not leave a `factory.toml`. `agents-md` has no user home and refuses `--scope user` and `--hooks`.
- **`init` in an existing project** (a `factory.toml` is there, no `--import`) writes the adapter only. Before, it failed with "never overwrites", which left no way to refresh the adapter after an upgrade. `--repo`, `--name` and `--base` are ignored in that case.
- **Ownership.** A whole file carries `<!-- factory:generated <version> -->`; a file at its path without it is skipped with a message. A block lives between `<!-- factory:begin -->` and `<!-- factory:end -->`. Anything the CLI changes is first copied to `<name>.factory-bak-<n>` (first free n); an unchanged file is not written and gets no backup, so a second run leaves nothing new. A write that fails (a locked file) says which file and keeps the old content (L-161).
- **Settings merge** (`--hooks`): one `PreToolUse` group whose command is `factory hook pre-tool` (the installed entry point, never a path, L-149). Groups whose commands all start with `factory hook ` are the CLI's own and are replaced; every other group and key stays. Invalid JSON or an unexpected shape refuses with no change. Comparison is by content, so a settings file that already holds the entry is not rewritten.
- **The hook** reads the tool call on stdin and denies, with the host's deny JSON, a write to `.factory/runs/<id>/` `events.jsonl`, `events.lock`, `artifacts/`, `reviews/`, `contracts/` or `raw/` (file tools, a path argument of an MCP tool, or a shell command with a redirect to such a path or a writing verb next to it). It fails closed on unreadable input. It is regex defence in depth (L-17, L-109 to L-115 show such guards are bypassable); the gates stay authoritative. `plan.json`, `notes/`, `media/`, `input/`, `ship/` and `report/` stay writable because agents write them. A reviewer writes its review file outside the run folder.
- The v0 role guard and fail-fast hooks are not ported (see `docs/transition-from-v0.md`).

## Remote of push intents

`[[repo]] remote` (default `origin`) feeds the `remote` of every `push` intent, mirror pushes included (`docs/schema/factory-toml.md`). The refs the CLI reads locally (`<remote>/<base>` in `git.py`, `ship.behind`, `mirror.py`, `review_run.py`) follow the configured remote since the independent review (m10). `remote` is still unvalidated free text.

## Version and tag

The tag is `1.0.0-alpha` and the package version `1.0.0a1`: setuptools normalises `1.0.0-alpha` to `1.0.0a1`, and `[project] cli` pins and `factory --version` use the normalised form. Nothing was changed; the tag is created by the owner after the independent review.

## Budgets

Python core: 8,161 lines (cap 8,500); `harness.py` 49, `adapters.py` 77, `hook.py` 49, `cli.py` 367 (cap 400 per module). Generated files: see above, none over 100 lines. New tests are in-process with temp directories (`tests/test_adapters.py`, about 0.3 s).

## Lessons

L-161 is the only M7 `test` row (`test_adapters.py`, `M7_COVERED`). L-130, L-149, L-160, L-169 and L-170 are `by-design` and have no tag; L-168 stays with M1 and L-01 with M2. Every one of the 143 `test` rows is now in a milestone set and has a tagged test; no lesson is pending for the alpha.

## Open questions

1. Can the host deny tracker and change writes for the worker without naming providers (a pattern such as `mcp__dev_mcp__*_create_*`)? Until it is known to work, the worker's read-only rule is prose.
2. Should the browser runner ship inside the wheel? A tag install has no `browser/` folder, so `[checks.browser] bundled` must point at a checkout (`docs/install.md`).
3. Should the local `origin/<base>` reads follow `remote` too?
4. The hook runs one process per tool call (about the cost of `factory --version`). Measure it on the owner's machine before recommending `--hooks` by default.

## Review follow-up: findings deferred or answered in one line

- m6 (clean flag sampled once per batch): deferred. `verify_reasons` re-reads dirtiness at every gate and refuses a dirty worktree, so a record made while a check dirtied the tree never reaches a pass; a per-check sample would need `run_one` restructured for a case the gate already closes.
- m9 (hook matcher hardcodes `mcp__dev_mcp__.*`; project hooks go to the shared `settings.json`; `merge_hook` owns any `factory hook ` group): deferred. The matcher name is the product's own server name, the shared-file choice is documented in `docs/install.md`, and a teammate without the CLI can drop the `--hooks` flag; changing the target file is a product decision for the owner.
- m10, dismissal key without the repo (`thread:<id>`): deferred. A same-id thread in two repos of one run is rare, and the user's dismissal names the finding it saw.
- Mirror changes are not read back by the ship gate (m6 open question 1): still open and disclosed.
- Hook (m1) is documented in `hook.py` as a guard against accidents, not a sandbox: it normalises paths and handles `cd <run dir> &&` and `$VAR/` targets as best effort; arbitrary indirection (an `eval`, a script that builds the path) is still out of its reach by design.
- Same-diff gate (m2): binary, added and deleted files now count. The moved-line limit stays: the net-count signature keeps no position, so one line applied in another function of the same file is "equal" (documented in `mirrordiff.signature`).

## Follow-ups after the independent review (APPROVE at bef770d)

Three review rounds; no blocker or major is open. Minor follow-ups, not fixed for the alpha:

- `gate ship` is not idempotent after a post-ship refresh: the second re-run fails and sends the run back to the ship phase (`ship.py`).
- `plan amend --decision D-n` consumes the decision through the loop event, not the lock, so `gate reproduce` keeps failing until a second decision is recorded; `plan amend` without `--decision` leaves D-n unconsumed and reusable.
- The reproduction-decision keyword `reprod` also matches words that ask for a reproduction (accepted under L-126).
- Hook: some reads (`grep "rm -rf"`, `sed -n`, `node script.js` on artifacts) are still denied, and deleting a whole run directory with `rm -rf` is allowed. Out of scope by design: `pushd`, `cd` into the runs directory then `cp`, `[IO.File]`.
- Mirror diff: a binary deleted on both sides from different old blobs may show as divergent (not reproduced).
- Full suite wall time is about 131 s on the owner's machine: under the 180 s cap, over the 120 s target.
