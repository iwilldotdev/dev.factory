"""Epics: a parent run over child runs. `epic.json` gives each child its `depends_on`; `epic plan` computes the waves (a cycle is refused)
and starts the children, a same-repo dependent branches from its dependency's head, `next <epic>` hands out at most
`[orchestration] max_parallel` ready children, and the epic ships the children's intents in order with one ticket comment [L-88, L-91, L-92, L-184, L-185].
A batch of review runs is an epic whose children depend on nothing."""
from __future__ import annotations

import json
from pathlib import Path

from . import envproc as ep, gates, git, human, lifecycle, next as nxt, ship as shipmod
from .run import Run
from .util import FactoryError, fwd, read_json, safe_id, sha256_file, write_atomic

DONE = ("ship", "feedback", "done")  # the head of a child is stable once its review passed


def waves(children: list[dict]) -> list[list[str]]:
    """Dependency waves; a cycle or an unknown dependency is refused."""
    deps, out = {c["ticket"]: set(c.get("depends_on", [])) for c in children}, []
    for t, d in deps.items():
        if t in d or d - deps.keys():
            raise FactoryError(f"{t}: depends_on names " + ("itself" if t in d else ", ".join(sorted(d - deps.keys())) + ", which is not a child of this epic"))
    left = dict(deps)
    while left:
        wave = sorted(t for t, d in left.items() if not d - {x for w in out for x in w})
        if not wave:
            raise FactoryError(f"depends_on cycle among: {', '.join(sorted(left))} [L-185]")
        out.append(wave)
        left = {t: d for t, d in left.items() if t not in wave}
    return out


def plan(project, run: Run, data: dict | None, actor: str | None, batch: bool = False) -> dict:
    """Validate the children, compute the waves, start each child run and record `epic_planned`."""
    if (st := run.state())["kind"] != "epic":
        raise FactoryError(f"run {run.id} is a {st['kind']} run: epic plan is for --kind epic")
    path = run.dir / "epic.json"
    data = data if data is not None else (read_json(path) if path.is_file() else None)
    kids = (data or {}).get("children")
    if not isinstance(kids, list) or not kids or any(not isinstance(c, dict) or not c.get("ticket") for c in kids):
        raise FactoryError(f"write {fwd(path)}: " + '{"children": [{"ticket": "ABC-2", "repos": ["web"], "kind": "feature", "depends_on": ["ABC-1"]}]}')
    if len({c["ticket"] for c in kids}) != len(kids):
        raise FactoryError("epic.json: a ticket appears twice")
    ws = waves(kids)
    for c in kids:
        child = project.run(safe_id(c["ticket"]))
        if not child.exists():
            lifecycle.start(project, c["ticket"], c.get("kind"), c.get("repos") or None, actor, parent=run.id)
        elif child.state()["parent"] != run.id:
            raise FactoryError(f"run {child.id} exists and belongs to another parent")
    return run.append({"type": "epic_planned", "children": [{"ticket": c["ticket"], "kind": c.get("kind"), "repos": c.get("repos") or [], "depends_on": c.get("depends_on") or []} for c in kids], "waves": ws, "batch": batch}, actor)


def children(project, st: dict, detail: bool = False) -> list[dict]:
    """One row per child: its run, phase, blockers, owner and whether it can start; `detail` adds the human checks it waits for and its env lock."""
    rows = []
    for c in (st["epic"] or {}).get("children", []):
        r = project.run(safe_id(c["ticket"]))
        cst = r.state() if r.exists() else None
        rows.append({"ticket": c["ticket"], "run": r.id, "depends_on": c.get("depends_on") or [], "phase": nxt.phase(r, project, cst) if cst else "missing",
                     "blockers": [b["id"] for b in cst["blockers"].values() if b["open"]] if cst else [], "owner": cst["owner"] if cst else None,
                     "kind": gates.effective_kind(r, cst) if cst else None})
    by = {r["ticket"]: r for r in rows}
    for r in rows if detail else []:
        cst = project.run(r["run"]).state() if r["phase"] != "missing" else None
        r["human_checks"] = sum(human.WAITING in x for x in gates.reasons_for(project.run(r["run"]), project, cst, "verify")) if cst and r["phase"] == "build" else 0
        r["env"] = [rc["name"] for rc in project.cfg["repo"] if rc.get("env") and (lk := ep.read_lock(project, rc["name"])) and lk.get("run") == r["run"] and ep.holder_alive(lk)]
    for r in rows:
        r["ready"] = r["phase"] not in (*DONE, "closed", "missing") and all(by[d]["phase"] in DONE for d in r["depends_on"])
    return rows


