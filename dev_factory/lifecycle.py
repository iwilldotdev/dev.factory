"""Run lifecycle: start, retype, repos, close, delete, status, list, log."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import acquire, git, gates, lock, next as nxt
from .project import Project
from .run import KINDS, Run, derive
from .util import FactoryError, fwd, safe_id


def resolve(project: Project, name: str | None, allow_closed: bool = False) -> Run:
    """The named run, or the only open run."""
    if name:
        run = project.run(name)
        if not run.exists():
            have = ", ".join(project.run_ids()) or "none"
            raise FactoryError(f"no run {name!r} (runs: {have})")
        return run
    ids = project.run_ids()
    open_ids = ids if allow_closed else [i for i in ids if not project.run(i).state()["closed"]]
    if len(open_ids) == 1:
        return project.run(open_ids[0])
    if not open_ids:
        raise FactoryError("no open run; start one with factory run start <ticket-id>")
    raise FactoryError(f"{len(open_ids)} open runs ({', '.join(open_ids)}); pass the run id")


def writable(project: Project, name: str | None) -> Run:
    """A run that can take new events: closed runs refuse them."""
    run = resolve(project, name)
    closed = run.state()["closed"]
    if closed:
        raise FactoryError(f"run {run.id} is closed ({closed.get('reason', '')}); start a new run or delete this one")
    return run


def start(project: Project, ticket: str, kind: str | None, repos: list[str] | None, actor: str | None, parent: str | None = None) -> Run:
    if kind is not None and kind not in KINDS:
        raise FactoryError(f"invalid kind {kind!r}; valid kinds: {', '.join(KINDS)}")
    rid = safe_id(ticket)
    run = project.run(rid)
    if run.exists():
        prev = run.state()["ticket"]
        if prev != ticket:
            raise FactoryError(f"ticket ids {prev!r} and {ticket!r} map to the same run folder {rid!r} [L-157]")
        raise FactoryError(f"run {rid} already exists; use factory status {rid}, or delete it first")
    names = [r["name"] for r in project.cfg["repo"]]
    repos = repos if repos is not None else (names if len(names) == 1 else [])
    unknown = [r for r in repos if r not in names]
    if unknown:
        raise FactoryError(f"unknown repo {', '.join(unknown)} (repos: {', '.join(names)})")
    run.append({"type": "run_started", "ticket": ticket, "kind": kind, "repos": repos, **({"parent": parent} if parent else {})}, actor)
    return run


def claim(run: Run, session: str, takeover: bool, actor: str | None) -> dict:
    """Record the session that works on a run; another session needs `takeover` [L-184]."""
    def build(evs):
        owner = derive(evs)["owner"]
        if owner and owner != session and not takeover:
            raise FactoryError(f"run {run.id} is claimed by session {owner!r}: use another run, or take it over with --takeover [L-184]")
        return None if owner == session else {"type": "claim", "session": session, "takeover": bool(owner)}
    return run.append(build, actor)


def guard(run: Run, session: str | None, takeover: bool, actor: str | None) -> None:
    """`next` from a session other than the owner refuses unless it takes the run over [L-184]."""
    if (owner := run.state()["owner"]) and owner != (session or ""):
        if not (takeover and session):
            raise FactoryError(f"run {run.id} is claimed by session {owner!r}; pass --session <yours> --takeover to take it over [L-184]")
        claim(run, session, True, actor)


def retype(run: Run, kind: str, actor: str | None) -> dict:
    """Change the run kind: nothing is archived, and the intake gate has to pass again [L-32]."""
    if kind not in KINDS:
        raise FactoryError(f"invalid kind {kind!r}; valid kinds: {', '.join(KINDS)}")
    return run.append(lambda evs: {"type": "retype", "kind": kind, "previous": derive(evs)["kind"]}, actor)


def set_repos(project: Project, run: Run, repos: list[str], actor: str | None) -> dict:
    names = [r["name"] for r in project.cfg["repo"]]
    unknown = [r for r in repos if r not in names]
    if unknown or not repos:
        raise FactoryError(f"unknown repo {', '.join(unknown) or '(none given)'} (repos: {', '.join(names)})")
    return run.append({"type": "repos", "repos": repos}, actor)


def close(run: Run, reason: str, actor: str | None) -> dict:
    """End a run without gates [L-103]."""
    if not reason.strip():
        raise FactoryError("run close needs --reason")
    ev = run.append(lambda evs: None if derive(evs)["closed"] else {"type": "closed", "reason": reason.strip()}, actor)
    if ev is None:
        raise FactoryError(f"run {run.id} is already closed")
    return ev


def _force(func, path, _exc):
    os.chmod(path, 0o700)
    func(path)


def delete(project: Project, run: Run, confirmed: bool, decision: str | None = None) -> list[str]:
    """Remove one run directory (no archive, no parent/child protocol) [L-105], its clean worktrees and its branches with no
    commit of their own; return what was removed and what was kept."""
    if not confirmed:
        raise FactoryError("run delete removes the whole run directory; pass --yes")
    st = run.state()
    spent = lock.loops_used(st) + sum(1 for b in st["blockers"].values() if b["open"])
    if spent and decision is None:  # deleting would wipe the loop budget and the blockers: only the user may decide that
        raise FactoryError(f"run {run.id} has spent {lock.loops_used(st)} loop(s) or has open blockers; deleting it resets them. "
                           "Pass --decision <D-n> with the user's recorded decision to delete it anyway")
    lock.check_decision(st, decision)
    repos = {r["name"]: r for r in project.cfg["repo"]}
    trees = list({w["path"]: w for w in st["worktrees"] + st["mirrors"] if not w.get("throwaway") and w.get("path")}.values())
    out = []
    for w in trees:
        repo = repos.get(w["repo"])
        out += git.retire(Path(repo["path"]), w, repo["base"]) if repo else [f"kept worktree {w['path']}: repo {w['repo']} is not configured"]
    shutil.rmtree(run.dir, **{"onexc" if sys.version_info >= (3, 12) else "onerror": _force})
    return out


def branch(project: Project, run: Run, names: list[str] | None, actor: str | None, from_run: str | None = None) -> list[dict]:
    """Create the run worktree of each repo (default: the declared repos); a repo that already has a good one is left alone."""
    st = run.state()
    if not gates.intake_passed(run, project.cfg, st):
        raise FactoryError("gate intake has not passed; run it before creating worktrees")
    by_name = {r["name"]: r for r in project.cfg["repo"]}
    names = names or st["repos"]
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise FactoryError(f"unknown repo {', '.join(unknown)} (repos: {', '.join(by_name)})")
    kind = gates.effective_kind(run, st) or "feature"
    title = acquire.load(run, "ticket", st["ticket"])[0]["data"]["title"] if kind != "review" else ""
    out = []
    for n in names:
        have = git.current(st, n)
        if have and Path(have["path"]).is_dir() and have.get("setup") != "fail":
            continue
        if kind == "review":
            from . import review_run
            out.append(review_run.add_worktree(project, run, by_name[n], actor))
            continue
        from . import epic  # a stacked branch starts at its dependency's head [L-91, L-92]
        out.append(git.add_worktree(project, run, by_name[n], kind, title, actor, epic.base_ref(project, run, st, n, from_run)))
    return out


def status(project: Project, run: Run) -> dict:
    st = run.state()
    ph = nxt.phase(run, project, st)
    missing = [w["path"] for r in st["repos"] if (w := git.current(st, r)) and not Path(w["path"]).exists()]  # throwaway trees are gone by design
    out = {"run": run.id, "ticket": st["ticket"], "kind": gates.effective_kind(run, st), "phase": ph, "repos": st["repos"],
           "events": st["count"], "started": st["first_ts"], "updated": st["last_ts"],
           "open_questions": [q["id"] for q in st["questions"].values() if q["answered"] is None],
           "open_blockers": [b["id"] for b in st["blockers"].values() if b["open"]],
           "unconsumed_decisions": [d["id"] for d in st["decisions"].values() if not d["consumed"]],
           "gates": {n: g["result"] for n, g in st["gates"].items()}, "missing_worktrees": missing,
           "plan_lock": len(st["locks"]) or None, "loops": f"{lock.loops_used(st)}/{project.cfg['autonomy']['max_loops']}",
           "closed": st["closed"]["reason"] if st["closed"] else None}
    out["problems"] = [f"worktree is missing: {fwd(p)} (close the run with factory run close --reason, or recreate it)" for p in missing]
    return out


def listing(project: Project) -> list[dict]:
    rows = []
    for rid in project.run_ids():
        run = project.run(rid)
        st = run.state()
        rows.append({"run": rid, "ticket": st["ticket"], "kind": gates.effective_kind(run, st),
                     "phase": nxt.phase(run, project, st), "state": "closed" if st["closed"] else "open",
                     "updated": st["last_ts"]})
    return rows


def describe(ev: dict) -> str:
    t = ev.get("type")
    keys = {"run_started": ("ticket", "kind", "repos"), "retype": ("kind", "previous"), "repos": ("repos",),
            "contract": ("kind", "id", "source"), "media": ("attachment", "state", "file", "question"),
            "question": ("id", "text", "ref"), "answer": ("question", "user_said"), "decision": ("id", "user_said"),
            "human": ("kind", "text"), "blocker": ("id", "kind", "reason"), "unblock": ("blocker", "decision"),
            "gate": ("name", "result", "reasons"), "worktree": ("repo", "path", "branch", "setup"),
            "plan_locked": ("seq",), "loop": ("kind", "decision", "counts"),
            "evidence": ("check", "mode", "result", "sha"), "env": ("repo", "action", "kind", "reason"),
            "design_review": ("frame",), "design_refresh": ("frame", "changed"), "closed": ("reason",),
            "baseline": ("check", "runs"), "confirm": ("check", "contract"), "tokens": ("session", "input", "output"),
            "review": ("kind", "round", "verdict")}.get(t, ())
    return " ".join(f"{k}={ev[k]!r}" if isinstance(ev.get(k), str) and " " in ev[k] else f"{k}={ev[k]}"
                    for k in keys if ev.get(k) not in (None, [], ""))


def log_lines(run: Run, tail: int | None) -> list[str]:
    lines = [f"{e.get('ts', '?')} {e.get('type', '?'):<12} {describe(e)}".rstrip() for e in run.read()]
    return lines[-tail:] if tail else lines
