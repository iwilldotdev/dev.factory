# dev.factory

Ticket in, proven MRs out. dev.factory takes a Jira ticket or epic and delivers reviewed GitLab MRs whose acceptance criteria, UI included, are **proven by executed evidence**, not by model claims. Remote work goes through the `dev.mcp` server (Jira, GitLab, Figma, Confluence).

It succeeds dev.harness. The rule is **thin prose, thick mechanisms**: a short Claude Code skill orchestrates, and a stdlib Python CLI with collectors and gates decides. See [DESIGN.md](DESIGN.md) for the contracts.

## How it works

| Layer | What |
|---|---|
| `claude/skills/factory/` | Orchestrator runbook for the main session: phases intake → plan → build → prove → review → ship → feedback, one batched question round, ship authority |
| `claude/agents/` | Workers: `factory-scout` (haiku, lookups), `factory-author` (ACs, checks, Figma packs, diagnosis), `factory-builder` (code), `factory-auditor` (fresh, independent reviews) |
| `claude/hooks/` | `worker-guard.js` (role limits per worker) and `protect.js` (all sessions: nobody hand-writes run state, evidence, reviews, or baseline) |
| `factory/` | `factory` CLI: workspaces, runs, epics, env, Figma pack checks, baseline, `prove` (collectors write evidence), gates, review records, ship plan/verify, feedback ingest, metrics |
| `browser/` | Node + Playwright collectors: browser specs, visual diff against Figma, computed-style probes |

A phase ends only when `factory gate <run> <phase>` passes. Evidence is bound to the commit SHA it was collected at, and a dirty or stale record does not count.

**Epics** are a parent run plus one child run per ticket. `epic.json` declares repos and dependencies, and `factory epic plan` computes waves. Ready children run in parallel, each with its own worktree, gates, and loops, and a blocked child does not stop independent ones. Same-repo dependents branch from their dependency, which gives stacked MRs with `Depends on !N`. `factory epic ship plan` orders every MR and builds the single epic Jira comment. `factory epic status` is the progress view.

**Review runs** cover MRs the factory did not build: ask for your review queue, an MR URL, or tickets in code review. `factory review queue` filters the saved GitLab list (drafts, your own MRs, ones you approved), `factory review start` creates a review run (or a batch with one child per MR), and `factory run branch --mr` checks out the MR head in a detached worktree. The acceptance criteria come from the ticket, not the diff; the same collectors and two fresh auditors produce the evidence, and `factory review report` gives `ok`, `not-ok`, or `incomplete` per MR. Nothing is posted to GitLab or Jira unless you say so, and `factory review fix` turns a run into a normal ticket run on the MR branch.

**Parallel sessions.** With `orchestration.sessions` on in the workspace, a batch (epic or several MRs) opens one Claude session per ticket that you can open and follow in the app. The parent session only coordinates and shows `factory batch status`. A child claims its runs (`factory run claim`) and narrates every phase. Only one local environment fits the fixed ports, so children take turns through `factory env lease`; everything else runs in parallel. `factory run log <run>` prints a run's timeline. See DESIGN.md §11.

## Threat model

dev.factory defends against accidental or lazy false PASS by a cooperative model (claims without execution, checks that cannot fail, setup failures taken for reproductions, caps or checks loosened to go green, stale proof or reviews) and against workers exceeding their role. It does **not** defend against a deliberately adversarial orchestrator: all state is local files writable by the same OS user, so forged run.json events, hand-written `remote/` readbacks and files hidden by `.git/info/exclude` are accepted residual risks. Tamper-proof proof would need `prove` to run in another trust domain (GitLab CI); that is future work. See DESIGN.md §0.

## Requirements

- Python ≥ 3.10 (stdlib only), Node ≥ 20, git.
- Claude Code with the `dev.mcp` MCP server, a version with the `save_to` option on its read tools (see its README, "Saving payloads to disk").
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
- writes `bin/factory` and `bin/factory.cmd` shims and creates `FACTORY_HOME` (default `~/.factory`, or `--factory-home`).

