# Contracts, adapters, events and records (version 1)

All JSON is UTF-8. Hashes are lowercase hex sha256. "Canonical JSON" means sorted keys, `(",", ":")` separators and no trailing newline [L-167]. Timestamps are UTC ISO 8601 with a `Z` suffix. SHAs are always stored full (40 or 64 hex) [L-107].

## 1. Contract envelope

Contracts live in `contracts/<kind>/<safe-id>.json`. The CLI writes them through `fetch` or `ingest`.

```json
{
  "schema": "ticket/1",
  "source": "adapter",
  "raw": {"path": "raw/ticket/ABC-123.json", "sha256": "…"},
  "acquired_at": "2026-11-24T14:03:11Z",
  "adapter": {"cmd": "python ~/adapters/ticket.py ABC-123", "exit": 0, "duration_s": 1.4},
  "data": { }
}
```

| Field | Type | Req | Notes |
|---|---|---|---|
| `schema` | `ticket/1`, `change/1`, `change_list/1`, `pipeline/1`, `design_frame/1` or `design_image/1` | yes | |
| `source` | `adapter`, `agent` or `user` | yes | `user` = an agent-ingested contract that the user confirmed with `factory confirm <contract> --user-said "…"` [L-21, L-121] |
| `raw.path`, `raw.sha256` | string | yes | any gate that reads a contract whose raw hash differs fails [L-122] |
| `adapter` | object | only when `source` is `adapter` | the command is stored with secrets redacted |
| `data` | object | yes | per-kind fields below |

**Safe ids.** Characters outside `[A-Za-z0-9._-]` become `-`. If two ids map to the same safe name in one run, it is an error that names both [L-157].

**Proof grade.** A ticket or design frame may be `agent`: both are specification, not results. A `change` used by `ship verify` and a `pipeline` used by `gate ship` must be `adapter` or `user` [L-21].

**Validation.** A missing required field, a null required field, an unknown enum value or an unknown key is an error: exit 2, nothing written, and the message names the field and the allowed values [L-156].

## 2. Contract data

### `ticket/1`

| Field | Type | Req |
|---|---|---|
| `id` | string (the key people use) | yes |
| `url` | string | no |
| `title` | string | yes |
| `type` | `feature` or `bug` | yes; the adapter or agent maps provider types, and an unmappable type is an error [L-31] |
| `status` | string | yes (free text) |
| `description_md` | string (Markdown or plain text) | yes, may be empty |
| `acceptance` | list of strings | no |
| `labels` | list of strings | no |
| `attachments` | list of `{id, name, mime, size?, url?}` | yes, may be empty; `mime` is case-normalized, and the extension is used when it is missing [L-28, L-29] |
| `comments` | list of `{id, author, created_at, body_md}` | no |
| `links` | list of `{rel, target}` | no |

### `change/1`

| Field | Type | Req |
|---|---|---|
| `id` | string | yes |
| `url` | string | yes |
| `repo` | repo name in `factory.toml` | yes |
| `title` | string (without draft markers; `[providers.change] draft_title_marker`, a regex, strips one at ingest and sets `draft`) | yes |
| `description_md` | string | yes |
| `source_branch`, `target_branch` | string | yes |
| `head_sha` | full SHA | yes |
| `state` | `open`, `merged` or `closed` | yes |
| `draft` | bool, computed by the adapter [L-154] | yes |
| `reviewers` | list of SCM usernames | no |
| `approvals` | list of SCM usernames | no |
| `threads` | list of `{id, author, resolved: bool, body_md, file?, line?}` | no; feedback turns unresolved human threads into findings [L-20] |
| `pipelines` | list of pipeline ids, newest first | no |

### `change_list/1` (review queue)

| Field | Type | Req |
|---|---|---|
| `items` | list of `{id, url, repo, title, author, draft, ticket?, own?, approved?}`: `draft`, `own` (the user wrote it) and `approved` (the user approved it already) are computed by the adapter | yes; `review queue` drops the drafts, the own and the approved changes |

### `pipeline/1`

| Field | Type | Req |
|---|---|---|
| `id` | string | yes |
| `sha` | full SHA | yes; must equal the pushed head [L-155] |
| `status` | `pending`, `running`, `success`, `failed`, `canceled` or `skipped` | yes |
| `created_at` | timestamp | yes; used to pick the latest pipeline for the SHA |
| `url` | string | no |
| `jobs` | list of `{name, status, allow_failure}` | no |

