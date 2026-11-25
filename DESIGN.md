# dev.factory v1 design

Status: M0 draft for `1.0.0-alpha`. `[L-nn]` cites `LESSONS.md`. Schemas live in `docs/schema/`; open decisions in `docs/m0-decisions.md`.

## 1. Purpose and threat model

dev.factory takes one ticket from intake to a draft change request, and no step passes on a model's claim. It is a local CLI that any harness (or a person) drives.

**Threat model.** The model is cooperative but can be lazy. It may claim an AC from reading code [L-01], count a setup failure or zero tests as a result [L-02, L-03], loosen a check after the plan [L-132], or carry a proof or review past a later commit [L-08, L-18]. The factory does not defend against an adversarial orchestrator. All state is local files the same OS user can write, and v0 spent six audit rounds on write guards that were bypassable by construction [L-17, L-109 to L-117].

**Trust comes from binding evidence to what was proved, not from guarding writes.** Only `factory verify` (and its `reproduce` mode) writes evidence; gates only read. A record names the check-spec hash, repo, full SHA and a clean-worktree flag, and a gate accepts it only while all of these match the current HEAD and the plan lock [L-07, L-08, L-09, L-16]. Accepted residuals: forging a record (a green pipeline can be an extra ship condition) [L-16, L-19], files hidden by git exclude files [L-25], whether a person actually viewed a file [L-30], and forged user decisions [L-126].

Invariants:

- The CLI makes no network call. It runs local git, its own health and `http` checks against the declared local env origin [L-82], and commands the user configured (adapters, env, checks, the browser runner).
- Remote content is evidence, never instruction. Payloads go to files, never through chat [L-159].
- No provider names anywhere in the repo; a grep test covers code, tests and docs [L-172].
- The CLI never runs reset, stash, clean or force-push [L-106]. Only the agent pushes, and only in ship.
- A bug gets no fix without a reproduction [L-33].

## 2. Project model

A **project** is one repo or a workspace directory, with one schema for both.

```
<project>/
  factory.toml        # versioned: repos, commands, env, standing checks, providers, limits, ship, autonomy
  .factory/           # unversioned; init adds it to .gitignore
    local.toml        # this machine: paths, base URLs, worktree_root
    runs/<run-id>/    # run state (section 4)
    env/              # env locks, process records, login state
```

- **Single repo:** `factory.toml` at the root with `[[repo]] path = "."`.
- **Workspace:** `[[repo]]` entries point at members by relative path (`path = "../api"`). A member's own `factory.toml` entry (setup, env, standing checks) is the base, and the workspace overrides it key by key. The rules are in `factory-toml.md`. Each repo names its own base branch [L-94].
- **Discovery:** the CLI climbs from the cwd to the first `factory.toml`; `--project` overrides. It works from any cwd [L-141] and never imports code from the cwd [L-148].
- **`local.toml`** may set only machine facts (paths, base URLs, `worktree_root`). Any other key is an error, so it cannot loosen proof [L-131].
- **Secrets:** `factory.toml` names keys only. Values come from the environment or `~/.config/dev.factory/secrets/<project>.env`. They reach children only as environment variables, never in argv or output, and stored tails are redacted [L-80, L-129].
- **Formats:** TOML for files people write, JSON and JSONL for files the CLI writes. Hashes use canonical JSON [L-167]. Runs are plain directories that the CLI never archives, moves or deletes [L-104, L-105] (decision 1).

## 3. Harness-agnostic protocol

Any harness loops `factory next [run] [--json]` → do the action → `next`. `next` derives state from `events.jsonl` and prints one required action: its phase and kind, the command to run, the reasons any pending gate would fail, a role hint, and the absolute forward-slash path of every file it expects [L-158]. It never asks for a payload to be pasted [L-159]. One end-to-end test drives intake to ship through `next` alone [L-26].

**Adapters.** `factory init --harness <id>` writes thin files: a skill plus agent definitions for a subagent-capable harness, an `AGENTS.md` section, or nothing. Each is at most 100 lines, generated from the installed CLI, lists no tools, and states no rule the CLI does not enforce [L-118, L-119, L-160, L-171]. **Hooks** are optional hardening. No gate depends on one, so a hook failing open passes nothing [L-109, L-115].

