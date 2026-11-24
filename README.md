# dev.factory

Ticket in, proven changes out. dev.factory takes a ticket or epic and delivers reviewed change requests whose acceptance criteria, UI included, are **proven by executed evidence**, not by model claims. It is provider-agnostic: trackers, source-control hosts, pipelines and design sources are reached through whatever MCP server or CLI you already use, and a declarative **profile** in `FACTORY_HOME/profiles` tells the factory how to read their payloads.

It succeeds dev.harness. The rule is **thin prose, thick mechanisms**: a short Claude Code skill orchestrates, and a stdlib Python CLI with collectors and gates decides. See [DESIGN.md](DESIGN.md) for the contracts.

## How it works

| Layer | What |
|---|---|
| `claude/skills/factory/` | Orchestrator runbook for the main session: phases intake → plan → build → prove → review → ship → feedback, one batched question round, ship authority |
| `claude/agents/` | Workers: `factory-scout` (haiku, lookups), `factory-author` (ACs, checks, design frames, diagnosis, provider profiles), `factory-builder` (code), `factory-auditor` (fresh, independent reviews) |
| `claude/hooks/` | `worker-guard.js` (role limits per worker) and `protect.js` (all sessions: nobody hand-writes run state, evidence, reviews, or baseline) |
| `factory/` | `factory` CLI: workspaces, runs, epics, env, provider profiles and `ingest`, design frames, baseline, `prove` (collectors write evidence), gates, review records, ship plan/verify (intents), feedback ingest, metrics |
| `browser/` | Node + Playwright collectors: browser specs, visual diff against the design frame, computed-style probes |

A phase ends only when `factory gate <run> <phase>` passes. Evidence is bound to the commit SHA it was collected at, and a dirty or stale record does not count.

**Epics** are a parent run plus one child run per ticket. `epic.json` declares repos and dependencies, and `factory epic plan` computes waves. Ready children run in parallel, each with its own worktree, gates, and loops, and a blocked child does not stop independent ones. Same-repo dependents branch from their dependency, which gives stacked change requests with `Depends on` links. `factory epic ship plan` orders every change and builds the single epic ticket comment. `factory epic status` is the progress view.

**Review runs** cover change requests the factory did not build: ask for your review queue, a change URL, or tickets in code review. `factory review queue --list <file>` filters the saved open-changes list (drafts, your own changes, ones you approved), `factory review start --change <alias>#<number>` (or a change URL) creates a review run (or a batch with one child per change), and `factory run branch --change <number>` checks out the change head in a detached worktree. The acceptance criteria come from the ticket, not the diff; the same collectors and two fresh auditors produce the evidence, and `factory review report` gives `ok`, `not-ok`, or `incomplete` per change. Nothing is posted to the SCM or the tracker unless you say so, and `factory review fix` turns a run into a normal ticket run on the change's branch.

**Parallel sessions.** With `orchestration.sessions` on in the workspace, a batch (epic or several changes) opens one Claude session per ticket that you can open and follow in the app. The parent session only coordinates and shows `factory batch status`. A child claims its runs (`factory run claim`) and narrates every phase. Only one local environment fits the fixed ports, so children take turns through `factory env lease`; everything else runs in parallel. `factory run log <run>` prints a run's timeline. See DESIGN.md §11.

## Threat model

dev.factory defends against accidental or lazy false PASS by a cooperative model (claims without execution, checks that cannot fail, setup failures taken for reproductions, caps or checks loosened to go green, stale proof or reviews) and against workers exceeding their role. It does **not** defend against a deliberately adversarial orchestrator: all state is local files writable by the same OS user, so forged run.json events, hand-written raw payloads and files hidden by `.git/info/exclude` are accepted residual risks. Tamper-proof proof would need `prove` to run in another trust domain (a CI job); that is future work. See DESIGN.md §0.

## Requirements