### `design_frame/1`

| Field | Type | Req |
|---|---|---|
| `id` | string (frame id as the source writes it) | yes |
| `name` | string | yes |
| `fidelity` | `exact` or `approx` | yes; approx only from `factory design add` + agent data, and never replaces an exact frame with the same id [L-188] |
| `source` | `{file: string, version: string or null, fetched_at}` | yes; `version` required for `exact` (freshness) |
| `image` | `design_image` id | yes |
| `root` | Node | yes |
| `unknowns` | list of strings | yes, may be empty; values the adapter or agent could not map, never guessed [L-156] |

**Node fields:**

| Field | Type | Req |
|---|---|---|
| `id`, `name` | string | yes |
| `type` | `frame`, `group`, `text`, `shape`, `image`, `component`, `instance` or `other` | yes |
| `visible` | bool, default `true` | no |
| `box` | `{x, y, w, h}` in frame CSS pixels | yes |
| `render_box` | `{x, y, w, h}` including effects | no; drives the crop [L-55] |
| `opacity` | number from 0 to 1 | no |
| `fills`, `strokes` | list of `{type: solid, gradient or image; color: "#rrggbb"; opacity}`; strokes add `weight` and `align` | no |
| `effects` | list of `{type: drop_shadow, inner_shadow, layer_blur or bg_blur; color; offset {x, y}; radius; spread}` | no; drive the effect margins of the crop |
| `corner_radius` / `corner_radii` | number / `[tl, tr, br, bl]` | no |
| `layout` | `{mode: none, horizontal or vertical; gap; padding {t, r, b, l}}` | no |
| `text` | `{characters, font_family, font_size, font_weight, line_height_px, letter_spacing, align, color}` | no |
| `children` | list of Node | no |
| `estimated` | `{dotted path: true}` | required for `approx`: every number and color in the node must be listed; ingest walks them all and rejects an unflagged value |

**Property table** (fixed, vendor-neutral; used by `design expect` and by the style-map check at `gate plan` [L-187]):

