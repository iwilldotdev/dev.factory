"""Ship: neutral intents that the harness executes with the user's tools, and the comparison of the saved readbacks [L-19 to L-21, L-108, L-145, L-151 to L-155]."""
from __future__ import annotations

import json
from pathlib import Path

from . import acquire, git, lock, plan as planmod, review, shipbody
from .run import Run
from .util import FactoryError, fwd, read_json, sha256_file, write_atomic

AUTO = {"push", "change_request", "set_draft", "readback", "pipeline"}  # what `draft-auto` may run without asking; ticket writes always ask
TICKET = ("none", "comment", "transition", "both")


def path_of(run: Run, name: str) -> Path:
    return run.dir / "ship" / name


def load(run: Run) -> dict | None:
    p = path_of(run, "plan.json")
    return read_json(p) if p.is_file() else None


def behind(path: Path, base: str, remote: str = "origin") -> dict:
    """Commits of the target that HEAD lacks, from local refs only (the CLI never fetches), with a note when a fetch is needed [L-108]."""
    for ref in (f"{remote}/{base}", base):
        if git.rev(path, ref):
            n = int(git.git(path, "rev-list", "--count", f"HEAD..{ref}").out.strip() or 0)
            note = (f"compared with {ref} as last fetched; run git fetch first if the remote may have moved" if ref != base
                    else f"no {remote}/{base} here, compared with the local {base}: run git fetch first")
            return {"count": n, "ref": ref, "note": note}
    return {"count": None, "ref": None, "note": f"base branch {base} not found: run git fetch first"}


def changes(run: Run, st: dict, repo: str, after: int = -1) -> list[tuple[dict, dict]]:
    """(contract event, envelope) of the `change` contracts of a repo recorded after event index `after`, oldest first."""
    out = []
    for (kind, ident), ev in st["contracts"].items():
        if kind == "change" and ev["at"] > after:
            env = acquire.load(run, "change", ident)[0]
            if env and env["data"]["repo"] == repo:
                out.append((ev, env))
    return sorted(out, key=lambda x: x[0]["at"])


def _existing(run: Run, st: dict, repo: str) -> dict | None:
    open_ = [env["data"] for _, env in changes(run, st, repo) if env["data"]["state"] == "open"]
    return open_[-1] if open_ else None


