# Migrating in-flight runs and the workspace

A run or workspace created before the contract layout keeps old file names and old key names. The factory ships one generic tool and no provider knowledge: `factory migrate apply <run> <map.json> --dry-run`, then `factory migrate apply <run> <map.json> --digest <sha256> --user-said "<gist>"` (or `--all <map.json>`). The agent writes the **map** (it knows the old names; the repo does not) and keeps it outside the repo. A worker (author or builder) drafts it under `FACTORY_HOME/migration-drafts/<name>.json`; `FACTORY_HOME/migrations/**` is main-session-owned (workers cannot write there), so the main session copies the draft that the user approved to `FACTORY_HOME/migrations/<name>.json` (or applies the draft file directly).

## When to migrate

- Runs in `intake` or `plan` (nothing locked, or a lock that is cheap to redo): migrate.
- Runs in `build` or later: migrate only when the user prefers it to finishing on the old release. Ask in the question round.
- `ship-plan.json` of an older schema is not migrated: `factory ship plan` refuses it ("re-run ship plan"); the plan is derived data.
- Evidence and reviews are not touched: they reference checks and commit heads, not remote payloads.

## Map format

A JSON list of operations (or `{"ops": [...]}`), applied in order. Every operation is idempotent, so a map can be re-run after fixing it; `run.json` is saved only when all operations succeeded.

| Op | Fields | Effect |
|---|---|---|
| `rename_key` | `file`, `path`, `to` | Rename the key at `path` (supports `[*]`, must end in a key) in `file`; `file: "run.json"` edits the run itself (non-CLI-owned keys only). Refuses when `to` already exists beside it. |
| `move_file` | `from`, `to` | Move files matching `from` (a run-relative pattern, `*` captured, `{1}`, `{2}` in `to`). Same content at the destination: the source is dropped; different content: error. |
| `ingest` | `kind`, `raw`, optional `repo`, `profile`, `format` | Run the normal `factory ingest` on every file matching `raw` (id = file stem) with the workspace profile of the kind's role, writing `contracts/<kind>/<id>.json`. |
| `replace_text` | `file`, `from`, `to` | Literal replace in the KEYS of a JSON file (not `run.json`); plain-text replace for other file types. |
| `ws_rename_key` | `path`, `to` | Rename a key in the run's `workspace.json` (once; later runs find it already renamed). |
| `ws_replace_text` | `from`, `to` | Literal replace in the keys of the run's `workspace.json`. |

After the operations the tool marks the run `contracts_schema: 2`, re-seals the plan lock when the run has one (same sequence number, current workspace digest), and records an event `migrate {map_sha256, old_lock, new_lock}`. This re-seal is a deliberate, recorded exception to "locked after plan"; show the user the old and new lock digests.

## Guardrails (main session only)

`factory migrate apply` is **main-session only** (workers cannot run it; the hooks deny it) and is a two-step flow: (1) `--dry-run` prints the map's sha256 and, per run, every operation with its target file (matches of `move_file`/`ingest` patterns, refused targets marked `REFUSED`) and writes nothing; show that output to the user; (2) after a yes, `--digest <sha256 printed by the dry run> --user-said "<gist of the user's approval of this map>"` applies it. Apply refuses a missing or different digest (the map changed after the user looked). The map's sha256 and the gist are recorded as a user decision and in the `migrate` event.

- Refused targets (nothing is applied): any file or pattern under `contracts/`, `evidence/`, `reviews/`, `baseline/` (re-ingest or re-run the collectors instead); a wildcard first path segment; `replace_text` on `run.json`; `rename_key` on `run.json` of CLI-owned fields (`events`, `plan_lock`, `decisions`, `proved`, `evidence`, `reviews`, `blockers`, `sessions`, `phase`, `contracts_schema`, `schemaVersion`, `id`, `rev`, `integrity`, `closed`).
- Before applying, the plan lock must be clean (`lock_errors` empty): a run whose plan files or workspace already drifted is refused, so the re-seal can only absorb the map's own operations. Fix the drift or replan first.

## Procedure (agent)

1. Prerequisite: the profiles the workspace needs exist and pass `factory profile check` (`references/profile.md`), and the workspace names them in `providers`.
2. List what the old run has: `ls` the run folder, Grep key names in `run.json`, `acs.json`, `workspace.json`. Write down every old file pattern and key.
3. Write `FACTORY_HOME/migrations/<name>.json` mapping each old name to its new one. Typical groups:
   - run state keys (`rename_key` on `run.json`, path with `[*]`: a list of change records, an issue-number key);
   - raw payload files (`move_file` from the old folder to `raw/<kind>/{1}.<ext>`), then `ingest` of each kind (ticket before change; design image before design frame);
   - check fields in `acs.json` (`replace_text` on the old file-reference key);
   - workspace keys (`ws_rename_key`, `ws_replace_text`) for the neutral names: per-repo SCM project path, SCM user, ticket mode/comment/transition, reviewer identities.
4. Dry-read: apply to ONE run first (`factory migrate apply <run> <map.json> --digest <sha256> --user-said "<gist>"`, after `--dry-run`), check `factory run status <run>` and `factory gate <run> <phase>` for its phase, then `--all`.
5. Re-fetch what cannot be derived: an old raw payload that the profile cannot map (shape changed) is saved again with the `FETCH` hint the gate prints. Report each such file to the user.

## Synthetic example

Old run files `legacy/ticket-ACME-7.json` and `legacy/design-12_34.json`, an old key `changes_old` with `num`, a `file_ref` check field, an old workspace key `repo_path_remote`:

```json
[
  {"op": "rename_key", "file": "run.json", "path": "changes_old", "to": "changes"},
  {"op": "rename_key", "file": "run.json", "path": "changes[*].num", "to": "number"},
  {"op": "move_file", "from": "legacy/ticket-*.json", "to": "raw/ticket/{1}.json"},
  {"op": "ingest", "kind": "ticket", "raw": "raw/ticket/*.json"},
  {"op": "replace_text", "file": "acs.json", "from": "file_ref", "to": "source_file"},
  {"op": "ws_rename_key", "path": "repos[*].repo_path_remote", "to": "scm_project"}
]
```

Use only names read from the actual run; never copy this example's keys.

## After migrating

`factory workspace check <ws>` green; `factory gate <run> <phase>` for the run's current phase; `factory ingest <run> --check` shows no stale contract. A run whose `acs.json` still carries an old check key fails `gate plan` and names the op to add.
