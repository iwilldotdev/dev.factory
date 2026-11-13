"""`factory epic start|plan|status|ship plan|ship verify` (DESIGN §6).

An epic is a parent run (kind "epic") coordinating one child run per ticket.
"""

from __future__ import annotations

import json

from ..core import (SCHEMA_VERSION, FactoryError, GateFailed, event,
                    now, read_json, repo_conf, run_dir,
                    run_load, run_save, workspace_load, write_json)
from . import ship

FINISHED = ("ship", "feedback", "done")  # a child in one of these has passed review
PLANNED = ("build", "prove", "review", "ship", "feedback", "done")


def _emit(args, payload, text: str) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False) if getattr(args, "json", False) else text)


def child_id(ws_name: str, key: str) -> str:
    return f"{ws_name.lower()}-{key.lower()}"


def _new_run(ws: dict, run_id: str, tickets: list[dict], kind: str, risk: str, **extra) -> dict:
    stamp = now()
    run = {
        "schemaVersion": SCHEMA_VERSION, "id": run_id, "workspace": ws["name"], "kind": kind,
        "tickets": tickets, "repos": {}, "targets": list((ws.get("ship") or {}).get("targets") or []),
        "phase": "intake", "risk": risk, "loops": {"build": 0, "review": 0},
        "decisions": [], "blockers": [], "events": [], "sessions": [], "created": stamp, "updated": stamp,
    }
    run.update(extra)
    for sub in ("remote", "figma", "checks", "evidence", "reviews", "baseline"):
        (run_dir(run_id) / sub).mkdir(parents=True, exist_ok=True)
    event(run, "phase", **{"from": None, "to": "intake"})
    run_save(run)
    return run


def _children(parent: dict) -> list[dict]:
    return [run_load(c) for c in parent.get("children", [])]


def _require_epic(run_id: str) -> dict:
    run = run_load(run_id)
    if run.get("kind") != "epic":
        raise FactoryError(f"run {run_id} is not an epic (kind={run.get('kind')})")
    return run


# ---------- start ----------

def cmd_start(args) -> int:
    ws = workspace_load(args.workspace)
    epic_key = args.epic.upper()
    keys = [t.upper() for t in args.tickets]
    if len(set(keys)) != len(keys):
        raise FactoryError("duplicate ticket keys")
    if epic_key in keys:
        raise FactoryError("the epic key cannot also be one of its tickets")
    bugs = {b.upper() for b in args.bug or []}
    parent_id = child_id(ws["name"], epic_key)
    ids = [child_id(ws["name"], k) for k in keys]
    for rid in [parent_id, *ids]:
        if (run_dir(rid) / "run.json").exists():
            raise FactoryError(f"run {rid} already exists")
    _new_run(ws, parent_id, [{"key": epic_key, "type": "feature", "parent": None, "depends_on": []}],
             "epic", args.risk, children=ids)
    for key, rid in zip(keys, ids):
        _new_run(ws, rid, [{"key": key, "type": "bug" if key in bugs else "feature",
                            "parent": epic_key, "depends_on": []}], "ticket", args.risk, parent=parent_id)
    _emit(args, {"epic": parent_id, "children": ids}, f"started epic {parent_id} with children: {', '.join(ids)}")
    return 0


# ---------- plan ----------

def compute_waves(deps: dict[str, list[str]]) -> tuple[list[list[str]], list[str]]:
    """Topological levels. Returns (waves, keys left in a cycle)."""
    remaining = {k: set(v) for k, v in deps.items()}
    done: set[str] = set()
    waves: list[list[str]] = []
    while remaining:
        level = [k for k, d in remaining.items() if d <= done]
        if not level:
            return waves, sorted(remaining)
        waves.append(level)
        done.update(level)
        for k in level:
            del remaining[k]
    return waves, []


