"""`factory batch status <parent>`: one table for the children of an epic or review-batch parent run."""

from __future__ import annotations

import json

from ..core import (FactoryError, current_owner, factory_home, read_json, run_dir, run_load, session_short)
from . import gate
from .run import next_action

PARENT_KINDS = ("epic", "review-batch")
FINISHED = ("ship", "feedback", "done")  # a child in one of these has passed review


def children_of(parent: dict) -> list[dict]:
    return [run_load(c) for c in parent.get("children", [])]


def mr_readbacks(run_id: str) -> list[str]:
    d = run_dir(run_id) / "remote"
    return sorted(p.name for p in d.glob("mr-*.json") if not p.name.endswith("-discussions.json")) if d.is_dir() else []


def base_rows(parent: dict) -> list[dict]:
    """Generic per-child facts (id, key, phase, wave, loops, blockers, deps, MR readbacks, ready)."""
    children = children_of(parent)
    phase_of = {c["tickets"][0]["key"]: c["phase"] for c in children}
    rows = []
    for c in children:
        tk = c["tickets"][0]
        deps = tk.get("depends_on") or []
        open_blockers = [b for b in c.get("blockers", []) if not b.get("resolved")]
        deps_done = all(phase_of.get(d) in FINISHED for d in deps)
        ready = deps_done and c["phase"] not in FINISHED and c["phase"] != "blocked"
        rows.append({"id": c["id"], "key": tk["key"], "phase": c["phase"], "wave": c.get("wave"),
                     "loops": dict(c.get("loops", {})), "open_blockers": len(open_blockers),
                     "depends_on": deps, "mrs": mr_readbacks(c["id"]), "ready": ready})
    return rows


def open_human_checks(run_id: str) -> int:
    """Checks of kind human in acs.json without a passing evidence file (read only)."""
    path = run_dir(run_id) / "acs.json"
    if not path.is_file():
        return 0
    try:
        checks = read_json(path).get("checks") or []
    except FactoryError:
        return 0
    open_ = 0
    for chk in checks:
        if not isinstance(chk, dict) or chk.get("type") != "human" or not chk.get("id"):
            continue
        passed = False
        for p in (run_dir(run_id) / "evidence").glob(f"{chk['id']}*.json"):
            if p.stem != chk["id"] and not p.stem.startswith(chk["id"] + "@"):
                continue
            try:
                passed = passed or read_json(p).get("status") == "pass"
            except FactoryError:
                continue
        if not passed:
            open_ += 1
    return open_


def lease_holder(ws: str) -> str | None:
    path = factory_home() / "env" / ws / "lease.json"
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except FactoryError:
        return None
    if not isinstance(data, dict):
        return None
    for k in ("run", "holder", "owner", "session"):
        if data.get(k):
            return str(data[k])
    return "held"


def rows(parent: dict) -> list[dict]:
    out = []
    for row, c in zip(base_rows(parent), children_of(parent)):
        try:
            action = next_action(c)
            missing = gate.evaluate(c["id"], c["phase"]) if c["phase"] in gate.EVALUATORS else []
        except FactoryError as err:
            action, missing = f"cannot evaluate: {err}", []
        mrs = [f"{m.get('repo')}!{m.get('iid')}" for m in c.get("mrs") or [] if isinstance(m, dict)]
        owner = current_owner(c)
        row.update({"kind": c.get("kind"), "review_mrs": mrs, "next": action, "missing": len(missing),
                    "open_human_checks": open_human_checks(c["id"]),
                    "owner": session_short(owner["session"]) if owner else None,
                    "lease": lease_holder(c["workspace"])})
        out.append(row)
    return out


def cmd_status(args) -> int:
    parent = run_load(args.parent)
    if parent.get("kind") not in PARENT_KINDS:
        raise FactoryError(f"run {args.parent} is not a batch parent (kind={parent.get('kind')}; "
                           f"expected {' or '.join(PARENT_KINDS)})")
    data = rows(parent)
    if getattr(args, "json", False):
        print(json.dumps({"id": parent["id"], "kind": parent["kind"], "phase": parent["phase"],
                          "children": data}, indent=2, ensure_ascii=False))
        return 0
    print(f"{parent['id']}  kind={parent['kind']}  phase={parent['phase']}")
    print("child\tticket\tMR\tphase\tblockers\thuman\towner\tlease\tnext")
    for r in data:
        print("\t".join([r["id"], r["key"], ",".join(r["review_mrs"]) or "-", r["phase"], str(r["open_blockers"]),
                         str(r["open_human_checks"]), r["owner"] or "-", r["lease"] or "-", r["next"]]))
    if not data:
        print("no children")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("batch", help="visibility over the children of an epic or review-batch")
    bs = parser.add_subparsers(dest="batch_cmd", required=True)
    p = bs.add_parser("status")
    p.add_argument("parent")
    p.set_defaults(func=cmd_status)