def render(rows: list[dict]) -> str:
    return "\n".join(f"{r['run']:<20} {r['phase']:<9} {'ready' if r['ready'] else '-':<6} owner={r['owner'] or '-'} blockers={','.join(r['blockers']) or '-'}"
                     + (f" human_checks={r['human_checks']} env={','.join(r['env']) or '-'}" if "env" in r else "")
                     + (f" depends_on={','.join(r['depends_on'])}" if r["depends_on"] else "") for r in rows) or "no children: factory epic plan"


def phase(project, st: dict) -> str:
    if not st["epic"]:
        return "plan"
    batch = st["epic"].get("batch")  # review runs end at their report and ship nothing
    if any(r["phase"] not in (("done",) if batch else DONE) + ("closed",) for r in children(project, st)):
        return "children"
    if batch:
        return "done"
    return "done" if st["shipped"] and st["ships"] and st["shipped"][-1]["result"] == "pass" and st["shipped"][-1]["at"] > st["ships"][-1]["at"] else "ship"


def base_ref(project, run: Run, st: dict, repo: str, from_run: str | None = None) -> str | None:
    """The head of the dependency's worktree in the same repo: the stacked branch starts there, and its branch point is that head [L-91, L-92]."""
    deps = [safe_id(d) for d in _entry(project, st).get("depends_on", [])] if st["parent"] and not from_run else [from_run] if from_run else []
    for d in deps:
        w = git.current(project.run(d).state(), repo) if project.run(d).exists() else None
        if w and Path(w["path"]).is_dir():
            return git.head(Path(w["path"]))
    return None


def _entry(project, st: dict) -> dict:
    parent = project.run(st["parent"]).state()
    return next((c for c in (parent["epic"] or {}).get("children", []) if c["ticket"] == st["ticket"]), {})


def sibling_tree(project, st: dict, repo: str) -> Path | None:
    """A dependency that this run does not declare is served from the sibling child's worktree, else from the main path [L-88]."""
    if not st["parent"]:
        return None
    for c in (project.run(st["parent"]).state()["epic"] or {}).get("children", []):
        if c["ticket"] != st["ticket"] and (w := git.current(project.run(safe_id(c["ticket"])).state(), repo)) and Path(w["path"]).is_dir():
            return Path(w["path"])
    return None


