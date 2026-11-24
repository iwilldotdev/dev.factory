# Sessions: parent and child

A batch (epic or review-batch) is followed in the Claude desktop app as linked sessions: one **parent** session that coordinates, and one **child** session per **ticket** (covering that ticket's API and React runs). The user can open, read and steer each one. The Progress and Worker questions rules in SKILL.md apply to both.

Parallel children are capped by `orchestration.max_parallel` (default 3): start at most that many child sessions at once and start the next as one finishes. A child that stops for any reason (usage limit, block, user stop) releases its lease and env first: `factory env down <ws> <repo> --run <id>` for envs it started, then `factory env lease release <ws> --run <id>`; a held lease of a stopped child blocks everyone else.

Only one run at a time holds a given repo slot of the local env (per-repo env lease; `slots` and `shareable` repos let several runs share the env). ACs, unit/api checks and code review run in parallel; `env up` plus browser/visual proof wait their turn.

## When

- `workspace.json` has `orchestration: {sessions: true, per: "ticket"}` **and** the batch has 2+ tickets → session mode.
- Epic with 3+ tickets or expected to exceed a working day, `sessions` not set: recommend it to the user anyway (context limits; one session loses detail on compaction). Use one session only if they decline.
- Compaction/resume brief (parent or child): include the `factory batch status <parent>` output and the open decisions/blockers from run.json.
- Otherwise (single ticket, no opt-in) → normal run in this session with workers.
- `mcp__ccd_session__start_session` missing (plain CLI, not the app) or refused → fallback: subagent mode as in epic.md / review.md, still following the Progress rule (task list per child, one line per phase/worker, worker summaries). Say in one line that sessions were unavailable and why.

## Parent mode

1. **Intake the batch**: `factory review start …` (review-batch) or `factory epic start` + `epic plan` (epic). Print `factory-run: <parent-id>` once.
2. **One consolidated question round** for every ticket (env gaps, credential keys, targets, design source access, worker questions that reduce proof). Record each answer in the affected children (`run human`, `run unblock`, `run decide --by user --user-said`), so the child sessions read them from the run, not from this chat.
3. **Start the sessions**: `mcp__ccd_session__mark_chapter` "Sessões do lote" (batch sessions), then one `mcp__ccd_session__start_session` per ticket (`initiation: "user_asked"` when the user asked for the batch run; else `"offer"`):
   - `context: "fresh"`, `use_worktree: false` (run worktrees already exist under `FACTORY_HOME`);
   - `title`: `DEV-XXXX · review` or `DEV-XXXX · build` (the ticket key);
   - brief: workspace, the ticket's run ids (API and React), their worktree paths, the recorded user decisions (decision numbers + one-line gist), the risk level, and the instruction to run `/factory session <run-id...>`. Nothing else; the child reads the rest from the runs.
   - Write a bold title line and one sentence before each call. For an epic, start only the children of the ready wave; start the next wave's sessions when `batch status` shows them ready.
4. **Then only coordinate.** Do not run child phases here.
   - On each `<side-sessions-event>`: `factory batch status <parent>` and reply with a compact matrix (ticket, phase, next gate, blockers, open `human` checks, owning session, lease holder). Check a child's claim against the CLI before relaying it ("a note is a claim").
   - A child blocked on a user question: batch it with any other open questions, ask once, record the answer in that child (`run human` / `run decide --by user`), then `SendMessage` the child session that it may continue.
   - `factory run log <child> --tail 20` when you need to see what a child did.
   - Parent gates and report: `factory gate <parent> …`, `factory review report <parent>` / `factory epic ship plan|verify <parent>`.
   - **Every remote write** (change comment, approval, ticket comment/transition, push, change create) happens here, and only with the user's explicit ok (Gate S for builds, `remote_actions` for reviews).
5. Finish: `factory metrics <parent>` and the short report from SKILL.md. Offer to archive the child sessions only on the user's yes.

## Child mode (`/factory session <run-id...>`)

1. `factory run claim <run-id...>`. If it refuses because another live session holds a run, stop and tell the user (`--takeover` only on the user's say-so).
2. Print `factory-run: <id>` once per run.
3. Task list = the phases of each run; follow the Progress rule. Use your own fresh workers (workers.md); the parent's workers are not yours.
4. Run the phases with their gates exactly as a single ticket (phases.md, review.md). Read the decisions the parent recorded with `factory run status` / `factory run log`; a new question that only the user can answer: `run block`, then one short `SendMessage` to the parent session and end the turn.
5. **No remote writes, no writes to the parent run.** Reads saved raw into your own run folders (then `factory ingest`) are fine.
6. **UI phase** (browser/visual/style checks, React runs):
   1. `factory env lease acquire <ws> --run <id> --wait` in the background, announced with the queue position it prints. Keep doing non-env work (ACs, unit/api checks, code review) while waiting.
   2. With the lease: `factory env up <ws> <repo> --run <id>`, `factory env login <ws> <repo> --run <id>` (`--user <email>` when the ticket names one), then `factory prove <id>` for browser/visual/style/screenshot. Bug run whose user said yes to visual evidence: take the lease before any fix commit too, for `factory prove <id> --repro --assume-env` on the clean worktree (before screenshots), then release it.
   3. **Exploratory pass** in the built-in browser (`mcp__Claude_Browser__*`, read the `built-in-browser` skill first): open the local React app, walk the ticket's flows, compare with the design frames. Output: findings (screen, step, expected vs actual, design frame) in `notes/exploration.md`, and Playwright specs with the real selectors you saw, handed to the author as checks. The exploratory pass is **never a PASS**: only `factory prove` evidence and gates count.
   4. `factory env down <ws> <repo> --run <id>`, then `factory env lease release <ws> --run <id>`. Release also on block or failure; never hold the lease while waiting on the user.
   - Design source or env "inaccessible" needs the failed `factory design check` / `factory env up` output recorded (`run block`), not an assumption.
7. **End**: a short summary per run (phase reached, gates passed, ACs proven vs `human`, findings, blockers) and the verdict (`ok` / `not-ok` / `incomplete` for reviews; change-ready for builds). The parent receives it as the session note, so put the verdict in the first lines.
