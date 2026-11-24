# Provider profiles: guided authoring

The factory knows no provider. A **profile** is data in `FACTORY_HOME/profiles/<name>/` that maps one provider's raw payloads to the factory contracts, tells agents how to acquire them (hints), and tells the guards what counts as a remote write. Roles: `tickets` (kinds `ticket`), `scm` (`change`, `change_list`, `pipeline`), `design` (`design_frame`, `design_image`). The workspace picks one profile per role: `"providers": {"tickets": "<name>", "scm": "<name>", "design": "<name>"}`; one profile may serve several roles (`"role": ["tickets","scm"]`).

**Ownership.** `profiles/<name>/` is APPROVED data and the user's: gates and ingest read only it, and a worker never writes there. A worker writes a **draft** under `FACTORY_HOME/profile-drafts/<name>/` (`profile.json` + `samples/`). Only `factory profile approve <name> --digest <draft digest from check> --user-said "<gist>"` (main session, after the user reviewed the draft and the `check` output) promotes a valid draft: it copies it to `profiles/<name>/` with an atomic swap (the previous version goes to `profiles/.backups/<name>-<timestamp>/`) and writes `approved.json`. Gates and ingest REFUSE a profile whose `profile.json` sha256 differs from the one in `approved.json` (hand edits, a swapped file), naming the approve command.

```
FACTORY_HOME/profile-drafts/<name>/    worker-written draft (same layout as below, no approved.json)
FACTORY_HOME/profiles/<name>/          approved, read-only for workers
  profile.json
  approved.json                        {"file_sha256", "digest", "approved_at", "user_said"}
  samples/<kind>/<n>.<ext>             real, anonymised raw payloads (never invented)
  samples/<kind>/<n>.expected.json     golden contract (optional)
  samples/<kind>/<n>.ctx.json          ingest context for that sample, e.g. {"repo": "api"} (optional)
  samples/<kind>/<n>.<input>.json      extra named input, e.g. 1.jobs.json for a pipeline (optional)
```

## Workflow (author worker; the main session relays the questions)