def plan(run: Run, project, ticket_mode: str | None, update: bool, allow_behind: bool, decision: str | None, pipeline: bool,
         actor: str | None) -> dict:
    """Write ship/plan.json and ship/body.md for the current heads; every intent is for the harness to run [L-145]."""
    from . import gates
    cfg, st = project.cfg, run.state()
    mode = ticket_mode or cfg["ship"]["ticket"]
    if mode not in TICKET:
        raise FactoryError(f"invalid --ticket {mode!r}; valid values: {', '.join(TICKET)}")
    if mode in ("transition", "both") and not cfg["ship"].get("transition_to"):
        raise FactoryError("ship.transition_to is not set in factory.toml, so there is no transition to plan")
    why = gates.reasons_for(run, project, st, "review")
    if why:
        raise FactoryError("ship plan needs the review gate to hold:\n" + "\n".join(f"  - {r}" for r in why))
    pl, lk = planmod.load_plan(run)[0], lock.current(st)
    ticket = acquire.load(run, "ticket", st["ticket"])[0]["data"]
    heads, by = review.heads_of(st, pl["repos"]), {r["name"]: r for r in cfg["repo"]}
    bh = {r: behind(Path(git.current(st, r)["path"]), by[r]["base"], by[r]["remote"]) for r in pl["repos"]}
    late = [f"{r} is {b['count']} commit(s) behind {b['ref']}" for r, b in bh.items() if b["count"]]
    if late and not allow_behind:
        raise FactoryError(f"{'; '.join(late)}: merge or rebase the base first, or ask the user and pass --allow-behind --decision <Q-n> [L-108]")
    if late:
        if decision is None:
            raise FactoryError("--allow-behind needs --decision <Q-n|D-n> with the user's recorded answer [L-126]")
        lock.check_decision(st, decision)
    prev = {r: _existing(run, st, r) for r in pl["repos"]}
    for r, p in prev.items():
        if update and p is None:
            raise FactoryError(f"{r}: no open change was read back yet, so there is nothing to update: plan without --update")
        if p and not update:
            raise FactoryError(f"{r}: change {p['id']} already exists: plan its new body with factory ship plan --update")
    rs = shipbody.rows(st, planmod.effective_checks(pl, cfg), lk, heads)
    text, ttl, draft = shipbody.body(st, ticket, pl, lk, rs, heads, bh), shipbody.title(cfg, ticket), cfg["ship"]["draft"]
    intents: list[dict] = []

    def add(name: str, **kw) -> None:
        auto = cfg["autonomy"]["ship"] == "draft-auto" and name in AUTO and kw.get("draft", True)
        intents.append({"id": f"I-{len(intents) + 1}", "intent": name, **kw, "needs_user": not auto})

    for r in pl["repos"]:
        w, p = git.current(st, r), prev[r]
        if p is None or p["head_sha"] != heads[r]:
            add("push", repo=r, remote=by[r]["remote"], branch=w["branch"], sha=heads[r])
        add("change_request", repo=r, action="update" if p else "create", change_id=p["id"] if p else None, source_branch=w["branch"],
            target_branch=by[r]["base"], title=ttl, draft=draft, body_file=fwd(path_of(run, "body.md")),
            reviewers=[x["scm"] for x in cfg["ship"].get("reviewers", [])])
        if p and p["draft"] != draft:
            add("set_draft", repo=r, change_id=p["id"], draft=draft)
        raw, data = path_of(run, f"readback/{r}.raw.json"), path_of(run, f"readback/{r}.json")
        add("readback", repo=r, contract="change", raw=fwd(raw), contract_file=fwd(data),
            ingest=f'factory ingest change --raw "{fwd(raw)}" --contract "{fwd(data)}"', adapter=bool(cfg["providers"].get("change", {}).get("fetch")))
        if pipeline or cfg["ship"]["require_pipeline"]:
            praw, pdata = fwd(path_of(run, f"readback/{r}-pipeline.raw.json")), fwd(path_of(run, f"readback/{r}-pipeline.json"))
            add("pipeline", repo=r, sha=heads[r], contract="pipeline", optional=not cfg["ship"]["require_pipeline"],
                ingest=f'factory ingest pipeline --raw "{praw}" --contract "{pdata}"')
    from . import mirror  # a hotfix: one more change per mirror, titled with its own target ticket key [L-100]
    for m in mirror.applied(st):
        sha = git.head(Path(m["path"]))
        add("push", repo=m["repo"], remote=by[m["repo"]]["remote"], branch=m["branch"], sha=sha, target=m["target"])
        add("mirror_change_request", repo=m["repo"], target=m["target"], sha=sha, source_branch=m["branch"], target_branch=m["target_branch"],
            title=shipbody.title(cfg, {**ticket, "id": m["ticket"]}), draft=draft, body_file=fwd(path_of(run, "body.md")))
    if mode in ("comment", "both"):
        add("issue_comment", ticket=ticket["id"], body_md=f"A draft change for {ticket['id']} is ready for review: {ttl}\n\nChange: <the change url from the readback>")
    if mode in ("transition", "both"):
        add("issue_transition", ticket=ticket["id"], to=cfg["ship"]["transition_to"])
    sp = {"schema": 1, "run": run.id, "ticket": ticket["id"], "mode": cfg["autonomy"]["ship"], "title": ttl, "draft": draft, "heads": heads,
          "plan_hash": lk["plan_hash"], "core_hash": lk["core_hash"], "update": update, "behind": bh, "body_file": fwd(path_of(run, "body.md")),
          "rows": [{k: r[k] for k in ("ac", "check", "result", "ref")} for r in rs], "intents": intents}
    write_atomic(path_of(run, "body.md"), text)
    write_atomic(path_of(run, "plan.json"), json.dumps(sp, indent=2) + "\n")
    run.append({"type": "ship_planned", "plan_sha256": sha256_file(path_of(run, "plan.json")), "body_sha256": sha256_file(path_of(run, "body.md")),
                "heads": heads, "update": update, "decision": decision if late else None}, actor)
    return sp


