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

## Requirements

- Python ≥ 3.10 (stdlib only), Node ≥ 20, git.
- Claude Code with the `dev.mcp` MCP server.
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

Re-running it is safe. Add `<profile>/bin` to `PATH`.

`--remove-harness` backs up to `<profile>/backups/harness-<timestamp>/` and removes `dev-*` skills, agents, the harness guard, `dev-harness-delegation.md`, and the harness sections of `CLAUDE.md`. Linked skills (symlinks or junctions) are unlinked; their targets are never touched. If a profile folder is itself a link shared with other profiles, changes inside it need `--yes`.

## Use

In Claude Code, mention a ticket or epic (`ABC-123`, a Jira URL) or say "factory". The skill starts or resumes the run, asks one batched question round (environment, credentials file keys, targets, unknowns), and works until ship. It asks before every remote write the ship plan marks `needs_user`. Credentials go in `FACTORY_HOME/secrets/<ws>.env`, never in chat.

CLI directly: `factory --help`, `factory run status <run>`, `factory epic status <parent>`, `factory metrics report`.

## Tests

```sh
python -m unittest discover -s tests
node --test claude/hooks/test/
cd browser && npm test
```

## License

MIT, see [LICENSE](LICENSE).