- adds the `save_to` settings to the `dev.mcp` entry of the Claude MCP config (`--mcp-config <file>`; default `~/.claude.json` for `~/.claude`, `<profile>/.claude.json` for other profiles when it exists): `DEV_MCP_SAVE_ROOTS=<FACTORY_HOME>/runs` plus `<tmp>/dev.mcp` (joined with the OS path delimiter), `DEV_MCP_SAVE_ALLOW_DOT=.factory`, `DEV_MCP_SPILL_KB=64`. Only missing keys are added, other keys are untouched, the file is backed up to `<profile>/backups/` first, and `--dry-run` prints the diff. Without a `dev.mcp` entry it prints a note; set the same variables in that server's `env` by hand. Restart Claude Code afterwards.

Re-running it is safe. Add `<profile>/bin` to `PATH`.

Why: the agent never pastes Jira, GitLab, or Figma payloads. When a gate lacks a remote file, the CLI prints the exact call, for example `figma_get_design_context fileKey=<k> nodeIds=3544:5710 save_to=C:/Users/x/.factory/runs/<id>/figma/3544-5710.json overwrite=true` (`--json` lists them as `fetch: [{tool, args}]`), and dev.mcp writes the file itself. `protect.js` also denies `save_to` aimed at run state, evidence, reviews, baseline, or the review report.

`--remove-harness` backs up to `<profile>/backups/harness-<timestamp>/` and removes `dev-*` skills, agents, the harness guard, `dev-harness-delegation.md`, and the harness sections of `CLAUDE.md`. Linked skills (symlinks or junctions) are unlinked; their targets are never touched. If a profile folder is itself a link shared with other profiles, changes inside it need `--yes`.

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
| `repos[]` | `alias`, `path` (main clone), `quality` (typecheck, lint, test commands), `env` (`start`, `health`, `base_url`, `ports`, optional `login` selectors) |
| `ship` | `targets` (branches MRs go to), `draft_default`, Jira transition, optional `hotfix: {base, mirror}` |
| `branch_format`, `commit.header_regex` | Branch names and commit header rule |
| `autonomy` | `ship: ask` or `draft-auto`, loop caps |
| `rules[]` | Project checks that run in every prove, for example forbidden imports |
| `orchestration` | `{sessions: true, per: "ticket"}` turns on one app session per ticket for batches (section 4) |
| `env.lease` | `true` when several runs share one local env with fixed ports: they queue for it |

Credentials go in `FACTORY_HOME/secrets/<ws>.env`, never in chat: `<ALIAS>_USER` and `<ALIAS>_PASS` (or plain `USER`/`PASS`), plus `<ALIAS>_PASS_<slug>` for a ticket-specific user. Run `factory workspace check acme` again until it prints `OK`.

### 2. Pick a mode

Start a **new session** in the product repo and say what you want. The skill recognises these requests:

| You want | Say | What you get |
|---|---|---|
| Deliver a feature ticket | `/factory DEV-123` or a Jira URL | Branches per repo, proven ACs, two reviews, draft MRs after your yes |
| Fix a bug | `/factory DEV-123` (Jira type Bug), or "this is a bug" | Same, plus a reproduction that fails on the base before any fix, and `diagnosis.md` |
| Hotfix homolog and pack | "hotfix DEV-123" | One run: fix on homolog, cherry-picked to pack, proven on both, MRs cross-linked |
| Deliver an epic | `/factory EPIC-10` (an epic key) | One child run per ticket in dependency waves, stacked MRs with `Depends on !N`, one Jira comment |
| Review one MR | "revisar MR" plus the MR URL | Verdict `ok` / `not-ok` / `incomplete` against the ticket's ACs, with evidence |
| Review several MRs | "revisar" plus several URLs, "review my queue", or "tickets em code review" | A batch run with one child per MR (or per ticket, in sessions) and a verdict table |
| Fix what a review found | "fix it" after a review | `review fix` turns the review run into a normal run on the MR branch |
| Resume | "resume DEV-123" or the run id | Continues from `factory run status`'s next action |

Risk is raised to `high` (Opus auditors, both review rounds) when the work touches auth, money, data, migration, concurrency, a public contract or security. You can also say "high risk".

### 3. What happens, and where it stops for you

Every mode goes through phases, and each phase ends only when `factory gate <run> <phase>` passes:

