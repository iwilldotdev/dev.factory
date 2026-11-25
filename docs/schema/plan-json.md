# `plan.json` schema (version 1)

The agent writes `.factory/runs/<run-id>/plan.json` during the plan phase. `gate plan` validates it and locks it [L-132]. After the lock, the only way to change it is `factory plan amend`. The lock itself is an event written by the CLI, not a field of this file (see the end of this page).

## Top level

| Key | Type | Req | Notes |
|---|---|---|---|
| `schema` | `1` | yes | |
| `kind` | `"feature"` or `"bug"` | yes | must equal the run kind derived from the ticket contract [L-31] |
| `repos` | list of repo names | yes | affected repos: each gets a worktree and must have commits past `branch_point`. Env dependencies are not listed here [L-87] |
| `acs` | list of AC objects | yes, at least 1 | |
| `checks` | list of check objects | yes, at least 1 | |
| `fix_commit` | string (ref) | no | default `"HEAD"`; used by `red_green` [L-34] |
| `risk` | `"low"`, `"medium"` or `"high"` | no | default `"low"`. `medium` and `high` require `notes/design.md` [L-176]; `high` requires both review kinds from two reviewer hints |
| `login_user` | string slug | no | selects the `<KEY>_<slug>` secrets and a login state per user [L-81] |
| `evidence` | `{ "screenshots": bool }` | no | `true` (set from an answered question) requires a `screenshot` check per touched UI repo [L-56, L-190] |

`cmd`, `quality`, `human`, `http`, `browser`, `visual`, `style` and `screenshot` checks run in this build. A bug `cmd` check with `reproduce` must carry `red_green`, and `red_green` is only valid together with `reproduce`. Runtime checks (every type but `cmd`) need the repo's `[repo.env]`, a healthy env at `gate plan`, and pass a dry run that does not end in `setup`, `error` or `env` [L-86].

### `quality`

| Key | Type | Req | Default |
|---|---|---|---|
| `run` | command string | yes | none |
| `cwd` | repo-relative path | no | `"."` |
| `parse` | `lines`, `json:<dotted path to a list in stdout>` or `regex:<failure id regex>` (group 1 when present) | no | `lines` |
| `scoped` | command template that contains `{changed_files}` | no | none |

A standing `[[repo.check]]` with `type = "quality"` takes the same keys. The failure ids of one run are normalized (colors and the worktree and checkout paths are stripped [L-62]); a failing exit with no id is the id `exit:<code>`. `verify` takes the baseline by itself the first time (`factory baseline` does it on request): the command runs up to 3 times at `branch_point` in a throwaway worktree (two agreeing runs end it), ids in every run are stable and ids in some runs are quarantined [L-61]. The check fails only on ids that are neither stable nor quarantined. A `setup` result (not found, zero tests) is never a baseline. `verify --scoped` runs `scoped` with the files from `branch_point..HEAD`; that record never satisfies a gate, so the full command still runs before `gate review` [L-60].

A parse that reads noise is caught from generic signals: ids under exit 0, or `parse = "lines"` turning most of a long output into ids, add a `warning` to the record and the baseline; a baseline whose quarantined ids outnumber both its stable ids and 20 fails as `setup` (ids that change between identical runs are timestamps, durations and counters). `factory check` warns on a quality check without `parse`. Quarantined counts appear in the qa reviewer's request and in the Gate S briefing.

Any check that runs in a worktree (every type but the runtime ones) must only read: when the worktree status changes while it runs, its result is `setup` and later records from that worktree in the same batch are not clean. The baseline runs follow the same rule.

### `human`

| Key | Type | Req | Notes |
|---|---|---|---|
| `text` | string | yes | what the user is asked to confirm; shown in the change body next to their words |
| `reason` | string | no | why no command can check it |

`verify` never runs it: it is `pending` (exit stays 0) until `factory confirm <id> --user-said "<words>"` records a `human` evidence record bound to the HEAD of every planned repo. A new commit asks again. A bug plan refuses an AC whose only checks are human, and a design AC still needs its `visual` and `style` checks [L-24]. A pending human check is deferred: it does not hold the verify gate or the reviews; the user is asked once at the ship briefing, and `gate ship` needs the confirmation at the final commits. `factory confirm <id> --rejected --user-said "<words>"` records a rejection: a finding of the delivery that spends one loop and sends the run back to build [L-24, L-138].