def validate(parent: dict, ws: dict) -> tuple[list[str], dict, list[list[str]]]:
    """Returns (errors, epic.json data, waves)."""
    path = run_dir(parent["id"]) / "epic.json"
    if not path.is_file():
        return ["epic.json missing"], {}, []
    try:
        data = read_json(path)
    except FactoryError as err:
        return [str(err)], {}, []
    errors: list[str] = []
    if data.get("schemaVersion") != 1:
        errors.append("epic.json: schemaVersion must be 1")
    tickets = data.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        return errors + ["epic.json: no tickets"], data, []
    ws_name = ws["name"]
    expected = {c: c for c in parent.get("children", [])}
    keys: list[str] = []
    for t in tickets:
        key = str(t.get("key", "")).upper()
        if not key:
            errors.append("epic.json: ticket without key")
            continue
        if key in keys:
            errors.append(f"epic.json: duplicate ticket {key}")
        keys.append(key)
        if child_id(ws_name, key) not in expected:
            errors.append(f"epic.json: ticket {key} is not a child of {parent['id']}")
        for alias in t.get("repos") or []:
            try:
                repo_conf(ws, alias)
            except FactoryError:
                errors.append(f"epic.json: {key}: repo {alias} is not in workspace {ws_name}")
    present = set(keys)
    for cid in expected:
        prefix = ws_name.lower() + "-"
        key = cid[len(prefix):].upper() if cid.startswith(prefix) else cid.upper()
        if key not in present:
            errors.append(f"epic.json: child {cid} is missing")
    deps: dict[str, list[str]] = {}
    for t in tickets:
        key = str(t.get("key", "")).upper()
        mine = [str(d).upper() for d in t.get("depends_on") or []]
        for d in mine:
            if d not in present:
                errors.append(f"epic.json: {key} depends on unknown ticket {d}")
            if d == key:
                errors.append(f"epic.json: {key} depends on itself")
        deps[key] = [d for d in mine if d in present and d != key]
    waves, cyclic = compute_waves(deps)
    if cyclic:
        errors.append(f"epic.json: dependency cycle among {', '.join(cyclic)}")
    return errors, data, waves


def cmd_plan(args) -> int:
    parent = _require_epic(args.parent)
    ws = workspace_load(parent["workspace"])
    errors, data, waves = validate(parent, ws)
    if errors:
        raise GateFailed(errors)
    by_key = {str(t["key"]).upper(): t for t in data["tickets"]}
    for wave_idx, wave in enumerate(waves):
        for key in wave:
            t = by_key[key]
            child = run_load(child_id(ws["name"], key))
            tk = child["tickets"][0]
            tk["depends_on"] = [str(d).upper() for d in t.get("depends_on") or []]
            if t.get("summary"):
                tk["summary"] = t["summary"]
            if t.get("area"):
                tk["area"] = t["area"]
            child["wave"] = wave_idx
            child["planned_repos"] = list(t.get("repos") or [])
            stack = {}
            for dep in tk["depends_on"]:
                for alias in by_key[dep].get("repos") or []:
                    if alias in child["planned_repos"] and alias not in stack:
                        stack[alias] = child_id(ws["name"], dep)
            child["stack_from"] = stack
            run_save(child)
    parent["waves"] = waves
    parent["epic_plan"] = {k: {"repos": by_key[k].get("repos") or [], "depends_on": by_key[k].get("depends_on") or []}
                           for k in by_key}
    event(parent, "epic_plan", waves=waves)
    run_save(parent)
    _emit(args, {"waves": waves}, "waves: " + " | ".join(", ".join(w) for w in waves))
    return 0


# ---------- status ----------

def child_rows(parent: dict) -> list[dict]:
    from . import batch  # shared row builder (also serves `factory batch status`)
    return batch.base_rows(parent)


def cmd_status(args) -> int:
    parent = _require_epic(args.parent)
    rows = child_rows(parent)
    ready = [r["key"] for r in rows if r["ready"]]
    if getattr(args, "json", False):
        print(json.dumps({"id": parent["id"], "phase": parent["phase"], "waves": parent.get("waves"),
                          "children": rows, "ready": ready}, indent=2))
        return 0
    print(f"{parent['id']}  phase={parent['phase']}  waves={parent.get('waves')}")
    print("child\tphase\twave\tbuild/review loops\tblockers\tMRs")
    for r in rows:
        print(f"{r['key']}\t{r['phase']}\t{r['wave']}\t{r['loops'].get('build', 0)}/{r['loops'].get('review', 0)}"
              f"\t{r['open_blockers']}\t{len(r['mrs'])}")
    print("ready: " + (", ".join(ready) if ready else "none"))
    return 0


# ---------- ship ----------

def _epic_conf(ws: dict, parent: dict) -> dict:
    key = parent["tickets"][0]["key"]
    return next((e for e in ws.get("epics", []) if e.get("key") == key), {})


def _sort_children(parent: dict, ws: dict) -> list[dict]:
    waves = parent.get("waves") or []
    wave_of = {child_id(ws["name"], k): i for i, w in enumerate(waves) for k in w}
    return sorted(_children(parent), key=lambda c: wave_of.get(c["id"], 0))


