"""Hotfix mirror: the run builds, verifies and reviews the base only. After `gate review`, `factory mirror <repo> --to <target>`
cherry-picks the fix commits onto a worktree `{kind}/{key}-{target}` of the target branch; a conflict is a `tooling` blocker for the
builder. `verify --mirror` runs the `mirror: true` checks from those trees, and `gate ship` needs the same-diff result [L-41, L-93, L-95 to L-100]."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from . import config, evidence, gates, git, human, mirrordiff, plan as planmod
from .run import Run
from .util import FactoryError, fwd


def targets(cfg: dict, repo: str) -> dict[str, str]:
    """target name -> branch: `[[hotfix.targets]]` for every repo, `[[repo.mirror]]` for one."""
    rc = next(r for r in cfg["repo"] if r["name"] == repo)
    return {m["name"]: m["branch"] for m in (*cfg.get("hotfix", {}).get("targets", []), *rc.get("mirror", []))}


def latest(st: dict, repo: str, target: str, results=("applied", "conflict")) -> dict | None:
    return next((m for m in reversed(st["mirrors"]) if m["repo"] == repo and m["target"] == target and m["result"] in results), None)


def applied(st: dict) -> list[dict]:
    keys = dict.fromkeys((m["repo"], m["target"]) for m in st["mirrors"])
    return [m for k in keys if (m := latest(st, *k)) and m["result"] == "applied"]


def tree_event(m: dict) -> dict:
    return {"type": "worktree", "repo": m["repo"], "path": m["path"], "branch": m["branch"], "branch_point": m["branch_point"], "throwaway": False, "setup": "pass"}


def _conflicts(path: Path) -> list[str]:
    return git.git(path, "diff", "--name-only", "--diff-filter=U", check=False).out.split()


def start(project, run: Run, repo: str, target: str, ticket: str | None, actor: str | None) -> dict:
    """Create the mirror worktree and cherry-pick the base fix commits; run it again after resolving a conflict by hand."""
    cfg, st = project.cfg, run.state()
    if gates.effective_kind(run, st) != "hotfix":
        raise FactoryError("mirror is for hotfix runs: factory run retype --kind hotfix")
    if not gates.passed(run, project, st, "review"):
        raise FactoryError("the review gate has to pass before the fix is mirrored: " + "; ".join(gates.reasons_for(run, project, st, "review")[:3]))
    branches = targets(cfg, repo) if any(r["name"] == repo for r in cfg["repo"]) else {}
    if target not in branches:
        raise FactoryError(f"repo {repo} has no mirror target {target!r} (targets: {', '.join(branches) or 'none'}; set [[repo.mirror]] or [[hotfix.targets]])")
    rc, base_wt = next(r for r in cfg["repo"] if r["name"] == repo), git.current(st, repo)
    main, prev = Path(rc["path"]), latest(st, repo, target)
    if prev is not None and Path(prev["path"]).is_dir():
        return _resume(run, prev, actor)
    tip = git.rev(main, branches[target]) or git.rev(main, f"{rc.get('remote', 'origin')}/{branches[target]}")
    if tip is None:
        raise FactoryError(f"target branch {branches[target]!r} does not exist in {fwd(main)}")
    key = ticket or st["ticket"]
    branch = git.branch_name("{kind}/{key}-" + git.slug(target), "hotfix", key, "")
    path = config.worktree_root(cfg) / run.id / f"{repo}-{git.slug(target)}"
    if branch == base_wt["branch"] or git.rev(main, f"refs/heads/{branch}") or path.exists():
        raise FactoryError(f"the mirror branch {branch!r} or {fwd(path)} already exists: base and mirror must not collide [L-93]")
    git.git(main, "worktree", "add", "-b", branch, str(path), tip)
    commits = git.git(Path(base_wt["path"]), "rev-list", "--reverse", "--no-merges", f"{base_wt['branch_point']}..HEAD").out.split()
    ev = {"type": "mirror", "repo": repo, "target": target, "branch": branch, "path": fwd(path), "branch_point": tip, "target_branch": branches[target],
          "ticket": key, "commits": commits, "setup": git.run_setup(cfg, rc, path)["setup"]}
    res = git.git(path, "cherry-pick", *commits, check=False)
    files = _conflicts(path) if res.exit else []
    if res.exit and not files:
        raise FactoryError(f"cherry-pick failed in {fwd(path)}: {(res.err or res.out).strip()[-300:]}")
    run.append({**ev, "result": "conflict" if files else "applied"}, actor)
    if files:
        human.block(run, "tooling", f"cherry-pick onto {target} conflicts in {', '.join(files)} ({fwd(path)}): resolve them, git cherry-pick --continue, "
                    f"then factory mirror {repo} --to {target} again and unblock this", actor)
    return run.state()["mirrors"][-1]


def _resume(run: Run, prev: dict, actor: str | None) -> dict:
    path = Path(prev["path"])
    if prev["result"] == "applied":
        raise FactoryError(f"{prev['repo']} is already mirrored to {prev['target']} ({prev['branch']})")
    if _conflicts(path) or git.git(path, "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD", check=False).exit == 0 or git.dirty(path):
        raise FactoryError(f"the cherry-pick in {fwd(path)} is not finished: resolve the conflicts and git cherry-pick --continue")
    run.append({**{k: v for k, v in prev.items() if k not in ("v", "ts", "cli", "at")}, "result": "applied"}, actor)
    return run.state()["mirrors"][-1]


def verify(project, run: Run, opts) -> dict:
    """Run every `mirror: true` check from each mirror tree, one batch per target so a failure in one never cancels the other [L-41, L-99]."""
    from . import verify as vf
    todo = applied(run.state())
    if not todo:
        raise FactoryError("no mirror worktree: run factory mirror <repo> --to <target> first")
    out = {"mode": "verify", "results": [], "stopped": None, "ok": True, "exit": 0}
    for m in todo:
        with git.overlay({m["repo"]: tree_event(m)}):
            s = vf.verify(project, run, replace(opts, mirror=m["target"], repo=opts.repo or m["repo"]))
        out["results"] += [{**r, "check": f"{r['check']}@{m['target']}"} for r in s["results"]]
        out.update(ok=out["ok"] and s["ok"], exit=max(out["exit"], s["exit"]), stopped=out["stopped"] or s["stopped"])
    return out


def compare(st: dict, m: dict) -> dict:
    w = git.current(st, m["repo"])
    return mirrordiff.compare(Path(w["path"]), w["branch_point"], Path(m["path"]), m["branch_point"])


def _heads(st: dict, m: dict) -> dict:
    return {"base": git.head(Path(git.current(st, m["repo"])["path"])), "mirror": git.head(Path(m["path"]))}


def accept(run: Run, repo: str, target: str, decision: str, actor: str | None) -> dict:
    """Record the user's decision that names every divergent file; it covers these commits only [L-95]."""
    st = run.state()
    m = latest(st, repo, target, ("applied",))
    if m is None:
        raise FactoryError(f"{repo} is not mirrored to {target}")
    d = st["decisions"].get(decision)
    if d is None or d["consumed"]:
        raise FactoryError(f"decision {decision} is not recorded or was already consumed: ask the user first [L-126]")
    files = sorted(compare(st, m)["divergent"])
    if not files:
        raise FactoryError("the mirror diff is equal: nothing to accept")
    miss = [f for f in files if f not in (d["user_said"] or "")]
    if miss:
        raise FactoryError(f"the decision must name every divergent file; it does not name: {', '.join(miss)}")
    return run.append({"type": "mirror", "repo": repo, "target": target, "result": "accepted", "decision": decision, "files": files, "heads": _heads(st, m)}, actor)