def compute(project, run: Run, session: str | None = None) -> dict:
    """`factory next <epic>`: the plan step, the ready children (at most `max_parallel`, claimed ones counted), or the epic ship."""
    st = run.state()
    ph = phase(project, st)
    rows = children(project, st) if st["epic"] else []
    flag = nxt._flag(project)
    out = {"run": run.id, "ticket": st["ticket"], "kind": "epic", "phase": ph, "action": None, "kind_of_action": "command", "command": None, "files": [], "reasons": [],
           "role": "orchestrator", "decisions": [], "refused": [], "gate": ph, "children": rows}
    if st["closed"]:
        out.update(action="the run is closed", kind_of_action="none")
    elif ph == "plan":
        out.update(action="write epic.json (one child per ticket, with repos, kind and depends_on), then plan the epic", command=f"factory epic plan{flag}", files=[fwd(run.dir / "epic.json")])
    elif ph == "children":
        cap = project.cfg["orchestration"]["max_parallel"]
        running = [r for r in rows if r["owner"] and r["phase"] not in (*DONE, "closed")]
        free = [r for r in rows if r["ready"] and (not r["owner"] or r["owner"] == session)][:max(cap - len([r for r in running if r["owner"] != session]), 0)]
        out["ready"] = [r["run"] for r in free]
        out.update(action=(f"start these children, one session each (max_parallel {cap}): " + ", ".join(r["run"] for r in free)) if free else
                   "no child can start now: wait for the running children or their dependencies", command=f"factory claim {free[0]['run']} --session <hint>{flag}" if free else None,
                   kind_of_action="command" if free else "none")
    elif ph == "ship":
        sp = st["ships"][-1] if st["ships"] else None
        out.update(action="plan the epic ship: ordered intents of every child and one ticket comment" if not sp else "execute the intents of ship/plan.json in order, "
                   "then run factory ship verify on each child, then verify the epic", command=f"factory epic ship {'verify' if sp else 'plan'}{flag}")
    else:
        out.update(action="every child shipped; tell the user the changes in order", kind_of_action="none")
    return out


def ship_plan(project, run: Run, actor: str | None) -> dict:
    st = run.state()
    rows = children(project, st)
    if phase(project, st) not in ("ship", "done"):
        raise FactoryError("every child needs a passing review gate first: " + ", ".join(r["run"] for r in rows if r["phase"] not in (*DONE, "closed")))
    order = [t for w in st["epic"]["waves"] for t in w]
    by, intents, kids = {r["ticket"]: r for r in rows}, [], []
    for t in order:
        if by[t]["phase"] == "closed":
            continue
        sp = shipmod.load(project.run(by[t]["run"]))
        if sp is None:
            raise FactoryError(f"{t}: no ship plan: factory ship plan --run {by[t]['run']}")
        links = [{"ticket": d, "branch": next(i["source_branch"] for i in (shipmod.load(project.run(by[d]["run"])) or {"intents": []})["intents"] if i["intent"] == "change_request")}
                 for d in by[t]["depends_on"] if by[d]["phase"] != "closed" and shipmod.load(project.run(by[d]["run"]))]
        mine = [{**i, "id": f"{t}/{i['id']}", "child": t, **({"depends_on_changes": links} if i["intent"] == "change_request" and links else {})}
                for i in sp["intents"] if i["intent"] not in ("issue_comment", "issue_transition")]  # one ticket comment for the epic
        intents += mine
        kids.append({"ticket": t, "run": by[t]["run"], "depends_on": by[t]["depends_on"], "title": sp["title"]})
    intents.append({"id": "I-comment", "intent": "issue_comment", "ticket": st["ticket"], "needs_user": True,
                    "body_md": f"Draft changes for {st['ticket']}, in merge order:\n" + "\n".join(f"- {k['ticket']}: {k['title']}" + (f" (depends on {', '.join(k['depends_on'])})" if k["depends_on"] else "") for k in kids)})
    body = {"schema": 1, "run": run.id, "ticket": st["ticket"], "children": kids, "intents": intents}
    p = run.dir / "ship" / "plan.json"
    write_atomic(p, json.dumps(body, indent=2) + "\n")
    run.append({"type": "ship_planned", "plan_sha256": sha256_file(p), "heads": {}, "update": False, "decision": None}, actor)
    return body


def ship_verify(project, run: Run, actor: str | None) -> dict:
    """The epic is shipped when every child's own ship gate passed (each child verified its readbacks)."""
    st = run.state()
    if not st["ships"]:
        raise FactoryError("no epic ship plan: factory epic ship plan")
    bad = [f"{r['run']}: the ship gate has not passed (phase {r['phase']})" for r in children(project, st) if r["phase"] not in ("feedback", "done", "closed")]
    state = {"result": "fail" if bad else "pass", "heads": {}, "mismatches": bad, "plan_sha256": st["ships"][-1]["plan_sha256"]}
    run.append({"type": "ship_verified", **state}, actor)
    return state