**User input.** One question round, at intake; later phases decide, record the assumption and report it, and ask only for an ambiguity that blocks the work. `factory ask "<text>" [--ref attachment:<id>]` opens `Q-n`, and `factory answer Q-n --user-said "<words>"` closes it with a decision a later `--decision Q-n` consumes once. `factory decide --user-said` records an unsolicited decision [L-126]. After `gate plan`, a new question blocks the run. `factory block --kind question|decision|infra|tooling` lists the valid kinds when given a bad one [L-140], and each blocker appends exactly one event [L-152]. Human events are typed [L-177]. After two identical env-kind failures, `next` reports the run blocked and stops suggesting that command [L-168, L-169].

## 4. Runs and events

`factory run start <ticket-id> [--repos a,b]` creates `.factory/runs/<run-id>/`, using a safe-filename id; a collision is an error that names both ids [L-157].

| Path | Writer | Content |
|---|---|---|
| `events.jsonl` | CLI | append-only; the only run state |
| `raw/<kind>/<id>.<ext>` | adapter or agent | untouched payload (design images as PNG) |
| `contracts/<kind>/<id>.json` | CLI (`fetch`, `ingest`, `design add`) | validated contract |
| `media/` | agent | ticket media |
| `plan.json`, `checks/**` | agent | plan, specs, style maps (locked at `gate plan`) |
| `diagnosis.md` | agent | bug only |
| `artifacts/<check>/<attempt>/` | `verify` | output, screenshots, diffs |
| `ship/` | CLI, then adapter or agent | intents, body, readbacks |

There is no `run.json`. Phase, loops, locks and blockers are derived from events, so concurrent writers cannot lose state [L-15, L-164]. Each append is one line written in one call under a short file lock [L-165]. Atomic writes retry on Windows sharing violations [L-163]. Events record the CLI version [L-150]. `factory status` names a missing worktree or head, and `run close --reason` ends a run without gates [L-103].

## 5. Flow and gates

```
intake -> plan -> [reproduce] -> build <-> verify -> review -> ship -> feedback -> done
```

**Run kinds:**

| Kind | Flow |
|---|---|
| `feature`, `bug` | the flow above |
| `hotfix` | `bug` plus a post-review mirror step and the same-diff gate |
| `epic` | a parent run over child runs in dependency waves |
| `review` | intake, plan, verify, review, report; it starts from an existing change |

Feedback turns change threads and failed pipeline jobs into findings and returns to build ⇄ verify. `docs/parity-map.md` gives each composite flow; all of them reuse the gates below.

`factory gate <name>` reads only. Exit 0 appends a pass event, and exit 1 lists every failing reason. Every gate after `plan` re-checks the lock first (section 6).

| Gate | Passes when |
|---|---|
| `intake` | A valid ticket contract exists with a matching raw hash [L-122, L-156]. The run kind equals the contract `type` [L-31]; a kind change re-runs intake and nothing is archived [L-32]. Every media attachment, matched by id with the mime case-normalized, is `seen` with one file, or `unseen` with an open question naming `attachment:<id>` [L-28, L-29]. At least one affected repo is declared. |
| `plan` | `plan.json` is valid. Every AC has a check. Patterns have at least 6 characters and 4 literal word characters [L-04]. Each bug `reproduce` AC has an `expect_fail_pattern` check [L-33]. Worktrees exist, with `branch_point` recorded and `setup` passed [L-06, L-72, L-89]. Each runtime repo's env is healthy, with all problems in one message, and new runtime checks pass a dry run that is not `setup` or `error` [L-86]. The design rules of section 9 hold. No question is open. On pass, the plan is locked. |
| `reproduce` (bug) | `diagnosis.md` has non-empty `## Root cause`, `## Evidence`, `## Ruled out` [L-38], and a `## Scenario` that says `ticket scenario used` or lists each deviation with its reason [L-27]. Each `reproduce` AC has a `reproduced` record at the base SHA under the locked spec hash [L-03, L-05, L-33]. `diagnosis.md` joins the lock. |
| `verify` | Every effective check (plan plus standing) has a `pass` at its repo's current HEAD, from a clean worktree, under the locked spec hash. For runtime checks, every served repo's head is current and the server started at it [L-01, L-07, L-08, L-09, L-12, L-69, L-85]. `flaky` never counts [L-14]. Each affected repo has a commit past `branch_point` [L-87]. Each bug `cmd` reproduction has a red-green pass [L-34]. |
| `review` | The `verify` conditions hold. The latest `code` review is APPROVE, bound to the current SHAs and the core-plan hash. The latest `qa` review is APPROVE, bound to the current SHAs and the full plan hash [L-18, L-137]. A plan with a screen also needs the latest `uat` review APPROVE at the current SHAs. A review stays bound across a clean base merge, a rebase with the same patches, or a fold with the same tree; a `uat` review also across commits that touch only tests. No minor finding of the delivery is open (`scope` `"out"` excepted). Each approx design frame has a user design review (section 9). |
| `ship` | The `review` conditions hold. Exact design frames are fresh (section 9). Each change readback has proof-grade source [L-21] and matches the ship plan. With `require_pipeline`, the latest pipeline for the pushed head is `success`, or a user waiver names that pipeline id [L-20, L-155]. |
| `report` (review runs) | The plan lock holds, the latest `code` and `qa` reviews cover the current commits (their verdicts are the outcome, not a failure), and a `review report` is recorded for them [L-185]. |
| `ship` on a hotfix | Also: every mirror target is mirrored, its `mirror: true` checks pass at the mirror head, the ship plan has one titled change per mirror, and the mirror diff equals the base fix or a user decision names the divergent files [L-41, L-95, L-100]. |