def build_epic_plan(parent_id: str, allow_behind: bool = False) -> dict:
    parent = _require_epic(parent_id)
    ws = workspace_load(parent["workspace"])
    if not parent.get("waves"):
        raise FactoryError("run `factory epic plan` first (no waves)")
    ship_conf = ws.get("ship") or {}
    mode = (ws.get("autonomy") or {}).get("ship", "ask")
    wave_of = {child_id(ws["name"], k): i for i, w in enumerate(parent["waves"]) for k in w}
    pending = [c for c in _children(parent) if c["phase"] not in FINISHED]
    if pending:
        raise GateFailed([f"{c['id']}: phase {c['phase']}, not review-passed" for c in pending])

    in_epic = {c["tickets"][0]["key"] for c in _children(parent)}
    plans: dict[str, dict] = {}
    errors: list[str] = []
    for c in _children(parent):
        try:
            plans[c["id"]] = ship.build_plan(c["id"], allow_behind, internal_deps=in_epic)
        except GateFailed as err:
            errors.extend(f"{c['id']}: {r}" for r in err.reasons)
    if errors:
        raise GateFailed(errors)

    target_pos = {t: i for i, t in enumerate(dict.fromkeys(
        [*(ship_conf.get("targets") or []), *(m["target"] for p in plans.values() for m in p["mrs"])]))}
    repo_pos = {}
    for p in plans.values():
        for m in p["mrs"]:
            try:
                repo_pos[m["repo"]] = repo_conf(ws, m["repo"]).get("order", 999)
            except FactoryError:
                repo_pos[m["repo"]] = 999
    order = {c["id"]: i for i, c in enumerate(_children(parent))}
    entries = []
    for cid, plan in plans.items():
        for m in plan["mrs"]:
            entries.append((wave_of.get(cid, 0), repo_pos[m["repo"]], m["repo"], target_pos[m["target"]], order[cid], cid, m))
    entries.sort(key=lambda e: e[:5])

    by_child: dict[str, list[dict]] = {}
    for e in entries:
        by_child.setdefault(e[5], []).append(e[6])
    child_deps = {c["id"]: [child_id(ws["name"], d) for d in c["tickets"][0].get("depends_on") or []]
                  for c in _children(parent)}
    mrs = []
    for _w, _r, _a, _t, _o, cid, m in entries:
        mr = dict(m, child=cid, depends_on=[dict(d, child=cid) for d in m["depends_on"]])
        links = []
        for dep in child_deps.get(cid, []):
            cands = [x for x in by_child.get(dep, []) if x["target"] == m["target"]]
            same = [x for x in cands if x["repo"] == m["repo"]]
            for x in (same or cands):
                links.append({"child": dep, "repo": x["repo"], "target": x["target"]})
        mr["depends_on"] = mr["depends_on"] + links
        if links:
            mr["description"] = mr["description"].rstrip("\n") + "\n\n## Depends on (other tickets)\n\n" + "\n".join(
                f"Depends on {{mr_url:{d['child']}:{d['repo']}:{d['target']}}}" for d in links) + "\n"
        mrs.append(mr)

    actions = []
    for mr in mrs:
        for a in plans[mr["child"]]["actions"]:
            if a["type"] in ("push", "mr", "readback") and a.get("repo") == mr["repo"] and a.get("target") == mr["target"]:
                actions.append(dict(a, child=mr["child"]))
    epic_key = parent["tickets"][0]["key"]
    epic_conf = _epic_conf(ws, parent)
    jira = None
    if ship_conf.get("jira_comment") == "mr-links":
        content = [{"type": "paragraph", "content": [
            {"type": "text", "text": f"{m['child']} {m['repo']} ({m['target']}): "},
            {"type": "inlineCard", "attrs": {"url": f"{{mr_url:{m['child']}:{m['repo']}:{m['target']}}}"}}]} for m in mrs]
        jira = {"issues": [epic_key], "adf": {"version": 1, "type": "doc", "content": content},
                "epic_ship_comment": epic_conf.get("ship_comment")}
        actions.append({"type": "jira_comment", "issues": [epic_key], "needs_user": ship._needs_user(mode, "jira_comment")})
    for cid, plan in plans.items():
        for a in plan["actions"]:
            if a["type"] == "jira_transition":
                actions.append(dict(a, child=cid))
    if epic_conf.get("jira_transition") and not ship_conf.get("jira_transition"):
        actions.append({"type": "jira_transition", "issues": [epic_key], "name": epic_conf["jira_transition"],
                        "needs_user": ship._needs_user(mode, "jira_transition")})

    plan = {"schemaVersion": 1, "epic": parent_id, "run": parent_id, "autonomy": mode, "waves": parent["waves"],
            "children": list(plans), "mrs": mrs, "jira_comment": jira, "actions": actions,
            "visual_evidence": [dict(v, child=cid) for cid, p in plans.items() for v in p.get("visual_evidence") or []]}
    write_json(run_dir(parent_id) / "ship-plan.json", plan)
    return plan


