"""Review runs: a run that starts from an existing change. Phases: intake (the change contract), plan, verify (the checks at the change head,
in a detached worktree), review, report. `review report` writes a verdict and neutral intents that are never run by the CLI or by `next`
[L-185]; `review fix` turns the run into a ticket run on the change branch and keeps its ACs and evidence."""
from __future__ import annotations

import json
from pathlib import Path

from . import acquire, config, contracts, gates, git, lifecycle, proc, review as rv
from .run import Run
from .util import FactoryError, fwd, read_json, safe_id, write_atomic

VERDICTS = ("ok", "not-ok", "incomplete")


def queue(project, file: str | None) -> list[dict]:
    """The `change_list` contract with drafts, the user's own changes and changes the user approved already dropped."""
    prov = project.cfg["providers"].get("change_list", {})
    if file:
        payload = {"contract": read_json(Path(file))}
    elif prov.get("fetch"):
        argv = proc.substitute(proc.split_template(prov["fetch"]), {"id": "queue", "project_dir": fwd(project.dir), "run_dir": fwd(project.factory_dir)})
        res = proc.run(argv, cwd=project.dir, env=proc.child_env({"FACTORY_KIND": "change_list", "FACTORY_ID": "queue"}), timeout_s=prov.get("timeout_s", 60))
        if res.exit != 0:
            raise FactoryError(f"the change_list adapter failed (exit {res.exit}): {(res.err or res.out).strip()[-300:]}", 1)
        try:
            payload = json.loads(res.out)
        except ValueError as exc:
            raise FactoryError(f"adapter output is not one JSON object: {exc}") from exc
    else:
        raise FactoryError("no change list: set [providers.change_list] fetch, or pass --file <change_list data as JSON>")
    data = contracts.validate("change_list", payload.get("contract") if isinstance(payload, dict) else None)
    return [i for i in data["items"] if not (i["draft"] or i.get("own") or i.get("approved"))]


def start(project, ids: list[str], repo: str | None, ticket: str | None, actor: str | None) -> list[Run]:
    """One review run per change (`review-<id>` or the linked ticket); several share a parent whose children depend on nothing."""
    from . import epic
    names = [r["name"] for r in project.cfg["repo"]]
    repo = repo or (names[0] if len(names) == 1 else None)
    if repo not in names:
        raise FactoryError(f"name the repo of the change(s) with --repo ({', '.join(names)})")
    if ticket and len(ids) > 1:
        raise FactoryError("--ticket names one change's ticket: start the changes one at a time")
    runs, parent = [], None
    if len(ids) > 1:
        parent = lifecycle.start(project, f"review-batch-{ids[0]}-{len(ids)}", "epic", None, actor)
    for cid in ids:
        run = lifecycle.start(project, ticket or f"review-{cid}", "review", [repo], actor, parent=parent.id if parent else None)
        if project.cfg["providers"].get("change", {}).get("fetch"):
            acquire.fetch(run, project.cfg, project.dir, "change", cid, repo)
        runs.append(run)
    if parent:
        epic.plan(project, parent, {"children": [{"ticket": r.state()["ticket"], "kind": "review", "repos": [repo]} for r in runs]}, actor, batch=True)
    return runs


def change_of(run: Run, st: dict) -> dict | None:
    """The latest `change` contract of the run."""
    ids = sorted((ev["at"], i) for (k, i), ev in st["contracts"].items() if k == "change")
    return acquire.load(run, "change", ids[-1][1])[0]["data"] if ids else None


def intake_reasons(run: Run, cfg: dict, st: dict) -> list[str]:
    """A review run needs the change contract, not a ticket: the checks and reviews are about that change."""
    out = [f"{kind} {ident}: {p}" for (kind, ident) in st["contracts"] if kind == "change" for p in acquire.load(run, kind, ident)[1]]
    if not any(k == "change" for k, _ in st["contracts"]):
        out.append("no change contract: factory fetch change <id> --repo <repo> (or ingest it); a review run starts from an existing change")
    known = {r["name"] for r in cfg["repo"]}
    return out + ([] if st["repos"] else ["no affected repo is declared: factory run repos <name>"]) + [f"affected repo {r!r} is not in factory.toml" for r in st["repos"] if r not in known]