### Other types

| `type` | Keys | Rule |
|---|---|---|
| `screenshot` | `url` or `spec`, `viewport`, `state_spec` | artifact only (before on base, after at HEAD); names no AC [L-56] |

Any check may set `mirror: true` (hotfix runs: also run by `verify --mirror` [L-41]). An `http` check may set `session: "<user>"` [L-52].

## AC

| Key | Type | Req | Notes |
|---|---|---|---|
| `id` | `^AC-[0-9]+$` | yes | unique |
| `text` | string | yes | one verifiable behaviour |
| `source` | string | yes | where it comes from: `ticket:description`, `ticket:acceptance#2`, `ticket:comment#<id>`, `answer:Q-3` |
| `reproduce` | bool | no | bug runs: default `true`. A `reproduce` AC needs a check with `reproduce` [L-33] |
| `reason` | string | when `reproduce` is `false` in a bug run | e.g. "regression guard for adjacent behaviour" |
| `design` | list of `design_frame` ids | no | UI AC with a design source: needs at least one `visual` and one `style` check per frame [L-186] |

Every AC must be named by at least one check's `ac` [L-01]. An AC that cannot be automated is not allowed in the alpha. It goes to the plan question round instead [L-24].

## Check: common keys

| Key | Type | Req | Notes |
|---|---|---|---|
| `id` | `^CHK-[0-9]+$` | yes | unique; standing checks use `<repo>:<id>` and come from config |
| `type` | `"cmd"`, `"quality"`, `"human"`, `"http"`, `"browser"`, `"visual"`, `"style"` or `"screenshot"` | yes | |
| `repo` | repo name in `repos` | yes | worktree it runs in, or whose env it calls |
| `ac` | list of AC ids | yes, at least 1 | |
| `timeout_s` | int | no | default from `[checks]` [L-42] |
| `files` | list of run-relative paths under `checks/` | no | fixtures and data hashed into `spec_hash`. Undeclared files are ignored [L-58] |
| `reproduce` | `{ "expect_fail_pattern": "<regex>" }` | bug ACs | must fail on base with this text [L-05]. Pattern of at least 6 characters, with 4 or more literal word characters [L-04] |

### `cmd`