`prove` is gone as a phase. A green pipeline never proves an AC [L-19].

## 6. Plan lock and `plan amend`

The `plan_locked` event holds:

- `plan_hash` and `core_hash` (the plan without checks)
- a `spec_hash` per check
- the hashes of declared spec, style-map and fixture files only; screenshots and output are ignored [L-58]
- `config_hash`, covering the proof-relevant config: commands, runner, timeouts, limits, setup and env commands, but not paths or ports [L-131, L-188]
- the raw hashes of contracts [L-122]

A `spec_hash` covers the canonical check entry, the resolved defaults and command, the declared file hashes, and for `visual`/`style` the frame and image contract hashes [L-09, L-59]. Repo files are bound by SHA.

Any drift fails every later gate and `verify` (`plan changed after lock` or `config changed after plan`) [L-131, L-132] until `factory plan amend --reason "<why>" [--decision Q-n]`. Amend re-validates and appends a new lock with the changed ids, the reason and the decision [L-133, L-134]. A checks-only amend keeps the code review [L-137].

**Loop budget.** `autonomy.max_loops` (default 5) covers unapproved amends and review CHANGES rounds that have a blocker or major finding [L-135, L-138]. When it is exhausted, the command refuses and the run blocks for the user [L-136]. Counters are derived from events [L-134]. Build⇄verify iterations are not loops.

## 7. Evidence, reuse and ordering

`verify [--check <id>] [--fresh] [--json]` appends one `evidence` event per attempt, so failures are never overwritten [L-15]. A record holds the following (full shape in `contracts.md`):

- check, spec hash, repo, mode (`verify`, `reproduce`, `red` or `green`)
- full SHA, worktree, and clean, where untracked files count [L-07]
- `heads` of every exercised repo [L-12], and `served` (path, start SHA, pid with its create time) [L-69, L-85]
- redacted command, exit code, duration, a tail of 40 lines or 4 KB
- result, kind and reason, artifacts, attempt, and the record's own hash

**Classification** [L-02, L-03, L-05, L-45, L-139]:

- Exit codes 126, 127 and 9009, setup markers (including localized ones) and zero-test markers make the result `setup`, never `pass` or `reproduced`.
- The exit code decides first, then `expect_pattern`.
- A retry that passes is `flaky` only after an assertion failure.

Children get closed stdin and an English locale [L-44, L-45].

**Reuse:** a record with the same spec hash, same `heads`, clean and `pass` is not re-run [L-10, L-12]. `--check` runs any declared check [L-63].

**Order:** last-failed, then new or changed, then cheapest type (`cmd` < `http` < `browser` < `style` < `visual`) [L-11].

**Fail fast:**

- Each check has a wall-clock cap that kills the whole process tree [L-42, L-43].
- An env failure skips the repo's remaining runtime checks [L-46].
- Two identical consecutive failures stop the batch [L-47].

**Exit codes:** 0 pass, 1 check failed, 2 usage, config or lock error, 3 env [L-48].

## 8. Checks