def proof_reasons(run: Run, st: dict, kind: str, ident: str, cev: dict) -> list[str]:
    """A readback or pipeline used as proof needs an adapter, or the user's confirmation recorded after it was ingested [L-21]."""
    env = read_json(run.dir / cev["path"])
    if env["source"] == "adapter" or any(c.get("contract") == f"{kind}:{ident}" and c["at"] > cev["at"] for c in st["confirms"]):
        return []
    return [f"{kind} {ident} was written by the agent: it is proof only through an adapter ([providers.{kind}] fetch) or the user's "
            f"confirmation: factory confirm {kind}:{ident} --user-said \"<their words>\" [L-21]"]


def _compare(sp: dict, it: dict, d: dict, head: str) -> list[str]:
    r, out = it["repo"], []
    for key, want in (("title", it["title"]), ("draft", it["draft"]), ("source_branch", it["source_branch"]),
                      ("target_branch", it["target_branch"]), ("head_sha", head)):
        if d[key] != want:
            out.append(f"{r}: the change {key.replace('_', ' ')} is {d[key]!r}, the plan says {want!r}"
                       + (" (push the branch, then read the change back again)" if key == "head_sha" else " [L-153, L-154]" if key == "draft" else ""))
    if d["state"] != "open":
        out.append(f"{r}: the change is {d['state']}, not open")
    out += [f"{r}: reviewer {x} is not on the change [L-143]" for x in it["reviewers"] if x not in d.get("reviewers", [])]
    lines = d["description_md"].splitlines()
    miss = [f"{x['ac']} x {x['check']} {x['result']}" for x in sp["rows"] if not any(x["ac"] in ln and x["check"] in ln and x["result"] in ln for ln in lines)]
    if miss:
        out.append(f"{r}: the change description lacks the AC x check x status table rows: {', '.join(miss[:4])}"
                   + (f" and {len(miss) - 4} more" if len(miss) > 4 else "") + " [L-153]")
    if shipbody.LOCAL_LINK.search(d["description_md"]):
        out.append(f"{r}: the change description links to a local path that the SCM cannot open [L-144]")
    return out


def pipeline_reasons(run: Run, st: dict, repo: str, head: str, listed: str | None) -> list[str]:
    """`require_pipeline`: the newest pipeline for the pushed head is success, or the user waived that very pipeline [L-19, L-20, L-155]."""
    made = []
    for (kind, ident), ev in st["contracts"].items():
        if kind == "pipeline":
            made.append((ev, acquire.load(run, "pipeline", ident)[0]))
    ours = sorted((m for m in made if m[1]["data"]["sha"] == head), key=lambda m: m[1]["data"]["created_at"])
    pick = next((m for m in made if m[1]["data"]["id"] == listed), None) if listed else (ours[-1] if ours else None)
    if listed and pick is None:
        return [f"{repo}: pipeline {listed} is the newest on the change but has no contract: fetch or ingest it [L-155]"]
    if pick is None:
        return [f"{repo}: no pipeline contract for the pushed head {head[:7]}: read the newest pipeline back (the project requires a green pipeline)"
                + (f"; {len(made)} pipeline(s) exist for other commits" if made else "")]
    ev, env = pick
    d = env["data"]
    if (probs := acquire.load(run, "pipeline", d["id"])[1]):
        return probs
    if d["sha"] != head:
        return [f"{repo}: the newest pipeline {d['id']} ran for {d['sha'][:7]}, not the pushed head {head[:7]} [L-155]"]
    out = proof_reasons(run, st, "pipeline", d["id"], ev)
    if d["status"] != "success" and not any(w["pipeline"] == d["id"] for w in st["waivers"]):
        out.append(f"{repo}: pipeline {d['id']} is {d['status']}: it has to be success, or the user waives that pipeline: "
                   f"factory waive pipeline:{d['id']} --user-said \"<their words>\" [L-20]")
    return out


def fresh_reasons(run: Run, st: dict, pl: dict, lk: dict) -> list[str]:
    sp = load(run)
    if not st["ships"] or sp is None:
        return ["no ship plan: run factory ship plan"]
    ev, heads, out = st["ships"][-1], review.heads_of(st, pl["repos"]), []
    if sp["heads"] != heads or ev["heads"] != heads:
        out.append("the ship plan is for other commits (HEAD moved): run factory ship plan --update (or without --update before the first change)")
    if sp["plan_hash"] != lk["plan_hash"]:
        out.append("the plan changed after the ship plan: run factory ship plan again")
    if sha256_file(path_of(run, "plan.json")) != ev["plan_sha256"] or not path_of(run, "body.md").is_file() \
            or sha256_file(path_of(run, "body.md")) != ev["body_sha256"]:
        out.append("ship/plan.json or ship/body.md was edited after it was planned: run factory ship plan again")
    return out


