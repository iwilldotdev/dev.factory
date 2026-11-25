# `factory.toml` schema (version 1)

`factory.toml` is versioned and written by people. `.factory/local.toml` is per machine and unversioned. The CLI reads both with `tomllib` and rejects unknown keys (exit 2, with the key path) [L-156].

Levels used below:

- **R**: a repo's own file (`[[repo]] path = "."`)
- **W**: a workspace file
- **L**: `local.toml`

Composition rules (DESIGN §2):

- In a workspace, a member file's `[[repo]]` entry is the base. The workspace's entry for that path overrides it key by key, and nested tables merge per key.
- Lists are replaced, except `check` (merged by `id`) and `secrets.keys` (unioned).
- A member's `[project]`, `[checks]`, `[providers]`, `[ship]` and `[autonomy]` apply only when that member is used alone.
- `local.toml` is applied last, and only to the keys listed in its own section.

## Top level

| Key | Type | Default | Req | Level | Notes |
|---|---|---|---|---|---|
| `schema` | int | — | yes | R, W | must be `1` |

## `[project]`

| Key | Type | Default | Req | Level | Notes |
|---|---|---|---|---|---|
| `name` | string `^[a-z0-9][a-z0-9-]{0,62}$` | — | yes | R, W | names the secrets file |
| `cli` | string (version spec, e.g. `">=1.0.0a1,<1.1"`) | none | no | R, W | `factory check` fails when the installed CLI is outside it [L-149, L-150] |

## `[[repo]]` (one or more)

| Key | Type | Default | Req | Level | Notes |
|---|---|---|---|---|---|
| `path` | string, relative to this file | — | yes | R (`"."`), W | L may override via `[repo.<name>]` |
| `name` | string `^[A-Za-z0-9._-]+$` | basename of the path | no | R, W | unique; used in plan, evidence and the env lock |
| `base` | string | `"main"` | no | R, W | this repo's base branch [L-94] |
| `remote` | string | `"origin"` | no | R, W | the git remote of this repo that `ship plan` names in its `push` intents (the CLI never pushes; the harness does). Refs the CLI reads locally (`origin/<base>`) are unchanged |
| `branch` | template: `{kind}`, `{key}`, `{slug}` | `"{kind}/{key}-{slug}"` | no | R, W | run branch name |
| `setup` | command string | none | no | R, W | runs in every new worktree and throwaway worktree; failure blocks the check, never counts as a result [L-06, L-72] |
| `files_from_main` | list of repo-relative paths | `[]` | no | R, W | gitignored files copied into worktrees [L-64, L-65] |
| `commit_pattern` | regex | none | no | R, W | see decision 6; checked over `branch_point..HEAD` only, and not over commits the base already has [L-89, L-90] |
| `commit_forbidden_trailers` | list of trailer names (e.g. `Co-Authored-By`) | `[]` | no | R, W | a commit in the same range that carries one fails the check, merges included [L-90] |

A repo with `commit_pattern` or `commit_forbidden_trailers` gets an implicit standing check `<repo>:commits`; `verify` runs it like any other check, so its result is evidence bound to the SHA.

### `[repo.env]` (optional, required for runtime checks)