- **`cmd`:** `run`, `cwd`, `expect_exit`, `expect_pattern`, `timeout_s`. Quality commands are standing `cmd` checks [L-63].
- **`http`:** method, path, optional body, `expect_status`, `expect_json` subset. The origin is always the env `base_url` [L-82]. Failures never quote the body [L-83]. `session` sends the headers that the env's `login_http` printed, kept in memory only [L-52].
- **`browser`:** a declared spec run by the bundled runner, or by the project's own `runner` template if set. The exit code decides, and screenshots are artifacts [L-56]. One retry, used only for flaky classification [L-14].
- **`visual`, `style`:** section 9.
- **`quality`:** a `cmd` with failure-id parsing. It compares against a baseline at `branch_point` (unstable ids are quarantined) and has an optional scoped template [L-60 to L-62].
- **`human`:** satisfied only by `factory confirm --user-said`, bound to the current SHAs. It never alone proves a bug or design AC [L-24].
- **`screenshot`:** artifacts only. Before images come from base in `reproduce`, after images from HEAD [L-56].
- **Runtime binding:** before a runtime check, the env lock must show a factory-started process from the checked worktree at its HEAD. Otherwise there is one restart, then `error`. There is no weaker mode [L-13, L-69, L-85].
- **Reproduce:** `factory reproduce` runs bug checks in a throwaway worktree at `branch_point`, never the run worktree [L-36]. It serves runtime checks from it, then restores the run env; a failed restore exits 3 [L-37]. `reproduced` needs a non-setup failure matching `expect_fail_pattern` [L-03, L-05].
- **Red-green:** `verify` runs it for bug runs once the check passes at HEAD. In a throwaway worktree, red runs at `fix_commit~1` with `test_files` from git at `fix_commit` and must be `reproduced`. Green runs at `fix_commit` and must pass [L-34, L-35]. Runtime-only reproductions are covered by reproduce plus verify.

## 9. Design pillar

If an AC has UI and a design source exists, the alpha proves parity with the design [L-186].

**Contracts.**

- **`design_frame`** holds `id`, `name` and `fidelity` (`exact` or `approx`). It has `source {file, version}` (`version` required for exact) and a reference to its image. Its `root` is a tree of normalized nodes:
  - `id`, `name`, `type`
  - `box` and `render_box`
  - `fills`, `strokes`, `effects`
  - `corner_radius` or `corner_radii`
  - `layout` (mode, gap, padding)
  - `text` (characters, family, size, weight, line height, letter spacing, align, color)
  - `children`
  - for approx, an `estimated` map listing every number and color

  Colors are `#rrggbb`.
- **`design_image`** is a PNG at scale 1 with its dimensions.

**Acquisition** follows the pivot's two routes (section 12) with no mapping DSL [L-124]. An image adapter writes the PNG to `{out}` [L-121]. `factory design add <png> --name N` creates an approx image and a skeleton frame. The agent then writes the frame data, and ingest rejects any number or color not flagged `estimated`. A frame is specification, not a proof result, so `source: agent` is accepted, as for ticket text (decision 17).

**Checks.**

- **`visual`** loads `url` at `viewport` with login state. It can first run an optional `state_spec` to reach drawers, popovers or toasts [L-51]. It screenshots the `selector` or the viewport, and crops the frame PNG to the node `box`. The crop uses `render_box` and effect margins, so export shadows do not count [L-55]. The pixel diff must stay at or below `max_diff_ratio`. A size mismatch fails unless the check declares `fit` (the capture is scaled to the frame, recorded in evidence), and masks (each at most 2% of the area, with a reason) are listed in evidence. The app, design and diff images are the before/after record [L-56].
- **`style`** probes computed styles for the selectors in a style map. Expected values come only from frame facts, through a fixed vendor-neutral property table (box → width/height, fills → background or text color, strokes → border color/width, corner radii → border radii, layout → gap/padding, text → font, line height, letter spacing, alignment, content). `gate plan` rejects any value that is not a frame fact [L-187]. `factory design expect <frame> --node <id> --selector <css>` writes the skeleton. An empty expected text, a missing border against an expected width, and a `normal` line height all fail [L-54].
- `max_diff_ratio` and `tolerance_px` stay within `[limits]` ceilings that sit in the config hash [L-188].

**Approx rules.**