def add_worktree(project, run: Run, repo: dict, actor: str | None) -> dict:
    """A detached worktree at the change head; the branch point is where the change left the base. Nothing is fetched here."""
    st, main = run.state(), Path(repo["path"])
    chg = change_of(run, st)
    if chg is None:
        raise FactoryError("no change contract yet: fetch or ingest it before creating the worktree")
    base = git.rev(main, repo["base"]) or git.rev(main, f"origin/{repo['base']}")
    if git.rev(main, chg["head_sha"]) is None or base is None:
        raise FactoryError(f"the change head {chg['head_sha'][:7]} is not in {fwd(main)}: git fetch the branch {chg['source_branch']!r} yourself first (the CLI never fetches)")
    path = config.worktree_root(project.cfg) / run.id / repo["name"]
    if path.exists():
        raise FactoryError(f"{fwd(path)} exists but is not a worktree of this run")
    git.git(main, "worktree", "add", "--detach", str(path), chg["head_sha"])
    bp = git.git(main, "merge-base", base, chg["head_sha"]).out.strip()
    return run.append(git._event(repo, path, None, bp, False, git.run_setup(project.cfg, repo, path)), actor)


def reviewed(st: dict, heads: dict) -> bool:
    """Both reviews exist for the current commits; their verdict is the outcome of the run, not a failure of it."""
    return all((e := rv.latest(st, k)) and e["repos"] == heads for k in rv.BOUND)


def reported(st: dict, heads: dict) -> bool:
    return bool(st["reports"] and st["reports"][-1]["heads"] == heads)


def report_reasons(run: Run, project, st: dict) -> list[str]:
    from . import lock
    heads = rv.heads_of(st, st["repos"])
    out = lock.problems(run, project.cfg, st) if st["locks"] else ["the plan is not locked: run factory gate plan"]
    out += [] if reviewed(st, heads) else ["no code and qa review at the current commits: record both"]
    return out + ([] if reported(st, heads) else ["no review report at the current commits: factory review report --file <report.json>"])


def report(run: Run, project, data, actor: str | None) -> dict:
    """Validate the report, write `report/plan.json` with neutral intents (none runs by itself) and record `review_report`."""
    st = run.state()
    heads = rv.heads_of(st, st["repos"])
    if not reviewed(st, heads):
        raise FactoryError("record the code and qa reviews of the current commits first (factory review record)")
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS or not str(data.get("summary", "")).strip():
        raise FactoryError(f"the report is an object with verdict ({', '.join(VERDICTS)}) and a summary; optional findings, comment_md, ticket_comment_md")
    approved = all(rv.latest(st, k)["verdict"] == "APPROVE" for k in rv.BOUND)
    if (data["verdict"] == "ok") != approved and data["verdict"] != "incomplete":
        raise FactoryError(f"the verdict {data['verdict']!r} contradicts the reviews ({'both APPROVE' if approved else 'a review asked for CHANGES'})")
    chg = change_of(run, st) or {}
    body = data.get("comment_md") or data["summary"]
    acts = [{"intent": "change_comment", "repo": st["repos"][0], "change_id": chg.get("id"), "body_md": body}]
    if data["verdict"] == "ok":
        acts.append({"intent": "change_approve", "repo": st["repos"][0], "change_id": chg.get("id")})
    if data.get("ticket_comment_md"):
        acts.append({"intent": "issue_comment", "ticket": st["ticket"], "body_md": data["ticket_comment_md"]})
    acts = [{"id": f"I-{n}", **a, "needs_user": True, "auto": False} for n, a in enumerate(acts, 1)]
    write_atomic(run.dir / "report" / "plan.json", json.dumps({"schema": 1, "run": run.id, "verdict": data["verdict"], "heads": heads, "remote_actions": acts}, indent=2) + "\n")
    return run.append({"type": "review_report", "verdict": data["verdict"], "summary": data["summary"], "findings": data.get("findings", []), "heads": heads,
                       "actions": [a["intent"] for a in acts]}, actor)


def fix(project, run: Run, actor: str | None) -> dict:
    """Check out the change branch in the run worktree and retype the run (feature or bug, as its plan says): the plan lock, ACs and
    evidence stay, and the intake gate has to pass again with the real ticket."""
    from . import plan as planmod
    st = run.state()
    chg, plan = change_of(run, st), planmod.load_plan(run)[0]
    if st["kind"] != "review" or chg is None or plan is None:
        raise FactoryError("review fix is for a review run that has a change contract and a plan")
    w, br = git.current(st, st["repos"][0]), chg["source_branch"]
    main = Path(next(r["path"] for r in project.cfg["repo"] if r["name"] == st["repos"][0]))
    have = git.rev(main, f"refs/heads/{br}")
    if have not in (None, w["branch_point"]) and have != git.head(Path(w["path"])):
        raise FactoryError(f"the local branch {br!r} is at {have[:7]}, not at the change head: update or remove it yourself, the CLI never moves a branch")
    git.git(Path(w["path"]), "checkout", *(("-b", br) if have is None else (br,)))
    run.append({**{k: v for k, v in w.items() if k not in ("v", "ts", "cli", "actor")}, "branch": br}, actor)
    return lifecycle.retype(run, plan["kind"], actor)
