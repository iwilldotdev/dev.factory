# M0 decisions: one question round

Each item has a recommendation (**Rec**) and its trade-off. Answer with the item numbers you change; silence means the recommendation stands.

Already decided by the owner:

- Design is in the alpha.
- The alpha keeps every v0 capability (`docs/parity-map.md`).
- Options that remove a feature are therefore not offered.

## From PIVOT §7

1. **Where run state lives.** **Rec:** `<project>/.factory/runs/` (gitignored).
   - For: no index; runs sit next to the code; the CLI never moves runs [L-104, L-105].
   - Against: deleting or recloning the checkout loses runs.
   - Alternative: a user directory keyed by project path. It survives reclones but needs an index and breaks when the path moves.

2. **Config file name.** **Rec:** `factory.toml`.
   - Alternative: `dev-factory.toml`, which avoids collisions with other tools.

3. **Installation.** **Rec:** install from a git tag with a standard isolated Python app installer (the `factory` entry point); no `install.py`, no shims. `factory init --harness [--hooks]` writes adapters.
   - For: no shim re-parsing [L-130], independent of any working tree [L-149], same on both OSes [L-162].
   - Against: the user needs that installer once.

4. **Adapter examples.** **Rec:** a separate repo whose CI validates the examples with `factory ingest`. The core grep ban stays absolute [L-172].
   - Alternative: `examples/` excluded from the grep, which weakens the ban.

5. **Minimum Python.** **Rec:** 3.11 (`tomllib`).
   - Alternative: 3.10 plus a vendored TOML parser, which breaks stdlib-only.

## Surfaced by the design

6. **Commit-format check.** Parity keeps it. **Rec:** an optional per-repo `commit_pattern` over `branch_point..HEAD` (stacked branches start at the dependency head); base-integration merges skip only the header [L-89 to L-92].

7. **Default worktree location.** **Rec:** `<user cache dir>/dev.factory/worktrees/<project>`, overridable in `local.toml`. Worktrees inside the repo get scanned by the main checkout's test runners and watchers.

8. **Human checks.** Parity keeps them. **Rec:** a `human` check is satisfied by `factory confirm <check> --user-said`, bound to the current SHAs. It never alone proves a bug or design AC, is listed in the ship body, and pending ones do not count as review loops [L-24, L-138].
   - Alternative: the confirmation survives later commits (the v0 behaviour). This is less re-asking, but the confirmation is weaker.

9. **Checks-only amend keeps the code review (L-137).** **Rec:** yes. Two review kinds: `code` is bound to the plan without checks, and `qa` to the full plan.

10. **Changed-values scan (L-39).** **Rec:** a required `changed_values` list in the `code` review.

11. **`gate verify` strictness.** **Rec:** every planned and standing check passes at HEAD, not just one check per AC [L-01].

12. **Flaky results.** **Rec:** `flaky` never satisfies `gate verify`; a later clean attempt does [L-14].

13. **Harness adapter ids vs the grep ban.** **Rec:** an explicit allowlist for harness adapter ids and their generated files only. Provider names stay banned everywhere.

14. **Browser runner.** **Rec:** bundle a minimal Node runner (`browser/`, about 820 lines) with the commands `login`, `visual`, `style`, `spec`, `batch` and `screenshot`. It is installed only when a plan has UI checks.
    - `browser` specs may use the project's own runner through `[checks.browser] runner`.
    - `visual`, `style`, `screenshot` and `batch` always use the bundled runner, because the crop, diff and probe rules are proof rules [L-54, L-55, L-57].
    - Cost: a second runtime and a second CI job.

15. **One loop budget.** **Rec:** `autonomy.max_loops = 3` covers unapproved amends, review CHANGES rounds with blocker or major findings, and feedback rounds [L-135, L-136].

16. **Caps under parity (they replace PIVOT §3 once approved).** The budgets are in `docs/parity-map.md` §2.

    | Cap | PIVOT | Proposed | Why |
    |---|---|---|---|
    | Python core | 5,000 | **8,500** | 8,420 estimated: base 4,750 + design 700 + parity 2,970. That is still under half of v0's ~18,000 lines for the same capabilities. |
    | Module | 400 | 400 (kept) | New capabilities get their own modules (`mirror`, `epic`, `review_run`, `quality`, `feedback`, `envdata`, `envlock`) |
    | Node runner | none | **900** | 820 estimated (v0: 860) |
    | Python suite | 120 s | **180 s**, parallel, Windows + Linux | Adds an end-to-end fixture each for hotfix, epic, review and feedback. Unit tests stay in temp dirs with copied fixture repos. |
    | Runner suite | none | **90 s**, separate CI job | needs a real headless browser |
    | CLI overhead | 1 s | 1 s (kept) | |
    | Adapter | 100 lines | 100 lines per file (kept); at most 3 files per harness | sessions and epic prose need their own file |
    | DESIGN.md | 10 pages | 10 pages (kept) | parity detail lives in `docs/parity-map.md` |

    - **Rec: A.** Approve the table as is.
    - **B.** Same table, but `mirror`, `epic` and `review_run` are counted as a "composite" budget (1,040) next to a 7,400 core. Same code, a different reporting line.

17. **Exact frames acquired by the agent.** **Rec:** accept `source: agent` for `design_frame` and `design_image`. They are specification, like ticket text, and stay bound by raw hash and `source.version`.
    - Alternative: exact frames require an adapter, and agent-acquired frames count as approx.

18. **Parity items I would drop if you allow it** (each is kept unless you say drop):
    - **a.** Env slots and port offsets (parity row 57, 80 lines). They caused most v0 env incidents [L-75 to L-79]; the per-env lock plus `--wait` serializes safely.
    - **b.** Token metrics adapter (row 75, 60 lines). It depends on each harness exposing usage, and the v0 numbers were wrong twice [L-182, L-183].
    - **c.** `verify --estimate` (row 42, 50 lines). `metrics` history answers the same question after one run.
    - **Rec:** keep all three for parity, but build them last in their milestone.

19. **Mechanisms with real v0 usage among the dropped ones.** Checked against the run event logs: `fit` (one visual check), `target_paths` (hotfix runs), `run rewind` (4 times), `prove --fast` (2 times), `amend-check` (2 times).
    - **Answer:** keep `fit`. `target_paths` stays dropped as mapped (no base-dependency replacement). `rewind`, `--fast` and `amend-check` are covered by SHA-bound invalidation, reuse and `plan amend`.

## Answers (M0 approved, 2026-11-24)

- Decisions 1-18: the recommendation stands for every item. Decision 16 caps replace PIVOT §3: core 8,500 lines, module 400, Node runner 900, Python suite 180 s and runner suite 90 s in parallel on Windows and Linux, CLI overhead 1 s, adapters 100 lines per file with at most 3 files, DESIGN.md 10 pages. Decision 18 keeps slots, the token metrics adapter and `verify --estimate`, built last in their milestones.
- Decision 19: keep `fit`; drop `target_paths` as mapped.