1. `factory profile new <name> --role tickets|scm|design`. It scaffolds a DRAFT (`profile-drafts/<name>/profile.json` and `samples/<kind>/`) and prints this workflow.
2. **Ask the user** for the provider and for REAL sample payloads per kind (what the provider's API or CLI returned, anonymised). Never invent or "reconstruct" a sample. A worker cannot ask: return `status: blocked` with the question, name the kinds and what to save where (`profile-drafts/<name>/samples/<kind>/<n>.<ext>`).
3. When a field is ambiguous (which key is the status, what a timestamp format is, whether a list is complete or paged), research the provider's **public API documentation** on the web and cite the page in the profile `notes`. Still ambiguous: ask the user (`blocked` with the question). Never guess a mapping to make a sample pass.
4. Write the mapping in `kinds.<kind>.map` (language below), the `refs`, the `hints`, and, from the provider's documentation, `cli_write_patterns`, `mcp_read_allow`, `save_args`.
5. `factory profile test <name> --draft [--kind K] [--sample N]` prints, per sample, the contract, the `unknowns` and the source expression of every field. **Show the contract and the unknowns to the user.** Save goldens with `--draft --write-golden` (writes `<n>.expected.json` next to the sample) once the user agrees, and re-check later with `--golden` (diff against them; `profile test|check` without `--draft` read the approved profile, read-only). Exit 2 on any error.
6. `factory profile check <name> --draft`: valid iff every declared kind maps at least one sample with no error and validates against its contract schema; it also lints the mapping (unknown operators, unknown hint placeholders, regexes that do not compile, `refs` without the required named groups). Exit 2 when invalid. `factory workspace check <ws>` fails when `providers.<role>` names a missing or invalid profile.
7. **The user approves.** Show the user the draft's contracts, `unknowns` and golden diff, AND the full output of `factory profile check <name> --draft`: it prints the **draft digest** (`DIGEST sha256:...`) and the three guard fields the worker guard will trust once approved (`mcp_read_allow`, `cli_write_patterns`, `save_args`) plus warnings (an empty `cli_write_patterns` next to acquisition hints). Lint rejects an `mcp_read_allow` entry that is `*`, `mcp__*`, a server-wide glob (`mcp__<server>__*`) or any glob that would match every tool (write tools included): list read tools one by one. After the user said yes to THAT output, the main session runs `factory profile approve <name> --digest <digest from check> --user-said "<gist of the user's approval>"` (main session only; it refuses a missing or different digest, so a draft edited after the user looked cannot be approved, and refuses a draft that fails `profile check`). Approve prints the guard fields now in force and a unified diff against the current approved `profile.json` (`new profile` when there is none). A worker never runs `profile approve`: the hooks deny it, `python -m factory.cli ...` and inline interpreter code importing the `factory` package included. Unknowns the user accepts are listed by field name in `accepted_unknowns` before approving. After any edit of an approved profile, go through a draft and approve again: the digest recorded in each contract's provenance then changes, and `gate` prints a warning for contracts ingested under another digest (re-ingest them; `review report` prints the profile digests used).

Samples prove the mapping runs, not that it is right for every payload variant (custom fields differ per site). Surface `unknowns` at intake and extend samples when a real run shows a new shape.

## profile.v1 fields

| Field | Content |
|---|---|
| `schema`, `name`, `role` | `"profile.v1"`, the folder name, a role or a list of roles. |
| `refs` | Regexes with required named groups: `ticket_key` (`key`), `ticket_url` (`key`), `change_url` (`project`, `number`). They turn a pasted ticket key or change URL into workspace references (`factory run start`, `review start --change <url>`). |
| `kinds.<kind>` | `ext` (raw extension, default `json`; `png` for `design_image`), `map` (field -> expression), `optional` (contract fields allowed to be null: they land in `unknowns`), `inputs` (extra named raws, `["main","jobs"]`; `$jobs.` in paths; ingest `--raw jobs=<path>`; a trailing `?` marks an optional input, `["main","jobs?"]`: ingest does not require it, an absent optional input reads as null in paths, so the map can `coalesce` to jobs found in the main raw), `fidelity` (design frames: `exact` or `approx`). `design_image` has no map: ingest validates the PNG. |
| `hints.<kind>` | Acquisition template shown on `FETCH` lines (what to run to save the raw). Placeholders: `{key}`, `{id}`, `{number}`, `{scm_project}`, `{repo}`, `{sha}`, `{file}`, `{raw_path}` (absolute, forward slashes), `{raw_dir}`. A placeholder without a value stays as `<name>`; unknown placeholders fail `profile check`. The hint is advice for the agent (any MCP tool or CLI that saves the payload), never executed by the factory. |
| `cli_write_patterns` | Regexes. `worker-guard` denies a worker's shell command matching one (a provider CLI that writes remotely). Generic patterns (HTTP clients sending a body or a write method, `git push`) always apply; reading with a provider CLI is allowed when its output is redirected under `R/raw/`. |
| `mcp_read_allow` | Globs of MCP tool names the author certifies read-only although the name contains a write verb (`*` wildcard on the tool segment; a glob starting `mcp__` matches the full name). The guard otherwise scans the name tokens left to right: any write verb anywhere makes the tool a write; else the first read verb makes it a read; no known verb is a write (fail-closed). Vendor or namespace prefixes are therefore tolerated, but `normalize_*` is a write unless listed here. |
| `save_args` | Extra argument names that carry a destination path in this provider's tools (defaults: `save_to`, `output_path`, `output_file`, `out`, `dest`). The guard checks every such destination against the run's protected paths. |
| `notes` | Free text: cite the documentation pages used for ambiguous fields, with the date read. |
| `accepted_unknowns` | List of contract field names the user accepted as unfillable. |

Contracts: `ticket.v1` (`key`, `title`, `type`, `is_bug`, `status`, `description_md`, optional `acceptance_criteria`, `labels`, `parent`, `assignee`, `links`, `attachments[{id,name,mime,url}]`, `comments`), `change.v1` (`repo`, `number`, `url`, `title` without draft markers, `description_md`, branches, `head_sha`, `state` open|merged|closed, `draft`, `approvals{known,by}`, `threads`, `pipelines`, ...), `change_list.v1`, `pipeline.v1` (`id`, `sha`, `status` pending|running|success|failed|canceled|skipped, `jobs[{id,name,status,allow_failure}]`), `design_frame.v1`. All text is Markdown or plain text, never HTML or a provider rich-text tree. A required field that maps to null is an error; an optional one is omitted and recorded in `unknowns`. Run `factory profile test` to see the exact required fields of a kind.

**Acceptance criteria.** Tickets may or may not carry them, depending on each team's formatting. Map `acceptance_criteria` only when the format has them (a custom field or a heading: inspect the samples, or ask the user). When the contract has none, the factory derives ACs at intake from the ticket: the author proposes them and the user confirms in the question round.

**Review threads.** Whether a thread is resolvable depends on the SCM tool. The agent uses whatever MCP tool or CLI the SCM profile's `change` hint names and must fetch the discussion data that identifies resolvable threads; the profile maps them into `threads`. If the thread mapping was written from documentation only (no real sample with a resolvable thread), say so in the profile `notes`, and validate the first real occurrence with `factory profile test`.

## Mapping language

A mapping is a JSON object whose leaves are expressions `{"op": args}`. Anything that is not an operator object is a literal. Evaluation is pure and deterministic: no I/O, time, random, assignment or loops except `each`/`tree`; regexes are bounded.

Scopes: `$` the main raw document; `$name` an extra named input; `@` the current item inside `each`/`tree`; `@@` its parent; `$ctx` the ingest context (`repo`, `scm_project`, `number`, `key`, `id`, `file`, plus `--ctx name=value`).

Paths: `{"path": "a.b[0].c"}`: dotted keys, `["a.b"]` quoted key, `[0]`/`[-1]` index, `[*]` wildcard (array), `[?key==lit]` / `[?key!=lit]` filter. A missing key is `null` (no error); a wildcard over null is `[]`; indexing a non-array or keying a non-object is an error. A projection (`a[*].b`) drops nulls.

| Group | Operators |
|---|---|
| Value | `path`, `lit` (any JSON), `coalesce:[e...]` (first non-null, non-empty) |
| Boolean | `any:[e...]`, `all:[e...]`, `not:e`, `eq:[a,b]`, `in:[a,[lit...]]` (case-sensitive; wrap in `lower`), `exists:e` |
| String | `startswith:[s,p]`, `regex:[s,pat]` (bool), `regex_group:[s,pat,g]` (null on no match), `regex_sub:[s,pat,repl]`, `lower`, `upper`, `trim`, `join:[arr,sep]`, `split:[s,sep]`, `concat:[e...]` (null when a part is null) |
| Control | `if:{cond, then, else?}` (`else` defaults to null), `type_is:[e, "array"\|"object"\|"string"\|"number"\|"bool"\|"null"]` -> bool |
| Colour | `rgb_hex:{r, g, b, scale?}` -> lowercase `#rrggbb` (`scale` 1 = channels 0..1, 255 = channels 0..255; a null channel gives null, a non-number is an error) |
| Convert | `to_int`, `to_float`, `to_bool` (bool, `"true"`/`"false"`, 0/1), `date:{of, formats:[...]}` -> ISO-8601 UTC (formats: `iso`, `epoch_s`, `epoch_ms`, or a `strptime` pattern) |
| Enum | `enum:{of, map:{"A":"x"}, default?}`; without `default` an unmapped value is an ERROR (used for `state` and `status`; never guessed) |
| Structure | `object:{k:e}` (omits null keys), `array:[e...]`, `each:{over, map, where?, flatten?}` (drops null items) |
| Rich text | `html_to_md:e` (fixed tag set); `text_from_tree:{of, config}` |
| Tree | `tree:{root, children, map}`: builds a node recursively (`@` = the raw node; depth cap 64, node cap 5000) |

`text_from_tree` config (rich-text tree -> Markdown, node-type strings come from the profile): `type_key` (default `type`), `text_node`, `text_key`, `children_key`, `block` (role -> node type, roles `paragraph`, `heading`, `bullet_list`, `ordered_list`, `list_item`, `code_block`, `blockquote`), `level_key` (heading level), `marks_key` + `marks` (`{"strong":"**","em":"_","code":"`"}`), `link` (`{type, url_key}`), `mention` (`{type, text_key}`), `hard_break`, `unknown_node` (`descend` renders children and records an unknown; `error` fails).

**Null is not empty.** An optional array that the payload does not carry must stay unknown, not become `[]`: map it with `each` over the bare `path` (`{"each": {"over": {"path": "threads"}, ...}}`), which gives null over null (omitted and listed in `unknowns`), and NOT over `threads[*]` (a wildcard over null is `[]`, which would claim "no threads"). The feedback gate treats a change without `threads` as unknown (fails) and an explicit empty list as none. Use `[*]` only when "absent" really means "none".

**Native contracts.** `factory ingest ... --format native` is accepted only for `design_frame` with `fidelity: approx` (hand-described from a mockup) and for `design_image`. Tickets, changes, change lists, pipelines and exact frames always go through a profile; the gates reject a contract whose provenance says `format: native` for those kinds. A change taken from a saved changes list (`review start`) is derived by the CLI from the mapped `change_list` contract; its provenance points at the list raw and the item index (`provenance.item`).

**Mapping limits.** Ingest and `profile test` run the mapping in a child process killed after `limits.mapping_timeout_s` of the workspace (default 10 s); a pattern that backtracks forever becomes a mapping error. `--ctx` cannot override the names the CLI sets (`id`, `key`, `number`, `repo`, `scm_project`).

Errors vs unknowns: a required contract field that evaluates to null, a type error (`to_int` of "abc", an unmapped `enum`), an `each` item that errors, or a contract that fails its schema are ERRORS (ingest exits 2 and writes nothing). An optional field that is null is omitted and listed in `unknowns`.

Minimal ticket map (synthetic provider):

```json
"ticket": {"ext": "json", "map": {
  "key": {"path": "id"}, "title": {"path": "head.title"}, "type": {"path": "head.kind"},
  "is_bug": {"in": [{"lower": {"path": "head.kind"}}, ["bug", "defect"]]},
  "status": {"path": "head.state.name"},
  "description_md": {"coalesce": [{"path": "body_text"}, {"lit": ""}]}},
  "optional": ["labels", "parent"]}
```

## Related

Intake of a real ticket needs a working profile first; `references/phases.md` intake says how raws land in `R/raw/<kind>/<id>.<ext>` and are ingested. A profile written for an older payload shape is re-checked with `factory profile test --golden`; contracts ingested under an older profile digest are reported by `factory ingest <run> --check`.