| Key | Type | Default | Req | Notes |
|---|---|---|---|---|
| `up` | command string | — | yes | started by the factory in its own process group [L-67] |
| `down` | command string | none | no | run before the group is stopped |
| `health` | URL path or absolute local URL | `"/"` | no | healthy = status < 500, redirects not followed [L-73] |
| `base_url` | URL (loopback host only) | — | yes | sole origin for `http`/`browser` checks [L-82]; L may override |
| `ready_timeout_s` | int | `120` | no | `env up` waits itself [L-84] |
| `depends_on` | list of repo names | `[]` | no | started first, from the run worktree when the repo is in the run [L-71] |
| `vars` | table string → string | `{}` | no | extra env vars for `up`; L may override |
| `login_spec` | repo-relative action spec (`.json`, DESIGN section 10) | none | no | run by the runner's `login` command after health. It writes login state under the project's env directory (`<repo>.state.json`), which browser, visual and style checks reuse. Login succeeds when the browser ends on the env origin, or, when the spec declares `"done": {"cookie": "<name>"}` or `{"url": "<regex>"}` (an app that redirects to another app after login), when that holds. Credentials come only from env vars [L-80] |
| `login_secrets` | list of secret key names | `[]` | no | passed to the login spec as env vars; with plan `login_user = "qa-admin"` each `KEY` is read from `KEY_QA_ADMIN` [L-81] |
| `login_http` | command string | none | no | prints a JSON headers object for `http` checks with `session`; output is never stored [L-52] |
| `seed`, `reset` | command strings | none | no | run by `factory env seed/reset` with `FACTORY_DATA_PREFIX` derived from the run id; `verify` resets once before the runtime batch [L-50] |
| `preflight` | list of command strings | `[]` | no | run after health; failure = env kind `data` [L-50] |
| `warmup` | list of URL paths | `[]` | no | requested once after `up` [L-40] |
| `env_patterns` | list of regexes | `[]` | no | failure output that means env, not product [L-49] |
| `shareable` | bool | `false` | no | a run may use another run's live env when it serves the same commit; the env is never restarted for it [L-76] |

### `[[repo.check]]` (standing checks, optional)

These run for every run that affects the repo. They are ordinary `cmd` checks with id `<repo>:<id>` [L-63]. Keys are the `cmd` check keys from `plan-json.md` (`id`, `run`, `cwd`, `expect_exit`, `expect_pattern`, `timeout_s`), minus `ac`, plus `vars`: a table of environment variables for the command, as the CI job sets them (e.g. `vars = { NODE_OPTIONS = "--max-old-space-size=3072" }`).

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `cmd` or `quality` | `cmd` | a `quality` check compares failure ids with a baseline at `branch_point` (`plan-json.md`) [L-60 to L-62] |
| `parse` | `lines`, `json:<path>` or `regex:<failure id regex>` | `lines` | `quality` only: how failure ids are read from the output |
| `scoped` | command template with `{changed_files}` | none | `quality` only: run by `verify --scoped` on the files changed in `branch_point..HEAD` [L-60] |
| `kind` | `check` or `guideline` | `check` | a `guideline` has `id` and `text` and no `run`; it is not a check, and the `code` review must cite it |
| `text` | string | none | the guideline text shown to the reviewer |

## `[checks]` (defaults)

| Key | Type | Default | Level | Notes |
|---|---|---|---|---|
| `cmd_timeout_s` | int | `600` | R, W | wall-clock cap [L-42] |
| `http_timeout_s` | int | `30` | R, W | |
| `browser_timeout_s` | int | `300` | R, W | |
| `expect_timeout_ms` | int | `10000` | R, W | browser assertion wait; part of the spec hash [L-59] |
| `tail_lines` | int | `40` | R, W | evidence tail (also capped at 4 KB) |

### `[checks.browser]`

| Key | Type | Default | Level | Notes |
|---|---|---|---|---|
| `bundled` | path to the installed bundled runner | the runner shipped with the CLI | L | `factory check` prints the install command when it is missing and a plan needs it |
| `runner` | command template: `{spec}`, `{base_url}`, `{out}`, `{timeout_ms}`, `{storage_state}` | none (bundled `spec` command) | R, W, L | optional override for `browser` spec checks only; exit 0 = pass. `visual` and `style` always use the bundled runner |
| `batch` | bool | `true` | R, W | one runner process and one browser for a repo's `browser`, `visual`, `style` and `screenshot` checks [L-57] |
| `headless` | bool | `true` | L | |

## `[limits]` (ceilings; part of the locked config hash [L-188])

| Key | Type | Default | Level | Notes |
|---|---|---|---|---|
| `max_diff_ratio` | number from 0 to 1 | `0.03` | R, W | ceiling and default for `visual` on exact frames |
| `tolerance_px` | number | `2` | R, W | ceiling for style-map `tolerance_px` on exact frames |

### `[limits.approx]` (applied only when the frame contract is `approx`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `px` | number | `4` | style px tolerance |
| `color_delta_e` | number | `6` | CIE76 ΔE for colors |
| `font_size_px` | number | `2` | font size tolerance |
| `max_diff_ratio` | number | `0.10` | `visual` ceiling |