def next_step(project, run: Run, st: dict, flag: str) -> tuple[str, str] | None:
    """What a hotfix does between the review gate and the ship plan: mirror each target, verify the mirrors, make the diff equal."""
    if gates.effective_kind(run, st) != "hotfix":
        return None
    plan = planmod.load_plan(run)[0]
    pairs = [(repo, t, latest(st, repo, t)) for repo in plan["repos"] for t in targets(project.cfg, repo)]
    for repo, t, m in pairs:
        if m is None:
            return f"mirror the reviewed fix onto target {t} (a worktree {{kind}}/{{key}}-{t}; a conflict becomes a tooling blocker)", f"factory mirror {repo} --to {t}{flag}"
    for repo, t, m in pairs:
        if m["result"] == "applied" and not evidence_ok(project, plan, st, repo, m):
            return "run the mirror: true checks from the mirror worktrees (runtime checks are served from there)", f"factory verify --mirror{flag}"
    for repo, t, m in pairs:
        if m["result"] == "applied" and not compare(st, m)["equal"] and not any(a["target"] == t and a["result"] == "accepted" for a in st["mirrors"]):
            return (f"the mirror diff of {t} differs from the base fix: make it equal, or ask the user and record a decision that names the files",
                    f"factory mirror diff {repo} --to {t}{flag}")
    return None