- Python ≥ 3.10 (stdlib only), Node ≥ 20, git.
- Claude Code. For each provider (tracker, SCM, design source) either an MCP server whose read tools can save a payload to a file path (a save argument such as `save_to`) or a read-only CLI whose output can be redirected to a file. The factory ships no provider knowledge: see "Provider profiles" below.
- `browser/`: `npm ci && npm run build` (Playwright browsers: `npx playwright install chromium`).

## Install into a Claude profile

```sh
python install.py --profile ~/.claude --dry-run            # see every action
python install.py --profile ~/.claude                      # install
python install.py --profile ~/.claude --remove-harness     # back up and remove dev.harness first
```

The installer:

- copies the skill, agents (hook paths filled in), and hooks;
- merges the `protect.js` PreToolUse hook into `settings.json`, keeping other keys;
- writes the delegation block into `CLAUDE.md` between `<!-- dev.factory:start -->` and `<!-- dev.factory:end -->`;
- writes `bin/factory` and `bin/factory.cmd` shims and creates `FACTORY_HOME` (default `~/.factory`, or `--factory-home`);
- renders the agents' `tools:` lists: each `factory-*` agent file carries a `{{MCP_TOOLS}}` placeholder that becomes `mcp__<server>__*` for every MCP server configured for the target profile (server names only, read from `--mcp-config <file>`, else `~/.claude.json` for `~/.claude`, else `<profile>/.claude.json`; `--mcp-servers a,b` overrides), and **prints the servers it used**. With none configured the agents get no MCP entries and the factory works with CLI acquisition. A server added later needs a re-install.

The installer does not change any MCP server's own configuration. An MCP tool that saves payloads to disk must be allowed to write under `FACTORY_HOME/runs`: set that in the server's own settings, then restart Claude Code.

Re-running it is safe. Add `<profile>/bin` to `PATH`.