| Node fact | Computed style |
|---|---|
| `box.w`, `box.h` | `width`, `height` |
| `fills[0].color` | `backgroundColor` (non-text node) or `color` (text node) |
| `text.color` | `color` |
| `strokes[0].color`, `strokes[0].weight` | `borderColor`, `borderWidth` |
| `corner_radius` | `borderRadius` |
| `corner_radii[0..3]` | `borderTopLeftRadius`, `borderTopRightRadius`, `borderBottomRightRadius`, `borderBottomLeftRadius` (plus `borderRadius` when all four are equal) |
| `opacity` | `opacity` |
| `layout.gap` | `gap` |
| `layout.padding.t, .r, .b, .l` | `paddingTop`, `paddingRight`, `paddingBottom`, `paddingLeft` |
| `text.font_family`, `.font_size`, `.font_weight` | `fontFamily`, `fontSize`, `fontWeight` |
| `text.line_height_px`, `.letter_spacing`, `.align` | `lineHeight`, `letterSpacing`, `textAlign` |
| `text.characters` | `text` (the element's text content) |

**Comparison rules:**

- Colors compare exactly, or within ΔE76 `limits.approx.color_delta_e` for approx frames.
- px values compare within `tolerance_px`, and font size within `limits.approx.font_size_px` for approx frames.
- `opacity` compares within 0.01.
- `fontFamily` compares on the first family. `text` must match exactly and must be non-empty.
- An expected `borderWidth` > 0 against a missing border fails, and so does a computed `lineHeight` of `normal` [L-54].

### `design_image/1`

| Field | Type | Req |
|---|---|---|
| `id` | string (same id as its frame) | yes |
| `width`, `height` | int (decoded from the PNG header) | yes |
| `scale` | `1` (CSS pixels) | yes |

The raw is the PNG itself (`raw/design_image/<id>.png`), and its sha256 is `raw.sha256`. A file that does not decode as PNG is refused. Conversion from other formats is the agent's job, recorded in the contract's `adapter` or `note`.

## 3. Raw payload storage

- **Adapter:** `raw/<kind>/<safe-id>.json` holds stdout byte for byte. The adapter may embed the provider payload under `raw`.
- **Agent:** the agent saves the provider payload to `raw/<kind>/<safe-id>.<ext>` with any tool, writes the contract data to a file, and runs `factory ingest <kind> --raw <file> --contract <file>`. `--stdin` reads the contract data from stdin, so nothing passes through chat [L-159].
- **Design:** `factory design add <png> --name N [--id ID]` copies the PNG to `raw/design_image/`, writes its contract and an approx frame skeleton. The agent then writes the frame data and runs `factory ingest design_frame`.
- **Media:** media files go under `media/`, and `factory media seen <attachment-id> --file media/<f>` or `factory media unseen <attachment-id> --question Q-n` records each one [L-28, L-29].

## 4. Adapter invocation contract

Configured as `[providers.<kind>] fetch = "<template>"`.

- **Placeholders:**

  | Placeholder | Value |
  |---|---|
  | `{id}` | entity id given to `factory fetch` |
  | `{repo}` | repo name |
  | `{repo_path}` | absolute repo path |
  | `{branch}` | run branch |
  | `{sha}` | HEAD |
  | `{run_dir}` | absolute run directory |
  | `{project_dir}` | absolute project directory |
  | `{out}` | absolute path where a binary raw must be written (`design_image`: the PNG); stdout still carries the contract JSON |

  The template is split into argv first (POSIX rules on every OS), then each placeholder is substituted inside its token. Values are never re-parsed, and no shell is involved [L-130]. An unknown placeholder fails `factory check`.
- **Process:**
  - stdin is closed [L-44].
  - The cwd is the project directory.
  - The environment is the user's, plus the provider's `secrets` keys and `FACTORY_KIND`, `FACTORY_ID` and `FACTORY_RUN_DIR`. Secrets never go into argv [L-80].
  - The forced English locale applies [L-45].
- **Stdout:** exactly one JSON object, `{"contract": {<data for the kind>}, "raw": <any JSON, optional>}`. Anything else is exit-2-class invalid output.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | success |
  | 3 | not found (reported as "no such <kind> <id>") |
  | 4 | auth or credentials (the reason names the missing secret key, never a value) |
  | other | adapter error; the last 20 stderr lines are shown redacted |
- **Timeout:** `timeout_s` (default 60). On expiry the process tree is killed and the result is `timeout` [L-42, L-43].
- **Network:** the CLI itself never does network I/O; the adapter is the user's code. The core ships no adapter (decision 4).

## 5. `events.jsonl`

Append-only, one canonical JSON object per line, written with an exclusive append lock [L-15, L-164, L-165].

Common fields on every event:

| Field | Meaning |
|---|---|
| `v` | `1` |
| `ts` | timestamp |
| `type` | event type below |
| `cli` | CLI version [L-150] |
| `actor` | optional free-text hint from the harness, audit trail only [L-127] |

| `type` | Extra fields |
|---|---|
| `run_started` | `ticket`, `kind`, `repos`, and `parent` (the epic run id) for a child run |
| `retype` | `kind`, `previous`: the run kind changed, so `gate intake` has to pass again [L-32] |
| `repos` | `repos`: the affected repos were (re)declared |
| `contract` | `kind`, `id`, `path`, `source`, `raw_sha256`; `refresh: true` on a design re-acquisition, and on a `change` or `pipeline` read again after the ship gate for the feedback phase (`fetch` or `ingest` with `--refresh`) |
| `media` | `attachment`, `state` (`seen` or `unseen`), `file` or `question` |
| `question` | `id` (`Q-n`), `text`, `ref` |
| `answer` | `question`, `user_said`, `decision` (id, consumable once) [L-126] |
| `decision` | `id` (`D-n`), `user_said` |
| `human` | `kind` (`answer`, `decision`, `env-hint` or `correction`), `text` [L-177] |
| `worktree` | `repo`, `path`, `branch`, `branch_point`, `throwaway` (bool), `setup` (`pass`, `fail` or `none`), and on failure `setup_exit`, `setup_tail`. A repeated event for the same repo means the setup ran again |
| `gate` | `name`, `result` (`pass` or `fail`), `reasons` (list); a passing `reproduce` adds `diagnosis` (sha256 of `diagnosis.md`) |
| `plan_locked` | lock fields from `plan-json.md`, `seq` (1 = initial lock) |
| `loop` | `kind` (`amend`, `review` or `feedback`), `decision` or null, `counts` (bool) [L-134, L-135]. A `review` loop adds `heads`, `round` and `review` (the kind): one fix round spends one loop however many reviewers found it. A loop that spends a recorded decision also closes the open `loop budget exhausted` blocker with an `unblock` event |
| `env` | `repo`, `action` (`up`, `down`, `restart` or `fail`), `pid`, `create_time`, `group`, `cwd`, `sha`, `run`; a `fail` adds `kind` (`files`, `lock`, `port`, `start`, `health`, `warmup`, `data`, `login` or `restore`) and a redacted `reason` |
| `evidence` | evidence record, §6 |
| `baseline` | `check`, `repo`, `spec_hash`, `branch_point`, `runs` (1 to 3), `stable` and `quarantined` (sorted failure ids) of a `quality` check at the branch point [L-61] |
| `review` | review record, §7; `kind: feedback` is written by `factory feedback`, §7b |
| `blocker` | `id`, `kind` (`question`, `decision`, `infra` or `tooling`), `reason` [L-140, L-152] |
| `unblock` | `blocker`, `decision` |
| `ship_planned` | `plan_sha256` (of `ship/plan.json`), `body_sha256`, `heads`, `update` (bool), `decision` (the consumed `--allow-behind` decision, or null) |
| `ship_verified` | `result`, `heads`, `mismatches`, `plan_sha256`; an identical state is not recorded twice [L-151] |
| `design_review` | `frame`, `frame_hash` (raw hash plus contract data), `user_said`: one per approx frame version, needed by `gate review` [L-186] |
| `design_refresh` | `frame`, `version`, `raw_sha256`, `changed` (bool), `user_said` when no adapter re-acquired it: an exact frame checked after the last lock, needed by `gate ship` [L-09, L-122] |
| `confirm` | `check` or `contract` (`<kind>:<id>`), `user_said`, `heads` (human checks and user-confirmed facts). A contract confirmation covers only the ingest before it. A check confirmation is followed by its `evidence` record (§6) [L-21, L-24] |
| `waiver` | `pipeline` (an id), `user_said`: covers that pipeline only, never a newer one [L-20] |
| `claim` | `session`, `takeover` (bool) [L-184] |
| `slot` | `slot` (int): the run's env slot, set by `env up --slot` [L-78] |
| `dismiss` | `source` (`thread:<id>`), `finding`, `decision` (consumed), `user_said`: the user decided the thread needs no change, so it no longer counts [L-20, L-126] |
| `epic_planned` | `children` (`ticket`, `kind`, `repos`, `depends_on`), `waves` (lists of tickets), `batch` (true for a review batch, whose children depend on nothing and end at their report) [L-185] |
| `review_report` | `verdict` (`ok`, `not-ok` or `incomplete`), `summary`, `findings`, `heads`, `actions` (the intent names written to `report/plan.json`; none runs by itself) [L-185] |
| `tokens` | `session`, `input`, `output` [L-183] |
| `mirror` | `repo`, `target`, `branch`, `path`, `branch_point` (the target tip), `target_branch`, `ticket`, `commits`, `result` (`applied` or `conflict`) [L-93, L-95]. `result: accepted` adds `decision`, `files` (the divergent files the user's decision names) and `heads` (`base`, `mirror`): it covers those commits only. The mirror tree is not a `worktree` event, so the base worktree stays the run's |
| `closed` | `reason` [L-103] |

## 6. Evidence record (`type: evidence`)

```json
{"v":1,"ts":"…","type":"evidence","cli":"1.0.0a1",
 "check":"CHK-2","spec_hash":"…","repo":"api","mode":"verify","attempt":3,
 "sha":"<full HEAD>","worktree":"D:/wt/shop/ABC-123/api","clean":true,
 "heads":{"api":"…","web":"…"},
 "served":{"api":{"path":"D:/wt/shop/ABC-123/api","sha":"…","pid":4312,"create_time":1764000000.12}},
 "cmd":"GET /api/invoices?status=overdue","exit":null,"duration_s":0.41,
 "tail":"status 200; expect_json mismatch at $.items[0].status",
 "result":"fail","kind":"assert","reason":"expect_json mismatch at $.items[0].status",
 "artifacts":["artifacts/CHK-2/3/"],"hash":"<sha256 of canonical record without hash>"}
```

- `mode`: `verify`, `reproduce`, `red` or `green`. `red` and `green` records add `fix` (the full SHA of the fix commit; `sha` is then `fix^` or `fix`). A retry that passes after an assertion failure is the `flaky` record of the next attempt and adds `retry_of`. The implicit commit check has `check` `<repo>:commits`.
- `result`:

  | `result` | `kind` | Notes |
  |---|---|---|
  | `pass` | | |
  | `fail` | `assert` | |
  | `setup` | `setup` | |
  | `timeout` | `timeout` | |
  | `env` | `env` | |
  | `error` | `stale` or `internal` | |
  | `flaky` | | both attempts recorded [L-14, L-139] |
  | `skipped` | | reason `env` [L-46] |
  | `reproduced`, `not_reproduced` | | reproduce and red modes only [L-03, L-05] |
- `heads` and `served` are present only for runtime checks [L-12, L-69]; `warnings` lists dependencies served from a main path [L-71]. A runtime check that finds its env stale restarts it once, then ends `error` with kind `stale`; after an `env` result the repo's other runtime checks are `skipped` [L-46]. `verify` exits 3 for any of these.
- `visual` records add `details {diff_ratio, max_diff_ratio, crop, size: [w, h], masks (with their reasons), fidelity, scale (only with fit), reason (size or diff)}`, with artifacts `app.png`, `design.png` (the crop) and `diff.png` [L-55, L-56].
- `style` records add `details {fidelity, results: [{selector, property, expected, actual, ok, note}]}`.
- `quality` records add `details {scope (full or scoped), new (up to 50 ids), total, baseline_stable, quarantined}`. A `scoped` record (`verify --scoped`) is evidence of the run but never satisfies a gate [L-60].
- A `human` record is written by `factory confirm <check>`, not by `verify`: `cmd` is `human: <text>`, `output` is the user's words, and it adds `human_heads` (the HEAD of every planned repo) and `user_said`. It counts only while those heads are current [L-24].
- `tail`: the last `tail_lines` lines, at most 4 KB, with secrets redacted. Full output goes to `artifacts/`.

## 7. Review record (`type: review`)

`factory review record --kind code|qa --file review.json` accepts this input shape:

```json
{"verdict":"APPROVE","reviewer":"fresh-reviewer-1","repos":{"api":"3f9c2ab"},
 "findings":[{"id":"F1","severity":"minor","file":"src/Invoices/Discount.cs:42","text":"…","ac":"AC-1"}],
 "resolved":["1:F2"],
 "completeness":{"done":5,"gap":0,"not_checked":0},
 "exercises":[{"ac":"AC-1","exercises":true,"note":"CHK-1 asserts the exact total"}],
 "changed_values":[{"file":"src/ui/toast.ts:12","what":"duration 5s -> 20s","why":"…"}]}
```

- `exercises` is required for `qa`: one entry per AC [L-22].
- `changed_values` is required for `code`, and `[]` must be explicit [L-39].
- `repos` must be an object of `{repo: sha}` with prefixes of at least 7 hex characters; any other shape is refused with this example [L-107].
- `resolved` entries are `round:id`; a bare id means the previous round [L-147].
- `uat` (a change with a screen: a browser, visual, style or screenshot check) is the third review, after code and qa, by a reviewer who wrote neither. It needs `explored`: one `{flow, observed, evidence}` per flow used, `evidence` naming the exploration runs (`factory uat run <run>/uat/<script>`, recorded as evidence with mode `uat` at the current HEAD; they never satisfy a check). Every repo with a screen needs one such run at HEAD. `gate review` requires it whenever the plan has a screen.
- A finding may name `carries` (`round:id`): it re-lists a finding of an earlier round of the same kind; resolving it resolves the earlier ones it carries.
- A finding may carry `scope`: `"change"` (the default) or `"out"`. `gate review` fails while a minor finding of the delivery (`scope` `"change"`) is open: a delivery ships finished, so it is fixed and a later review lists it in `resolved` (a round of minor fixes spends no loop). `"out"` marks a finding that is not part of this delivery; it stays in the run and the briefing, never in the change.

The stored event adds:

| Field | Value |
|---|---|
| `kind` | `code` or `qa` |
| `round` | derived from earlier events |
| `repos` | full SHAs |
| `bound` | `{core_hash}` for code, `{plan_hash}` for qa [L-18, L-137] |
| `lock_seq` | the plan lock it was recorded under |
| `independent_of` | the actor hints on evidence and run-worktree events at that time; the reviewer name and `--actor` must not be among them |
| `resolved` | normalized to `round:id`; rounds count per kind |
| `hash` | sha256 of the canonical record |

Refusals [L-23]:

- APPROVE with `gap > 0`, `not_checked > 0`, any `blocker` or `major` finding, or any `exercises: false`
- SHAs that are not the current HEADs
- a run whose verify does not hold or whose plan lock drifted, and a reviewer name or `--actor` that is also a builder hint
- CHANGES with no finding; a `resolved` entry that names no finding of an earlier round
- a `code` review that does not cite every guideline rule of the planned repos in `guidelines` (`[{id, followed, note}]`, ids are `<repo>:<rule id>`), and APPROVE with `followed: false`

With `risk: high` the latest `code` and `qa` reviews must name two different reviewers, or `gate review` fails.

## 7b. Feedback review (`type: review`, `kind: feedback`)

`factory feedback` appends one record per round, after the ship gate passed and the change and pipeline contracts were read again (`--refresh`):

| Field | Value |
|---|---|
| `kind`, `round`, `verdict` | `feedback`, counted per kind, always `CHANGES` |
| `reviewer` | `factory feedback` |
| `repos` | the shipped heads (`{repo: full SHA}`) |
| `findings` | `{id, severity, file, text, repo, source}`: `source` is `thread:<id>` (an unresolved thread, `major`), `job:<pipeline id>/<job name>` (a failed job, `major`, or `minor` when `allow_failure`) or `pipeline:<id>` (failed with no job detail) |
| `completeness`, `bound`, `lock_seq`, `independent_of`, `hash` | as in section 7; `bound` is empty because no plan hash applies |

A finding whose `source` is already in a feedback record for the same heads is not new. A round with a `blocker` or `major` finding spends one loop (`loop` kind `feedback`). It does not move `review_at`, so the review and ship gates that passed stay valid until a commit moves HEAD; while the round is open (same heads, nothing read back since), the run is in build. `gate feedback` passes when the ship gate holds, every change was read back after it, no thread is unresolved, and the newest pipeline for the pushed head is `success` or waived by id. A `merged` change needs no thread fix. The ship gate does not compare a `refresh` readback with the plan.

## 8. Ship plan (`ship/plan.json`, written by `factory ship plan`)

`ship/body.md` is the change description, for a human reviewer who never sees the run: it is `ship/summary.md` as the orchestrator wrote it, in the project's language (ticket key and link, what changes, behaviour changed beyond the ticket, how it was tested in plain words). `ship plan` refuses a summary that is empty, misses the ticket key, links into the run folder or carries the run's bookkeeping (check, AC or decision ids, attempts, run paths, factory commands) [L-144]. The AC x check x status rows stay in `plan.json` and the Gate S briefing; `ship verify` checks that the change description is the planned body [L-153].

`ship/plan.json` holds `heads`, `title`, `draft`, `mode` (`autonomy.ship`), `behind` per repo (`count`, `ref`, `note`; a note says when `git fetch` is needed first [L-108]), `rows` (the table, which `ship verify` looks for in the readback), and `intents`, each with an `id`, `intent` and `needs_user`:

| `intent` | Fields | Runs without asking in `draft-auto` |
|---|---|---|
| `push` | `repo`, `remote`, `branch`, `sha` | yes |
| `change_request` | `repo`, `action` (`create` or `update`), `change_id`, `source_branch`, `target_branch`, `title`, `draft`, `body_file`, `reviewers` | only with `draft` true |
| `set_draft` | `repo`, `change_id`, `draft` | only with `draft` true |
| `readback` | `repo`, `raw`, `contract_file`, `ingest` (the command), `adapter` (true when `[providers.change] fetch` reads it back in `ship verify`) | yes |
| `pipeline` | `repo`, `sha`, `optional`, `ingest`; planned with `--pipeline` or `require_pipeline` | yes |
| `mirror_change_request` | hotfix runs, one per applied mirror: `repo`, `target`, `sha`, `source_branch`, `target_branch`, `title` (with the target's own ticket key [L-100]), `draft`, `body_file` | no |
| `issue_comment`, `issue_transition` | `ticket`, `body_md` or `to` | never |

Under `autonomy.ship = "ask"` every intent has `needs_user`. Ready and merge are not intents. The CLI runs none of them [L-145].

`ship verify` compares a `change` contract recorded after the plan with the intent: title, `draft` [L-154], branches, `head_sha` equal to the planned head, `state` open, planned reviewers, every table row and no local link [L-153, L-144]. The contract must be `source: adapter` or confirmed by the user after it was ingested [L-21]. With `ship.require_pipeline`, the newest pipeline of the change (or the newest contract for the pushed head) must be proof-grade, for the pushed SHA, and `success` or waived by id [L-20, L-155].