| Key | Type | Req | Default |
|---|---|---|---|
| `run` | command string | yes | — |
| `cwd` | repo-relative path | no | `"."` |
| `expect_exit` | int | no | `0` |
| `expect_pattern` | regex (same strength rule) | no | — |
| `red_green` | `{ "test_files": [repo-relative paths] }` | no (required in bug runs when this check is the AC's only reproduction) | red at `fix_commit~1` with `test_files` from git at `fix_commit`, then green at `fix_commit` [L-34, L-35] |
| `mutant` | command string | no (`cmd` and `quality` only) | after the check passes at HEAD, `verify` runs this command in a throwaway worktree at HEAD and runs the check there: it must fail (`mode: "mutant"`, result `pass` = killed). `gate verify` needs the kill at HEAD. A mutant that fails, changes nothing or that the check survives is not a kill |

A zero-test marker or setup marker in the output makes the result `setup` [L-02, L-03].

### `http`

| Key | Type | Req | Default |
|---|---|---|---|
| `method` | `GET`, `POST`, `PUT`, `PATCH` or `DELETE` | yes | — |
| `path` | string starting with `/` | yes | resolved against the repo env `base_url` only [L-82] |
| `headers` | table string → string | no | `{}`; values may be `${SECRET_KEY}` references, resolved at run time and never stored |
| `body` | JSON | no | — |
| `expect_status` | int | yes | — |
| `expect_json` | JSON | no | subset match. A failure reason reads `status <n>; expect_json mismatch at <json-path>` and never quotes the body [L-83] |

### `browser`

| Key | Type | Req | Default |
|---|---|---|---|
| `spec` | run-relative path under `checks/` | yes | hashed into `spec_hash`; run by the bundled runner (`spec` command), or by `[checks.browser] runner` when the project sets one |
| `expect_timeout_ms` | int | no | `[checks] expect_timeout_ms` [L-59] |

### Common keys of `visual` and `style`

| Key | Type | Req | Default |
|---|---|---|---|
| `frame` | `design_frame` id; must be in the `design` list of each AC named in `ac` | yes | — |
| `url` | path starting with `/`, resolved against the env `base_url` | yes | — |
| `viewport` | `[width, height]` | no | the frame image size |
| `actions` | list of one-key objects (`goto`, `click`, `fill`, `wait`, `press`, `expect_*`) run after `state_spec` | no | — |
| `settle_ms` | int | no | wait after the page is ready, before capture or probe |
| `state_spec` | run-relative browser spec under `checks/`, executed before capture or probe (opens a drawer, popover or toast) [L-51] | no | — |
| `wait_for` | CSS selector that must be visible before capture or probe | no | — |

There is no `fidelity` key: fidelity comes only from the frame contract [L-188].

### `visual`

| Key | Type | Req | Default |
|---|---|---|---|
| `node` | node id inside the frame | no | frame root |
| `selector` | CSS selector to screenshot | no | the whole viewport |
| `max_diff_ratio` | number | no | `[limits] max_diff_ratio`. The ceiling is that value (exact) or `limits.approx.max_diff_ratio` (approx) [L-188] |
| `masks` | list of `{x, y, w, h, reason}` in node coordinates | no | `[]`. Each mask is at most 2% of the area, and a reason is required |
| `fit` | bool | no | `false`. When true, a capture whose size differs from the frame is scaled to the frame before the diff, and the scale is recorded in evidence; when false, a size mismatch fails |

The design PNG is cropped to the node `box`, with the offset taken from `render_box` and effect margins [L-55]. A size mismatch between crop and screenshot fails with reason `size`.

### `style`

| Key | Type | Req | Default |
|---|---|---|---|
| `map` | run-relative path under `checks/style/`; skeleton from `factory design expect` | yes | — |

Style map file:

```json
{"frame": "landing-empty", "entries": [
  {"selector": "[data-test=empty-title]", "node": "12-40", "tolerance_px": 1,
   "expect": {"fontSize": 20, "fontWeight": 600, "lineHeight": 28, "color": "#1f2933", "text": "No invoices yet"}}
]}
```

Every `expect` value must be a fact of the node `node` through the property table in `contracts.md` §2 [L-187]. `tolerance_px` must not exceed `[limits] tolerance_px` (exact) or `limits.approx.px` (approx) [L-188].

## Validation at `gate plan` and `plan amend`

- Ids are unique.
- Every check's `repo` is in `repos`, and every `ac` id exists.
- Every AC is covered by at least one check.
- Regex strength holds for `expect_pattern` and `expect_fail_pattern` [L-04].
- In bug runs, every `reproduce` AC has a check with `reproduce` [L-33].
- Declared files exist.
- Every `design` frame has valid `design_frame` and `design_image` contracts with matching raw hashes. `exact` frames carry `source.version`. Every ticket link with `rel: design` is covered by a frame in the plan or an answered question [L-186, L-190].
- Style-map values are frame facts, and limits are within their ceilings [L-187, L-188].
- Runtime checks have env config and a runner where needed. New runtime checks pass a dry run that does not end in `setup` or `error` [L-86].

## Lock (written by the CLI in the `plan_locked` event)

| Field | Meaning |
|---|---|
| `plan_hash` | sha256 of canonical `plan.json` [L-167] |
| `core_hash` | sha256 of canonical `plan.json` without `checks`, which the code review binds to [L-137] |
| `spec_hashes` | `{check_id: sha256}` over: the canonical check entry, resolved defaults, the resolved command or runner template, and the hashes of its declared files. For `visual`/`style`, it also covers the frame and image contract hashes [L-09, L-59] |
| `files` | `{run-relative path: sha256}` of every declared spec and fixture file |
| `config_hash` | sha256 of the proof-relevant effective config [L-131] |
| `contracts` | `{contract path: raw_sha256}` [L-122] |
| `diagnosis` | sha256 of `diagnosis.md`. It is written on the passing `gate reproduce` event (`diagnosis`), not on the lock event, and a later change fails every gate until `gate reproduce` passes again |
| `acs` | `{ac id: sha256}` of each canonical AC, so an amend can list the changed ACs |
| `branch_points` | `{repo: sha}` recorded at worktree creation [L-89] |
| `amend` | on an amend: `reason`, `decision` (or null), `changed_acs`, `changed_checks`, `counts_loop` [L-134] |

## Example: feature

```json
{
  "schema": 1,
  "kind": "feature",
  "repos": ["api", "web"],
  "acs": [
    {"id": "AC-1", "text": "GET /api/invoices?status=overdue returns only overdue invoices", "source": "ticket:acceptance#1"},
    {"id": "AC-2", "text": "The invoices page shows an 'Overdue' filter that lists only overdue invoices", "source": "ticket:acceptance#2"},
    {"id": "AC-3", "text": "The overdue filter chip matches the design", "source": "ticket:links#design-1",
     "design": ["invoices-filter"]}
  ],
  "checks": [
    {"id": "CHK-1", "type": "cmd", "repo": "api", "ac": ["AC-1"],
     "run": "./scripts/test.sh --filter InvoiceFilterTests", "expect_pattern": "InvoiceFilterTests.*passed"},
    {"id": "CHK-2", "type": "http", "repo": "api", "ac": ["AC-1"],
     "method": "GET", "path": "/api/invoices?status=overdue",
     "headers": {"Authorization": "Bearer ${TEST_API_TOKEN}"},
     "expect_status": 200, "expect_json": {"items": [{"status": "overdue"}]}},
    {"id": "CHK-3", "type": "browser", "repo": "web", "ac": ["AC-2"],
     "spec": "checks/overdue-filter.spec.ts", "expect_timeout_ms": 15000},
    {"id": "CHK-4", "type": "visual", "repo": "web", "ac": ["AC-3"],
     "frame": "invoices-filter", "node": "88-12", "url": "/invoices", "viewport": [1440, 900],
     "selector": "[data-test=filter-bar]", "state_spec": "checks/open-filter.spec.ts", "max_diff_ratio": 0.02},
    {"id": "CHK-5", "type": "style", "repo": "web", "ac": ["AC-3"],
     "frame": "invoices-filter", "url": "/invoices", "map": "checks/style/filter-chip.json"}
  ]
}
```

## Example: bug

```json
{
  "schema": 1,
  "kind": "bug",
  "repos": ["api"],
  "fix_commit": "HEAD",
  "acs": [
    {"id": "AC-1", "text": "Saving an invoice with a 0% discount keeps the total unchanged", "source": "ticket:description"},
    {"id": "AC-2", "text": "Discounts above 0% still reduce the total", "source": "answer:Q-1",
     "reproduce": false, "reason": "regression guard; passes on base by design"}
  ],
  "checks": [
    {"id": "CHK-1", "type": "cmd", "repo": "api", "ac": ["AC-1"],
     "run": "./scripts/test.sh --filter ZeroDiscountKeepsTotal",
     "reproduce": {"expect_fail_pattern": "ZeroDiscountKeepsTotal.*expected 120\\.00"},
     "red_green": {"test_files": ["tests/Invoices/DiscountTests.cs"]}},
    {"id": "CHK-2", "type": "http", "repo": "api", "ac": ["AC-1"],
     "method": "POST", "path": "/api/invoices/preview", "body": {"amount": 120.0, "discount": 0},
     "expect_status": 200, "expect_json": {"total": 120.0},
     "reproduce": {"expect_fail_pattern": "expect_json mismatch at [$][.]total"}},
    {"id": "CHK-3", "type": "cmd", "repo": "api", "ac": ["AC-2"],
     "run": "./scripts/test.sh --filter DiscountReducesTotal"}
  ]
}
```

`factory reproduce` runs CHK-1 and CHK-2 in a throwaway worktree at the `api` branch point and needs `reproduced` for both, with each output matching its pattern. `verify` then needs CHK-1 to CHK-3 plus the `api:*` standing checks to pass at HEAD, and CHK-1 red-green to hold at `fix_commit`.
