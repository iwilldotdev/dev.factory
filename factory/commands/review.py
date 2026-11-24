"""`factory review record|queue|start|report|fix` (DESIGN §2.5, §7)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .. import contracts, integrity, mapping, profiles
from ..collectors.test import copy_main_files, files_from_main_of, prepare_worktree, setup_command
from ..core import (SCHEMA_VERSION, SESSION_HINT_VARS, code_repos, session_hint, now, FactoryError, GateFailed, event, factory_home, file_sha256, git, git_dirty,
                    git_head, load_evidence, read_json, repo_conf, repo_for_project,
                    repo_path_for_run, run_dir, run_load, run_save,
                    workspace_load, write_json)
from . import gate, ship
from . import ingest as ingest_cmd
from .epic import _new_run

SEVERITIES = ("blocker", "major", "minor")


def validate(rec: dict, kind: str, acs: list[str]) -> list[str]:
    errors = []
    if rec.get("verdict") not in ("APPROVE", "CHANGES"):
        errors.append("verdict must be APPROVE or CHANGES")
    if not rec.get("reviewer"):
        errors.append("reviewer is required")
    findings = rec.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be a list")
        findings = []
    ids = set()
    for f in findings:
        fid = f.get("id")
        if not fid or fid in ids:
            errors.append(f"finding id missing or duplicate: {fid!r}")
        ids.add(fid)
        if f.get("severity") not in SEVERITIES:
            errors.append(f"finding {fid}: severity must be one of {SEVERITIES}")
        if not f.get("text"):
            errors.append(f"finding {fid}: text is required")
    fixed = rec.get("resolved")
    if fixed is not None and not (isinstance(fixed, list) and all(isinstance(x, str) and x for x in fixed)):
        errors.append("resolved must be a list of finding ids from earlier rounds that are now fixed (omit it when none)")
    comp = rec.get("completeness")
    if not isinstance(comp, dict) or not all(isinstance(comp.get(k), int) and not isinstance(comp.get(k), bool)
                                             for k in ("done", "gap", "not_checked")):
        errors.append("completeness needs integer done, gap, not_checked")
        comp = None
    if rec.get("verdict") == "APPROVE":
        if any(f.get("severity") in ("blocker", "major") for f in findings):
            errors.append("APPROVE with open blocker/major findings (a `resolved` flag does not count: "
                          "resolution is shown by a later round)")
        if comp and (comp["gap"] > 0 or comp["not_checked"] > 0):
            errors.append(f"APPROVE with completeness gap {comp['gap']} / not_checked {comp['not_checked']}: "
                          "what was not verified cannot be approved")
    if kind == "qa" and acs:
        cover = {c.get("ac"): c for c in rec.get("ac_coverage") or []}
        for ac in acs:
            if ac not in cover:
                errors.append(f"qa review must state for {ac} whether its checks exercise it (ac_coverage)")
            elif cover[ac].get("exercises") is False and rec.get("verdict") == "APPROVE":
                errors.append(f"{ac}: checks do not exercise the AC; verdict cannot be APPROVE")
    return errors


AUDITOR_ROLE = "auditor"


def _same_commit(claimed, head: str) -> bool:
    """A claimed SHA matches the HEAD when it is the full SHA or a prefix of at least 7 hex characters."""
    if not isinstance(claimed, str):
        return False
    text = claimed.strip().lower()
    return len(text) >= 7 and bool(re.fullmatch(r"[0-9a-f]+", text)) and head.lower().startswith(text)


def cmd_record(args) -> int:
    if os.environ.get("FACTORY_ROLE") != AUDITOR_ROLE:
        raise FactoryError("review record is for the auditor: run it as `FACTORY_ROLE=auditor factory review record ...` "
                           "from the factory-auditor agent (the guard lets only that role set it)")
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    rec = read_json(Path(args.file))
    if not isinstance(rec, dict):
        raise FactoryError("review file must be a JSON object")
    if not code_repos(run):
        raise FactoryError("run has no code repos to bind the review to (env_only repos are not reviewed)")
    acs: list[str] = []
    acs_path = run_dir(run["id"]) / "acs.json"
    if acs_path.is_file():
        acs = [a["id"] for a in read_json(acs_path).get("acs", []) if a.get("id")]
    errors = validate(rec, args.kind, acs)
    shas = {alias: git_head(repo_path_for_run(run, ws, alias)) for alias in code_repos(run)}
    claimed = rec.get("repos")
    if claimed and not isinstance(claimed, dict):
        errors.append('"repos" must be an object mapping repo alias to the commit SHA reviewed, e.g. '
                      f'{{"repos": {{"{next(iter(shas))}": "{next(iter(shas.values()))}"}}}} (got {type(claimed).__name__}); '
                      "omit it to review the current HEADs")
    elif claimed:
        for alias, sha in shas.items():
            said = claimed.get(alias)
            if said is not None and not _same_commit(said, sha):
                errors.append(f"review was made against {alias}@{str(said)[:8]}, HEAD is {sha[:8]}")
    if errors:
        raise GateFailed(errors)
    rdir = run_dir(run["id"]) / "reviews"
    existing = [int(p.stem.rsplit("-", 1)[1]) for p in rdir.glob(f"{args.kind}-*.json")
                if p.stem.rsplit("-", 1)[1].isdigit()]
    rnd = max(existing, default=0) + 1
    lock = (run.get("plan_lock") or {}).get("digest") if isinstance(run.get("plan_lock"), dict) else None
    out = dict(rec, schemaVersion=SCHEMA_VERSION, kind=args.kind, round=rnd, repos=shas, recorded=now(), plan_lock=lock,
               plan_code=(run.get("plan_lock") or {}).get("code_scope") if isinstance(run.get("plan_lock"), dict) else None)
    hint = session_hint()
    if hint:
        out["recorded_by"] = hint
    else:
        out.pop("recorded_by", None)
    write_json(rdir / f"{args.kind}-{rnd}.json", out)
    event(run, "review", review_kind=args.kind, round=rnd, verdict=out["verdict"], findings=len(out["findings"]),
          digest=file_sha256(rdir / f"{args.kind}-{rnd}.json"), plan_lock=lock, **({"recorded_by": hint} if hint else {}))
    run_save(run)
    if getattr(args, "json", False):
        print(json.dumps({"kind": args.kind, "round": rnd, "verdict": out["verdict"]}))
    else:
        print(f"recorded {args.kind} review round {rnd}: {out['verdict']}")
    return 0




# ---------- change contracts ----------

REPORT_FILE = "review-report.json"


def _emit(args, payload, text: str) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False) if getattr(args, "json", False) else text)


def label(repo, number) -> str:
    """Neutral reference of a change: `repo#number`."""
    return f"{repo or '?'}#{number}"


def map_raw(ws: dict, kind: str, path: Path, ident: str, repo: str | None, number: int | None) -> dict:
    """Contract (no provenance) of a saved raw `change` / `change_list` payload, mapped in memory with the scm profile.
    Always mapped through the profile (a native contract file is refused). Nothing is written."""
    data = profiles.load_raw_json(path)
    _, prof = profiles.profile_for(ws, kind)
    if profiles.kind_spec(prof, kind) is None:
        raise FactoryError(f"the scm profile declares no kind {kind!r}; declared: {', '.join(profiles.declared_kinds(prof)) or 'none'}")
    ctx: dict = {"id": ident, "key": ident, "repo": repo or "?"}
    if number is not None:
        ctx["number"] = number
    proj = None
    if repo:
        try:
            proj = repo_conf(ws, repo).get("scm_project")
        except FactoryError:
            proj = None
    if proj:
        ctx["scm_project"] = proj
    try:
        res = profiles.map_docs(prof, kind, {"main": data}, ctx, profiles.mapping_timeout(ws))
    except mapping.MappingFailure as err:
        raise FactoryError(f"{path}: cannot map as {kind}: " + "; ".join(err.errors[:5])) from err
    return res["contract"]


def alias_of(ws: dict, item: dict, default: str | None = None) -> str | None:
    """Workspace repo alias of a change: the given default, else the scm project of the change, else its `repo`."""
    if default:
        return repo_conf(ws, default)["alias"]
    proj = str(item.get("project") or "").strip().lower()
    conf = repo_for_project(ws, proj) if proj else None
    if conf is None and proj:
        conf = next((c for c in ws.get("repos", []) if proj == str(c.get("name") or "").lower()), None)
    if conf is not None:
        return conf["alias"]
    named = item.get("repo")
    return named if any(c.get("alias") == named for c in ws.get("repos", [])) else None


def ticket_key(ws: dict, change: dict) -> str | None:
    """Ticket key found in the title, branch or description of a change contract, by the tickets profile
    `refs.ticket_key` (named group `key`). None when the workspace has no tickets profile or nothing matches."""
    try:
        _, prof = profiles.profile_for(ws, "ticket")
    except FactoryError:
        return None
    pattern = (prof.get("refs") or {}).get("ticket_key")
    if not isinstance(pattern, str):
        return None

    def find(text: str, flags: int = 0):
        m = re.search(pattern, text or "", flags)
        return (m.group("key") if m and "key" in m.groupdict() else m.group(0)).upper() if m else None

    for text in (change.get("title"), change.get("source_branch"), change.get("description_md")):
        key = find(text or "")
        if key:
            return key
    return find(change.get("source_branch") or "", re.I)


# ---------- queue ----------

def _same_user(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.lower() == b.lower())


def _default_repo(ws: dict, repo: str | None) -> str | None:
    if repo:
        return repo_conf(ws, repo)["alias"]
    repos = ws.get("repos", [])
    return repos[0]["alias"] if len(repos) == 1 and repos[0].get("alias") else None


def cmd_queue(args) -> int:
    ws = workspace_load(args.workspace)
    me = args.me or ws.get("scm_user")
    queue, skipped = [], []
    for file in args.lists:
        if not Path(file).is_file():
            from ..fetches import queue_fetches
            raise FactoryError(f"missing file: {file} (save the open changes list there, then re-run)",
                               fetch=queue_fetches(ws, Path(file).absolute(), args.repo))
        default = _default_repo(ws, args.repo)
        doc = map_raw(ws, "change_list", Path(file), Path(file).stem, default, None)
        for item in doc["items"]:
            alias = alias_of(ws, item, args.repo)
            ref = label(alias, item["number"])
            approvals = item["approvals"]
            if item["state"] != "open":
                skipped.append({"change": ref, "reason": f"state {item['state']}"})
            elif item["draft"]:
                skipped.append({"change": ref, "reason": "draft"})
            elif _same_user(me, item.get("author")):
                skipped.append({"change": ref, "reason": "authored by you"})
            elif me and approvals["known"] and any(_same_user(me, a) for a in approvals["by"]):
                skipped.append({"change": ref, "reason": "already approved by you"})
            else:
                queue.append({"change": ref, "repo": alias, "number": item["number"], "ticket": ticket_key(ws, item),
                              "title": item["title"], "author": item.get("author"), "source_branch": item["source_branch"],
                              "target_branch": item["target_branch"], "head_sha": item["head_sha"], "url": item["url"]})
    if getattr(args, "json", False):
        print(json.dumps({"queue": queue, "skipped": skipped}, indent=2, ensure_ascii=False))
        return 0
    for q in queue:
        print(f"{q['change']}\t{q['ticket'] or '-'}\t{q['target_branch']}\t{q['author']}\t{q['title']}")
    if not queue:
        print("queue empty")
    for s in skipped:
        print(f"skipped {s['change']}: {s['reason']}")
    return 0


# ---------- start ----------

SPEC_TICKET = re.compile(r"=([A-Za-z][A-Za-z0-9]*-\d+)$")


def parse_change_spec(ws: dict, spec: str) -> tuple[str, int, str | None]:
    """`alias#N[=TICKET]` or a change URL `[=TICKET]` -> (alias, number, ticket). A URL is parsed with the scm profile
    `refs.change_url` (named groups `project`, `number`) and its project resolved to a repo by `scm_project`."""
    ticket = None
    m = SPEC_TICKET.search(spec)
    if m:
        ticket, spec = m.group(1).upper(), spec[:m.start()]
    m = re.fullmatch(r"([^#=\s]+)#(\d+)", spec)
    if m:
        return repo_conf(ws, m.group(1))["alias"], int(m.group(2)), ticket
    if "://" in spec:
        try:
            _, prof = profiles.profile_for(ws, "change")
        except FactoryError as err:
            raise FactoryError(f"--change {spec!r}: a URL needs the scm profile ({err})") from err
        pattern = (prof.get("refs") or {}).get("change_url")
        found = re.search(pattern, spec) if isinstance(pattern, str) else None
        if not found:
            raise FactoryError(f"--change {spec!r}: the URL does not match the scm profile refs.change_url")
        conf = repo_for_project(ws, found.group("project"))
        if conf is None:
            raise FactoryError(f"--change {spec!r}: no workspace repo has scm_project {found.group('project')!r}")
        return conf["alias"], int(found.group("number")), ticket
    raise FactoryError(f"--change {spec!r}: expected <alias>#<number>[=TICKET] or a change URL[=TICKET]")


def _find_change(ws: dict, alias: str, number: int, sources: list[Path]) -> tuple[dict, Path, bool] | None:
    """(change contract without provenance, raw file, listed) from `--from` sources: a folder holding
    `<alias>-<number>.*` (one change), or a saved single change / changes list file (`listed`)."""
    safe = contracts.safe_id(f"{alias}-{number}")
    for src in sources:
        if src.is_dir():
            files = sorted(p for p in src.iterdir() if p.is_file() and p.stem == safe)
        else:
            files = [src] if src.is_file() else []
        for file in files:
            if file.stem == safe:
                return map_raw(ws, "change", file, f"{alias}-{number}", alias, number), file, False
            try:
                doc = map_raw(ws, "change_list", file, file.stem, alias, None)
            except FactoryError:
                continue
            for item in doc["items"]:
                if item["number"] == number and alias_of(ws, item, alias) == alias:
                    return {**item, "schema": "change.v1", "repo": alias}, file, True
    return None


def _review_child_id(ws: dict, ticket: str | None, alias: str, number: int, taken: set[str]) -> str:
    base = f"{ws['name'].lower()}-{ticket.lower()}-review"
    rid = base if base not in taken else f"{base}-{alias.lower()}-{number}"
    taken.add(rid)
    return rid


def _store_change(rid: str, alias: str, number: int, doc: dict, raw: Path, listed: bool) -> None:
    """Ingest the change of a started review run as `contracts/change/<alias>-<number>.json` (with provenance).
    A single change payload is mapped by the scm profile. An item of a saved changes list is mapped by the profile as
    a `change_list` (contract + raw copy in the run) and the CLI derives the change contract from that item; its
    provenance points to the list raw and the item index (never a native contract)."""
    ident = f"{alias}-{number}"
    if not listed:
        ingest_cmd.ingest_raw(rid, "change", str(raw), ident=ident, repo=alias)
        return
    ws = workspace_load(run_load(rid)["workspace"])
    lid = f"{raw.stem}"
    ingest_cmd.ingest_raw(rid, "change_list", str(raw), ident=lid, repo=alias)
    lst, problems = contracts.load_contract(rid, "change_list", lid)
    if lst is None or problems:
        raise FactoryError(f"{label(alias, number)}: changes list {raw.name} unusable: {'; '.join(problems)}")
    index = next((i for i, it in enumerate(lst["items"]) if it.get("number") == number and alias_of(ws, it, alias) == alias), None)
    if index is None:
        raise FactoryError(f"{label(alias, number)}: not an item of the changes list {raw.name}")
    item = {k: v for k, v in lst["items"][index].items() if k != "provenance"}
    change = contracts.order_contract("change", {**item, "schema": "change.v1", "repo": alias, "unknowns": lst.get("unknowns", [])})
    prov = dict(lst["provenance"])
    prov.update({"id": ident, "item": index, "source_hint": f"item {index} of the changes list {raw.name} (change_list/{contracts.safe_id(lid)})"})
    change["provenance"] = prov
    bad = contracts.validate("change", change)
    if bad:
        raise FactoryError(f"{label(alias, number)}: change derived from the list is invalid:\n  - " + "\n  - ".join(bad))
    write_json(contracts.contract_path(run_dir(rid), "change", contracts.safe_id(ident)), change)


def cmd_start(args) -> int:
    ws = workspace_load(args.workspace)
    specs = [parse_change_spec(ws, s) for s in args.change]
    if len({(a, n) for a, n, _ in specs}) != len(specs):
        raise FactoryError("duplicate --change")
    if args.ticket and len(specs) != 1:
        raise FactoryError("--ticket applies to a single change; use --change alias#number=TICKET in a batch")
    sources = [Path(s) for s in args.source or []]
    taken: set[str] = set()
    entries = []
    missing_payloads: list[tuple[str, dict, Path]] = []
    for alias, number, tk in specs:
        explicit = tk or (args.ticket.upper() if args.ticket else None)
        found = _find_change(ws, alias, number, sources)
        if found is None:
            from ..fetches import start_change_fetch
            folder = next((s for s in sources if s.is_dir()), None) or (factory_home() / "runs" / "_inbox")
            missing_payloads.append((label(alias, number), start_change_fetch(ws, alias, number, folder), folder))
            continue
        doc, raw, listed = found
        missing = [k for k in ("source_branch", "target_branch", "head_sha") if not doc.get(k)]
        if missing:
            raise FactoryError(f"{label(alias, number)}: payload lacks {', '.join(missing)}")
        ticket = explicit or ticket_key(ws, doc)
        if not ticket:
            raise FactoryError(f"{label(alias, number)}: no ticket key in title/branch/description; pass --ticket or =TICKET")
        change = {"repo": alias, "number": number, "source_branch": doc["source_branch"],
                  "target_branch": doc["target_branch"], "head_sha": doc["head_sha"], "ticket": ticket,
                  "title": doc["title"], "url": doc.get("url"), "author": doc.get("author"), "draft": doc["draft"]}
        entries.append((_review_child_id(ws, ticket, alias, number, taken), change, doc, raw, listed))
    if missing_payloads:
        labels = ", ".join(m[0] for m in missing_payloads)
        raise FactoryError(f"no saved payload for {labels}: save each change as <alias>-<number>.<ext> in a folder (or save "
                           f"the open changes list) and pass it with --from {missing_payloads[0][2]}",
                           fetch=[m[1] for m in missing_payloads])
    single = len(entries) == 1
    stamp = now()[2:16].replace("-", "").replace(":", "").replace("T", "-")
    parent_id = None if single else f"{ws['name'].lower()}-review-{stamp}"
    for rid in [e[0] for e in entries] + ([parent_id] if parent_id else []):
        if (run_dir(rid) / "run.json").exists():
            raise FactoryError(f"run {rid} already exists")
    ids = []
    for rid, change, doc, raw, listed in entries:
        tickets = [{"key": change["ticket"], "type": "feature", "parent": None, "depends_on": []}]
        extra = {"changes": [change], "targets": []}
        if parent_id:
            extra["parent"] = parent_id
        _new_run(ws, rid, tickets, "review", args.risk, **extra)
        _store_change(rid, change["repo"], change["number"], doc, raw, listed)
        ids.append(rid)
    if parent_id:
        seen = list(dict.fromkeys(e[1]["ticket"] for e in entries))
        _new_run(ws, parent_id, [{"key": k, "type": "feature", "parent": None, "depends_on": []} for k in seen],
                 "review-batch", args.risk, children=ids, changes=[e[1] for e in entries], targets=[])
    top = parent_id or ids[0]
    _emit(args, {"run": top, "children": ids if parent_id else [], "changes": [e[1] for e in entries]},
          f"started review {top}" + (f" with children: {', '.join(ids)}" if parent_id else ""))
    return 0


# ---------- run branch --change ----------

def branch_for_change(args, run: dict, ws: dict, conf: dict, main: Path) -> int:
    alias = conf["alias"]
    if run.get("kind") != "review":
        raise FactoryError(f"run {run['id']} is kind {run.get('kind')}; --change needs a review run (use a child of a batch)")
    change = next((m for m in run.get("changes", []) if m["repo"] == alias and m["number"] == args.change), None)
    if change is None:
        raise FactoryError(f"run {run['id']} has no change {label(alias, args.change)}")
    if alias in run["repos"]:
        raise FactoryError(f"repo {alias} already has a worktree in run {run['id']}")
    doc, problems = contracts.load_contract(run["id"], "change", f"{alias}-{args.change}")
    sha = (doc["head_sha"] if doc is not None and not problems else None) or change["head_sha"]
    if not git(main, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False):
        raise FactoryError(f"head sha {sha[:10]} of {label(alias, args.change)} is not in {main}: the orchestrator must run "
                           f"`git -C {main} fetch origin {change['source_branch']}` and retry")
    root = ws.get("worktree_root")
    if not root:
        raise FactoryError("workspace worktree_root is not set")
    from .run import run_short
    worktree = Path(root) / f"{run_short(run)}-{alias.lower()}"
    if worktree.exists():
        raise FactoryError(f"worktree path already exists: {worktree}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "-C", str(main), "worktree", "add", "--detach", str(worktree), sha],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FactoryError(f"git worktree add failed: {proc.stderr.strip()}")
    try:
        prepare_worktree(main, worktree, setup_command(ws, alias), (conf.get("env") or {}).get("env_vars"))
    except RuntimeError as err:
        print(f"WARNING: {err}; fix it and rerun the setup command in {worktree}")
    skipped: list[str] = []
    copied = copy_main_files(main, worktree, files_from_main_of(ws, alias), skipped)
    run["repos"][alias] = {"branch": change["source_branch"], "base": change["target_branch"], "worktree": str(worktree),
                           "head": git_head(worktree), "base_ref": gate.resolve_base(main, change["target_branch"]),
                           "change": args.change, "detached": True}
    change["head_sha"] = sha
    run_save(run)
    _emit(args, dict(run["repos"][alias], **({"copied_from_main": copied} if copied else {}),
                     **({"not_copied_from_main": skipped} if skipped else {})),
          f"{alias}: detached worktree for {label(alias, args.change)} at {sha[:10]} in {worktree}"
          + (f"\ncopied from main checkout: {', '.join(copied)}" if copied else "")
          + (f"\nnot copied from main checkout: {'; '.join(skipped)}" if skipped else ""))
    return 0


# ---------- report ----------

def _head(run: dict, ws: dict, alias: str) -> str | None:
    rec = run.get("repos", {}).get(alias)
    if not rec or not Path(rec.get("worktree", "")).is_dir():
        return None
    try:
        return git_head(repo_path_for_run(run, ws, alias))
    except FactoryError:
        return None


def _change_report(run: dict, ws: dict, ch: dict) -> dict:
    rid, alias = run["id"], ch["repo"]
    failures: list[str] = []
    gaps: list[str] = []
    head = _head(run, ws, alias)
    wt = repo_path_for_run(run, ws, alias) if head else None
    if head is None:
        gaps.append(f"{alias}: no change worktree (factory run branch {rid} {alias} --change {ch['number']})")
    elif head != ch["head_sha"]:
        gaps.append(f"{alias}: worktree HEAD {head[:8]} is not the change head {ch['head_sha'][:8]}")
    elif wt and git_dirty(wt):
        gaps.append(f"{alias}: worktree is dirty")

    def evidence_state(cid: str) -> tuple[str, str]:
        ev = load_evidence(rid, cid)
        if ev is None:
            return "missing", "no evidence"
        bad = integrity.evidence_error(run, cid)
        if bad:
            return "error", bad
        if ev.get("status") == "fail":
            return "fail", ev.get("summary", "")
        if ev.get("status") != "pass":
            return "error", ev.get("summary", "")
        if ev.get("dirty") or ev.get("git_sha") != head:
            return "stale", "evidence not bound to the change head sha or collected dirty"
        return "pass", ev.get("summary", "")

    acs_rows, humans = [], []
    counts = {"done": 0, "gap": 0, "not_checked": 0}
    acs_path = run_dir(rid) / "acs.json"
    data = read_json(acs_path) if acs_path.is_file() else None
    if data is None:
        gaps.append("acs.json missing: ACs were never written from the ticket")
        data = {"acs": [], "checks": []}
    checks = {c["id"]: c for c in data.get("checks") or [] if c.get("id")}
    for ac in data.get("acs") or []:
        rows = []
        for cid in ac.get("checks") or []:
            chk = checks.get(cid)
            if chk is None:
                rows.append({"id": cid, "type": "?", "status": "missing", "summary": "unknown check"})
                gaps.append(f"{ac.get('id')}: references unknown check {cid}")
                counts["gap"] += 1
            elif chk.get("type") == "human" and evidence_state(cid)[0] == "pass":
                rows.append({"id": cid, "type": "human", "status": "pass",
                             "summary": evidence_state(cid)[1] or chk.get("text", "")})
                counts["done"] += 1
            elif chk.get("type") == "human":
                rows.append({"id": cid, "type": "human", "status": "human", "summary": chk.get("text", "")})
                if chk not in humans:
                    humans.append(chk)
                counts["not_checked"] += 1
            else:
                status, summary = evidence_state(cid)
                rows.append({"id": cid, "type": chk.get("type"), "status": status, "summary": summary})
                if status == "pass":
                    counts["done"] += 1
                else:
                    counts["gap"] += 1
                    (failures if status == "fail" else gaps).append(f"{cid} ({ac.get('id')}): {status} {summary}".strip())
        auto = [r for r in rows if r["status"] != "human"]
        status = ("fail" if any(r["status"] == "fail" for r in rows)
                  else "pass" if auto and all(r["status"] == "pass" for r in auto)
                  else "human" if rows and not auto else "gap")
        acs_rows.append({"id": ac.get("id"), "text": ac.get("text", ""), "kind": ac.get("kind"),
                         "status": status, "checks": rows})
    extra = ([gate.quality_id(alias)] if repo_conf(ws, alias).get("quality") else []) + \
        [c["id"] for c in gate.rule_checks(ws, run)]
    for cid in extra:
        status, summary = evidence_state(cid)
        if status != "pass":
            (failures if status == "fail" else gaps).append(f"{cid}: {status} {summary}".strip())
    findings, rounds = [], {}
    for kind in ("code", "qa"):
        rec, rpath = gate.latest_review_file(rid, kind)
        if rec is None:
            gaps.append(f"no {kind} review recorded")
            continue
        bad = integrity.review_error(run, rpath, kind, rec.get("round"))
        if bad:
            gaps.append(bad)
            continue
        rounds[kind] = {"round": rec.get("round"), "verdict": rec.get("verdict")}
        if (rec.get("repos") or {}).get(alias) != head:
            gaps.append(f"{kind} review round {rec.get('round')} is stale for {alias}; re-review the change head")
        if rec.get("verdict") != "APPROVE":
            failures.append(f"{kind} review verdict {rec.get('verdict')}")
        for f in rec.get("findings") or []:
            findings.append({"review": kind, "id": f.get("id"), "severity": f.get("severity"), "file": f.get("file"),
                             "ac": f.get("ac"), "text": f.get("text"), "resolved": bool(f.get("resolved"))})
            if f.get("severity") in ("blocker", "major"):
                failures.append(f"{kind} {f.get('id')} [{f['severity']}] {f.get('file', '')} {f.get('text', '')}".strip())
    behind, note = None, None
    if wt:
        try:
            behind = ship.behind_count(wt, ch["target_branch"])
        except FactoryError as err:
            note = str(err)
    deps = ship.check_dependencies(run)
    verdict = "not-ok" if failures else "incomplete" if gaps else "ok"
    unverified = list(gaps) + [f"{h.get('id')}: human check: {h.get('text')}" for h in humans]
    ref = label(alias, ch["number"])
    actions = []  # write intents (Markdown bodies); the main session executes them only with the user's approval
    if findings:
        lines = [f"Review of {ref} at {ch['head_sha'][:8]}: **{verdict}**", ""]
        lines += [f"- [{f['severity']}] {f['id']} {f['file'] or ''} ({f['ac'] or 'no AC'}): {f['text']}" for f in findings]
        actions.append({"type": "change_comment", "repo": alias, "number": ch["number"], "body_md": "\n".join(lines),
                        "needs_user": True})
    if verdict == "ok":
        actions.append({"type": "change_approve", "repo": alias, "number": ch["number"], "needs_user": True})
    summary = [f"Review of {ref}{' ' + ch['url'] if ch.get('url') else ''}: **{verdict}**.",
               f"ACs proven: {sum(1 for a in acs_rows if a['status'] == 'pass')}/{len(acs_rows)}."]
    open_bm = [f for f in findings if f["severity"] in ("blocker", "major")]
    if open_bm:
        summary.append(f"Open blocker/major findings: {len(open_bm)}.")
    if unverified:
        summary += ["", "Could not verify:"] + [f"- {u}" for u in unverified]
    actions.append({"type": "issue_comment", "issues": [ch["ticket"]], "body_md": "\n".join(summary), "needs_user": True})
    return {"repo": alias, "number": ch["number"], "ticket": ch["ticket"], "title": ch.get("title"), "url": ch.get("url"),
            "source_branch": ch["source_branch"], "target_branch": ch["target_branch"], "head_sha": ch["head_sha"],
            "verdict": verdict, "failures": failures, "gaps": gaps, "acs": acs_rows, "human_checks": humans,
            "findings": findings, "reviews": rounds, "completeness": counts, "unverified": unverified,
            "freshness": {"behind": behind, "target": ch["target_branch"], "unmerged_deps": deps,
                          **({"note": note} if note else {})},
            "remote_actions": actions}


def _worst(verdicts: list[str]) -> str:
    for v in ("not-ok", "incomplete"):
        if v in verdicts:
            return v
    return "ok" if verdicts else "incomplete"


def _md(report: dict) -> str:
    lines = [f"# Review report: {report['run']}", "", f"Generated {report['generated']}. Verdict: **{report['verdict']}**", "",
             "| Change | Ticket | Verdict | Open blocker/major | ACs proven | Behind target |", "|---|---|---|---|---|---|"]
    for m in report["changes"]:
        open_bm = sum(1 for f in m["findings"] if f["severity"] in ("blocker", "major"))
        proven = sum(1 for a in m["acs"] if a["status"] == "pass")
        behind = m["freshness"]["behind"]
        lines.append(f"| {label(m['repo'], m['number'])} | {m['ticket']} | {m['verdict']} | {open_bm} | {proven}/{len(m['acs'])} | "
                     f"{'?' if behind is None else behind} |")
    for m in report["changes"]:
        lines += ["", f"## {label(m['repo'], m['number'])} {m['ticket']}: {m['title']}", f"{m['url'] or ''}", "",
                  f"Verdict: **{m['verdict']}** (head {m['head_sha'][:8]}, {m['source_branch']} -> {m['target_branch']})"]
        for r in m["failures"]:
            lines.append(f"- not ok: {r}")
        lines += ["", "| AC | Status | Checks |", "|---|---|---|"]
        for a in m["acs"]:
            cells = ", ".join(f"{c['id']}={c['status']}" for c in a["checks"])
            lines.append(f"| {a['id']}: {str(a['text']).replace('|', '/')} | {a['status']} | {cells} |")
        if m["findings"]:
            lines += ["", "Findings:"]
            for f in m["findings"]:
                lines.append(f"- [{f['severity']}] {f['id']} {f['file'] or ''} ({f['ac'] or 'no AC'}): {f['text']}")
        if m["unverified"]:
            lines += ["", "Could not verify:"] + [f"- {u}" for u in m["unverified"]]
        if m["freshness"]["unmerged_deps"]:
            lines += ["", "Unmerged dependencies:"] + [f"- {d}" for d in m["freshness"]["unmerged_deps"]]
        lines += ["", f"Completeness: {m['completeness']}"]
    if report.get("profiles"):
        lines += ["", "Profiles used to map the contracts:"] + [f"- {n}: {d}" for n, d in sorted(report["profiles"].items())]
    lines += ["", "Remote actions (nothing is posted without your approval):"]
    for a in report["remote_actions"]:
        ref = label(a["repo"], a["number"]) if a.get("number") else ""
        lines.append(f"- {a['type']} {ref} {','.join(a.get('issues', []))}".replace("  ", " ").rstrip())
    return "\n".join(lines) + "\n"


def _profile_digests(run_id: str) -> dict:
    """{profile name: digest} of the profiles that mapped the contracts of a run (from their provenance)."""
    out: dict[str, str] = {}
    for p in sorted((run_dir(run_id) / "contracts").glob("*/*.json")):
        try:
            prov = json.loads(p.read_text(encoding="utf-8")).get("provenance") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if prov.get("profile") not in (None, "native"):
            out[str(prov["profile"])] = str(prov.get("profile_digest"))
    return out


def build_report(run_id: str) -> dict:
    run = run_load(run_id)
    ws = workspace_load(run["workspace"])
    if run.get("kind") == "review-batch":
        reports = [build_report(c) for c in run.get("children", [])]
        changes = [m for r in reports for m in r["changes"]]
        report = {"children": list(run.get("children", [])), "changes": changes,
                  "verdict": _worst([m["verdict"] for m in changes]),
                  "remote_actions": [a for r in reports for a in r["remote_actions"]]}
    elif run.get("kind") == "review":
        changes = [_change_report(run, ws, ch) for ch in run.get("changes", [])]
        if not changes:
            raise FactoryError(f"run {run_id} has no changes")
        report = {"changes": changes, "verdict": _worst([m["verdict"] for m in changes]),
                  "remote_actions": [a for m in changes for a in m["remote_actions"]],
                  "repos_sha": {a: _head(run, ws, a) for a in run.get("repos", {})},
                  "review_rounds": {k: (gate.latest_review(run_id, k) or {}).get("round") for k in ("code", "qa")}}
    else:
        raise FactoryError(f"run {run_id} is kind {run.get('kind')}; review report needs a review run")
    report["profiles"] = {**{k: v for r in (reports if run.get("kind") == "review-batch" else []) for k, v in r.get("profiles", {}).items()},
                          **_profile_digests(run_id)}
    report.update(schemaVersion=SCHEMA_VERSION, kind="review-report", run=run_id, generated=now())
    write_json(run_dir(run_id) / REPORT_FILE, report)
    (run_dir(run_id) / "review-report.md").write_text(_md(report), encoding="utf-8")
    run = run_load(run_id)
    event(run, "review_report", verdict=report["verdict"], changes=len(report["changes"]))
    run_save(run)
    return report


def report_errors(run_id: str, run: dict, ws: dict) -> list[str]:
    """Gate `report` for a review run: the report exists and matches what is on disk now."""
    path = run_dir(run_id) / REPORT_FILE
    if not path.is_file():
        return [f"{REPORT_FILE} missing (factory review report {run_id})"]
    rep = read_json(path)
    errors = []
    for alias, sha in (rep.get("repos_sha") or {}).items():
        if _head(run, ws, alias) != sha:
            errors.append(f"{REPORT_FILE} is stale for {alias}; re-run factory review report")
    for kind, rnd in (rep.get("review_rounds") or {}).items():
        if (gate.latest_review(run_id, kind) or {}).get("round") != rnd:
            errors.append(f"{REPORT_FILE} predates {kind} review round; re-run factory review report")
    return errors


def cmd_report(args) -> int:
    report = build_report(args.run)
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print((run_dir(args.run) / "review-report.md").read_text(encoding="utf-8"), end="")
    print(f"wrote {run_dir(args.run) / REPORT_FILE} and review-report.md")
    return 0


# ---------- fix ----------

def cmd_fix(args) -> int:
    run = run_load(args.run)
    if run.get("kind") != "review":
        raise FactoryError(f"run {run['id']} is kind {run.get('kind')}; review fix converts a single review run")
    for ch in run.get("changes", []):
        rec = run["repos"].get(ch["repo"])
        if not rec:
            raise FactoryError(f"{label(ch['repo'], ch['number'])} has no worktree (create it with the branch command and --change first)")
        wt = Path(rec["worktree"])
        if not wt.is_dir():
            raise FactoryError(f"worktree missing: {wt}")
        if git_dirty(wt):
            raise FactoryError(f"{ch['repo']}: worktree has uncommitted changes")
        branch, sha = ch["source_branch"], ch["head_sha"]
        existing = git(wt, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if existing and existing != sha:
            raise FactoryError(f"local branch {branch} exists at {existing[:8]}, not the change head {sha[:8]}; "
                               f"resolve it first (no branch is moved by this command)")
        if existing:
            git(wt, "checkout", "-q", branch)
        else:
            git(wt, "checkout", "-q", "--no-track", "-b", branch)
        rec.pop("detached", None)
        rec.update(branch=branch, head=git_head(wt))
        run["targets"] = [ch["target_branch"]]
    event(run, "phase", **{"from": run["phase"], "to": "build"})
    event(run, "review_fix", changes=[label(c["repo"], c["number"]) for c in run["changes"]])
    changes = run.pop("changes")
    # change head at conversion per repo: gate build checks only commits after it (the change's own were reviewed already)
    run["review_origin"] = {"changes": changes, "converted": now(), "head_sha": {c["repo"]: c["head_sha"] for c in changes}}
    run["kind"] = "ticket"
    run["phase"] = "build"
    run["decisions"].append({"at": now(), "phase": "build", "text": "review fix: continuing on the change source branch", "by": "agent"})
    run_save(run)
    _emit(args, {"run": run["id"], "kind": "ticket", "phase": "build"},
          f"{run['id']} is now a ticket run in phase build on the change source branch(es)")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("review", help="record reviews; review existing changes")
    rs = parser.add_subparsers(dest="review_cmd", required=True)
    p = rs.add_parser("record")
    p.add_argument("run")
    p.add_argument("--kind", choices=["code", "qa"], required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_record)
    p = rs.add_parser("queue", help="filter saved open-changes lists into a review queue")
    p.add_argument("workspace")
    p.add_argument("--list", dest="lists", nargs="+", required=True, metavar="FILE",
                   help="saved changes list(s): raw payload or change_list contract")
    p.add_argument("--me", help="your SCM user (drops your own and already approved changes); default: workspace scm_user")
    p.add_argument("--repo", help="repo alias when the payload does not identify its project")
    p.set_defaults(func=cmd_queue)
    p = rs.add_parser("start", help="start a review run (several changes: batch parent + one child each)")
    p.add_argument("workspace")
    p.add_argument("--change", action="append", required=True, metavar="ALIAS#NUMBER[=TICKET]",
                   help="a change as <alias>#<number> or its URL (resolved by the scm profile), optionally =TICKET (repeatable)")
    p.add_argument("--ticket")
    p.add_argument("--from", dest="source", action="append", metavar="PATH",
                   help="folder with <alias>-<number>.<ext> (one change each), or a saved changes list / change file (repeatable)")
    p.add_argument("--risk", choices=["normal", "high"], default="normal")
    p.set_defaults(func=cmd_start)
    p = rs.add_parser("report", help="verdict per change; writes review-report.json and .md")
    p.add_argument("run")
    p.set_defaults(func=cmd_report)
    p = rs.add_parser("fix", help="convert a review run into a ticket run on the change source branch")
    p.add_argument("run")
    p.set_defaults(func=cmd_fix)
