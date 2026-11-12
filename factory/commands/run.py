"""`factory run start|status|list|branch|decide|block|unblock|human` (DESIGN §2.2, §3)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..collectors.test import link_deps
from ..core import (SCHEMA_VERSION, FactoryError, event, factory_home,
                    git, git_head, now, repo_conf, run_dir, run_load,
                    run_save, workspace_load, write_json)
from . import gate

BLOCKER_KINDS = ("env", "access", "question", "dependency")


def _emit(args, payload, text: str) -> None:
    print(json.dumps(payload, indent=2) if getattr(args, "json", False) else text)


def run_short(run: dict) -> str:
    prefix = run["workspace"].lower() + "-"
    return run["id"][len(prefix):] if run["id"].startswith(prefix) else run["id"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---------- start ----------

def cmd_start(args) -> int:
    ws = workspace_load(args.workspace)
    tickets = [t.upper() for t in args.tickets]
    bugs = {b.upper() for b in args.bug or []}
    run_id = f"{ws['name'].lower()}-{(args.epic or tickets[0]).lower()}"
    if (run_dir(run_id) / "run.json").exists():
        raise FactoryError(f"run {run_id} already exists")
    stamp = now()
    run = {
        "schemaVersion": SCHEMA_VERSION, "id": run_id, "workspace": ws["name"], "kind": "ticket",
        "tickets": [{"key": t, "type": "bug" if t in bugs else "feature",
                     "parent": args.epic.upper() if args.epic else None, "depends_on": []} for t in tickets],
        "repos": {}, "targets": list((ws.get("ship") or {}).get("targets") or []),
        "phase": "intake", "risk": args.risk,
        "loops": {"build": 0, "review": 0},
        "decisions": [], "blockers": [], "events": [], "sessions": [],
        "created": stamp, "updated": stamp,
    }
    for sub in ("remote", "figma", "checks", "evidence", "reviews", "baseline"):
        (run_dir(run_id) / sub).mkdir(parents=True, exist_ok=True)
    event(run, "phase", **{"from": None, "to": "intake"})
    run_save(run)
    _emit(args, {"run": run_id, "dir": str(run_dir(run_id))}, f"started {run_id} at {run_dir(run_id)}")
    return 0


# ---------- status / list ----------

def next_action(run: dict) -> str:
    phase = run["phase"]
    open_blockers = [b for b in run.get("blockers", []) if not b.get("resolved")]
    if phase == "done":
        return "nothing: run is done"
    if phase == "blocked":
        return "resolve blockers (factory run unblock <run> <n>) or decide with the user"
    if phase not in gate.EVALUATORS:
        return f"unknown phase {phase}"
    errors = gate.evaluate(run["id"], phase)
    if not errors:
        return f"factory gate {run['id']} {phase}"
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    suffix = f" [{len(open_blockers)} open blocker(s)]" if open_blockers else ""
    return f"{errors[0]}{more}{suffix}"


def cmd_status(args) -> int:
    run = run_load(args.run)
    open_blockers = [(i, b) for i, b in enumerate(run.get("blockers", [])) if not b.get("resolved")]
    try:
        action = next_action(run)
        errors = gate.evaluate(run["id"], run["phase"]) if run["phase"] in gate.EVALUATORS else []
    except FactoryError as err:
        action, errors = f"cannot evaluate: {err}", []
    if getattr(args, "json", False):
        print(json.dumps({"id": run["id"], "phase": run["phase"], "loops": run["loops"],
                          "blockers": [dict(b, n=i) for i, b in open_blockers],
                          "next": action, "missing": errors}, indent=2))
        return 0
    print(f"{run['id']}  phase={run['phase']}  risk={run.get('risk')}  "
          f"loops build={run['loops'].get('build', 0)} review={run['loops'].get('review', 0)}")
    for i, b in open_blockers:
        print(f"blocker #{i} [{b['kind']}] {b['text']}")
    print(f"next: {action}")
    for e in errors[1:6]:
        print(f"  - {e}")
    return 0


def cmd_list(args) -> int:
    root = factory_home() / "runs"
    rows = []
    for p in sorted(root.glob("*/run.json")) if root.is_dir() else []:
        try:
            r = run_load(p.parent.name)
        except FactoryError:
            continue
        rows.append({"id": r["id"], "phase": r["phase"], "updated": r.get("updated")})
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['id']}\t{r['phase']}\t{r['updated']}")
        if not rows:
            print("no runs")
    return 0


# ---------- branch ----------

def _for_target(args, run: dict, conf: dict, main: Path) -> int:
    """Record a per-target source branch (branches[target]) created from --base in the main repo."""
    alias = conf["alias"]
    rec = run["repos"].get(alias)
    if not rec:
        raise FactoryError(f"repo {alias} has no branch in run {run['id']}; create the main branch first")
    target = args.for_target
    name = args.name or f"{rec['branch']}-{target}"
    base = gate.resolve_base(main, args.base)
    if not git(main, "rev-parse", "--verify", "--quiet", base + "^{commit}", check=False):
        raise FactoryError(f"base ref {base} not found in {main}")
    if git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False):
        raise FactoryError(f"branch {name} already exists in {main}")
    git(main, "branch", "--no-track", name, base)
    rec.setdefault("branches", {})[target] = name
    run_save(run)
    _emit(args, {"target": target, "branch": name}, f"{alias}: target {target} ships from branch {name} (from {base})")
    return 0


def cmd_branch(args) -> int:
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    conf = repo_conf(ws, args.repo)
    alias = conf["alias"]
    main = Path(conf.get("path") or "")
    if not conf.get("path") or not (main / ".git").exists():
        raise FactoryError(f"repo {alias}: workspace path is missing or not a git repository: {conf.get('path')!r}")
    if args.mr:
        from . import review
        return review.branch_for_mr(args, run, ws, conf, main)
    if not args.base:
        raise FactoryError("--base is required (or --mr <iid> for a review run)")
    if args.for_target:
        return _for_target(args, run, conf, main)
    if alias in run["repos"]:
        raise FactoryError(f"repo {alias} already has a branch in run {run['id']}")
    root = ws.get("worktree_root")
    if not root:
        raise FactoryError("workspace worktree_root is not set")
    first = run["tickets"][0]
    branch = args.name or f"{'bugfix' if first['type'] == 'bug' else 'feature'}/{first['key']}-{slug(alias)}"
    worktree = Path(root) / f"{run_short(run)}-{alias.lower()}"
    if worktree.exists():
        raise FactoryError(f"worktree path already exists: {worktree}")
    base_branch = None
    if args.from_run:
        parent_rec = (run_load(args.from_run).get("repos") or {}).get(alias)
        if not parent_rec:
            raise FactoryError(f"run {args.from_run} has no branch for repo {alias}")
        base_branch = parent_rec["branch"]
        base = base_branch
    else:
        base = gate.resolve_base(main, args.base)
    if not git(main, "rev-parse", "--verify", "--quiet", base + "^{commit}", check=False):
        raise FactoryError(f"base ref {base} not found in {main}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--no-track", "-b", branch, str(worktree), base],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FactoryError(f"git worktree add failed: {proc.stderr.strip()}")
    link_deps(main, worktree)
    run["repos"][alias] = {"branch": branch, "base": args.base, "worktree": str(worktree),
                           "head": git_head(worktree), "base_ref": base}
    if base_branch:
        run["repos"][alias]["base_branch"] = base_branch
        run["repos"][alias]["base_run"] = args.from_run
    run_save(run)
    _emit(args, run["repos"][alias], f"{alias}: branch {branch} from {base} at {worktree}")
    return 0


# ---------- decide / block / unblock / human ----------

def cmd_decide(args) -> int:
    run = run_load(args.run)
    run["decisions"].append({"at": now(), "phase": run["phase"], "text": args.text, "by": args.by})
    run_save(run)
    _emit(args, {"decisions": len(run["decisions"])}, f"decision #{len(run['decisions']) - 1} recorded")
    return 0


def cmd_block(args) -> int:
    run = run_load(args.run)
    run["blockers"].append({"at": now(), "kind": args.kind, "text": args.text, "resolved": False})
    run_save(run)
    _emit(args, {"blocker": len(run["blockers"]) - 1}, f"blocker #{len(run['blockers']) - 1} recorded")
    return 0


def cmd_unblock(args) -> int:
    run = run_load(args.run)
    if not 0 <= args.n < len(run["blockers"]):
        raise FactoryError(f"no blocker #{args.n}")
    run["blockers"][args.n]["resolved"] = True
    run["blockers"][args.n]["resolved_at"] = now()
    if run["phase"] == "blocked" and not any(not b.get("resolved") for b in run["blockers"]):
        prev = next((e["data"].get("from") for e in reversed(run["events"])
                     if e["kind"] == "phase" and e["data"].get("to") == "blocked"), None) or "build"
        event(run, "phase", **{"from": "blocked", "to": prev})
        run["phase"] = prev
    run_save(run)
    _emit(args, {"phase": run["phase"]}, f"blocker #{args.n} resolved; phase {run['phase']}")
    return 0


def cmd_human(args) -> int:
    run = run_load(args.run)
    event(run, "human", note=args.note, phase=run["phase"])
    run_save(run)
    _emit(args, {"ok": True}, "human intervention recorded")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("run", help="manage runs")
    rs = parser.add_subparsers(dest="run_cmd", required=True)
    p = rs.add_parser("start")
    p.add_argument("workspace")
    p.add_argument("tickets", nargs="+")
    p.add_argument("--epic")
    p.add_argument("--risk", choices=["normal", "high"], default="normal")
    p.add_argument("--bug", action="append", help="ticket key that is a bug (repeatable)")
    p.set_defaults(func=cmd_start)
    p = rs.add_parser("status")
    p.add_argument("run")
    p.set_defaults(func=cmd_status)
    p = rs.add_parser("list")
    p.set_defaults(func=cmd_list)
    p = rs.add_parser("branch")
    p.add_argument("run")
    p.add_argument("repo")
    p.add_argument("--base")
    p.add_argument("--mr", type=int, help="review run: detached worktree at the saved head of this MR iid")
    p.add_argument("--name")
    p.add_argument("--from-run", help="child run id whose branch (same repo) this branch stacks on")
    p.add_argument("--for-target", help="record a per-target source branch (branches[target]) instead of the main branch")
    p.set_defaults(func=cmd_branch)
    p = rs.add_parser("decide")
    p.add_argument("run")
    p.add_argument("text")
    p.add_argument("--by", choices=["agent", "user"], default="agent")
    p.set_defaults(func=cmd_decide)
    p = rs.add_parser("block")
    p.add_argument("run")
    p.add_argument("kind", choices=BLOCKER_KINDS)
    p.add_argument("text")
    p.set_defaults(func=cmd_block)
    p = rs.add_parser("unblock")
    p.add_argument("run")
    p.add_argument("n", type=int)
    p.set_defaults(func=cmd_unblock)
    p = rs.add_parser("human")
    p.add_argument("run")
    p.add_argument("note")
    p.set_defaults(func=cmd_human)
