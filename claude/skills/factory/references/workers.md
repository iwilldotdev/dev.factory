# Workers: who, how to ask, how to read replies

| Worker | Model | Use for | Writes |
|---|---|---|---|
| `factory-scout` | haiku | Targeted lookups: code search, git history, any dev.mcp read whose answer is a few facts | Only a report under `runs/<id>/notes/` (or `runs/_inbox/<session>/` before a run exists) when asked |
| `factory-author` | sonnet (opus on high risk for diagnosis) | Intake pack (dev.mcp reads with `save_to`), `acs.json`, checks, style maps, Figma pack, bug diagnosis | `runs/<id>/{acs.json,checks,figma,remote,notes}`, docs, temp scratch |
| `factory-builder` | sonnet | Code in the run worktree, red tests, fixes from prove/review/feedback | Worktree + commits, `runs/<id>/checks` |
| `factory-auditor` | sonnet; **opus on high risk** | `code` and `qa` reviews, recorded with `FACTORY_ROLE=auditor factory review record` (only this role may set it) | Temp scratch, `runs/<id>/notes` |

- Use `factory-scout` instead of the built-in Explore.
- A worker that fails twice on sonnet: retry once with `model: opus`, then block.
- Builders: one fresh builder per batch of ~5–7 tasks; one writer at a time on the same files. Parallel builders only on different repos/worktrees.
- Auditors: always a new agent per review and per round. Never resume an auditor for another round. Never use the agent that wrote the code or the checks.
- dev.mcp payloads reach disk only through the tool's `save_to` (absolute path under `FACTORY_HOME/runs`, `overwrite: true`). Workers never paste a payload into a reply, never re-create it with Write, and read saved packs with targeted Grep/Read. Put the CLI's `FETCH` lines in the request.
- Hooks enforce the scopes above (`worker-guard.js`) and protect run files for everyone (`protect.js`). A denied tool call is a signal to re-plan, not to work around.

## Infra failures: the blocked contract

Time lost after the first infra failure (retrying `env up|check|prove`, waiting on timeouts) is the biggest cost of a run. Rules for every worker, repeated in the agent files:

- Infra signal = any `ENV-FAIL repo=<alias> kind=<unhealthy|stale|port|lease|start|timeout|usage> ...` line, `factory prove` exit 3 or `PROVE-FAIL check=<id> kind=env|timeout ...`, `factory env check` exit other than 0 (1 unhealthy, 3 stale, 4 port/lease conflict), a port/login/health failure, a missing tool or SDK.
- After the first signal: at most ONE self-repair that the docs allow (re-run `factory env check [repo] --run <run>` once; `prove` already restarts a stale env once; `env up|down`, `env lease` and `--auto-restart` belong to the main session, guard-enforced), then hand back in the same turn: `status: blocked`, `infra_signal: <the line>`, `first_seen_at: <time>`, `tried: [<commands in order>]`.
- Never wait on a timeout, never loop on the same failing command, never poll with `sleep` (denied): use `factory wait --file <path> [--contains <text>] | --cmd "<cmd>" --until-exit 0 | --url <url> [--status 2xx|3xx] [--timeout S] [--poll S]` (exit 0 satisfied, 1 timeout, one line).
- Never restart or stop an env you did not start. The request says who started it; when it says "env: up by orchestrator", a stale or unhealthy env is a `blocked` hand-back, not a restart.
- `hooks/failfast.js` enforces it: on the 2nd same-kind infra failure it tells the worker to hand back; a further `factory env up|check|prove` is denied. A healthy `env check` or a passing `prove` resets the count.
- Orchestrator on `blocked` with `infra_signal`: do not re-delegate the same task into the same env. Run `factory env check` yourself, fix or ask the user (`run block <kind>`), then delegate again with the new preflight result.

## Request format

Under ~120 words. Point to files; do not paste them.

```yaml
run: <run-id>
factory_home: <FACTORY_HOME>
task: <one sentence>
repo: <alias> @ <worktree path> (base <target>)    # builder/auditor
fetch: [<FETCH lines from the CLI, run as-is with save_to>]               # author/scout
inputs: [<paths: acs.json, checks, figma/<node>.json, notes/diagnosis.md, evidence/CHK-n.json>]
commit: {header_regex: "<from workspace>", forbidden_trailers: [...]}   # builder
rules: [<workspace rule ids + text; auditor also gets kind: guideline rules, text only>]   # builder/auditor
design: <R/notes/design.md when present>                               # builder/auditor
kind: code | qa ; round: <n> ; review_file: <temp path>                 # auditor
risk: normal | high
env_preflight: <last `factory env check` result: repo, exit code, ENV-FAIL line or "healthy", who started it>   # runtime-dependent work
budget: <minutes>                                                       # hard cap; at the cap hand back `blocked` with what you have
how_to: <reference path when the task follows one, e.g. references/figma-pack.md>
```

## Reply contract (all workers)

The orchestrator runs `factory env check` (preflight) before delegating any worker that needs a running env (browser/api/visual proof, prove fixes, QA review) and puts the result in `env_preflight`. A failing preflight is fixed or asked about first; workers are never sent into a known-bad env. Every request carries a `budget` (default: 10 min for a fix, 15 for plan/author work, 20 for a review); a worker that has not finished at the budget hands back `blocked` rather than keep trying.

Final message is only a YAML block: `status` (`done | blocked | failed`), `summary` (≤3 lines), `artifacts`, `evidence` (commands with exit codes, paths), plus role keys: author `questions`, builder `commits` and `red`, auditor `completeness` and `findings`. Evidence-or-zero: unverified claims are `not-checked`.

## Handling replies

- `blocked` with `infra_signal`: see the blocked contract above; relay the signal and what you do next in one line.
- `done`: verify cheaply (`git log --oneline -n`, `factory run status`, file exists, gate exit code). A report is a claim.
- `blocked` with `questions`: merge them into the single question round; do not ask piecemeal.
- `failed`: read the reason; re-delegate with the missing input, or block the run.
- Replan caused by a new user decision: first send a `factory-scout` to check whether the current code already satisfies it (cite file:line); dispatch a builder only for the gap.
- Final decision reports come from `run.json` decisions (`factory run log`), never from memory.
- Fix loops tell builders to use `factory prove <run> --fast`; you run `--final` once before `gate prove`. Timing/selector-only check fixes: `factory run amend-check`, not a replan.
- Summarise each worker reply in 1–2 chat lines (status, what changed, what is open): the user does not see the reply itself.
- A `human` check that the user confirms in chat is recorded by you, never by a worker: `factory run verify-human <run> <CHK-id> --note "<what the user confirmed>" --by user --user-said "<gist of the user's words>"`. Only after the user actually said it this conversation.
- A question or assumption that reduces proof (`human` check, `ui`→`logic`, no `env up`, no Figma fetch) goes to the user in the question round, with the failed attempt when it claims something is inaccessible.
