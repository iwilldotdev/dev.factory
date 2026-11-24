# Provider-agnostic dev.factory: implementation design

Status: design, implemented in steps S1-S7 on branch `feat/provider-agnostic` (S8, the user's own workspace, is data outside the repo). Audience: maintainers; `DESIGN.md` and `claude/skills/factory/references/{profile,migration}.md` describe the code as it is now, this file keeps the rationale and the decisions.
Philosophy and motivation: `docs/research/2026-11-23-providers-e-instalacao-unificada.md` (PT-BR). This file is the binding spec; where the study and this file differ, this file wins.

This document names no provider, vendor or tool: old code is cited by its shape ("the old remote issue payload") or by `file:line` on `main` at `df5945a`, and old key names are described generically. It is covered by the banned-term test (section 11) like every other file.

## 1. Goal and invariants

After S7 the factory code, agents, skill, hooks, installer, tests and docs contain no reference to any server, tool, vendor or provider-specific term (banned list in section 11). Provider knowledge is DATA: declarative profiles outside the repo. Kept as-is: phases and gates, evidence-or-zero, raw payloads preserved, contracts, write intents verified by readback, fail-closed guards, independent review.

Invariants the code must keep:

1. The CLI never touches the network and never invokes a provider tool (true today: `factory/fetches.py:1-7`).
2. Gates and collectors read CONTRACTS only, never raw payloads.
3. A contract is valid only while its `provenance.raw_sha256` equals the sha256 of the raw file on disk (stale otherwise).
4. Mapping is pure and deterministic: same raw + same profile digest = byte-identical contract.
5. Unknown format = error with the list of declared kinds and samples; nothing is inferred to make a gate pass.

## 2. Run layout

`R` = `FACTORY_HOME/runs/<id>/`.

| Path | Writer | Content |
|---|---|---|
| `R/raw/<kind>/<id>.<ext>` | agent (any tool's save arg, CLI redirect, or `factory ingest --stdin`) | Untouched provider payload. `<id>` is the safe form of the entity id (`PROJ-1`, `repo-12`, `3544-5710`). |
| `R/contracts/<kind>/<id>.json` | CLI only (`factory ingest`) | Normalized contract + `provenance`. |
| `R/design/` | author (approx specs) + CLI | `design add` outputs for approx frames (section 7). |
| the old remote-payload and design-pack folders | removed | Replaced by the two rows above. |

Kinds: `ticket`, `change`, `change_list`, `pipeline`, `design_frame`, `design_image`. File extension is declared per kind in the profile (`json` by default; `png` for `design_image`).

Provenance block (every contract):

```json
"provenance": {
  "profile": "acme-tickets", "profile_digest": "sha256:9f1c...",
  "raw_path": "raw/ticket/ACME-12.json", "raw_sha256": "ab34...",
  "ingested_at": "2026-11-24T10:00:00Z",
  "source_hint": "fetch_ticket key=ACME-12 save=<raw_path>"
}
```

`source_hint` is optional free text the agent passes with `--source-hint` (what it actually ran). `profile_digest` = sha256 of canonical-JSON `profile.json` (samples excluded). A contract whose `profile_digest` differs from the current profile is stale only for `factory ingest --check`; gates warn but do not fail (profile edits must not invalidate proofs silently: re-ingest is explicit).

## 3. Contracts

Common rules: JSON, `schema` field `<kind>.v1`, UTF-8, no `null` for required fields, optional fields are omitted or `null`, `unknowns[]` lists `{field, reason}` for every optional field the mapping could not fill. All text is Markdown or plain text, never HTML or any vendor rich-text tree.

### 3.1 ticket.v1

Derived from what the code reads: summary/title (`ship.py:71-83`), attachments with id/filename/mimeType/content (`media.py:55-75`), issue type for bug classification (`run.py:93-103`), parent key (`ship.py:~520`, epic parent lookup), status for readback.

| Field | Type | Req | Notes |
|---|---|---|---|
| `schema` | `"ticket.v1"` | y | |
| `key` | string | y | Matches the profile `refs.ticket_key` regex. |
| `url` | string | n | |
| `title` | string | y | Replaces `fields.summary`. |
| `type` | string | y | Provider type name as written (`Bug`, `Story`). |
| `is_bug` | bool | y | Computed by the profile (`any`/`in` over type names). Replaces hard-coded `ISSUE_BUG_TYPES` (`run.py:89`). |
| `status` | string | y | Display name. Used by `issue_transition` readback. |
| `description_md` | string | y | Empty string allowed. |
| `acceptance_criteria` | string[] | n | Profile decides: a custom field, or a section parsed with `regex`/`split`. |
| `labels` | string[] | n | |
| `parent` | `{key, title?}` | n | Epic or parent ticket. |
| `assignee` | `{id, name?}` | n | |
| `links` | `{kind, key?, url}[]` | n | Relations, change URLs, design URLs. |
| `attachments` | `{id, name, mime, url, size?}[]` | n | `mime` lowercased; empty mime falls back to extension as in `media.py:40-46`. |
| `comments` | `{id, author, created, body_md}[]` | n | Intake reads them (comment thread). |
| `unknowns` | `{field, reason}[]` | y | May be empty. |
| `provenance` | object | y | |

### 3.2 change.v1

Derived from `ship.py:167-185,555-600` (verify), `review.py:154-230` (the old change-facts reader), `feedback.py:11-60` (threads), `gate.py:1076-1150` (feedback gate).

| Field | Type | Req | Notes |
|---|---|---|---|
| `schema` | `"change.v1"` | y | |
| `repo` | string | y | Workspace alias; set from ingest context (`--repo`), or resolved by `scm_project` match against `project`. |
| `number` | int | y | Provider number (was the provider's per-project id). Neutral ref: `repo#number`. |
| `url` | string | y | |
| `project` | string | n | SCM project path, for alias resolution (`review.py:197-203`). |
| `title` | string | y | Draft markers REMOVED by the profile (so `_strip_draft`, `ship.py:529`, disappears). |
| `description_md` | string | y | |
| `source_branch`, `target_branch` | string | y | |
| `head_sha` | string | y | |
| `state` | `open\|merged\|closed` | y | Profile enum map. |
| `draft` | bool | y | Profile computes it (flag OR title prefix). Replaces `is_draft` logic at `ship.py:581`. |
| `author` | string | n | SCM user id/name. |
| `reviewers` | string[] | n | |
| `approvals` | `{known: bool, by: string[]}` | y | `known=false` when the payload carries no approval data (`review.py:177-193` returns None). |
| `threads` | thread[] | n | See below. Absent = unknown, not "none"; `[]` = known empty. |
| `pipelines` | `{id, sha?, status}[]` | n | Latest-first by profile order or sorted by numeric `id`; replaces the `pipelines` list read at `gate.py:1100`. |
| `unknowns`, `provenance` | | y | |

thread: `{id, resolved: bool, resolvable: bool, system: bool, notes: [{author, body_md, system: bool, path?, line?}]}`. The profile folds provider "flat notes as one-note threads" (`feedback.py:25-34`) into this shape.

### 3.3 change_list.v1

`{schema, items: change.v1-lite[]}` where each item has the required `change.v1` fields except `threads`/`pipelines`. Feeds `review queue` and `review start` (`review.py:154-170`, `253-310`).

### 3.4 pipeline.v1

Derived from `feedback.py:48-70` and `gate.py:1105-1148`; two raw inputs today (pipeline + jobs, `feedback.py:107`).

| Field | Type | Req | Notes |
|---|---|---|---|
| `id` | string | y | |
| `url` | string | n | |
| `sha` | string | y | Compared with the change `head_sha` (`gate.py:1148`). |
| `status` | `pending\|running\|success\|failed\|canceled\|skipped` | y | Profile enum map; unmapped provider status = error, never guessed. |
| `jobs` | `{id, name, stage?, status, allow_failure: bool, url?}[]` | y | `status` same enum. |

### 3.5 design_frame.v1 and design_image

See section 7. `design_image` is a binary kind with no mapping: `ingest` validates PNG signature/IHDR/non-zero size (the check of the old design module, `:15-24`) and records `{path, sha256, width, height}`.

## 4. Mapping language

A mapping is a JSON object whose leaves are expressions. Evaluated against one or more named raw documents.

### 4.1 Scopes

`$` = main raw document. `$name` = additional raw named at ingest (`--raw jobs=path`; used by `pipeline` for the jobs list). `@` = current item inside `each`/`tree`. `@@` = parent item inside nested `each`. `$ctx` = ingest context (`repo`, `scm_project`, `number`, `key`, `id`).

### 4.2 Paths

`{"path": "fields.attachment[*].mimeType"}`. Grammar: dotted keys; `["a.b"]` quoted key; `[0]`, `[-1]` index; `[*]` wildcard (yields array); `[?key==lit]` or `[?key!=lit]` filter over array of objects. Missing key at any depth = `null` (no error). Wildcard over null = `[]`. Indexing a non-array or keying a non-object = type error.

### 4.3 Operators

| Group | Operators (all `{"op": args}`) |
|---|---|
| Value | `path`, `lit` (any JSON), `coalesce:[e...]` (first non-null, non-empty-string) |
| Boolean | `any:[e...]`, `all:[e...]`, `not:e`, `eq:[a,b]`, `in:[a,[lit...]]` (case-sensitive unless wrapped in `lower`), `exists:e` |
| String | `startswith:[s,p]`, `regex:[s,pat]` (bool, `re.search`), `regex_group:[s,pat,g]` (group name or number, null if no match), `regex_sub:[s,pat,repl]`, `lower`, `upper`, `trim`, `join:[arr,sep]`, `split:[s,sep]`, `concat:[e...]` |
| Convert | `to_int`, `to_float`, `to_bool` (accepts bool, `"true"/"false"`, 0/1), `date:{of, formats:[...]}` -> ISO-8601 UTC (formats: `iso`, `epoch_s`, `epoch_ms`, or `strptime` pattern) |
| Enum | `enum:{of, map:{"A":"x"}, default?}`; without `default` an unmapped value is an ERROR (used for `state`, pipeline `status`) |
| Structure | `object:{k:e}`, `array:[e...]`, `each:{over:e, map:e, where?:e, flatten?:bool}` |
| Rich text | `html_to_md:e` (stdlib `html.parser`, fixed tag set: p, br, h1-6, ul/ol/li, a, strong/em, code/pre, img, blockquote, table-as-text); `text_from_tree:{of, config}` |
| Tree | `tree:{root:e, children:path, map:object}`: builds a node recursively, `@` = the raw node, children mapped with the same `map`. Depth cap 64, node cap 5000 (error beyond). |

`text_from_tree` config (rich-text tree -> Markdown; no vendor names, only node-type strings from the profile):

```json
{"text_node":"text","text_key":"text","children_key":"content",
 "block":{"paragraph":"p","heading":"h","bullet_list":"ul","ordered_list":"ol","list_item":"li","code_block":"pre","blockquote":"quote"},
 "marks_key":"marks","marks":{"strong":"**","em":"_","code":"`"},
 "link":{"type":"link","url_key":"attrs.href"},
 "mention":{"type":"mention","text_key":"attrs.text"},
 "unknown_node":"descend"}
```

`unknown_node`: `descend` (render children) or `error`. Every node type not in config and descended is recorded in `unknowns`.

### 4.4 Errors vs unknowns

| Situation | Result |
|---|---|
| Required contract field evaluates to null/missing | ERROR `contract field <f> required; expression <path> gave null` (ingest exits 2, no contract written) |
| Optional field null | Field omitted; `unknowns += {field, reason:"null"}` |
| Type error in an operator (e.g. `to_int` of "abc", `enum` unmapped) | ERROR with the operator, input value (truncated 80 chars) and JSON pointer of the expression |
| `each` item errors | Whole contract errors (no partial arrays) |
| Rich-text node descended without config | `unknowns` entry, not an error |
| Contract fails its `<kind>.v1` schema after mapping | ERROR listing every violation |

The evaluator has no loops other than `each`/`tree`, no assignment, no I/O, no time/random. Regex runs with a 1 s per-expression guard.

## 5. Profile file format

`FACTORY_HOME/profiles/<name>/profile.json` + `samples/<kind>/<n>.<ext>` (raw samples, anonymized) + optional `samples/<kind>/<n>.expected.json` (golden contract; used by `profile test --golden`) + `approved.json`.

**Ownership and approval.** `profiles/<name>/` is approved data and the user's: gates and ingest read only it. Workers write DRAFTS under `FACTORY_HOME/profile-drafts/<name>/` (`profile.json` + `samples/`). `factory profile approve <name> --user-said "<gist>"` (main session only) refuses a draft that fails `profile check`, copies it to `profiles/<name>/` with an atomic swap (the previous version goes to `profiles/.backups/<name>-<ts>/`) and writes `approved.json` `{"file_sha256": sha256 of profile.json bytes, "digest": profiles.digest(doc), "approved_at", "user_said"}`. Loading a profile for gates, ingest, review or fetch hints REFUSES one whose `profile.json` sha256 differs from `file_sha256`, naming the approve command. `profile new|test|check` work on drafts (`--draft`; `new` always creates one) and on approved profiles (read-only: no `--write-golden`).

**Inputs.** `kinds.<kind>.inputs` lists the named raws; a trailing `?` (`"jobs?"`) makes one optional: ingest does not require it and an absent optional input reads as null in paths, so the map may `coalesce` to data found in the main raw. **Null is not empty:** an optional array maps with `each` over the bare `path` (`{"path": "threads"}`), never `[*]`, so a payload without the field stays unknown (a wildcard over null is `[]` and would claim "none").

**Mapping limits.** `map_docs` runs in a child process killed after `limits.mapping_timeout_s` (default 10) so a backtracking regex cannot hang the CLI; the static regex checks remain.

Top level:

```json
{
  "schema": "profile.v1", "name": "acme", "role": "tickets|scm|design",
  "refs": {...}, "kinds": {...}, "hints": {...},
  "cli_write_patterns": [...], "mcp_read_allow": [...], "save_args": [...]
}
```

- `refs` (tickets/scm): `ticket_key` regex with named group `key`; `change_url` regex with named groups `project`, `number`; `ticket_url` regex with `key`. Replaces `DEFAULT_TICKET_RE` (`review.py:135`) and the `ticket_regex` workspace key.
- `hints`: acquisition templates. Placeholders: `{key}`, `{id}`, `{number}`, `{scm_project}`, `{repo}`, `{sha}`, `{raw_path}` (absolute, forward slashes), `{raw_dir}`. Unknown placeholders stay literal in angle brackets, as `<placeholders>` do today (`fetches.py:~37`).
- `cli_write_patterns`: regexes that worker-guard treats as remote writes (section 9).
- `mcp_read_allow`: globs of tool names (`*`-wildcard on the tool segment) that the profile author certifies read-only despite containing a write-verb token.
- `save_args`: extra argument names that carry a destination path in tools of this provider (default set in section 9).

### 5.1 Complete synthetic example: `acme` (all three roles in one profile for brevity)

```json
{
  "schema": "profile.v1", "name": "acme", "role": "tickets",
  "refs": {
    "ticket_key": "(?P<key>[A-Z][A-Z0-9]*-\\d+)",
    "ticket_url": "https://acme\\.example/browse/(?P<key>[A-Z][A-Z0-9]*-\\d+)",
    "change_url": "https://scm\\.example/(?P<project>.+?)/changes/(?P<number>\\d+)"
  },
  "hints": {
    "ticket": "acme-cli ticket get {key} --out {raw_path}",
    "change": "acme-cli change get {scm_project} {number} --with threads,approvals --out {raw_path}",
    "change_list": "acme-cli change list {scm_project} --state open --out {raw_path}",
    "pipeline": "acme-cli ci get {scm_project} {id} --out {raw_path}; jobs: acme-cli ci jobs {scm_project} {id} --out {raw_path}.jobs",
    "design_frame": "acme-cli design node {file} {id} --out {raw_path}",
    "design_image": "acme-cli design render {file} {id} --scale 1 --out {raw_path}"
  },
  "kinds": {
    "ticket": {
      "ext": "json",
      "map": {
        "key": {"path": "id"},
        "url": {"concat": ["https://acme.example/browse/", {"path": "id"}]},
        "title": {"path": "head.title"},
        "type": {"path": "head.kind"},
        "is_bug": {"in": [{"lower": {"path": "head.kind"}}, ["bug", "defect"]]},
        "status": {"path": "head.state.name"},
        "description_md": {"coalesce": [
          {"text_from_tree": {"of": {"path": "body"}, "config": {"text_node": "text", "text_key": "value",
             "children_key": "nodes", "block": {"paragraph": "p", "bullet_list": "ul", "list_item": "li"},
             "unknown_node": "descend"}}},
          {"lit": ""}]},
        "labels": {"path": "head.tags"},
        "parent": {"object": {"key": {"path": "head.parent.id"}}},
        "attachments": {"each": {"over": {"path": "files"}, "map": {"object": {
          "id": {"path": "@.id"}, "name": {"path": "@.name"},
          "mime": {"lower": {"path": "@.mime"}}, "url": {"path": "@.href"}}}}}
      },
      "optional": ["url", "labels", "parent", "attachments", "acceptance_criteria"]
    },
    "change": {
      "ext": "json",
      "map": {
        "repo": {"path": "$ctx.repo"},
        "number": {"to_int": {"path": "n"}},
        "url": {"path": "link"},
        "project": {"path": "proj"},
        "title": {"regex_sub": [{"path": "title"}, "^(?i:draft:\\s*)", ""]},
        "description_md": {"coalesce": [{"path": "text"}, {"lit": ""}]},
        "source_branch": {"path": "from"}, "target_branch": {"path": "into"},
        "head_sha": {"path": "tip"},
        "state": {"enum": {"of": {"path": "status"}, "map": {"OPEN": "open", "DONE": "merged", "DROPPED": "closed"}}},
        "draft": {"any": [{"to_bool": {"path": "wip"}}, {"regex": [{"lower": {"path": "title"}}, "^draft:"]}]},
        "reviewers": {"each": {"over": {"path": "reviewers"}, "map": {"path": "@.login"}}},
        "approvals": {"object": {
          "known": {"exists": {"path": "approvals"}},
          "by": {"each": {"over": {"path": "approvals[*]"}, "map": {"path": "@.login"}}}}},
        "threads": {"each": {"over": {"path": "threads"}, "map": {"object": {
          "id": {"path": "@.id"}, "resolved": {"to_bool": {"path": "@.closed"}},
          "resolvable": {"to_bool": {"path": "@.can_close"}}, "system": {"lit": false},
          "notes": {"each": {"over": {"path": "@.msgs"}, "map": {"object": {
            "author": {"path": "@.by"}, "body_md": {"path": "@.text"},
            "system": {"to_bool": {"path": "@.auto"}},
            "path": {"path": "@.file"}, "line": {"to_int": {"path": "@.line"}}}}}}}}}},
        "pipelines": {"each": {"over": {"path": "ci"}, "map": {"object": {
          "id": {"path": "@.id"}, "sha": {"path": "@.sha"},
          "status": {"enum": {"of": {"path": "@.state"}, "map": {"ok": "success", "bad": "failed", "run": "running", "wait": "pending"}}}}}}}
      },
      "optional": ["project", "reviewers", "threads", "pipelines", "author"]
    },
    "change_list": {
      "ext": "json",
      "map": {"items": {"each": {"over": {"path": "results[*]"}, "map": {"object": {
        "repo": {"path": "$ctx.repo"}, "number": {"to_int": {"path": "@.n"}}, "url": {"path": "@.link"},
        "title": {"path": "@.title"}, "description_md": {"coalesce": [{"path": "@.text"}, {"lit": ""}]},
        "source_branch": {"path": "@.from"}, "target_branch": {"path": "@.into"}, "head_sha": {"path": "@.tip"},
        "state": {"enum": {"of": {"path": "@.status"}, "map": {"OPEN": "open", "DONE": "merged", "DROPPED": "closed"}}},
        "draft": {"to_bool": {"path": "@.wip"}},
        "approvals": {"object": {"known": {"exists": {"path": "@.approvals"}}, "by": {"each": {"over": {"path": "@.approvals[*]"}, "map": {"path": "@.login"}}}}}}}}}}
    },
    "pipeline": {
      "ext": "json", "inputs": ["main", "jobs?"],
      "map": {
        "id": {"path": "id"}, "url": {"path": "web"}, "sha": {"path": "commit"},
        "status": {"enum": {"of": {"path": "state"}, "map": {"ok": "success", "bad": "failed", "run": "running", "wait": "pending", "stop": "canceled"}}},
        "jobs": {"each": {"over": {"path": "$jobs.items"}, "map": {"object": {
          "id": {"path": "@.id"}, "name": {"path": "@.name"}, "stage": {"path": "@.stage"},
          "status": {"enum": {"of": {"path": "@.state"}, "map": {"ok": "success", "bad": "failed", "run": "running", "wait": "pending", "stop": "canceled", "skip": "skipped"}}},
          "allow_failure": {"to_bool": {"path": "@.soft"}}}}}}
      }
    },
    "design_frame": {
      "ext": "json", "fidelity": "exact",
      "map": {
        "frame": {"object": {"id": {"path": "id"}, "name": {"path": "name"}}},
        "source": {"object": {"file": {"path": "$ctx.file"}, "version": {"path": "rev"}}},
        "root": {"tree": {"root": {"path": "node"}, "children": "kids",
          "map": {"object": {
            "id": {"path": "@.id"}, "name": {"path": "@.name"},
            "type": {"enum": {"of": {"path": "@.kind"}, "map": {"FRAME": "frame", "TEXT": "text", "RECT": "shape", "IMG": "image"}, "default": "other"}},
            "visible": {"coalesce": [{"to_bool": {"path": "@.visible"}}, {"lit": true}]},
            "box": {"object": {"x": {"path": "@.bounds.x"}, "y": {"path": "@.bounds.y"}, "w": {"path": "@.bounds.w"}, "h": {"path": "@.bounds.h"}}},
            "render_box": {"object": {"x": {"path": "@.paint_bounds.x"}, "y": {"path": "@.paint_bounds.y"}, "w": {"path": "@.paint_bounds.w"}, "h": {"path": "@.paint_bounds.h"}}},
            "fills": {"each": {"over": {"path": "@.fills[*]"}, "where": {"eq": [{"path": "@.kind"}, {"lit": "solid"}]},
              "map": {"object": {"type": {"lit": "solid"}, "color": {"path": "@.hex"}, "opacity": {"coalesce": [{"to_float": {"path": "@.alpha"}}, {"lit": 1}]}}}}},
            "corner_radius": {"to_float": {"path": "@.radius"}},
            "text": {"object": {"characters": {"path": "@.chars"}, "font_family": {"path": "@.font.family"},
              "font_size": {"to_float": {"path": "@.font.size"}}, "font_weight": {"to_int": {"path": "@.font.weight"}},
              "line_height_px": {"to_float": {"path": "@.font.lh"}}}}
          }}}}
      },
      "optional": ["render_box", "corner_radius", "text", "fills"]
    },
    "design_image": {"ext": "png"}
  },
  "cli_write_patterns": ["^\\s*acme-cli\\s+(?:ticket|change|ci)\\s+(?:create|edit|comment|approve|merge|transition|retry)\\b"],
  "mcp_read_allow": [],
  "save_args": ["out"]
}
```

A profile is split in practice into one profile per role (`providers.tickets|scm|design` in `workspace.json`); the example merges them to show every kind. A profile may declare only the kinds of its role.

Workspace reference:

```json
"providers": {"tickets": "acme", "scm": "acme", "design": "acme"},
"repos": [{"alias": "api", "scm_project": "team/api"}],
"scm_user": "wgoncalves",
"ship": {"ticket_mode": "comment+transition", "ticket_comment": "change-links", "ticket_transition": "In review",
         "reviewers": [{"scm_user": "ana", "ticket_user": "u-123"}]}
```

### 5.2 Validity

A profile is VALID iff, for every kind it declares, at least one file in `samples/<kind>/` maps with zero errors and validates against the contract schema. `factory workspace check` fails when `providers.<role>` names a missing or invalid profile. `factory profile check <name>` runs the same logic; it also lints the mapping (unknown operators, unknown placeholders in hints, regex that fail to compile, refs without named groups).

## 6. CLI surface

| Command | Behaviour |
|---|---|
| `factory profile new <name> --role R [--draft]` | Scaffolds a DRAFT `profile-drafts/<name>/profile.json` (empty `kinds` for the role) and `samples/`; prints the authoring workflow below. |
| `factory profile test <name> [--draft] [--kind K] [--sample N] [--golden]` | Maps each sample; prints the contract, `unknowns`, and per-field source expression. `--golden` diffs against `*.expected.json`. Exit 2 on any error. |
| `factory profile check <name> [--draft]` | Validity (5.2) + lint. Exit 2 when invalid. |
| `factory profile approve <name> --user-said "<gist>"` | Main session only. Promotes a valid draft to `profiles/<name>/` and writes `approved.json` (section 5). |
| `factory ingest <run> <kind> <raw> [--id ID] [--repo A] [--profile P] [--raw name=path ...] [--source-hint S] [--format native]` | Resolve profile (default: `providers.<role of kind>`), validate raw exists and sha, map, validate schema, write `R/contracts/<kind>/<id>.json`. `--stdin` writes stdin to `R/raw/<kind>/<id>.<ext>` first. `--format native` accepts a raw that already conforms to the contract (no mapping) ONLY for `design_frame` with `fidelity: approx` and for `design_image`; every other kind is refused, and `contracts.validate` rejects `provenance.format == native` for tickets, changes, change lists, pipelines and exact frames (gates and `load_contract` consumers see a problem). `--ctx` cannot set the names the CLI derives (`id`, `key`, `number`, `repo`, `scm_project`). A change taken from a saved list (`review start`) is derived by the CLI from the mapped `change_list` contract with `provenance.item` (index) and the list raw as `raw_path`. |
| `factory ingest <run> --check` | Re-hash every raw; report stale contracts. |
| `factory design add\|check\|expect <run> ...` | Section 7. Replaces the old design-pack `check` and `expect` commands (old design module `:262-296`). |
| `FETCH` lines (`gate`, `ship verify`, `feedback ingest`, `design check`) | Rendered from `hints.<kind>` of the profile; `--json` emits `{kind, id, raw_path, hint, ctx}` (the `{tool,args}` shape of `core.py:71-82` is removed). |

Authoring workflow (documented in the author agent and `references/profile.md`, driven by the author worker): (1) `profile new` (a draft under `profile-drafts/`); (2) ask the user for the provider and for real, anonymizable sample payloads per kind (never invent a sample); (3) when a field is ambiguous, research the provider's public API docs on the web and cite the page in a `notes` field of the profile; if still ambiguous, return `blocked` with the question; (4) write the mapping; (5) `profile test --draft` and show the user the contract and `unknowns`; (6) `profile check --draft`; (7) the user approves; the main session runs `profile approve <name> --user-said "<gist>"`. Unknowns the user accepts are listed in the profile `accepted_unknowns`.

Hint templates replace the builders in `fetches.py` (every `fetch_item("<tool>", ...)`, lines 35, 102, 109, 117, 149, 169, 193, 196, 209, 218). `fetches.py` reduces to `needs(kind, id, ctx) -> [{kind,id,raw_path,hint}]`: which contract is missing or stale for the phase.

## 7. Design pillar

`design_frame.v1` (one per frame) is the contract; the shape of the old design source's node payload, read in the old design module (`:131-205`), `gate.py:233-262,295-345` and `pngcrop.py:111-178`, is the template.

```json
{
  "schema": "design_frame.v1", "fidelity": "exact",
  "frame": {"id": "3544:5710", "name": "Empty list", "image": {"path": "raw/design_image/3544-5710.png", "width": 1440, "height": 900, "scale": 1}},
  "source": {"file": "file-ref", "version": "rev-77", "fetched_at": "..."},
  "root": { "<Node>": "..." },
  "unknowns": [], "provenance": {}
}
```

Node: `id` (str, req), `name` (str, req), `type` (`frame|group|text|shape|image|component|instance|other`, req), `visible` (bool, default true), `box` `{x,y,w,h}` (req), `render_box` `{x,y,w,h}` (opt; replaces `absoluteRenderBounds`, used by crop at `pngcrop.py:124-131`), `opacity` (0-1), `fills[]` and `strokes[]` (`{type: solid|gradient|image, color: "#rrggbb", opacity}`; strokes add `weight`, `align`), `effects[]` (`{type: drop_shadow|inner_shadow|layer_blur|bg_blur, color?, offset{x,y}, radius, spread}`; feeds `effect_margins`, `pngcrop.py:~95-108`), `corner_radius`, optional `corner_radii` `[tl,tr,br,bl]` (projects to `borderTopLeftRadius`..`borderBottomLeftRadius`, plus `borderRadius` when all four are equal; flagged in `estimated` for approx), `layout` (`{mode: none|horizontal|vertical, gap, padding{t,r,b,l}}`), `text` (`{characters, font_family, font_size, font_weight, line_height_px, letter_spacing, align, color}`), `children[]`, `estimated` (map of dotted field path -> `true`; required non-empty only for `approx`). Colors are normalized to `#rrggbb` by the profile (the REST 0-1 float triples at `gate.py:187-190` are the profile's job).

Consequences for current code:

- `gate.style_map_errors` (`gate.py:295`) validates expected CSS values against "same property family" values found by `payload_facts` (`gate.py:233`). New: facts are extracted from normalized node properties through a fixed table (`font_size -> fontSize`, `fills[0].color -> backgroundColor|color`, `layout.gap -> gap`, ...), so the walk is over contracts, not raw JSON. The table is code but vendor-neutral.
- `factory design expect <run> <frame> --selector S` builds the skeleton from the contract (old design module `:215-260`; `extract_expect` becomes a node-property -> CSS-name projection).
- The old design-crop function (`pngcrop.py:156`) reads `box`/`render_box`/`effects` from the contract and `frame.image.path`.
- `collectors/visual.py:16-43` and the old node-file helper use the frame contract's image path. Check field `node` stays; add `frame` alias; the old file-key check field becomes `source_file` (acs.json migration: author rewrites, plan lock handled in section 10).
- Revision check (`gate.py:515-531`, old design module `:100-120`) becomes: `source.version` required when `fidelity: exact`.

### 7.1 approx

`factory design add <run> <image> --name N --fidelity approx [--id ID]`: copies the image, decodes it (PNG only; other formats converted by the agent before, with the conversion command recorded in `--source-hint`), normalizes to PNG, writes `raw/design_image/<id>.png`, creates an empty frame contract skeleton (`fidelity: approx`, root = single `frame` node whose `box` = image size, `estimated: {"box": true}`). The author worker then writes `raw/design_frame/<id>.json` in native format (regions, approximate measures, sampled colors, texts), EVERY numeric/color value listed in `estimated`; `ingest design_frame --format native` rejects an approx spec with a numeric value not flagged `estimated` (the check walks every number and color).

Gate rules for approx (never counts as exact):

| Rule | Where |
|---|---|
| Style checks on estimated values use `max(tolerance_px, limits.approx.px)`; colors compare with CIE76 ΔE <= `limits.approx.color_delta_e`; font size within `limits.approx.font_size_px` | `gate plan` (`validate_acs`), collector `style` |
| `visual` check `max_diff_ratio` may rise to `limits.approx.max_diff_ratio` (still capped by workspace limits) | `gate plan`, `collectors/visual.py` |
| The `review` gate requires, per approx frame referenced by an AC, a recorded user design review: `factory run decide <run> "<frame>: design reviewed" --by user --user-said "<gist>" --design-review <frame>` | `gate review` |
| The ship description lists approx frames under "Design fidelity: approximate" | `ship plan` |

The approx ceilings and tolerances apply ONLY when the frame CONTRACT is `approx` (never because a check says so); a check with `fidelity: "approx"` on an exact frame is a plan error and keeps the exact ceilings. `--format native` produces only approx frames.

Defaults proposed: `approx = {px: 4, color_delta_e: 6, font_size_px: 2, max_diff_ratio: 0.10}` in `DEFAULT_LIMITS`; configurable in `workspace.limits.approx`.

## 8. Acquisition and write intents

### 8.1 Acquisition

The agent fetches with whatever tool exists and saves under `R/raw/<kind>/<id>.<ext>`; the main session (or author) runs `factory ingest`. Gates' `FETCH` output tells which `(kind, id)` is missing, the exact raw path, and the profile hint. Gates read contracts; a missing contract whose raw exists prints `factory ingest <run> <kind> <raw>`.

### 8.2 Intents (ship-plan.json `schemaVersion: 2`)

Replaces the old action types (push, create-or-update request, readback, tracker comment, tracker transition, update description; `ship.py:~500-523,640`, `epic.py:293-306`, `review.py:546-549`). `push` stays (git). Bodies are Markdown; the executing agent converts. No rich-text trees in the plan (the old document-tree comment builders, `ship.py:415` and `epic.py:294-297`, are removed). Link placeholders of the old request-URL form (`ship.py:~210`) become `{change_url:a:t}`, resolved from the readback contract by the executor.

| Intent | Fields | Readback (re-ingest, then compare) |
|---|---|---|
| `push` | repo, target, branch | local `git ls-remote` is NOT used; `change.head_sha` equals proven head |
| `change_request` | op `create\|update`, repo, target, title, body_md, source, target_branch, draft, reviewers | `change.v1`: `source_branch`, `target_branch`, `title` (draft-free), `draft`, `head_sha`, `reviewers` |
| `set_draft` | repo, target, draft | `change.draft` |
| `change_comment` | repo, target, body_md | `change.threads` or notes contain the body's first line (best-effort; `needs_user` always true) |
| `change_approve` | repo, target | `change.approvals.by` contains `scm_user` |
| `issue_comment` | issues[], body_md | `ticket.comments` contains body first line |
| `issue_transition` | issues[], to_status | `ticket.status == to_status` |
| `pipeline_watch` | repo, target, sha | `pipeline.sha == sha` and normalized `status` |
| `readback` | kind `change\|ticket\|pipeline`, id, save_to (raw path) | (the re-fetch itself; followed by `ingest`) |

`ship verify` (`ship.py:548-600`) loads `contracts/change/<repo>-<target>.json`; draft comparison uses the bool (no title-prefix logic); `local_link_errors` (`ship.py:410`) stays on `description_md`. `check_dependencies` (`ship.py:188-203`) scans change contracts with `state == merged` and a ticket-key match in title/description/source_branch (no `json.dumps` substring). `_needs_user` (`ship.py:~420`) uses the new type names.

Run state (`run.json`): the old request list -> `changes[]` with `number` instead of the provider's per-project id (`review.py:348`, `gate.py:1167-1174`, `batch.py:113`); display `repo#N`.

Workspace neutral names: `repos[].scm_project` (was the old SCM-named project key, `workspace.py:59`, `fetches.py:138`, `review.py:232`); `scm_user` (was the old SCM-named user key, `review.py:255`); `ship.reviewers[].scm_user` and `.ticket_user` (`ship.py:126-132`); `ship.ticket_mode` (was a tracker-named key with a tracker-named modes constant, `ship.py:67`); `ship.ticket_comment: "change-links"` (was a tracker-named comment key with a request-named value); `ship.ticket_transition` (was a tracker-named transition key, also in `epics[]`, `epic.py:304`); `ticket_regex` removed (profile `refs`). `title_format` keeps `{key} {summary} {repo} {target}`; the legacy request-named title format key is dropped.

## 9. Guards

Constraints (Claude Code docs): subagent `tools:` accepts `mcp__<server>__*` per server (a server-agnostic `mcp__*` is not documented for `tools:`); omitting `tools:` inherits all MCP tools; `disallowedTools` accepts `mcp__<server>__*` and `mcp__*` but not infix patterns like `mcp__*__create_*`; PreToolUse input carries `agent_type`/`agent_id` inside subagents; hook matchers are unanchored regexes.

Design:

1. **Tool lists generated.** `install.py` reads the MCP servers configured for the target profile (server NAMES only, from the host MCP config) and writes `tools: Read, Grep, Glob, Bash, Write, mcp__<server>__*, ...` into the rendered agents (`{{MCP_TOOLS}}` placeholder in `claude/agents/factory-*.md`; the repo files contain the placeholder, no names). `--mcp-servers a,b` overrides; none configured = no MCP entries (the factory still works with CLI acquisition). `disallowedTools` name lists are removed from all four agents (today: `factory-scout.md:7`, similar in author/auditor/builder).
2. **Read-only enforced in the hook.** `worker-guard.js` (already matched on tool names `mcp__` at `worker-guard.js:178`) classifies any `mcp__<server>__<tool>` call made inside a worker (`role` arg; `agent_type` from the hook input is cross-checked and, when present and not a `factory-*` worker, the guard exits 0 so it never touches the main session):
   - Split the tool segment into tokens (snake, kebab, camelCase).
   - `READ_VERBS` = get, list, search, read, fetch, find, lookup, query, describe, show, view, download, count, whoami, me, export, resolve.
   - `WRITE_VERBS` = create, update, save, add, edit, write, transition, merge, accept, delete, remove, post, send, approve, unapprove, publish, run, trigger, retry, cancel, play, apply, link, unlink, watch, upload, complete, reopen, close, label, assign, move, set, resolve_thread, rebase, revert, fork, star, push.
   - Rule (as implemented): tokens are scanned left to right; any write verb anywhere makes the tool WRITE; else the first read verb makes it READ (namespace prefixes tolerated); no known verb is WRITE; a profile `mcp_read_allow` glob makes it READ. Everything else is WRITE. Ambiguous or unknown = WRITE (fail-closed). `save_to`-style destination args are still checked by the existing rule (`worker-guard.js:178-184`); the arg-name set becomes `{save_to, output_path, output_file, out, dest}` plus profile `save_args`.
   - WRITE from any worker = deny, message `remote writes belong to the main session`. The author's MCP calls are also read-only; the author writes files with `Write`.
3. **CLI writes.** `worker-guard.js:32` (a blanket ban of two named provider CLIs whose message named a server) becomes: generic patterns (`curl|wget|http` with `-X|--request POST|PUT|PATCH|DELETE`, `-d|--data|-F|--form`; `git push` stays at `:25`) + every regex in the active profiles' `cli_write_patterns`. Read use of provider CLIs is allowed (output redirected under `R/raw/`). The guard loads profile patterns via `FACTORY_HOME/profiles/*/profile.json` named in `workspace.json` (read-only, fail-closed: unreadable profile = generic patterns only plus deny of unknown network CLIs is NOT attempted; documented limitation).
4. **protect.js.** The carve-out for the old design-pack folder (`inRunSub(p, home, ...)`, `protect.js:~49-53`) becomes `raw`: MCP/Bash/Write saves are allowed under `R/raw/**` only; `R/contracts/**` is CLI-owned (added to `isCliOwned`); `R/design/**` and `acs.json`, `checks/`, `diagnosis.md` stay author-owned (builder/auditor/scout denied, `worker-guard.js:178-184`).
5. **Main session.** Unchanged: remote writes are governed by the host permission system and the ship plan; the factory does not edit permission settings.

Hook tests (`claude/hooks/test/*.test.mjs`) gain a table-driven classifier test (>= 80 synthetic tool names, including ambiguous ones such as `do_thing`, `sync`, `get_and_delete`, camelCase `getCreateMeta`).

## 10. Gate changes per phase and in-flight migration

| Phase / command | Today | After |
|---|---|---|
| `intake` (`gate.py:564-575`) | `remote/issue-KEY.json` exists | `contracts/ticket/KEY.json` valid, fresh; `media.errors` reads `ticket.attachments` (`media.py:55-75`); bug classification from `ticket.is_bug` (`run.py:93-103,130`); a ticket with `is_bug: true` in a non-bug run FAILS intake (restart as bug, or record `run decide "<KEY>: not a bug - <reason>" --by user`); `ticket.unknowns` surfaced as gap-round input |
| `plan` (`gate.py:374-552,591`) | pack files + `meta.json` revision + style maps vs raw JSON | frames' contracts + image fresh; `exact` needs `source.version`; style maps checked against contract facts; approx tolerances; `design check` replaces the old pack check |
| `reproduce` | unchanged | unchanged |
| `build`, `prove` | unchanged | unchanged except `visual` collector reads frame image |
| `review` (`gate.py:1065`) | reviews + proof | + design-review decision per approx frame |
| `ship plan/verify` (`ship.py`) | old request payload + document-tree comment | intents + `change.v1` readback (section 8) |
| `feedback` (`gate.py:1076-1149`) | discussions + pipeline JSON | `feedback ingest` reads only the changes of `ship-plan.json` and reports stale or invalid contracts instead of dropping them; `change.threads` (absent = unknown, fails; explicit `[]` = none) unresolved count, `pipeline.v1` (latest by numeric id, `status == success`, `sha == change.head_sha`); waiver regexp on pipeline id unchanged |
| review runs (`gate.py:1167-1215`) | old per-request remote payload | `contracts/change/<alias>-<N>.json`; `review start/queue` ingest `change`/`change_list`; `--change alias#N[=KEY]` (`review.py:295`); queue `--me` -> `--me` matched against `approvals.by` using `scm_user` |
| `epic` (`epic.py:280-309`) | document-tree comment + provider-named actions | intents of section 8 with `child` |

`feedback.ingest` finding `source` values: `thread:<id>` and `job:<id>`; when deduplicating against `reviews/feedback-*.json` (`feedback.py:93-97`), a legacy `discussion:<id>` is treated as `thread:<id>`.

### 10.1 In-flight run migration

Rule: runs in phases `intake` and `plan` (nothing locked, or lock cheap to redo) are migrated; runs in `build` or later are migrated only if the user prefers it over finishing on the old release. The factory ships ONE generic tool and no provider knowledge:

`factory migrate apply <run|--all> <map.json> --user-said "<gist>"` (main session only; the user approves the map): applies a JSON list of operations, then bumps `run.json.schemaVersion`, re-seals the plan lock, and records a user decision and event `migrate {map_sha256, old_lock, new_lock, user_said}`. Targets inside `contracts/`, `evidence/`, `reviews/`, `baseline/` and CLI-owned `run.json` keys are refused, and a run whose plan lock already fails (`lock_errors`) is refused before anything is applied, so the re-seal absorbs only the map's own operations.

| Op | Effect |
|---|---|
| `{"op":"rename_key","file":"run.json","path":"<old list key>","to":"changes"}` (path supports `[*]`) | Rename key |
| `{"op":"move_file","from":"<old folder>/<old prefix>-*.json","to":"raw/ticket/{1}.json"}` | Move raw; `*` captured |
| `{"op":"ingest","kind":"ticket","raw":"raw/ticket/*.json"}` | Runs the normal ingest with the workspace profile |
| `{"op":"replace_text","file":"acs.json","from":"<old key>","to":"source_file"}` | Literal replace in JSON keys |

The map file is authored by an agent following `references/migration.md`, lives in `FACTORY_HOME/migrations/<name>.json`, and contains the provider-specific key names (so the repo does not). The workspace itself is migrated the same way (`ws` ops: the old SCM-named project key -> `scm_project`, etc.) once, in S8. Synthetic example in `references/migration.md`. `ship-plan.json` (schema 1) is not migrated: `ship plan` refuses a v1 plan with "re-run ship plan" (the plan is derived data). Evidence is not touched: it references checks and heads, not remote payloads; the lock re-seal is the only integrity-relevant change, and it is a recorded event rather than a silent rewrite. `FETCH`/`design` renames: a run whose `acs.json` still has the old file-key check field fails `gate plan` with the exact migrate op to run.

## 11. Test strategy

- All fixtures synthetic: the `acme` format (section 5.1), kept in `tests/fixtures/profiles/acme/` with samples and `*.expected.json`. A second synthetic profile `zeta` (different shapes: nested rich text, array-of-pairs) proves no acme assumptions leaked into the engine.
- Unit: one test per operator (typical, missing path, type error); path grammar fuzz; `text_from_tree` goldens; determinism (map twice = identical bytes); `tree` depth/node caps; regex timeout.
- Contract tests: schema validators per kind; `unknowns` population; stale detection (mutate raw -> `ingest --check`).
- Replace provider-shaped test fixtures in `tests/` (`fake_cli.py`, `test_fetch_hints.py`, `test_install.py`, `real_support.py`, review/epic/ship flow tests) with contract fixtures; flow tests run the real CLI end to end on `acme`.
- Guard classifier table test (section 9).
- Migration test: a synthetic v1 run directory + map file -> expected tree.
- **Grep regression test** `tests/test_provider_agnostic.py`: scans the repo (`git ls-files` plus untracked, non-ignored files) for the banned list, case-insensitive: provider and vendor names, the old server name, old tool-name and key fragments, and concrete `mcp__<server>__<tool>` names (only the synthetic server names used by tests and host-owned session/browser tools are allowed). The patterns live in the test file, built from fragments so the file does not match itself. Excluded paths: `docs/research/`, `CHANGELOG.md`, `Handoffs/`, `.claude/worktrees/`, `.tmp/`, `node_modules/`, `browser/dist/`, and the test file itself. A second assertion rejects any file under the repo named `profile.json` (the repo ships no provider profile).

## 12. Step plan

| Step | Scope | Files | Done when |
|---|---|---|---|
| **S1 core** | Contract schemas (pure-python validators, no deps), mapping engine, profile loader, `ingest`, `profile new/test/check`, provenance and stale check, `native` format | new `factory/contracts.py`, `factory/mapping.py`, `factory/profiles.py`, `factory/commands/{ingest,profile}.py`; `cli.py` registration; `core.py` helpers; `protect` rule for `raw/` and `contracts/` (hook change here, tests) | Operator and contract tests green; `acme` samples all map; `workspace check` validates `providers` |
| **S2 tickets** | `ticket.v1` consumers: intake gate, media, bug classification, ship `_ticket_summary`, `fetches` hints from profile; ticket refs from `refs`; `run start <ref>` accepts URL | `gate.py` intake, `media.py`, `run.py:89-153`, `ship.py:71-83`, `review.py:135,238`, `fetches.py` | Intake flow test on `acme` and `zeta`; media manifest unchanged semantics |
| **S3 design** | `design_frame.v1`/`design_image`, `factory design add/check/expect`, style-map check over contract facts, `pngcrop` + `visual` on contracts, approx rules, `limits.approx`, design-review decision | the old design module -> `design.py`, `gate.py:233-345,515-552`, `pngcrop.py`, `collectors/{visual,style}.py` | Exact and approx flow tests; approx never satisfies an `exact` requirement; review gate blocks without decision |
| **S4 delivery** | Intents, `change.v1`/`pipeline.v1` readback, `ship plan/verify/update-description`, epic, `pipeline_watch`, `ship-plan` v2, neutral workspace keys | `ship.py`, `epic.py`, `fetches.py`, `workspace.py:59`, `core.py:repo_conf` | `ship verify` passes on `acme` contracts; draft/title/sha/branch mismatches fail; epic plan has no rich-text tree |
| **S5 review/feedback/batch** | `change_list`, `threads`, `approvals`, `repo#N`, `review queue/start/report` intents, `feedback ingest` from contracts, batch display | `review.py`, `feedback.py`, `batch.py`, `gate.py:1076-1215` | Review and feedback flow tests on contracts; legacy `discussion:` dedupe test |
| **S6 guards, agents, installer** | Classifier, generated tool lists, CLI write patterns from profiles, `raw/` carve-out, remove name lists and env merge | `worker-guard.js`, `protect.js`, `protect-lib.js`, `claude/agents/*.md`, `install.py:11-12,345-400,636-680`, `scripts/install-profiles.ps1` | Guard table test; `install --dry-run` output has no provider names; agents render with and without MCP servers |
| **S7 skill/docs + grep test** | Rewrite `SKILL.md`, references (`design-pack`, `ship`, `review`, `phases`, `workers`, new `profile.md`, `migration.md`), `DESIGN.md`, `README`, `AGENTS`/snippets; add `tests/test_provider_agnostic.py` | `claude/skills/factory/**`, docs | Grep test green on the whole repo |
| **S8 user workspace** | In the user's FACTORY_HOME (not in the repo): author profiles through the guided workflow, validate with real samples, write the migration map, migrate workspace and in-flight runs, delete `docs/design/provider-agnostic.md` banned content if desired | `FACTORY_HOME/profiles/**`, `FACTORY_HOME/migrations/**` | `workspace check` green; one real run reaches `ship verify` through contracts |

S1 must land before anything else; S2, S3, S4 can be parallel after S1; S5 needs S4; S6 may start after S1; S7 last before S8.

## 13. Risks

1. **Mapping expressiveness.** Real payloads may need operators not listed (e.g. cross-array joins, base conversions). Mitigation: operator set is versioned in `profile.v1`; adding one is a code change with tests. The first real profile (S8) is the stress test.
2. **Profile authoring burden / wrong guesses.** The validity rule only proves the mapping runs on samples, not that it is right for all payload variants (e.g. custom fields vary per site). Mitigation: `unknowns` surfaced at intake, `--golden` files, and user approval at step 7 of the workflow.
3. **Rich-text breadth.** `text_from_tree` and `html_to_md` cover common nodes; tables, panels and embeds degrade to text with `unknowns`.
4. **Guard classifier false positives/negatives.** Verb tokens can mislead (a read tool named `get_or_create_cache`). Fail-closed blocks it (workers hit a denied read); the profile `mcp_read_allow` is the escape. A write tool with an unlisted verb and a leading read verb would pass: the write-verb list must be extended with real tool inventories during S8.
5. **Design `exact` coverage varies by source.** A source that returns code instead of nodes yields a thin frame with many `unknowns`; the gate must refuse `exact` when required node properties (`box`, fills for checked values) are absent.
6. **Lock re-seal during migration** changes what `gate plan` locked; justified by the recorded event, but it is a deliberate exception to "locked after plan".
7. **Windows paths** in hint templates and in `raw_path` (forward slashes required, as `core.py:66-68` does today).
8. **Stale-by-profile-edit policy** (warn, not fail: `gate` prints `WARN` for contracts whose `profile_digest` differs from the current approved profile) could hide a mapping change that alters meaning; `ingest --check` is advisory and `review` prints profile digests in the report.

## 14. Decisions on the open questions (binding)

1. Pipeline: named raws (`--raw jobs=path`), as specified.
2. `limits.approx` defaults stay as proposed; calibrated on the first real mockup run.
3. `factory migrate apply` stays in the repo as a generic, provider-free tool.
4. `change_comment`/`issue_comment` readback: first-line match, best effort, and `needs_user: true`.
5. worker-guard reads the active profiles on each call (no cache).
6. Agent tool lists are rendered at install; adding an MCP server needs a re-install, and `install.py` prints the servers it used.
7. Safe ids: characters outside `[A-Za-z0-9._-]` become `-`; two ids mapping to the same safe name in one run is an ERROR naming both.
8. Non-PNG to PNG conversion is an agent step, recorded in `--source-hint`.
9. Banned-term list (section 11) amendments: bare words that collide with common vocabulary (a CSS gradient keyword, colour names) are not banned; only vendor-specific forms of those names are. This file is NOT excluded from the test: its code citations are neutral and provider-specific migration details live in `FACTORY_HOME/migrations/` notes.
10. The protect-hook carve-out for `raw/` and `contracts/` moves from S1 to S6, so S1 is Python-only and S6 (hooks, agents, installer) can run in parallel with S2-S5.
11. Steps do not commit; focused tests per step; the full suite runs once at the end.

## 15. Open questions (original)

1. Single `raw/<kind>/<id>.<ext>` for `pipeline` has two inputs (pipeline + jobs): accept `--raw jobs=path` (this spec), or require agents to merge them? Spec picks named raws.
2. Default `limits.approx` values (px 4, ΔE 6, font 2, diff ratio 0.10) are proposals; need calibration on real mockups.
3. Should `factory migrate apply` exist in the repo at all, or should migrations be one-off scripts outside it? Spec keeps a generic, provider-free tool because run.json edits are hook-protected from agents.
4. `change_comment`/`issue_comment` readback is best-effort (first-line match); is that enough evidence, or should these intents stay `needs_user` with no readback requirement?
5. How are the profile `cli_write_patterns` obtained by worker-guard in a worker started before a profile is edited (caching, digest pinning)? Spec reads on each hook call; cost is one small file read.
6. Rendering of agents: install-time substitution (`{{MCP_TOOLS}}`, as `{{HOOKS_DIR}}` is today) means a newly added MCP server needs a re-install. Acceptable?
7. Provider ids with characters unsafe for filenames (`3544:5710`, `group/proj#42`): safe-name function is `node_file`-like; collisions policy not specified.
8. Whether `design_image` conversion from non-PNG belongs in the CLI (needs an image library, which the repo avoids; `pngcrop.py` is stdlib-only) or stays an agent step.