- Tolerances come from `limits.approx` (px, ΔE, font size, diff ratio).
- They follow the frame contract's fidelity, never a check field. An approx frame cannot replace an exact one with the same id.
- `factory design review <frame> --user-said` records the user's design review, bound to the frame hash, and `gate review` needs one per approx frame.
- The Gate S briefing lists approx frames and the user's design review.

**Gates.**

- **`plan`:**
  - Every ticket link with `rel: design` is covered by an ingested frame or by an answered question [L-190].
  - Each AC with `design` frames has at least one `visual` and one `style` check per frame [L-186].
  - Frames and images are valid, with matching raw hashes.
- **`verify`:** needs `visual`/`style` evidence like any runtime check.
- **`ship`:** `factory design refresh` re-acquires each exact frame after the last lock. A moved `source.version` or raw hash requires `plan amend` and re-verification [L-09, L-122].

## 10. Browser runner

The alpha bundles a minimal Node runner (`browser/`, plain JavaScript, about 500 lines on a separate budget of 900). It is optional for projects without UI, and `factory check` prints its install command when a plan needs it. It only renders, compares and probes: Python computes crop boxes and approx tolerances from contracts. The page can reach the env origin only; every other request is aborted.

**Contract:** `factory-browser <cmd> --out <dir> --base-url <url> [--storage-state <f>] [--viewport WxH] [--timeout-ms N] [--expect-timeout-ms N] [--headed]`.

| Command | Does |
|---|---|
| `login --spec <f> --state-out <f>` | runs an action spec with credentials from env vars only [L-80], keeps the storage state; ending on another origin is exit 3 [L-79] |
| `visual --url <path> --design <png> --crop x,y,w,h --max-diff-ratio R [--selector] [--mask x,y,w,h]... [--fit] [--fidelity]` | capture, crop the design, mask, diff; writes `app.png`, `design.png`, `diff.png` |
| `style --url <path> --map <f> [--px N --delta-e N --font-px N]` | computed-style probe against a style map |
| `screenshot [--url <path>] [--spec <f>] [--selector]` | capture only [L-56] |
| `spec --spec <f>` | a `.json` action spec, or a test file run by the browser library with the check's assertion timeout [L-59] |
| `batch --manifest <f>` | the entries `{id, argv, out, timeout_ms}` in one browser, one result each; one failing or stuck entry does not stop the others [L-57] |

Page flags of `visual`, `style` and `screenshot`: `--state-spec <f>` (repeatable), `--wait-for <css>`, `--settle-ms N`. A spec for `login`, `--state-spec` and a `.json` browser spec is `{"actions": [...]}` with actions `goto` (same origin), `click`, `fill` (values may be `${ENV_VAR}`), `press`, `wait`, `expect_visible`, `expect_text`, `expect_url` [L-51].

It writes `<out>/result.json` as `{status, summary, details, artifacts}`. **Exit codes:** 0 pass, 1 fail, 2 usage, 3 env (navigation or connection), 4 setup (browser missing). A watchdog enforces the timeout, and stdin is closed [L-42, L-44].

## 11. Environment

`[repo.env]` holds `up`, `down`, `health`, `ready_timeout_s`, `base_url`, `depends_on`, `files_from_main`, `vars` and `login_spec`.

- **Lock:** `.factory/env/<repo>.lock` is one lock per repo per machine, or per slot with `--slot n` and a `local.toml` port offset. There are no leases or TTLs, and `--wait` queues for the lock [L-75 to L-78]. A live holder is named in the refusal. A dead pid, or one with a mismatched create time, is a stale lock.
- **`env up`:**
  - It refuses when the health URL already answers [L-70].
  - It copies `files_from_main` without following links, never writes outside the worktree, removes copies that are not ignored, and fails with kind `files` when one is missing [L-64, L-65].
  - It starts `depends_on` first, from the run worktree when that repo is in the run, otherwise from the main path with a warning [L-71].
  - Each start gets its own process group (a hidden console on Windows) and a log file that is never empty [L-66].
  - The lock records pid, create time, group, cwd, start SHA and run [L-68, L-69].
  - Health means a status below 500 with no redirects followed, waited up to `ready_timeout_s` [L-73, L-84].
  - With `login_spec`, the runner writes login state to `.factory/env/`, per login user [L-81].
  - Optional `seed`, `reset`, `preflight`, `warmup` and `env_patterns` cover test data and cold servers [L-40, L-49, L-50].
  - A failure names its kind, the last 30 lines of the env log and one likely cause for that kind.