**The CLI runs from a snapshot.** The installer copies the runtime (`factory/`, `browser/dist`, `package.json`, and a link to the repo's `browser/node_modules`, so build the browser first with `npm ci && npm run build`) into `<profile>/factory-dist/` with a `provenance.json` (source path, git SHA, dirty flag, time). Editing this repo, switching its branch or leaving a half-finished change no longer affects live runs: **changes take effect only after you re-run `install.py`**. The previous snapshot is kept in `<profile>/backups/`. `factory --version` shows the sha and whether it runs from the snapshot; it (and `factory run status`) warns on stderr `installed snapshot is behind source (run install.py)` when the repo moved on. To test a checkout live without installing, set `FACTORY_SRC=<path to the checkout>` for that command (both shims honour it); unset it to go back to the snapshot. If the snapshot (or the `FACTORY_SRC` tree) has no `factory/` package, both shims stop with `factory: no factory/ package in <root>` and exit 2; they never fall back to the current directory. `install.py` stops before touching hooks or settings when it cannot replace a snapshot that a running process holds open (close shells with their cwd in it and running envs, then re-run). If `browser/dist` is older than `browser/src` the installer prints a warning (it does not run the build) and the behind-notice repeats it. Known limit of `factory.cmd`: pass arguments that contain `&`, `|`, `^` or `%VAR%` quoted (`factory x "a&b"`), because `%*` is re-parsed by cmd.

Why: the agent never pastes provider payloads. When a gate lacks a remote file, the CLI prints a `FETCH` line with the raw path and the profile's acquisition hint, for example `ticket ACME-12 -> C:/Users/x/.factory/runs/<id>/raw/ticket/ACME-12.json | acme-cli ticket get ACME-12 --out C:/Users/x/.factory/runs/<id>/raw/ticket/ACME-12.json` (`--json` lists them as `fetch: [{kind, id, raw_path, hint}]`). The agent saves the untouched payload there with any tool and runs `factory ingest`. `protect.js` also denies saves aimed at run state, evidence, reviews, baseline, contracts, or the review report; `worker-guard.js` denies any MCP call a worker makes that is not provably a read, and any shell command that sends a remote write (DESIGN.md section 5).

`--remove-harness` backs up to `<profile>/backups/harness-<timestamp>/` and removes `dev-*` skills, agents, the harness guard, `dev-harness-delegation.md`, and the harness sections of `CLAUDE.md`. Linked skills (symlinks or junctions) are unlinked; their targets are never touched. If a profile folder is itself a link shared with other profiles, changes inside it need `--yes`.

## Provider profiles

Providers are data, not code. A profile (`FACTORY_HOME/profiles/<name>/profile.json` plus real sample payloads under `samples/`) declares, per kind (`ticket`, `change`, `change_list`, `pipeline`, `design_frame`, `design_image`), how a raw payload maps to a neutral, validated **contract**. It also carries the `refs` that recognise ticket keys and change URLs, the acquisition `hints` printed on `FETCH` lines, and the `cli_write_patterns` / `mcp_read_allow` / `save_args` the guard hooks use. Workers write profile DRAFTS under `FACTORY_HOME/profile-drafts/<name>/`; only `factory profile approve <name> --digest <draft digest from check> --user-said "<gist>"` (main session) promotes one to `profiles/<name>/` with an `approved.json` whose sha256 gates and ingest verify. Gates and collectors read contracts only; a contract is valid only while the sha256 of its raw file is unchanged.

```sh
factory profile new acme-tickets --role tickets   # scaffold + the authoring workflow
factory profile test acme-tickets                 # contract, unknowns and per-field source for each sample
factory profile test acme-tickets --golden        # diff against samples/<kind>/<n>.expected.json (--write-golden saves them)
factory profile check acme-tickets                # valid iff every declared kind maps a sample; also lints
factory ingest <run> ticket <raw>                 # raw -> contracts/ticket/<id>.json with provenance
factory ingest <run> --check                      # list stale contracts
```

The workspace names the profile per role: `"providers": {"tickets": "acme-tickets", "scm": "acme-scm", "design": "acme-design"}`. Authoring is a guided task: the author worker asks you for the provider and real sample payloads (never invented), researches ambiguous fields in the provider's public API documentation, shows you the contract and its `unknowns`, and records your approval. See `claude/skills/factory/references/profile.md` (workflow, mapping language, profile fields) and `migration.md` (moving in-flight runs and workspaces with `factory migrate apply`).

## Playbook

How to use dev.factory after installing it. You talk to Claude Code; the `factory` skill runs the CLI. The commands below are shown so you know what happens and can check on a run yourself. Contracts and edge cases are in [DESIGN.md](DESIGN.md).

### 1. Set up a workspace (once per product)

A workspace describes your repos, how to start each one locally, and how to ship. It lives in `FACTORY_HOME/workspaces/<ws>/workspace.json`, outside your product repos.

```sh
factory workspace init acme                        # empty skeleton
factory workspace import acme --from <profile.json> # or convert a dev.harness .dev/project.json
factory workspace check acme                        # lists every missing field
```

Fill what `workspace check` reports:

| Key | What |
|---|---|
| `worktree_root` | Where run worktrees are created, for example `C:/Code/wt` |
| `repos[]` | `alias`, `scm_project` (the SCM's project path), `path` (main clone), `quality` (typecheck, lint, test commands), `env` (`start`, `health`, `base_url`, `ports`, optional `login` selectors, `hot_reload`, `files_from_main` (gitignored files such as `.env` copied from the main checkout into every new worktree), `warmup` (paths requested once after a cold `env up`, e.g. a dev server's first page, so the first check does not hit a cold server), `seed` {cmd, cwd, reset}, `preflight` [{name, cmd} or {name, request: {path|url, expect_status, expect_body_contains, expect_json, auth: "storage"}}], `slots` + `port_stride` (several runs of the repo at once, each on its own ports), `shareable` (runs that do not change the repo share one process), `login.handoff_rewrite`; `env_vars` and `start` may use `{base_url:<ALIAS>}` / `{port:<ALIAS>}` to point at another repo's instance in the run's slot), optional `quality_fast` (scoped quality for `prove --fast`, commands may use `{changed}`), optional `target_paths` ({target: absolute dir}: where a hotfix run serves that target for a repo without a run worktree; the hotfix base key applies only to base-target runtime, a mirror key to `--target <mirror>`; unused in other runs), optional `ui` (`true`/`false`: is this a UI repo, for the visual-evidence question; set it explicitly on every repo with an `env` block, `workspace check` warns otherwise and fails if it is not a boolean) |
| `providers` | `{tickets, scm, design}`: profile names in `FACTORY_HOME/profiles` |
| `ship` | `targets` (branches changes go to), `draft_default`, `ticket_mode` (`none`, `comment`, `transition`, `comment+transition`), `ticket_comment: "change-links"`, `ticket_transition` (a status name), `reviewers` (`{scm_user, ticket_user}`), `title_format`, optional `hotfix: {base, mirror}` |
| `branch_format`, `commit.header_regex` | Branch names and commit header rule |
| `autonomy` | `ship: ask` or `draft-auto`, loop caps |
| `rules[]` | Project checks that run in every prove, for example forbidden imports |
| `limits` | Optional caps: `max_diff_ratio`, `max_tolerance_px`, `approx` (tolerances for approximate design frames), `check_timeout_s`, `expect_timeout_ms`, and `browser_batch` (`false` runs every browser check in its own Playwright process; default: checks of one repo run together in one) |
| `orchestration` | `{sessions: true, per: "ticket"}` turns on one app session per ticket for batches (section 4) |
| `env.lease` | `true` when several runs share the local env: each repo is leased on its own (runs that touch different repos run together, `slots` give a repo several instances, `shareable` repos are shared); runs queue for a repo that is taken |