def reasons(run: Run, project, st: dict) -> list[str]:
    """Why `gate ship` fails once the review conditions hold: no plan, a stale plan, or a readback that differs from it [L-153]."""
    pl, lk = planmod.load_plan(run)[0], lock.current(st)
    out = fresh_reasons(run, st, pl, lk)
    if out:
        return out
    sp, plan_at = load(run), st["ships"][-1]["at"]
    for it in (i for i in sp["intents"] if i["intent"] == "change_request"):
        r, found = it["repo"], changes(run, st, it["repo"], plan_at)
        if not found:
            out.append(f"{r}: no change readback after the ship plan: execute the intents, save the change as JSON, ingest it with "
                       "factory ingest change (or configure providers.change.fetch) and run factory ship verify [L-153]")
            continue
        cev, env = found[-1]
        if cev.get("refresh") and cev["at"] > st["gates"].get("ship", {}).get("at", -1):  # read back again for feedback after the gate passed: what feedback judges, not what the plan promised
            continue
        d = env["data"]
        probs = acquire.load(run, "change", d["id"])[1]
        out += probs or proof_reasons(run, st, "change", d["id"], cev) + _compare(sp, it, d, sp["heads"][r])
        if project.cfg["ship"]["require_pipeline"] and not probs:
            out += pipeline_reasons(run, st, r, sp["heads"][r], (d.get("pipelines") or [None])[0])
    return out


def stage(run: Run, project, st: dict) -> tuple[str, list[str]]:
    """Where the ship phase stands: plan, execute (no readback yet), fix (a readback differs), verify or gate."""
    pl, lk = planmod.load_plan(run)[0], lock.current(st)
    fresh = fresh_reasons(run, st, pl, lk)
    if fresh:
        return "plan", fresh
    why = reasons(run, project, st)
    if any("no change readback" in w for w in why):
        return "execute", why
    if why:
        return "fix", why
    last = st["shipped"][-1] if st["shipped"] and st["shipped"][-1]["at"] > st["ships"][-1]["at"] else None
    return ("gate" if last and last["result"] == "pass" else "verify"), why


def verify(run: Run, project, actor: str | None) -> dict:
    """Read the changes back (adapter, when configured, otherwise what the agent ingested) and record one `ship_verified` event [L-151]."""
    cfg, st = project.cfg, run.state()
    sp = load(run)
    if sp is None or not st["ships"]:
        raise FactoryError("no ship plan: run factory ship plan first")
    errs: list[str] = []
    for it in (i for i in sp["intents"] if i["intent"] == "change_request"):
        r = it["repo"]
        extra = {"branch": it["source_branch"], "sha": sp["heads"][r]}
        known = (changes(run, st, r) or [(None, {"data": {"id": None}})])[-1][1]["data"]
        steps = [("change", it["change_id"] or known["id"] or it["source_branch"])]
        for kind, ident in steps + [("pipeline", None)]:
            if not cfg["providers"].get(kind, {}).get("fetch") or (kind == "pipeline" and not cfg["ship"]["require_pipeline"]):
                continue
            if kind == "pipeline":
                found = changes(run, run.state(), r, st["ships"][-1]["at"])
                ident = (found[-1][1]["data"].get("pipelines") or [None])[0] if found else None
                if ident is None:
                    continue
            try:
                acquire.fetch(run, cfg, project.dir, kind, ident, r, extra)
            except FactoryError as exc:
                errs.append(f"{r}: reading the {kind} back through the adapter failed: {exc}")
    st = run.state()
    mism = errs + reasons(run, project, st)
    state = {"result": "fail" if mism else "pass", "heads": sp["heads"], "mismatches": mism, "plan_sha256": st["ships"][-1]["plan_sha256"]}
    ev = run.append(lambda evs: None if (last := next((e for e in reversed(evs) if e.get("type") == "ship_verified"), None))
                    and all(last.get(k) == v for k, v in state.items()) else {"type": "ship_verified", **state}, actor)
    return {**state, "recorded": ev is not None}