## `[providers.<kind>]` (kind = `ticket`, `change`, `change_list` (the review queue; `{id}` is `queue`), `pipeline`, `design_frame` or `design_image`; all optional)

| Key | Type | Default | Notes |
|---|---|---|---|
| `fetch` | command template | — | placeholders in `contracts.md` §4; without it the agent uses `factory ingest`. `design_image` adapters write the PNG to `{out}` |
| `timeout_s` | int | `60` | |
| `secrets` | list of secret key names | `[]` | passed to the adapter as env vars only [L-80] |

## `[ship]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `title` | template: `{key}`, `{summary}` | `"[{key}] - {summary}"` | an empty or null value falls back to the default [L-142] |
| `draft` | bool | `true` | |
| `ticket` | `"none"`, `"comment"`, `"transition"` or `"both"` | `"none"` | default for `ship plan --ticket` [L-145] |
| `transition_to` | string | none | required when `ticket` is `"transition"` or `"both"` |
| `reviewers` | list of `{scm = "...", tracker = "..."}` | `[]` | both identities, validated by `factory check` [L-143] |
| `require_pipeline` | bool | `false` | makes `gate ship` require a green pipeline contract [L-19, L-155] |
| `base_update` | `"rebase"` or `"merge"` | `"rebase"` | how a moved base enters the run branch: a rebase keeps only the change's own commits (its push carries `force_with_lease` and asks the user); a review stays approved when the rebased commits carry the same patches |

## `[autonomy]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_loops` | int, 0 to 10 | `3` | one budget for amends and review rounds [L-135, L-136] |
| `ship` | `"ask"` or `"draft-auto"` | `"ask"` | |

## `[orchestration]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_parallel` | int | `1` | ready epic or review children that `next <parent>` hands out at once [L-184] |

## `[feedback]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `ignore_authors` | list of SCM usernames | `[]` | unresolved threads by these authors never become findings or gate reasons. Empty by default: every unresolved thread counts [L-20] |

## `[metrics]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `tokens` | command template: `{run}`, `{session}` | none | optional; prints `{session, input, output}` JSON; `factory metrics --tokens --session <id>` records it [L-183] |
| `idle_minutes` | int | `30` | a gap longer than this with no open blocker is `idle_hours`, not phase time |

## `[hotfix]`

| Key | Type | Default | Notes |
|---|---|---|---|
| `targets` | list of `{name, branch}` per repo (`[[repo.mirror]]` in repo entries) | `[]` | mirror targets; branch names add `{target}` [L-93] |

## `[secrets]`

| Key | Type | Default | Level | Notes |
|---|---|---|---|---|
| `keys` | list of `^[A-Z][A-Z0-9_]*$` | `[]` | R, W (unioned) | names only; values never in any file under the project [L-129] |

Each key resolves in this order:

1. An environment variable with the same name.
2. `${XDG_CONFIG_HOME:-~/.config}/dev.factory/secrets/<project.name>.env`, one `KEY=value` per line, `#` comments allowed. The same path is used on Windows, under the user profile.

`factory check` reports missing keys by name, never values. Values reach children only as env vars and are redacted (`***`) from stored tails.

## `.factory/local.toml`

| Key | Type | Notes |
|---|---|---|
| `worktree_root` | path | default `<user cache dir>/dev.factory/worktrees/<project.name>`, which keeps worktrees out of the repo tree (decision 7) |
| `[repo.<name>] path` | path | where this member lives on this machine |
| `[repo.<name>.env] base_url`, `health`, `vars` | as above | ports and hosts |
| `[checks.browser] runner`, `bundled`, `headless` | template / path / bool | local runner paths |
| `[repo.<name>.slots] port_offset`, `count` | int, int | `env up --slot n` (before the plan locks) records the slot in the run and adds `n * port_offset` to the port of `base_url` and an absolute `health`; `{port}`, `{base_url}`, `{<repo>.port}` and `{<repo>.base_url}` in `up`, `down`, `seed`, `reset`, `health` and `vars` are derived. Each slot has its own lock, log and login state, and evidence records the slot and its base URL [L-78] |

Any other key is an error. Paths, ports and base URLs are excluded from the plan's `config_hash`, while every command and cap is included, so `local.toml` cannot loosen proof [L-131].