def verify(parent_id: str) -> list[str]:
    parent = _require_epic(parent_id)
    errors: list[str] = []
    if not parent.get("children"):
        return ["epic has no children"]
    for cid in parent["children"]:
        errors.extend(f"{cid}: {e}" for e in ship.verify(cid))
    if not errors:
        event(parent, "ship", children=list(parent["children"]))
        run_save(parent)
    return errors


def _cmd_ship_plan(args) -> int:
    plan = build_epic_plan(args.parent, args.allow_behind)
    if getattr(args, "json", False):
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    print(f"ship-plan.json: {len(plan['mrs'])} MR(s) across {len(plan['children'])} child run(s)")
    for i, a in enumerate(plan["actions"], 1):
        who = "ASK" if a["needs_user"] else "auto"
        print(f"{i}. [{who}] {a['type']} {a.get('child', '')} {a.get('repo', '')} {a.get('target', '')}".rstrip())
    return 0


def _cmd_ship_verify(args) -> int:
    errs = verify(args.parent)
    if errs:
        raise GateFailed(errs)
    print("OK epic ship verified")
    return 0


# ---------- gate evaluators (called by gate.py for kind == "epic") ----------

def _eval_intake(parent: dict, ws: dict) -> list[str]:
    errors = [] if parent.get("children") else ["epic has no children"]
    for i, b in enumerate(parent.get("blockers", [])):
        if b.get("kind") == "question" and not b.get("resolved"):
            errors.append(f"open question blocker #{i}: {b.get('text')}")
    return errors


def _eval_plan(parent: dict, ws: dict) -> list[str]:
    errors, _data, waves = validate(parent, ws)
    if not errors and parent.get("waves") != waves:
        errors.append("epic plan not applied (factory epic plan)")
    for c in _children(parent):
        if c["phase"] not in PLANNED:
            errors.append(f"{c['id']}: plan gate not passed (phase {c['phase']})")
    return errors


def _eval_build(parent: dict, ws: dict) -> list[str]:
    return [f"{c['id']}: phase {c['phase']}, review not passed (needs ship or done)"
            for c in _children(parent) if c["phase"] not in FINISHED]


def _eval_ship(parent: dict, ws: dict) -> list[str]:
    errors = []
    for cid in parent.get("children", []):
        errors.extend(f"{cid}: {e}" for e in ship.verify(cid))
    return errors


EVALUATORS = {"intake": _eval_intake, "plan": _eval_plan, "build": _eval_build, "ship": _eval_ship}
PHASES = ["intake", "plan", "build", "ship", "done"]


def register(sub) -> None:
    parser = sub.add_parser("epic", help="coordinate an epic across child runs")
    es = parser.add_subparsers(dest="epic_cmd", required=True)
    p = es.add_parser("start")
    p.add_argument("workspace")
    p.add_argument("epic")
    p.add_argument("--tickets", nargs="+", required=True)
    p.add_argument("--bug", action="append", help="ticket key that is a bug (repeatable)")
    p.add_argument("--risk", choices=["normal", "high"], default="normal")
    p.set_defaults(func=cmd_start)
    p = es.add_parser("plan")
    p.add_argument("parent")
    p.set_defaults(func=cmd_plan)
    p = es.add_parser("status")
    p.add_argument("parent")
    p.set_defaults(func=cmd_status)
    p = es.add_parser("ship")
    ss = p.add_subparsers(dest="epic_ship_cmd", required=True)
    pl = ss.add_parser("plan")
    pl.add_argument("parent")
    pl.add_argument("--allow-behind", action="store_true")
    pl.set_defaults(func=_cmd_ship_plan)
    v = ss.add_parser("verify")
    v.add_argument("parent")
    v.set_defaults(func=_cmd_ship_verify)
