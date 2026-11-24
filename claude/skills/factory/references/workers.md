# Workers: who, how to ask, how to read replies

| Worker | Model | Use for | Writes |
|---|---|---|---|
| `factory-scout` | haiku | Targeted lookups: code search, git history, any MCP or CLI read whose answer is a few facts | Only a report under `runs/<id>/notes/` (or `runs/_inbox/<session>/` before a run exists) when asked |
| `factory-author` | sonnet (opus on high risk for diagnosis) | Intake pack (raw payloads saved and ingested), `acs.json`, checks, style maps, design frames, bug diagnosis, provider profiles ([profile.md](profile.md)) | `runs/<id>/{acs.json,checks,design,raw,notes}`, `FACTORY_HOME/profile-drafts/<name>/**` (profile DRAFTS only), docs, temp scratch |
| `factory-builder` | sonnet | Code in the run worktree, red tests, fixes from prove/review/feedback | Worktree + commits, `runs/<id>/checks` |
| `factory-auditor` | sonnet; **opus on high risk** | `code` and `qa` reviews, recorded with `FACTORY_ROLE=auditor factory review record` (only this role may set it) | Temp scratch, `runs/<id>/notes` |

- Use `factory-scout` instead of the built-in Explore.
- A worker that fails twice on sonnet: retry once with `model: opus`, then block.
- Builders: one fresh builder per batch of ~5–7 tasks; one writer at a time on the same files. Parallel builders only on different repos/worktrees.
- Auditors: always a new agent per review and per round. Never resume an auditor for another round. Never use the agent that wrote the code or the checks.
- Provider payloads reach disk untouched at `R/raw/<kind>/<id>.<ext>`: an MCP tool's save argument (absolute path under `FACTORY_HOME/runs`, overwrite allowed), a read-only CLI with its output redirected, or `factory ingest --stdin`; then `factory ingest` maps them to contracts. Workers never paste a payload into a reply, never re-create it with Write, and read saved raws and contracts with targeted Grep/Read. Put the CLI's `FETCH` lines in the request.
- **Worker guard.** Hooks enforce the scopes above (`worker-guard.js`) and protect run files for everyone (`protect.js`). Inside a worker every `mcp__<server>__<tool>` call is classified from the tool name: READ by one rule: tokens are scanned left to right, any write verb anywhere makes it a WRITE, otherwise the first read verb (get, list, search, read, fetch, find, lookup, query, describe, show, view, download, count, export, ...) makes it READ, so vendor or namespace prefixes are tolerated; no known verb is a WRITE (`normalize_*` is WRITE unless allowed); a profile's `mcp_read_allow` can certify an exception. Anything else is a WRITE and is denied ("remote writes belong to the main session"), fail-closed, including unknown or ambiguous names. Shell commands are denied when they are a generic remote write (HTTP client sending a body or a write method, `git push`) or match a profile `cli_write_patterns` regex. Saves are allowed only under `R/raw/**`; `R/contracts/**` is written by the CLI alone. Agent `tools:` lists carry `mcp__<server>__*` entries rendered at install time for the servers configured then (a new server needs a re-install). Added write verbs: drop, destroy, purge, upsert, insert, patch, put, import, comment, reply, dismiss, mark, deploy, execute, invoke, sync, lock, unlock, toggle, rerun, enable, disable, clone (full list in `guard-lib.js`). Save destinations are found by argument NAME at any nesting depth (`save`, `out`, `output`, `dest`, `target_path`, `file_path` in any casing, plus profile `save_args`), so `savePath` or `opts.save_to` is judged like `save_to`. Shell commands are judged per command segment after normalisation: `VAR=1` assignments, `env`/`command`/`exec`/`nice`/`time`/`sudo`/`xargs` prefixes, `bash -c` / `sh -c` / `pwsh -Command` bodies, `( ... )`, `$( ... )` and backticks are unwrapped before the generic and profile patterns (and the reserved `factory` verbs) apply; the generic patterns cover bundled curl flags (`-sXPOST`), `--data-*`, `-T`, wget `--method`/`--post-*`, httpie `http POST` / data items, and PowerShell `Invoke-RestMethod|Invoke-WebRequest|irm|iwr` with `-Method Post|Put|Patch|Delete` or `-Body`. **Profile drafts vs approved profiles.** A worker (author, builder) writes a provider profile only as a DRAFT under `FACTORY_HOME/profile-drafts/<name>/**`; `FACTORY_HOME/profiles/**`, `FACTORY_HOME/workspaces/**` and `FACTORY_HOME/migrations/**` are main-session-only (migration map drafts go under `FACTORY_HOME/migration-drafts/`) (denied by every route: Write/Edit, shell redirects, `cp`/`mv`/`tee`/`sed -i`/interpreter writes, MCP save arguments). The main session approves with `factory profile approve <name> --digest <draft digest from check>`, which writes `profiles/<name>/approved.json` with the sha256 of `profile.json`. The guard reads a profile (`cli_write_patterns`, `mcp_read_allow`, `save_args`) only when `approved.json` exists and its `file_sha256` matches the current `profile.json` bytes; an unapproved or edited profile is ignored (generic rules only, with a stderr note) and drafts are never read. Reserved for the main session: `factory migrate` (all subcommands), `factory profile approve`, and `factory profile new` without `--draft`; workers may run `factory profile new --draft`, `profile test` and `profile check`. A denied tool call is a signal to re-plan, not to work around.

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
fetch: [<FETCH lines from the CLI: save at raw_path with the hinted tool, then factory ingest>]   # author/scout
inputs: [<paths: acs.json, checks, contracts/design_frame/<id>.json, notes/diagnosis.md, evidence/CHK-n.json>]
commit: {header_regex: "<from workspace>", forbidden_trailers: [...]}   # builder
rules: [<workspace rule ids + text; auditor also gets kind: guideline rules, text only>]   # builder/auditor
design: <R/notes/design.md when present>                               # builder/auditor
kind: code | qa ; round: <n> ; review_file: <temp path>                 # auditor
risk: normal | high
env_preflight: <last `factory env check` result: repo, exit code, ENV-FAIL line or "healthy", who started it>   # runtime-dependent work
budget: <minutes>                                                       # hard cap; at the cap hand back `blocked` with what you have
how_to: <reference path when the task follows one, e.g. references/design-pack.md or references/profile.md>
```

## Reply contract (all workers)

The orchestrator runs `factory env check` (preflight) before delegating any worker that needs a running env (browser/api/visual proof, prove fixes, QA review) and puts the result in `env_preflight`. A failing preflight is fixed or asked about first; workers are never sent into a known-bad env. Every request carries a `budget` (default: 10 min for a fix, 15 for plan/author work, 20 for a review); a worker that has not finished at the budget hands back `blocked` rather than keep trying.

Final message is only a YAML block: `status` (`done | blocked | failed`), `summary` (≤3 lines), `artifacts`, `evidence` (commands with exit codes, paths), plus role keys: author `questions`, builder `commits` and `red`, auditor `completeness` and `findings`. Evidence-or-zero: unverified claims are `not-checked`.

## Handling replies

- `blocked` with `infra_signal`: see the blocked contract above; relay the signal and what you do next in one line.
- `done`: verify cheaply (`git log --oneline -n`, `factory run status`, file exists, gate exit code). A report is a claim.
- `blocked` with `questions`: merge them into the single question round; do not ask piecemeal.
- **Cap on consecutive `blocked`: 2.** Two `blocked` hand-backs in a row from the same worker role on the same step (same task, same inputs) end the resending: the third request is never the same request. The main session re-scopes the task (smaller or different inputs, another worker, a different split), changes the approach (replan, another check, another env), or asks the user (`run block <kind>` plus one batched message). Say which of the three in the chat line. An infra `blocked` follows the blocked contract above instead.
- `failed`: read the reason; re-delegate with the missing input, or block the run.
- Replan caused by a new user decision: first send a `factory-scout` to check whether the current code already satisfies it (cite file:line); dispatch a builder only for the gap.
- Final decision reports come from `run.json` decisions (`factory run log`), never from memory.
- Fix loops tell builders to use `factory prove <run> --fast`; you run `--final` once before `gate prove`. Timing/selector-only check fixes: `factory run amend-check`, not a replan.
- Summarise each worker reply in 1–2 chat lines (status, what changed, what is open): the user does not see the reply itself.
- A `human` check that the user confirms in chat is recorded by you, never by a worker: `factory run verify-human <run> <CHK-id> --note "<what the user confirmed>" --by user --user-said "<gist of the user's words>"`. Only after the user actually said it this conversation.
- A question or assumption that reduces proof (`human` check, `ui`→`logic`, no `env up`, no design fetch) goes to the user in the question round, with the failed attempt when it claims something is inaccessible.