Credentials go in `FACTORY_HOME/secrets/<ws>.env`, never in chat: `<ALIAS>_USER` and `<ALIAS>_PASS` (or plain `USER`/`PASS`), plus `<ALIAS>_PASS_<slug>` for a ticket-specific user. Run `factory workspace check acme` again until it prints `OK`.

### 2. Pick a mode

Start a **new session** in the product repo and say what you want. The skill recognises these requests:

| You want | Say | What you get |
|---|---|---|
| Deliver a feature ticket | `/factory DEV-123` or a ticket URL | Branches per repo, proven ACs, two reviews, draft change requests after your yes |
| Fix a bug | `/factory DEV-123` (ticket type bug), or "this is a bug" | Same, plus a reproduction that fails on the base before any fix, and `diagnosis.md` |
| Hotfix on two targets | "hotfix DEV-123" | One run: fix on the base target, cherry-picked to the mirror target, proven on both, changes cross-linked |
| Deliver an epic | `/factory EPIC-10` (an epic key) | One child run per ticket in dependency waves, stacked change requests with `Depends on` links, one ticket comment |
| Review one change | "review" plus the change URL | Verdict `ok` / `not-ok` / `incomplete` against the ticket's ACs, with evidence |
| Review several changes | several URLs, "review my queue", or "tickets in code review" | A batch run with one child per change (or per ticket, in sessions) and a verdict table |
| Fix what a review found | "fix it" after a review | `review fix` turns the review run into a normal run on the change branch |
| Resume | "resume DEV-123" or the run id | Continues from `factory run status`'s next action |

Risk is raised to `high` (Opus auditors, both review rounds) when the work touches auth, money, data, migration, concurrency, a public contract or security. You can also say "high risk".

### 3. What happens, and where it stops for you

Every mode goes through phases, and each phase ends only when `factory gate <run> <phase>` passes:

```
intake → plan → [reproduce: bugs] → build → prove → review → ship → feedback → done
review runs: intake → plan → prove → review → report → done
```

It stops for you at four points only:

1. **The question round, before the plan gate**, as a single message. It covers missing env or credentials, targets, product decisions, and any worker assumption that would weaken the proof, such as a check left to a human, a UI AC without a visual check, or a skipped design fetch or `env up`. "Inaccessible" comes with the failed attempt quoted. When the run touches UI, it also asks whether you want **visual evidence**: before/after screenshots of a fix (the base and the fixed HEAD, same steps), or screenshots of the implemented screens for a feature. A yes is enforced. The gates require the screenshots, and Gate S shows them to you.
2. **Blockers** that only you can clear: access, an env that will not start, an exceeded loop cap.
3. **Gate S, before ship.** You see the changes, ACs against evidence, the remaining `human` checks, approximate design frames, the before/after screenshots you asked for, and the ticket comment and transition, each as an intent of `ship-plan.json` (push, change request, readback, ticket comment, ticket transition). Nothing is pushed until you say yes. `autonomy.ship: draft-auto` pre-authorises only push, readbacks and draft change requests.
4. **Review runs never post.** The verdict stays local until you ask for a change comment or a ticket comment.

Anything you say while it runs counts as steering. It is recorded with `factory run human` and acted on. Changing the ACs or the workspace after the plan gate needs a replan, and the skill handles it.

When you confirm a `human` check yourself ("I tested it, the PDF looks right"), say so in chat. The skill records it with `factory run verify-human`, and it stops counting as an open review finding.

**Blocking problems surface fast.** Before handing runtime work to a worker, the orchestrator runs `factory env check` as a preflight. Each browser check has a cap of 120 s by default, and an env failure skips the remaining runtime checks instead of waiting on each timeout. A worker that hits an infra failure gets one repair attempt, then must report `blocked` with the exact `ENV-FAIL` line, and a hook enforces this. So "the env won't start" reaches you within minutes, not after a long silence.

### 4. Batches in parallel sessions

With `orchestration.sessions` on and two or more tickets (an epic, or several changes), the session you started becomes the **parent**:

- After the intake and the single question round, it opens **one linked session per ticket** in the app sidebar, titled `DEV-123 · review` or `DEV-123 · build`. Open any of them to watch the phases and worker summaries, or to steer it.
- Each child runs `/factory session <run-ids>`, claims its runs (`factory run claim`) and narrates every phase.
- For UI proof, a child takes its turn on the local env (`factory env lease acquire --wait`), brings it up from its worktree, runs the browser, visual and style collectors, and explores the screens in the app's built-in browser against the design frames. The exploration produces findings and specs, never a PASS.
- The parent only coordinates: a `factory batch status` matrix after each child reports, the parent report, and every remote write after your ok.

Without the app's session tool, for example in the plain CLI, the same batch runs with hidden workers, but still narrates every phase.

### 5. Follow a run

| Command | Shows |
|---|---|
| `factory run list` | Every run with its phase |
| `factory run status <run>` | Phase, loops, open blockers, owner session and the next required action |
| `factory run mirror-diff <run> [--json]` | Hotfix runs: whether each mirror branch carries the same changes as the base fix (exit 0 equal, 1 divergent, with the divergent files and hunks); `gate prove` (and `gate build` after a rewind) and `ship plan` enforce it |
| `factory env check <ws> [--run <run>] [--target <t>]` | Whether the env is healthy, stale, or blocked by a port or the lease (exit 0/1/3/4); runs `env.preflight` after the health probe. Hotfix runs: `env up ... --run <run> --target <mirror>` serves the mirror worktree, `prove <run> --target <mirror>` proves only that target |
| `factory env seed\|reset-data <ws> <repo> --run <run>` | Run `env.seed.cmd` / `env.seed.reset` with `FACTORY_DATA_PREFIX=UAT<ticket digits>`; `prove` resets once before the runtime batch |
| `factory workspace list` / `workspace show <ws>` | Workspaces and their config, with secrets redacted |
| `factory run log <run> --tail 30` | The timeline of gates, evidence, reviews, steering and lease events |
| `factory batch status <parent>` | Children of an epic or review batch: phase, next gate, blockers, open `human` checks, owner, lease holder |
| `factory env lease status <ws> [--repo A]` | Who holds each repo slot, who shares it, and who is queued |
| `factory metrics <run>` / `factory metrics report` | Wall time, human messages, gate failures, loops |