- **`env down`:** stops exactly the recorded group, and only when the create time matches [L-67, L-68]. Worktrees get their own dependencies through `setup`, and cleanup never follows links [L-72].

## 12. Contracts and acquisition

Contracts: `ticket`, `change`, `pipeline`, `design_frame`, `design_image`. Their schemas are fixed and validated in stdlib code. A null required field or an unmapped enum is an error [L-156]. There are no mapping DSL and no profiles [L-120, L-124, L-125].

1. **Adapter:** `[providers.<kind>] fetch` runs via `factory fetch <kind> <id>`. The template is split into argv before substitution, with no shell [L-130], stdin closed and a timeout. The CLI validates stdout JSON, stores the raw, and records `source: adapter` with the raw hash [L-121, L-122].
2. **No adapter:** the agent saves the raw and writes the contract, and `factory ingest` validates it as `source: agent` [L-123].

Ticket text and design frames may be `agent`. A remote fact used as proof (a change readback or pipeline) needs `adapter` or a recorded user confirmation (`factory confirm`) [L-21]. A raw that drifted from its hash fails any gate reading it [L-122]. Adapters live outside the core (decision 4).

## 13. Reviews, roles, ship, metrics

**Roles.** The orchestrator decides, talks to the user, runs gates, does remote writes and pushes. The builder writes code and checks and runs `verify`. The reviewer starts fresh and is never the builder. Roles are advice, and recorder hints are only an audit trail [L-127, L-128, L-170].

**Review.** `factory review record --kind code|qa|uat --file f` binds the review to full SHAs (a prefix of 7 or more characters is accepted, and a bad `repos` shape is rejected with an example [L-107]) and to `core_hash` (code) or `plan_hash` (qa). It refuses:

- APPROVE with gaps, not-checked items or blocker/major findings [L-23]
- `qa` without per-AC `exercises`, or APPROVE with `exercises: false` [L-22]
- `code` without a `changed_values` list [L-39]

Resolution ids are `round:id` [L-147]; a finding re-listed in a later round names the earlier one in `carries`, and resolving it resolves the chain. Every finding has `scope` `"change"` (default: fixed before ship, minor ones too; a round of minor fixes, or an amend that closes them with `--resolves`, spends no loop) or `"out"` (not this delivery: kept in the run, never in the change).

**UAT.** A plan with a screen (a browser, visual, style or screenshot check) takes a third review, `uat`, after code and qa, by a reviewer who wrote neither. It explores the screens logged in through scripts under `<run>/uat/` run by `factory uat run` (evidence with mode `uat`: screenshots and output, never a check result), cites those runs in `explored`, and finds what the checks do not look for; a defect the change causes is a finding even outside the ACs.

**Ship.** `factory ship plan [--ticket none|comment|transition]` writes neutral intents: push (with `force_with_lease` when it rewrites a pushed branch, after a rebase or a fold), change, readback, pipeline (optional), and ticket comment or transition, never auto-run [L-145]. It refuses while the branch is behind its base (a moved base comes in by rebase, `ship.base_update`) or still has `fixup!` commits, which are folded into their targets first. The title is `[key] - summary` [L-142]; reviewer identities are checked by `factory check` [L-143]. The body is `ship/summary.md`, written for a human reviewer who never sees the run, in the project's language; it is refused while empty, without the ticket key, with a local link [L-144] or with the run's bookkeeping (check, AC or decision ids, attempts, run paths). The AC × check × result table, the commits-behind count [L-108], findings outside the delivery and human checks go to the Gate S briefing for the user [L-146].

The agent executes with the user's tools and saves readbacks. `ship verify` compares title, `draft` [L-154], branches, head SHA and that the description is the planned body [L-153], and de-duplicates identical states [L-151]. `autonomy.ship = "ask"` asks before any push; `"draft-auto"` may push and open drafts. Ready and merge are never automatic.

**Metrics.** `factory metrics` derives these from events on demand [L-179]:

- time per phase, with reproduce separate [L-178]
- verify time
- gate failures
- loops by kind [L-134]
- `blocked_hours` [L-180]
- human events by type [L-177]
- escaped blocker/major findings [L-181]

The core never parses transcripts. Tokens come only from an optional `[metrics] tokens` adapter [L-182, L-183].

## 14. Module map and budgets