def evidence_ok(project, plan: dict, st: dict, repo: str, m: dict) -> bool:
    head, lk = git.head(Path(m["path"])), st["locks"][-1]
    return all(evidence.satisfied(st, c["id"], "verify", lk["spec_hashes"][c["id"]], head) for c in planmod.effective_checks(plan, project.cfg) if c.get("mirror") and c["repo"] == repo)


def reasons(run: Run, project, st: dict) -> list[str]:
    """Why `gate ship` fails on a hotfix: a target not mirrored, a conflict, mirror checks without a pass at the mirror head, a different
    diff, or a ship plan that lacks the mirror changes [L-41, L-95, L-100]."""
    if gates.effective_kind(run, st) != "hotfix":
        return []
    from . import ship
    plan, out, sp, lk = planmod.load_plan(run)[0], [], ship.load(run) or {"intents": []}, st["locks"][-1]
    for repo in plan["repos"]:
        for t in targets(project.cfg, repo):
            m = latest(st, repo, t)
            if m is None or m["result"] != "applied":
                out.append(f"{repo}: hotfix target {t} is " + (f"not mirrored: factory mirror {repo} --to {t}" if m is None else "mirrored with a conflict: resolve it in the mirror worktree"))
                continue
            path = Path(m["path"])
            head = git.head(path)
            if git.dirty(path):
                out.append(f"{repo}@{t}: the mirror worktree has uncommitted changes")
            out += [f"{c['id']}@{t}: no passing verify --mirror record at {head[:7]} [L-41]" for c in planmod.effective_checks(plan, project.cfg)
                    if c.get("mirror") and c["repo"] == repo and not evidence.satisfied(st, c["id"], "verify", lk["spec_hashes"][c["id"]], head)]
            if not any(i["intent"] == "mirror_change_request" and i["repo"] == repo and i["target"] == t and i["sha"] == head for i in sp["intents"]):
                out.append(f"{repo}@{t}: the ship plan has no change for this mirror head: run factory ship plan")
            diff, heads = compare(st, m), _heads(st, m)
            ok = next((a for a in reversed(st["mirrors"]) if a["repo"] == repo and a["target"] == t and a["result"] == "accepted"), None)
            if not diff["equal"] and not (ok and ok["heads"] == heads and ok["files"] == sorted(diff["divergent"])):
                out.append(f"{repo}@{t}: the mirror diff differs from the base fix in " + ", ".join(f"{f} ({c})" for f, c in diff["divergent"].items())
                           + ": make it equal, or record the user's decision naming these files and run factory mirror diff --decision <id> [L-95]")
    return out