## Example: single repo

```toml
schema = 1

[project]
name = "billing-web"
cli = ">=1.0.0a1,<1.1"

[[repo]]
path = "."
name = "web"
base = "develop"                       # this repo's base branch
setup = "./scripts/install-deps.sh"    # own deps per worktree, never a link
files_from_main = [".env.local"]       # gitignored, copied into each worktree

[repo.env]
up = "./scripts/dev-server.sh"
health = "/healthz"
base_url = "http://127.0.0.1:5173"
ready_timeout_s = 90
login_spec = "tests/login.spec.ts"     # runner writes the login state used by UI checks
login_secrets = ["TEST_USER", "TEST_PASSWORD"]

[[repo.check]]                          # standing check: every run on this repo
id = "lint"
run = "./scripts/lint.sh"

[[repo.check]]
id = "unit"
run = "./scripts/test.sh"
timeout_s = 900

[checks]
cmd_timeout_s = 600
expect_timeout_ms = 15000

[limits]
max_diff_ratio = 0.02                  # stricter than the default 0.03
tolerance_px = 1

[limits.approx]
px = 4
color_delta_e = 6
font_size_px = 2
max_diff_ratio = 0.10

[providers.ticket]
fetch = "python ~/adapters/ticket.py {id}"
secrets = ["TRACKER_TOKEN"]

[providers.design_frame]
fetch = "python ~/adapters/design.py frame {id}"
secrets = ["DESIGN_TOKEN"]

[providers.design_image]
fetch = "python ~/adapters/design.py image {id} --png {out}"
secrets = ["DESIGN_TOKEN"]

[providers.change]
fetch = "python ~/adapters/change.py {repo_path} {branch}"
secrets = ["SCM_TOKEN"]

[ship]
draft = true
ticket = "comment"
reviewers = [{ scm = "ana.lima", tracker = "5f1c2e" }]

[autonomy]
max_loops = 3
ship = "draft-auto"

[secrets]
keys = ["TRACKER_TOKEN", "SCM_TOKEN", "DESIGN_TOKEN", "TEST_USER", "TEST_PASSWORD"]
```

## Example: workspace with two members

```
code/
  shop/factory.toml        # workspace
  api/factory.toml         # member, own file
  web/factory.toml         # member, own file
```

`code/api/factory.toml` (also usable alone):

```toml
schema = 1
[project]
name = "shop-api"

[[repo]]
path = "."
name = "api"
setup = "./scripts/restore.sh"
files_from_main = ["appsettings.Local.json"]

[repo.env]
up = "./scripts/run-api.sh"
health = "/health"
base_url = "http://127.0.0.1:5000"

[[repo.check]]
id = "unit"
run = "./scripts/test.sh"

[secrets]
keys = ["DB_PASSWORD"]
```

`code/web/factory.toml` follows the same shape with `name = "web"`, its own env (`base_url = "http://127.0.0.1:5173"`) and a standing `lint` check.

`code/shop/factory.toml`:

```toml
schema = 1
[project]
name = "shop"                          # secrets file: shop.env

[[repo]]
path = "../api"                        # inherits api's setup, env and checks
base = "release/2.x"                   # overrides only the base branch

[[repo]]
path = "../web"
[repo.env]
depends_on = ["api"]                   # merged into web's own [repo.env]
[[repo.check]]
id = "lint"                            # same id: replaces web's lint
run = "./scripts/lint.sh --strict"

[checks.browser]
runner = "./scripts/browser-test.sh {spec} --base-url {base_url} --output {out}"

[providers.ticket]
fetch = "python ~/adapters/ticket.py {id}"

[ship]
require_pipeline = true

[autonomy]
ship = "ask"

[secrets]
keys = ["TEST_USER"]                   # union with members: DB_PASSWORD, TEST_USER
```

Effective `web` entry: web's own `setup`, `files_from_main` and env, plus `depends_on = ["api"]`. Its checks are the workspace `lint` plus anything else web declares.

`code/shop/.factory/local.toml`:

```toml
worktree_root = "D:/wt/shop"
[repo.web.env]
base_url = "http://127.0.0.1:5174"     # port taken on this machine
```