Run artefacts live in `FACTORY_HOME/runs/<run>/`: `acs.json`, `evidence/`, `reviews/`, `review-report.md` and `ship-plan.json`. Hooks keep them CLI-written.

### 6. Clean up

```sh
factory run delete <run...> --yes          # zip into FACTORY_HOME/archive/runs, verify, then remove
factory run delete <parent> <children...> --yes --force   # unfinished runs need --force
git -C <main clone> worktree remove <worktree>             # worktrees are listed, not removed
factory env lease release <ws> --run <run>                 # free the lease (and stop that run's envs)
factory run close <run> --reason "abandoned"               # end a run you will not finish; then delete it
```

### 7. When a gate says…

| Message | Meaning | What to do |
|---|---|---|
| `FETCH <kind> <id> -> <raw_path> \| <hint>` | A remote file is missing | Nothing: the skill runs the hint with the tool it names, saves at `raw_path`, then runs `factory ingest` |
| `workspace changed after plan; replan` | `workspace.json` was edited after the plan lock | Let the skill replan, with your decision recorded |
| `the proof no longer matches the worktrees` | A commit or edit came after prove | `factory run rewind <run> prove "<reason>"`, then prove again |
| `env … already up … for <other worktree>` / `lease held by …` | Another run has that repo or slot | Wait in the queue, or `env down` and release it if that run is finished |
| `run … is claimed by session …` | Another session drives this run | Continue there, or `factory run claim <run> --takeover` |
| `missing secrets file` / login failure | Credentials not set | Add the keys to `FACTORY_HOME/secrets/<ws>.env` |
| `ENV-FAIL repo=… kind=unhealthy\|start\|timeout` | The env did not come up or answer | Fix what `detail=` names (port, SDK, DB), then `factory env check <ws> --run <run>` |
| `ENV-FAIL … kind=db` | A response body showed a database or connection failure ("SSL Authentication Error"), not a product bug | Restart the API or fix the connection, then re-run `prove` |
| `flaky: CHK-n` in `run status`, `FLAKY:` in `prove` | The check failed earlier at the same sha and check digest and passes now (history in `evidence/history/`) | Not a gate failure; stabilise the check or the env warm-up |
| `ENV-FAIL … kind=data` | An `env.preflight` request found stale or expired test data (for example the test user's project) | Fix the data `detail=` names (reset the dump), then re-run `env check` / `prove` |
| `ENV-FAIL … kind=files` | a file listed in `env.files_from_main` is missing in the served worktree | Create it (copy from the main checkout) and re-run `env up` / `prove` |
| `ENV-FAIL … kind=seed\|preflight` | `env.seed` command or an `env.preflight` entry exited non-zero | Fix the data or DB state `detail=` names, then re-run `env check` / `prove` |
| `ENV-FAIL … kind=stale` | The env runs an older commit | `factory env check <ws> --run <run> --auto-restart` (prove also does it once) |
| `ENV-FAIL … kind=port` | The port is held by something factory did not start | Stop that process yourself. Factory never kills it |
| `PROVE-FAIL check=… kind=timeout` | A check hit its cap | Fix the check, or raise `timeout_s` on it |
| `no tests matched` | A test filter ran zero tests | Fix the filter in the check |

CLI help for everything else: `factory --help`, `factory <command> --help`.

## Tests

```sh
python -m unittest discover -s tests
node --test claude/hooks/test/
cd browser && npm test
```

## License

MIT, see [LICENSE](LICENSE).
