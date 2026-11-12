# Workers: who, how to ask, how to read replies

| Worker | Model | Use for | Writes |
|---|---|---|---|
| `factory-scout` | haiku | Targeted lookups: code search, git history, any dev.mcp read whose answer is a few facts | Only a report under `runs/<id>/notes/` when asked |
| `factory-author` | sonnet (opus on high risk for diagnosis) | Intake pack (dev.mcp reads with `save_to`), `acs.json`, checks, style maps, Figma pack, bug diagnosis | `runs/<id>/{acs.json,checks,figma,remote,notes}`, docs, temp scratch |
| `factory-builder` | sonnet | Code in the run worktree, red tests, fixes from prove/review/feedback | Worktree + commits, `runs/<id>/checks` |
| `factory-auditor` | sonnet; **opus on high risk** | `code` and `qa` reviews, recorded with `factory review record` | Temp scratch, `runs/<id>/notes` |

- Use `factory-scout` instead of the built-in Explore.
- A worker that fails twice on sonnet: retry once with `model: opus`, then block.
- Builders: one fresh builder per batch of ~5–7 tasks; one writer at a time on the same files. Parallel builders only on different repos/worktrees.
- Auditors: always a new agent per review and per round. Never resume an auditor for another round. Never use the agent that wrote the code or the checks.
- dev.mcp payloads reach disk only through the tool's `save_to` (absolute path under `FACTORY_HOME/runs`, `overwrite: true`). Workers never paste a payload into a reply, never re-create it with Write, and read saved packs with targeted Grep/Read. Put the CLI's `FETCH` lines in the request.
- Hooks enforce the scopes above (`worker-guard.js`) and protect run files for everyone (`protect.js`). A denied tool call is a signal to re-plan, not to work around.

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
rules: [<workspace rule ids + text>]                                   # builder/auditor
kind: code | qa ; round: <n> ; review_file: <temp path>                 # auditor
risk: normal | high
how_to: <reference path when the task follows one, e.g. references/figma-pack.md>
```

## Reply contract (all workers)

Final message is only a YAML block: `status` (`done | blocked | failed`), `summary` (≤3 lines), `artifacts`, `evidence` (commands with exit codes, paths), plus role keys: author `questions`, builder `commits` and `red`, auditor `completeness` and `findings`. Evidence-or-zero: unverified claims are `not-checked`.

## Handling replies

- `done`: verify cheaply (`git log --oneline -n`, `factory run status`, file exists, gate exit code). A report is a claim.
- `blocked` with `questions`: merge them into the single question round; do not ask piecemeal.
- `failed`: read the reason; re-delegate with the missing input, or block the run.
- Relay what matters: the user does not see worker replies.