```
intake → plan → [reproduce: bugs] → build → prove → review → ship → feedback → done
review runs: intake → plan → prove → review → report → done
```

It stops for you at four points only:

1. **The question round, before the plan gate**, as a single message. It covers missing env or credentials, targets, product decisions, and any worker assumption that would weaken the proof, such as a check left to a human, a UI AC without a visual check, or a skipped Figma fetch or `env up`. "Inaccessible" comes with the failed attempt quoted.
2. **Blockers** that only you can clear: access, an env that will not start, an exceeded loop cap.
3. **Gate S, before ship.** You see the MRs, ACs against evidence, the remaining `human` checks, and the Jira comment and transition. Nothing is pushed until you say yes. `autonomy.ship: draft-auto` pre-authorises only push plus draft MRs.
4. **Review runs never post.** The verdict stays local until you ask for an MR or Jira comment.

Anything you say while it runs counts as steering. It is recorded with `factory run human` and acted on. Changing the ACs or the workspace after the plan gate needs a replan, and the skill handles it.

### 4. Batches in parallel sessions

With `orchestration.sessions` on and two or more tickets (an epic, or several MRs), the session you started becomes the **parent**:

- After the intake and the single question round, it opens **one linked session per ticket** in the app sidebar, titled `DEV-123 · review` or `DEV-123 · build`. Open any of them to watch the phases and worker summaries, or to steer it.
- Each child runs `/factory session <run-ids>`, claims its runs (`factory run claim`) and narrates every phase.
- For UI proof, a child takes its turn on the local env (`factory env lease acquire --wait`), brings it up from its worktree, runs the browser, visual and style collectors, and explores the screens in the app's built-in browser against Figma. The exploration produces findings and specs, never a PASS.
- The parent only coordinates: a `factory batch status` matrix after each child reports, the parent report, and every remote write after your ok.

Without the app's session tool, for example in the plain CLI, the same batch runs with hidden workers, but still narrates every phase.

### 5. Follow a run

| Command | Shows |
|---|---|
| `factory run list` | Every run with its phase |
| `factory run status <run>` | Phase, loops, open blockers, owner session and the next required action |
| `factory run log <run> --tail 30` | The timeline of gates, evidence, reviews, steering and lease events |
| `factory batch status <parent>` | Children of an epic or review batch: phase, next gate, blockers, open `human` checks, owner, lease holder |
| `factory env lease status <ws>` | Who holds the local env and who is queued |
| `factory metrics <run>` / `factory metrics report` | Wall time, human messages, gate failures, loops |

Run artefacts live in `FACTORY_HOME/runs/<run>/`: `acs.json`, `evidence/`, `reviews/`, `review-report.md` and `ship-plan.json`. Hooks keep them CLI-written.

### 6. Clean up

```sh
factory run delete <run...> --yes          # zip into FACTORY_HOME/archive/runs, verify, then remove
factory run delete <parent> <children...> --yes --force   # unfinished runs need --force
git -C <main clone> worktree remove <worktree>             # worktrees are listed, not removed
factory env lease release <ws> --run <run>                 # free the env if a run left it held
```

### 7. When a gate says…

| Message | Meaning | What to do |
|---|---|---|
| `FETCH <tool> … save_to=…` | A remote file is missing | Nothing: the skill runs that dev.mcp call as printed |
| `workspace changed after plan; replan` | `workspace.json` was edited after the plan lock | Let the skill replan, with your decision recorded |
| `the proof no longer matches the worktrees` | A commit or edit came after prove | `factory run rewind <run> prove "<reason>"`, then prove again |
| `env … already up … for <other worktree>` / `lease held by …` | Another run has the env | Wait in the queue, or `env down` and release it if that run is finished |
| `run … is claimed by session …` | Another session drives this run | Continue there, or `factory run claim <run> --takeover` |
| `missing secrets file` / login failure | Credentials not set | Add the keys to `FACTORY_HOME/secrets/<ws>.env` |

CLI help for everything else: `factory --help`, `factory <command> --help`.

## Tests

```sh
python -m unittest discover -s tests
node --test claude/hooks/test/
cd browser && npm test
```

## License

MIT, see [LICENSE](LICENSE).