Python 3.11 or later, stdlib only, with modules capped at 400 lines each.

| Part | Lines |
|---|---|
| Base modules below | 5,450 |
| Parity modules (`docs/parity-map.md` §2: `quality`, `feedback`, `mirror`, `epic`, `review_run`, `envdata`, `envlock`, `human`/`screenshot`, plus growth within caps) | 2,970 |
| **Python core total** | **8,420** |
| Node runner | 820 |

The caps of decision 16 replace PIVOT §3. On 2026-11-25 the owner raised the core cap from 8,500 to 10,000 lines (quality over the cap; it rises again when needed). The table above is the original estimate.

| Module | Lines | Module | Lines |
|---|---|---|---|
| `config` | 300 | `verify` (order, reuse, reproduce, red-green, `--mirror`) | 380 (378 now; cap 400) |
| `project` (discovery, init, check) | 200 | `git` (worktrees, branch point, clean) | 300 |
| `run` (events, state) | 250 | `env` | 400 |
| `contracts` (validation, fetch, ingest) | 300 | `gates` | 350 |
| `plan` (validation, lock, amend, loops) | 350 | `review` | 200 |
| `proc` (spawn, timeout, tree kill, classify) | 250 | `ship` | 350 |
| `checks/cmd`, `checks/http`, `checks/browser` | 150, 150, 120 | `metrics` | 200 |
| `checks/visual`, `checks/style` | 120, 120 | `next` | 300 |
| `design` (frames, PNG header, table, expect, crop box, approx, freshness) | 400 | `cli` | 370 |

M6 modules (lines then): `mirror` 176, `mirrordiff` 38, `epic` 180, `review_run` 146, `slots` 58, `cli_composite` 85. Core now about 8699 lines of 10,000 (`uat` and the delivery rules came after M7).

Command overhead is at most 1 s, excluding checks. The CLI installs through the standard Python entry point, apart from any working tree, with an optional version pin [L-130, L-149] (decision 3).

## 15. Test strategy

- **Budget:** the Python suite runs in at most 180 s in parallel on Windows and Linux CI, per proposed decision 16 [L-162, L-173].
- **Runner:** a stdlib runner pools modules with a hard per-module timeout and full output to a file [L-44, L-174].
- **Isolation:** temp directories, no network, port 0 only [L-174].
- **Fixtures:**
  - Fixture git repos are built once, then copied per test.
  - Fake adapters, the env server and a fake browser runner implementing section 10's contract are Python scripts.
  - Design fixtures are tiny frames and PNGs written with stdlib `zlib`.
  - Paths contain spaces and apostrophes [L-162].
- **Composite runs:** one end-to-end fixture each for hotfix, epic, review and feedback, driven through `next`.
- **Node runner:** its own suite, at most 90 s, runs a headless browser against local static pages on both OSes. It covers crop margins, size mismatch, masks, and the probe failure rules [L-54, L-55].
- **Lesson mechanism:** each `test` row of `LESSONS.md` maps to at least one test tagged `@lesson("L-nn")`. A meta-test fails on an untagged `test` row or a tag on a non-`test` row.
- **Structural tests:**
  - the grep ban [L-172]
  - no reset, stash, clean or force-push in git call sites [L-106]
  - adapters at most 100 lines [L-171]
  - module ceilings
  - a decoy package in the cwd [L-148]
  - one end-to-end run through `next` [L-26]

## 16. Parity and milestones

The alpha keeps every v0 capability (owner decision). `docs/parity-map.md` lists the 76 v0 capabilities and commands, each with its v1 redesign, lessons, line estimate and milestone. The few drops are only where v1 makes the capability unnecessary:

| Dropped | Replaced by |
|---|---|
| profiles and migrations | adapters |
| write guards and digests | evidence binding |
| `rewind` | SHA binding |
| `prove --fast` | reuse plus ordering |
| `target_paths` | `depends_on` |

Milestones:

| M | Content |
|---|---|
| M1 | project, run and contracts |
| M2 | plan and `cmd` verify |
| M3 | review and ship on a real single-repo ticket (stop) |
| M4 | workspace, env, `http`, `browser`, login and design |
| M5 | feedback, human checks, quality baseline and metrics |
| M6 | hotfix, epics, review runs, sessions and slots (stop) |
| M7 | adapters and release; `1.0.0-alpha` means full parity |
