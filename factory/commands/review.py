"""`factory review record|queue|start|report|fix` (DESIGN §2.5, §7)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .. import integrity
from ..collectors.test import prepare_worktree, setup_command
from ..core import (SCHEMA_VERSION, SESSION_HINT_VARS, code_repos, session_hint, now, FactoryError, GateFailed, event, factory_home, file_sha256, git, git_dirty,
                    git_head, load_evidence, read_json, repo_conf,
                    repo_path_for_run, run_dir, run_load, run_save,
                    workspace_load, write_json)
from . import gate, ship
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




# ---------- GitLab payloads (tolerant) ----------

DEFAULT_TICKET_RE = r"[A-Z][A-Z0-9]*-\d+"
REPORT_FILE = "review-report.json"


def _emit(args, payload, text: str) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False) if getattr(args, "json", False) else text)


def _unwrap(data):
    """MCP envelopes: {"content": [{"type": "text", "text": "<json>"}]} -> parsed JSON."""
    if isinstance(data, dict) and isinstance(data.get("content"), list) and data["content"] \
            and all(isinstance(c, dict) and isinstance(c.get("text"), str) for c in data["content"]):
        try:
            return _unwrap(json.loads(data["content"][0]["text"]))
        except json.JSONDecodeError:
            return data
    return data


def mr_items(data) -> list[dict]:
    """Merge request objects from a list, a wrapper dict, or a single MR."""
    data = _unwrap(data)
    if isinstance(data, list):
        return [d for d in (_unwrap(x) for x in data) if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("merge_requests", "mergeRequests", "items", "results", "mrs", "data"):
            if isinstance(data.get(key), list):
                return mr_items(data[key])
        for key in ("merge_request", "mergeRequest", "mr", "data"):
            if isinstance(data.get(key), dict):
                return mr_items(data[key])
        if "iid" in data:
            return [data]
    return []


def _user(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("username") or value.get("login")
    return str(value) if value else None


def _approvers(item: dict) -> set[str] | None:
    """Usernames that approved, or None when the payload carries no approval data."""
    lists = []
    if isinstance(item.get("approved_by"), list):
        lists.append(item["approved_by"])
    appr = item.get("approvals")
    if isinstance(appr, dict) and isinstance(appr.get("approved_by"), list):
        lists.append(appr["approved_by"])
    if not lists:
        return None
    out = set()
    for entry in (e for lst in lists for e in lst):
        name = _user(entry.get("user") if isinstance(entry, dict) and entry.get("user") else entry)
        if name:
            out.add(name)
    return out


def project_path(item: dict) -> str | None:
    refs = item.get("references")
    full = refs.get("full") if isinstance(refs, dict) else None
    if isinstance(full, str) and "!" in full:
        return full.split("!")[0]
    url = item.get("web_url") or ""
    m = re.match(r"https?://[^/]+/(.+?)/-/merge_requests/\d+", url) or re.match(r"https?://[^/]+/(.+?)/merge_requests/\d+", url)
    return m.group(1) if m else (item.get("project_path") or None)


def mr_facts(item: dict) -> dict:
    refs = item.get("diff_refs") if isinstance(item.get("diff_refs"), dict) else {}
    title = str(item.get("title") or "")
    draft = bool(item.get("draft") or item.get("work_in_progress")) or \
        bool(re.match(r"\s*(draft:|\[draft\]|wip:)", title, re.I))
    return {
        "iid": item.get("iid"),
        "title": title,
        "description": str(item.get("description") or ""),
        "source_branch": item.get("source_branch"),
        "target_branch": item.get("target_branch"),
        "head_sha": item.get("sha") or refs.get("head_sha") or item.get("head_sha"),
        "draft": draft,
        "author": _user(item.get("author")),
        "reviewers": [u for u in (_user(r) for r in item.get("reviewers") or []) if u],
        "web_url": item.get("web_url"),
        "state": item.get("state"),
        "approved_by": _approvers(item),
        "project": project_path(item),
    }


def repo_for(ws: dict, facts: dict, default: str | None = None) -> str | None:
    if default:
        return repo_conf(ws, default)["alias"]
    proj = (facts.get("project") or "").lower()
    for conf in ws.get("repos", []):
        if proj and proj in ((conf.get("gitlab_project") or "").lower(), (conf.get("name") or "").lower()):
            return conf["alias"]
    return None


def ticket_key(ws: dict, facts: dict) -> str | None:
    pattern = ws.get("ticket_regex") or DEFAULT_TICKET_RE

    def find(text: str, flags: int = 0):
        m = re.search(pattern, text or "", flags)
        return (m.group(1) if m and m.groups() else m.group(0)).upper() if m else None

    for text in (facts.get("title"), facts.get("source_branch"), facts.get("description")):
        key = find(text or "")
        if key:
            return key
    return find(facts.get("source_branch") or "", re.I)


# ---------- queue ----------

def cmd_queue(args) -> int:
    ws = workspace_load(args.workspace)
    me = args.me or ws.get("gitlab_user")
    queue, skipped = [], []
    for file in args.mrs_json:
        if not Path(file).is_file():
            from ..fetches import queue_fetches
            raise FactoryError(f"missing file: {file} (save gitlab_list_merge_requests there)",
                               fetch=queue_fetches(ws, Path(file).absolute(), args.repo))
        for item in mr_items(read_json(Path(file))):
            f = mr_facts(item)
            alias = repo_for(ws, f, args.repo)
            label = f"{alias or '?'}!{f['iid']}"
            if f["state"] and f["state"] != "opened":
                skipped.append({"mr": label, "reason": f"state {f['state']}"})
            elif f["draft"]:
                skipped.append({"mr": label, "reason": "draft"})
            elif me and f["author"] and f["author"].lower() == me.lower():
                skipped.append({"mr": label, "reason": "authored by you"})
            elif me and f["approved_by"] is not None and me.lower() in {a.lower() for a in f["approved_by"]}:
                skipped.append({"mr": label, "reason": "already approved by you"})
            else:
                queue.append({"mr": label, "repo": alias, "iid": f["iid"], "ticket": ticket_key(ws, f),
                              "title": f["title"], "author": f["author"], "source_branch": f["source_branch"],
                              "target_branch": f["target_branch"], "head_sha": f["head_sha"], "web_url": f["web_url"]})
    if getattr(args, "json", False):
        print(json.dumps({"queue": queue, "skipped": skipped}, indent=2, ensure_ascii=False))
        return 0
    for q in queue:
        print(f"{q['mr']}\t{q['ticket'] or '-'}\t{q['target_branch']}\t{q['author']}\t{q['title']}")
    if not queue:
        print("queue empty")
    for s in skipped:
        print(f"skipped {s['mr']}: {s['reason']}")
    return 0


# ---------- start ----------

def _parse_spec(spec: str) -> tuple[str, int, str | None]:
    m = re.fullmatch(r"([^!=\s]+)!(\d+)(?:=([A-Za-z][A-Za-z0-9]*-\d+))?", spec)
    if not m:
        raise FactoryError(f"--mr {spec!r}: expected <alias>!<iid>[=TICKET]")
    return m.group(1), int(m.group(2)), (m.group(3) or "").upper() or None


def _find_payload(alias: str, iid: int, sources: list[Path]) -> dict | None:
    for src in sources:
        if src.is_dir():
            cand = src / f"mr-{alias}-{iid}.json"
            files = [cand] if cand.is_file() else []
        else:
            files = [src] if src.is_file() else []
        for file in files:
            for item in mr_items(read_json(file)):
                if item.get("iid") == iid:
                    return item
    return None


def _review_child_id(ws: dict, ticket: str | None, alias: str, iid: int, taken: set[str]) -> str:
    base = f"{ws['name'].lower()}-{ticket.lower()}-review"
    rid = base if base not in taken else f"{base}-{alias.lower()}-{iid}"
    taken.add(rid)
    return rid


def cmd_start(args) -> int:
    ws = workspace_load(args.workspace)
    specs = [_parse_spec(s) for s in args.mr]
    if len({(a, i) for a, i, _ in specs}) != len(specs):
        raise FactoryError("duplicate --mr")
    if args.ticket and len(specs) != 1:
        raise FactoryError("--ticket applies to a single MR; use --mr alias!iid=TICKET in a batch")
    sources = [Path(s) for s in args.source or []]
    taken: set[str] = set()
    entries = []
    missing_payloads: list[tuple[str, dict, Path]] = []
    for alias_in, iid, tk in specs:
        alias = repo_conf(ws, alias_in)["alias"]
        explicit = tk or (args.ticket.upper() if args.ticket else None)
        pre = [run_dir(f"{ws['name'].lower()}-{explicit.lower()}-review") / "remote"] if explicit else []
        item = _find_payload(alias, iid, [*sources, *pre])
        if item is None:
            from ..fetches import start_payload_fetch
            folder = next((s for s in sources if s.is_dir()), None) or (factory_home() / "runs" / "_inbox")
            missing_payloads.append((f"{alias}!{iid}", start_payload_fetch(ws, alias, iid, folder), folder))
            continue
        facts = mr_facts(item)
        missing = [k for k in ("source_branch", "target_branch", "head_sha") if not facts[k]]
        if missing:
            raise FactoryError(f"{alias}!{iid}: payload lacks {', '.join(missing)}")
        ticket = explicit or ticket_key(ws, facts)
        if not ticket:
            raise FactoryError(f"{alias}!{iid}: no ticket key in title/branch/description; pass --ticket or =TICKET")
        mr = {"repo": alias, "iid": iid, "source_branch": facts["source_branch"], "target_branch": facts["target_branch"],
              "head_sha": facts["head_sha"], "ticket": ticket, "title": facts["title"], "web_url": facts["web_url"],
              "author": facts["author"], "draft": facts["draft"]}
        entries.append((_review_child_id(ws, ticket, alias, iid, taken), mr, item))
    if missing_payloads:
        labels = ", ".join(m[0] for m in missing_payloads)
        raise FactoryError(f"no saved payload for {labels}: save each dev.mcp merge request response as "
                           f"mr-<alias>-<iid>.json and pass the folder (or the queue file) with --from {missing_payloads[0][2]}",
                           fetch=[m[1] for m in missing_payloads])
    single = len(entries) == 1
    stamp = now()[2:16].replace("-", "").replace(":", "").replace("T", "-")
    parent_id = None if single else f"{ws['name'].lower()}-review-{stamp}"
    for rid in [e[0] for e in entries] + ([parent_id] if parent_id else []):
        if (run_dir(rid) / "run.json").exists():
            raise FactoryError(f"run {rid} already exists")
    ids = []
    for rid, mr, item in entries:
        tickets = [{"key": mr["ticket"], "type": "feature", "parent": None, "depends_on": []}]
        extra = {"mrs": [mr], "targets": []}
        if parent_id:
            extra["parent"] = parent_id
        _new_run(ws, rid, tickets, "review", args.risk, **extra)
        write_json(run_dir(rid) / "remote" / f"mr-{mr['repo']}-{mr['iid']}.json", item)
        ids.append(rid)
    if parent_id:
        seen = list(dict.fromkeys(e[1]["ticket"] for e in entries))
        _new_run(ws, parent_id, [{"key": k, "type": "feature", "parent": None, "depends_on": []} for k in seen],
                 "review-batch", args.risk, children=ids, mrs=[e[1] for e in entries], targets=[])
    top = parent_id or ids[0]
    _emit(args, {"run": top, "children": ids if parent_id else [], "mrs": [e[1] for e in entries]},
          f"started review {top}" + (f" with children: {', '.join(ids)}" if parent_id else ""))
    return 0


# ---------- run branch --mr ----------

def branch_for_mr(args, run: dict, ws: dict, conf: dict, main: Path) -> int:
    alias = conf["alias"]
    if run.get("kind") != "review":
        raise FactoryError(f"run {run['id']} is kind {run.get('kind')}; --mr needs a review run (use a child of a batch)")
    mr = next((m for m in run.get("mrs", []) if m["repo"] == alias and m["iid"] == args.mr), None)
    if mr is None:
        raise FactoryError(f"run {run['id']} has no MR {alias}!{args.mr}")
    if alias in run["repos"]:
        raise FactoryError(f"repo {alias} already has a worktree in run {run['id']}")
    saved = run_dir(run["id"]) / "remote" / f"mr-{alias}-{args.mr}.json"
    items = mr_items(read_json(saved))
    sha = (mr_facts(items[0])["head_sha"] if items else None) or mr["head_sha"]
    if not git(main, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False):
        raise FactoryError(f"head sha {sha[:10]} of {alias}!{args.mr} is not in {main}: the orchestrator must run "
                           f"`git -C {main} fetch origin {mr['source_branch']}` and retry")
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
    run["repos"][alias] = {"branch": mr["source_branch"], "base": mr["target_branch"], "worktree": str(worktree),
                           "head": git_head(worktree), "base_ref": gate.resolve_base(main, mr["target_branch"]),
                           "mr": args.mr, "detached": True}
    mr["head_sha"] = sha
    run_save(run)
    _emit(args, run["repos"][alias], f"{alias}: detached worktree for !{args.mr} at {sha[:10]} in {worktree}")
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


def _mr_report(run: dict, ws: dict, mr: dict) -> dict:
    rid, alias = run["id"], mr["repo"]
    failures: list[str] = []
    gaps: list[str] = []
    head = _head(run, ws, alias)
    wt = repo_path_for_run(run, ws, alias) if head else None
    if head is None:
        gaps.append(f"{alias}: no MR worktree (factory run branch {rid} {alias} --mr {mr['iid']})")
    elif head != mr["head_sha"]:
        gaps.append(f"{alias}: worktree HEAD {head[:8]} is not the MR head {mr['head_sha'][:8]}")
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
            return "stale", "evidence not bound to the MR head sha or collected dirty"
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
            gaps.append(f"{kind} review round {rec.get('round')} is stale for {alias}; re-review the MR head")
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
            behind = ship.behind_count(wt, mr["target_branch"])
        except FactoryError as err:
            note = str(err)
    deps = ship.check_dependencies(run)
    verdict = "not-ok" if failures else "incomplete" if gaps else "ok"
    unverified = list(gaps) + [f"{h.get('id')}: human check: {h.get('text')}" for h in humans]
    actions = []
    if findings:
        actions.append({"type": "mr_comment", "repo": alias, "iid": mr["iid"], "needs_user": True})
    if verdict == "ok":
        actions.append({"type": "mr_approve", "repo": alias, "iid": mr["iid"], "needs_user": True})
    actions.append({"type": "jira_comment", "issues": [mr["ticket"]], "needs_user": True})
    return {"repo": alias, "iid": mr["iid"], "ticket": mr["ticket"], "title": mr.get("title"), "web_url": mr.get("web_url"),
            "source_branch": mr["source_branch"], "target_branch": mr["target_branch"], "head_sha": mr["head_sha"],
            "verdict": verdict, "failures": failures, "gaps": gaps, "acs": acs_rows, "human_checks": humans,
            "findings": findings, "reviews": rounds, "completeness": counts, "unverified": unverified,
            "freshness": {"behind": behind, "target": mr["target_branch"], "unmerged_deps": deps,
                          **({"note": note} if note else {})},
            "remote_actions": actions}


def _worst(verdicts: list[str]) -> str:
    for v in ("not-ok", "incomplete"):
        if v in verdicts:
            return v
    return "ok" if verdicts else "incomplete"


def _md(report: dict) -> str:
    lines = [f"# Review report: {report['run']}", "", f"Generated {report['generated']}. Verdict: **{report['verdict']}**", "",
             "| MR | Ticket | Verdict | Open blocker/major | ACs proven | Behind target |", "|---|---|---|---|---|---|"]
    for m in report["mrs"]:
        open_bm = sum(1 for f in m["findings"] if f["severity"] in ("blocker", "major"))
        proven = sum(1 for a in m["acs"] if a["status"] == "pass")
        behind = m["freshness"]["behind"]
        lines.append(f"| {m['repo']}!{m['iid']} | {m['ticket']} | {m['verdict']} | {open_bm} | {proven}/{len(m['acs'])} | "
                     f"{'?' if behind is None else behind} |")
    for m in report["mrs"]:
        lines += ["", f"## {m['repo']}!{m['iid']} {m['ticket']}: {m['title']}", f"{m['web_url'] or ''}", "",
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
    lines += ["", "Remote actions (nothing is posted without your approval):"]
    for a in report["remote_actions"]:
        iid = f"!{a['iid']}" if a.get("iid") else ""
        lines.append(f"- {a['type']} {a.get('repo', '')}{iid} {','.join(a.get('issues', []))}".rstrip())
    return "\n".join(lines) + "\n"


def build_report(run_id: str) -> dict:
    run = run_load(run_id)
    ws = workspace_load(run["workspace"])
    if run.get("kind") == "review-batch":
        reports = [build_report(c) for c in run.get("children", [])]
        mrs = [m for r in reports for m in r["mrs"]]
        report = {"children": list(run.get("children", [])), "mrs": mrs, "verdict": _worst([m["verdict"] for m in mrs]),
                  "remote_actions": [a for r in reports for a in r["remote_actions"]]}
    elif run.get("kind") == "review":
        mrs = [_mr_report(run, ws, mr) for mr in run.get("mrs", [])]
        if not mrs:
            raise FactoryError(f"run {run_id} has no MRs")
        report = {"mrs": mrs, "verdict": _worst([m["verdict"] for m in mrs]),
                  "remote_actions": [a for m in mrs for a in m["remote_actions"]],
                  "repos_sha": {a: _head(run, ws, a) for a in run.get("repos", {})},
                  "review_rounds": {k: (gate.latest_review(run_id, k) or {}).get("round") for k in ("code", "qa")}}
    else:
        raise FactoryError(f"run {run_id} is kind {run.get('kind')}; review report needs a review run")
    report.update(schemaVersion=SCHEMA_VERSION, kind="review-report", run=run_id, generated=now())
    write_json(run_dir(run_id) / REPORT_FILE, report)
    (run_dir(run_id) / "review-report.md").write_text(_md(report), encoding="utf-8")
    run = run_load(run_id)
    event(run, "review_report", verdict=report["verdict"], mrs=len(report["mrs"]))
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
    for mr in run.get("mrs", []):
        rec = run["repos"].get(mr["repo"])
        if not rec:
            raise FactoryError(f"{mr['repo']}!{mr['iid']} has no worktree (factory run branch --mr first)")
        wt = Path(rec["worktree"])
        if not wt.is_dir():
            raise FactoryError(f"worktree missing: {wt}")
        if git_dirty(wt):
            raise FactoryError(f"{mr['repo']}: worktree has uncommitted changes")
        branch, sha = mr["source_branch"], mr["head_sha"]
        existing = git(wt, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        if existing and existing != sha:
            raise FactoryError(f"local branch {branch} exists at {existing[:8]}, not the MR head {sha[:8]}; "
                               f"resolve it first (no branch is moved by this command)")
        if existing:
            git(wt, "checkout", "-q", branch)
        else:
            git(wt, "checkout", "-q", "--no-track", "-b", branch)
        rec.pop("detached", None)
        rec.update(branch=branch, head=git_head(wt))
        run["targets"] = [mr["target_branch"]]
    event(run, "phase", **{"from": run["phase"], "to": "build"})
    event(run, "review_fix", mrs=[f"{m['repo']}!{m['iid']}" for m in run["mrs"]])
    mrs = run.pop("mrs")
    # MR head at conversion per repo: gate build checks only commits after it (the MR's own were reviewed already)
    run["review_origin"] = {"mrs": mrs, "converted": now(), "head_sha": {m["repo"]: m["head_sha"] for m in mrs}}
    run["kind"] = "ticket"
    run["phase"] = "build"
    run["decisions"].append({"at": now(), "phase": "build", "text": "review fix: continuing on the MR source branch", "by": "agent"})
    run_save(run)
    _emit(args, {"run": run["id"], "kind": "ticket", "phase": "build"},
          f"{run['id']} is now a ticket run in phase build on the MR source branch(es)")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("review", help="record reviews; review existing MRs")
    rs = parser.add_subparsers(dest="review_cmd", required=True)
    p = rs.add_parser("record")
    p.add_argument("run")
    p.add_argument("--kind", choices=["code", "qa"], required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_record)
    p = rs.add_parser("queue", help="filter saved gitlab_list_merge_requests responses into a review queue")
    p.add_argument("workspace")
    p.add_argument("--mrs-json", nargs="+", required=True)
    p.add_argument("--me", help="your GitLab username (drops your own and already approved MRs)")
    p.add_argument("--repo", help="repo alias when the payload does not identify its project")
    p.set_defaults(func=cmd_queue)
    p = rs.add_parser("start", help="start a review run (several MRs: batch parent + one child each)")
    p.add_argument("workspace")
    p.add_argument("--mr", action="append", required=True, metavar="ALIAS!IID[=TICKET]")
    p.add_argument("--ticket")
    p.add_argument("--from", dest="source", action="append", metavar="PATH",
                   help="folder with mr-<alias>-<iid>.json, or a saved list/MR response (repeatable)")
    p.add_argument("--risk", choices=["normal", "high"], default="normal")
    p.set_defaults(func=cmd_start)
    p = rs.add_parser("report", help="verdict per MR; writes review-report.json and .md")
    p.add_argument("run")
    p.set_defaults(func=cmd_report)
    p = rs.add_parser("fix", help="convert a review run into a ticket run on the MR source branch")
    p.add_argument("run")
    p.set_defaults(func=cmd_fix)
