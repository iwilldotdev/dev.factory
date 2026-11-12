"""`factory run start|status|list|branch|decide|block|unblock|human|replan|rewind` (DESIGN §2.2, §3)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..collectors.test import prepare_worktree, setup_command
from ..core import (SCHEMA_VERSION, FactoryError, GateFailed, event, factory_home,
                    git, git_dirty, git_head, now, repo_conf, run_dir, run_load,
                    run_save, workspace_load, write_json)
from .. import integrity
from . import gate

BLOCKER_KINDS = ("env", "access", "question", "dependency", "conflict")


def _emit(args, payload, text: str) -> None:
    print(json.dumps(payload, indent=2) if getattr(args, "json", False) else text)


def run_short(run: dict) -> str:
    prefix = run["workspace"].lower() + "-"
    return run["id"][len(prefix):] if run["id"].startswith(prefix) else run["id"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def branch_name(run: dict, ws: dict, alias: str, target: str | None, mirror: bool = False) -> str:
    """Branch name from workspace branch_format (feature|bug|hotfix); legacy default when no format applies."""
    first = run["tickets"][0]
    kind = "hotfix" if run.get("hotfix") else ("bug" if first["type"] == "bug" else "feature")
    formats = ws.get("branch_format") or {}
    fmt = formats.get(kind) or (formats.get("bug") if kind == "hotfix" else None)
    if not fmt:
        base = f"{'bugfix' if first['type'] == 'bug' else 'feature'}/{first['key']}-{slug(alias)}"
        return f"{base}-{target}" if mirror and target else base
    target = target or ""
    values = {"key": first["key"], "slug": slug(first.get("summary") or alias), "target": target,
              "target_upper": target.upper(), "package": (ws.get("ship") or {}).get("package") or ""}
    try:
        return fmt.format(**values)
    except (KeyError, IndexError, ValueError) as err:
        raise FactoryError(f"branch_format.{kind} {fmt!r} is invalid: {err}") from err


# ---------- start ----------

def cmd_start(args) -> int:
    ws = workspace_load(args.workspace)
    tickets = [t.upper() for t in args.tickets]
    bugs = {b.upper() for b in args.bug or []}
    hotfix = None
    targets = list((ws.get("ship") or {}).get("targets") or [])
    if getattr(args, "hotfix", False):
        from .workspace import hotfix_config, hotfix_config_errors
        cfg = hotfix_config(ws)
        if not cfg:
            raise FactoryError("--hotfix needs ship.hotfix in workspace.json (or a target named homolog)")
        bad = hotfix_config_errors(dict(ws, ship=dict(ws.get("ship") or {}, hotfix=cfg)))
        if bad:
            raise FactoryError("; ".join(bad))
        hotfix = {"base": cfg["base"], "mirror": list(cfg.get("mirror") or [])}
        targets = [hotfix["base"], *hotfix["mirror"]]
        if not args.feature:
            bugs |= set(tickets)
    run_id = f"{ws['name'].lower()}-{(args.epic or tickets[0]).lower()}"
    if (run_dir(run_id) / "run.json").exists():
        raise FactoryError(f"run {run_id} already exists")
    stamp = now()
    run = {
        "schemaVersion": SCHEMA_VERSION, "id": run_id, "workspace": ws["name"], "kind": "ticket",
        "tickets": [{"key": t, "type": "bug" if t in bugs else "feature",
                     "parent": args.epic.upper() if args.epic else None, "depends_on": []} for t in tickets],
        "repos": {}, "targets": targets,
        "phase": "intake", "risk": args.risk,
        "loops": {"build": 0, "review": 0},
        "decisions": [], "blockers": [], "events": [], "sessions": [],
        "created": stamp, "updated": stamp,
    }
    if hotfix:
        run["hotfix"] = hotfix
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
    branch = args.name or branch_name(run, ws, alias, args.base)
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
    run["repos"][alias] = {"branch": branch, "base": args.base, "worktree": str(worktree),
                           "head": git_head(worktree), "base_ref": base}
    if base_branch:
        run["repos"][alias]["base_branch"] = base_branch
        run["repos"][alias]["base_run"] = args.from_run
    run_save(run)
    warn = _prepare(ws, conf, main, worktree)
    _emit(args, dict(run["repos"][alias], **({"setup_error": warn} if warn else {})),
          f"{alias}: branch {branch} from {base} at {worktree}" + (f"\nWARNING: {warn}" if warn else ""))
    return 1 if warn else 0


def _prepare(ws: dict, conf: dict, main: Path, worktree: Path) -> str | None:
    """Same preparation as the temp worktrees of baseline/repro/red-green (quality.setup or node_modules link)."""
    try:
        prepare_worktree(main, worktree, setup_command(ws, conf["alias"]), (conf.get("env") or {}).get("env_vars"))
    except RuntimeError as err:
        return f"{err}; fix it and rerun the setup command in {worktree}"
    return None


def cmd_mirror(args) -> int:
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    hf = run.get("hotfix")
    if not hf:
        raise FactoryError("run mirror is for hotfix runs (run start --hotfix)")
    target = args.to
    if target not in (hf.get("mirror") or []):
        raise FactoryError(f"{target!r} is not a mirror target of this hotfix ({hf.get('mirror')})")
    conf = repo_conf(ws, args.repo)
    alias = conf["alias"]
    rec = run["repos"].get(alias)
    if not rec:
        raise FactoryError(f"repo {alias} has no branch in run {run['id']}")
    if target in (rec.get("worktrees") or {}):
        raise FactoryError(f"{alias} already mirrored to {target} ({rec['worktrees'][target]}); "
                           "if a cherry-pick is in progress there, resolve it and `git cherry-pick --continue`")
    base_wt = Path(rec["worktree"])
    if not any(e["kind"] == "gate_pass" and e["data"].get("phase") == "build" for e in run.get("events", [])):
        raise GateFailed([f"gate build has not passed for {hf['base']}; commit the fix and run `factory gate {run['id']} build` first"])
    if git_dirty(base_wt):
        raise GateFailed([f"{alias}: {hf['base']} worktree has uncommitted changes; commit before mirroring"])
    main = Path(conf.get("path") or "")
    base_ref = gate.resolve_base(base_wt, rec.get("base", hf["base"]))
    mb = git(base_wt, "merge-base", "HEAD", base_ref)
    shas = git(base_wt, "rev-list", "--reverse", f"{mb}..{rec['branch']}").split()
    if not shas:
        raise GateFailed([f"{alias}: no commits between merge-base and {rec['branch']} to mirror"])
    root = ws.get("worktree_root")
    worktree = Path(root) / f"{run_short(run)}-{alias.lower()}-{target}"
    if worktree.exists():
        raise FactoryError(f"worktree path already exists: {worktree}")
    name = args.name or branch_name(run, ws, alias, target, mirror=True)
    start = gate.resolve_base(main, target)
    if not git(main, "rev-parse", "--verify", "--quiet", start + "^{commit}", check=False):
        raise FactoryError(f"base ref {start} not found in {main}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--no-track", "-b", name, str(worktree), start],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FactoryError(f"git worktree add failed: {proc.stderr.strip()}")
    warn = _prepare(ws, conf, main, worktree)
    if warn:
        print(f"WARNING: {warn}")
    rec.setdefault("branches", {})[target] = name
    rec.setdefault("worktrees", {})[target] = str(worktree)
    pick = subprocess.run(["git", "-C", str(worktree), "cherry-pick", *shas],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if pick.returncode != 0:
        files = [f for f in git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).splitlines() if f]
        text = f"cherry-pick conflict mirroring {alias} to {target} in {worktree}: " + ", ".join(files or ["?"])
        run.setdefault("blockers", []).append({"at": now(), "kind": "conflict", "text": text,
                                               "files": files, "resolved": False})
        event(run, "mirror", repo=alias, target=target, status="conflict", files=files)
        run_save(run)
        raise GateFailed([text,
                          f"worktree left mid-cherry-pick at {worktree}: a builder resolves the files, `git add` them and runs "
                          "`git cherry-pick --continue`; never --abort or reset; then `factory run unblock`"])
    event(run, "mirror", repo=alias, target=target, status="ok", commits=len(shas))
    run_save(run)
    _emit(args, {"target": target, "branch": name, "worktree": str(worktree), "commits": len(shas)},
          f"{alias}: mirrored {len(shas)} commit(s) to {target} on {name} at {worktree}")
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
        prev = next((e["data"].get("resume") or e["data"].get("from") for e in reversed(run["events"])
                     if e["kind"] == "phase" and e["data"].get("to") == "blocked"), None) or "build"
        event(run, "phase", **{"from": "blocked", "to": prev})
        run["phase"] = prev
    run_save(run)
    _emit(args, {"phase": run["phase"]}, f"blocker #{args.n} resolved; phase {run['phase']}")
    return 0


def cmd_replan(args) -> int:
    """The one legit way to change acs.json/checks/figma/diagnosis after the plan gate: back to plan, lock cleared."""
    run = run_load(args.run)
    if run.get("kind") not in ("ticket", "review", None):
        raise FactoryError(f"run replan is for ticket and review runs, not {run.get('kind')}")
    if run["phase"] in ("intake", "plan", "done"):
        raise FactoryError(f"run {run['id']} is in phase {run['phase']}; nothing to replan")
    loops = run.setdefault("loops", {"build": 0, "review": 0})
    loops["replan"] = loops.get("replan", 0) + 1
    changed = integrity.changed_files(run) if isinstance(run.get("plan_lock"), dict) else []
    event(run, "replan", reason=args.reason, **{"from": run["phase"]}, changed=changed, loop=loops["replan"])
    event(run, "loop", replan=loops["replan"])
    run.pop("plan_lock", None)
    gate._set_phase(run, "plan")
    run_save(run)
    _emit(args, {"phase": "plan", "replan": loops["replan"]},
          f"phase plan (replan {loops['replan']}); change the plan files, then `factory gate {run['id']} plan`")
    return 0


REWIND_TO = ("build", "prove")


def cmd_rewind(args) -> int:
    """Back to build or prove after the worktree moved (rebase/merge, fixes) past the proof."""
    run = run_load(args.run)
    phases, _ = gate.machine(run)
    if run.get("kind") == "epic" or args.phase not in phases:
        raise FactoryError(f"{run.get('kind')} runs cannot rewind to {args.phase}")
    current = run["phase"]
    if current == "blocked":
        raise FactoryError("run is blocked: resolve the blockers first (factory run unblock)")
    if current not in phases or phases.index(current) <= phases.index(args.phase):
        raise FactoryError(f"run {run['id']} is in phase {current}; rewind goes back to an earlier phase")
    loops = run.setdefault("loops", {"build": 0, "review": 0})
    loops["build"] = loops.get("build", 0) + 1
    event(run, "rewind", reason=args.reason, **{"from": current, "to": args.phase}, loop=loops["build"])
    event(run, "loop", build=loops["build"])
    gate._set_phase(run, args.phase)
    run_save(run)
    _emit(args, {"phase": args.phase, "loops": loops}, f"phase {current} -> {args.phase} (build loop {loops['build']})")
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
    p.add_argument("--hotfix", action="store_true", help="hotfix run: targets = [ship.hotfix.base, *mirror]; tickets are bugs")
    p.add_argument("--feature", action="store_true", help="with --hotfix: tickets stay features")
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
    p = rs.add_parser("mirror", help="hotfix: cherry-pick the base-target commits into a mirror target worktree")
    p.add_argument("run")
    p.add_argument("repo")
    p.add_argument("--to", required=True)
    p.add_argument("--name")
    p.set_defaults(func=cmd_mirror)
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
    p = rs.add_parser("replan", help="back to plan to change acs.json/checks/figma/diagnosis (clears the plan lock)")
    p.add_argument("run")
    p.add_argument("reason")
    p.set_defaults(func=cmd_replan)
    p = rs.add_parser("rewind", help="back to build or prove after the worktree moved past the proof")
    p.add_argument("run")
    p.add_argument("phase", choices=REWIND_TO)
    p.add_argument("reason")
    p.set_defaults(func=cmd_rewind)
