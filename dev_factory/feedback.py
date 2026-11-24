"""The feedback phase after ship: unresolved change threads and failed pipeline jobs become findings of a `feedback` review, and the run
goes back to build, verify, review and ship (an `update` of the same changes) until the threads are resolved and the pipeline is green
or waived [L-19, L-20, L-155]. The CLI reads only contracts; the adapters or the agent refresh them."""
from __future__ import annotations

from pathlib import Path

from . import acquire, lock, ship
from .run import Run
from .util import FactoryError, canonical, fwd, sha256_bytes

BLOCKING = ("blocker", "major")


def shipped_at(st: dict) -> int:
    """Index of the passing ship gate event, or -1: contracts read after it are the feedback."""
    g = st["gates"].get("ship")
    return g["at"] if g and g["result"] == "pass" else -1


def refreshed(run: Run, st: dict, repo: str) -> tuple[dict, dict] | None:
    found = ship.changes(run, st, repo, shipped_at(st))
    return found[-1] if found else None


def _excerpt(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "..."


def newest_pipeline(run: Run, st: dict, listed: str | None, head: str) -> dict | None:
    """The pipeline the change lists first, else the newest contract for the shipped head [L-155]."""
    made = [acquire.load(run, "pipeline", ident)[0] for (kind, ident) in st["contracts"] if kind == "pipeline"]
    made = [e["data"] for e in made if e]
    if listed:
        return next((d for d in made if d["id"] == listed), None)
    ours = sorted((d for d in made if d["sha"] == head), key=lambda d: d["created_at"])
    return ours[-1] if ours else None


def counts(st: dict, cfg: dict, thread: dict) -> bool:
    """An unresolved thread counts unless the user dismissed it or its author is in `[feedback] ignore_authors` (empty by default) [L-20]."""
    return not thread["resolved"] and thread["author"] not in cfg["feedback"]["ignore_authors"] and f"thread:{thread['id']}" not in {d["source"] for d in st["dismissals"]}


def findings(run: Run, st: dict, heads: dict[str, str], cfg: dict) -> list[dict]:
    """One finding per unresolved thread (`thread:<id>`) and per failed job of the newest pipeline (`job:<pipeline>/<name>`) [L-20]."""
    out = []
    for repo, head in heads.items():
        got = refreshed(run, st, repo)
        if got is None or acquire.load(run, "change", got[1]["data"]["id"])[1]:
            continue
        d = got[1]["data"]
        for t in d.get("threads", []):
            if counts(st, cfg, t):
                loc = t.get("file", "") + (f":{t['line']}" if t.get("file") and t.get("line") else "")
                out.append({"severity": "major", "file": loc, "text": _excerpt(f"{t['author']}: {t['body_md'].strip()}", 600), "repo": repo,
                            "source": f"thread:{t['id']}"})
        pl = newest_pipeline(run, st, (d.get("pipelines") or [None])[0], head)
        if pl is None or pl["sha"] != head or pl["status"] != "failed":
            continue
        failed = [j for j in pl.get("jobs", []) if j["status"] == "failed"]
        out += [{"severity": "minor" if j["allow_failure"] else "major", "file": "", "repo": repo, "source": f"job:{pl['id']}/{j['name']}",
                 "text": f"pipeline {pl['id']}: job {j['name']} failed" + (" (allowed to fail)" if j["allow_failure"] else "")} for j in failed]
        if not failed:
            out.append({"severity": "major", "file": "", "repo": repo, "source": f"pipeline:{pl['id']}", "text": f"pipeline {pl['id']} failed (no failed job in the contract)"})
    return out


def seen(st: dict, heads: dict) -> set[str]:
    """Sources already turned into findings at these commits: the same source is not a new finding until the code moves."""
    return {f["source"] for e in st["reviews"] if e.get("kind") == "feedback" and e["repos"] == heads for f in e["findings"]}


def open_round(st: dict, heads: dict) -> dict | None:
    """The latest feedback round while its findings wait for a fix: same commits, and nothing read back since. A dismissed thread waits for nothing."""
    rounds, gone = [e for e in st["reviews"] if e.get("kind") == "feedback"], {d["source"] for d in st["dismissals"]}
    if not rounds or rounds[-1]["repos"] != heads or not any(f["severity"] in BLOCKING and f["source"] not in gone for f in rounds[-1]["findings"]):
        return None
    late = any(ev["kind"] in ("change", "pipeline") and ev["at"] > rounds[-1]["at"] for ev in st["contracts"].values())
    return None if late else rounds[-1]


def reasons(run: Run, project, st: dict) -> list[str]:
    """Why `gate feedback` fails once the ship gate holds: a change not read back, unresolved threads, a pipeline that is not green [L-19, L-20, L-155]."""
    sp, out = ship.load(run), []
    for repo, head in sp["heads"].items():
        got = refreshed(run, st, repo)
        if got is None:
            out.append(f"{repo}: the change was not read back after the ship gate: factory feedback refreshes it through the adapter, or save it and run "
                       f"factory ingest change --refresh (and the same for its pipeline)")
            continue
        ev, env = got
        d = env["data"]
        if (probs := acquire.load(run, "change", d["id"])[1]):
            out += probs
            continue
        out += ship.proof_reasons(run, st, "change", d["id"], ev)
        if d["state"] == "merged":
            continue
        if d["state"] == "closed":
            out.append(f"{repo}: change {d['id']} is closed: reopen it, or close the run with factory run close --reason")
        if d["head_sha"] != head:
            out.append(f"{repo}: the change is at {d['head_sha'][:7]}, the shipped commit is {head[:7]}: push the branch, read the change back again, or plan an update")
        out += [f"{repo}: thread:{t['id']} is unresolved ({_excerpt(t['author'] + ': ' + t['body_md'])}): fix it, reply and resolve it, then read the change back [L-20]"
                for t in d.get("threads", []) if counts(st, project.cfg, t)]
        listed = (d.get("pipelines") or [None])[0]
        if listed or project.cfg["ship"]["require_pipeline"]:
            out += ship.pipeline_reasons(run, st, repo, head, listed)
    heads = sp["heads"]
    if (fb := open_round(st, heads)):
        out.append(f"feedback round {fb['round']} has {len(fb['findings'])} finding(s) at these commits: fix them in the worktree(s), commit, run verify, "
                   "review and ship the update")
    return out


def stage(run: Run, project, st: dict) -> tuple[str, list[str]]:
    """Where the feedback phase stands: refresh (nothing read back yet), ingest (new findings to record), wait, or gate."""
    why = reasons(run, project, st)
    if not why:
        return "gate", why
    sp = ship.load(run)
    if any("was not read back" in w for w in why):
        return "refresh", why
    if [f for f in findings(run, st, sp["heads"], project.cfg) if f["source"] not in seen(st, sp["heads"])]:
        return "ingest", why
    return "wait", why


def _refresh(run: Run, project, st: dict, sp: dict) -> list[str]:
    """Read every change and its newest pipeline again through the configured adapters; returns what was refreshed."""
    cfg, done = project.cfg, []
    for repo, head in sp["heads"].items():
        prev = ship.changes(run, st, repo)
        if not cfg["providers"].get("change", {}).get("fetch") or not prev:
            continue
        d = prev[-1][1]["data"]
        env = acquire.fetch(run, cfg, project.dir, "change", d["id"], repo, {"branch": d["source_branch"], "sha": head}, {"refresh": True})
        done.append(f"change {d['id']}")
        listed = (env["data"].get("pipelines") or [None])[0]
        if listed and cfg["providers"].get("pipeline", {}).get("fetch"):
            acquire.fetch(run, cfg, project.dir, "pipeline", listed, repo, {"branch": d["source_branch"], "sha": head}, {"refresh": True})
            done.append(f"pipeline {listed}")
    return done


def ingest(run: Run, project, decision: str | None, fetch: bool, actor: str | None) -> dict:
    """`factory feedback`: record the findings that are new at these commits as one `feedback` round. A round with a blocker or
    major finding spends a loop of the single budget; past it the run blocks for the user [L-135, L-136]."""
    from . import gates
    st = run.state()
    if not gates.passed(run, project, st, "ship"):
        raise FactoryError("feedback starts after the ship gate passed: run factory next and finish the ship phase first")
    sp = ship.load(run)
    refreshed_now = _refresh(run, project, st, sp) if fetch else []
    st = run.state()
    heads = sp["heads"]
    live = findings(run, st, heads, project.cfg)
    fresh = [f for f in live if f["source"] not in seen(st, heads)]
    if not fresh:
        return {"round": None, "findings": [], "refreshed": refreshed_now, "unresolved": len(live)}
    lk = lock.current(st)
    rounds = [e for e in st["reviews"] if e.get("kind") == "feedback"]
    rn = len(rounds) + 1
    if any(f["severity"] in BLOCKING for f in fresh):
        lock.spend(run, project.cfg, st, "feedback", decision, actor, {"heads": heads, "round": rn})
    rec = {"type": "review", "kind": "feedback", "round": rn, "verdict": "CHANGES", "reviewer": "factory feedback", "repos": heads,
           "findings": [{"id": f"F{i}", **f} for i, f in enumerate(fresh, 1)], "resolved": [], "bound": {}, "lock_seq": lk["seq"], "independent_of": [],
           "completeness": {"done": 0, "gap": len(fresh), "not_checked": 0}}
    rec["hash"] = sha256_bytes(canonical(rec).encode("utf-8"))
    run.append(rec, actor)
    return {"round": rn, "findings": rec["findings"], "refreshed": refreshed_now, "unresolved": len(live)}


def dismiss(run: Run, finding: str, decision: str | None, actor: str | None) -> dict:
    """The user decided that one review thread needs no change: it stops counting. Only a recorded decision does it, and only for a thread;
    a red pipeline is waived with its own command [L-20, L-126]."""
    st = run.state()
    if decision is None:
        raise FactoryError("--dismiss needs --decision <Q-n|D-n>: the user's recorded decision that this finding needs no change")
    lock.check_decision(st, decision)
    known = [f for e in st["reviews"] if e.get("kind") == "feedback" for f in e["findings"]]
    hit = next((f for f in reversed(known) if finding in (f["id"], f["source"])), None)
    if hit is None or not hit["source"].startswith("thread:"):
        raise FactoryError(f"{finding!r} is not a thread finding of a feedback round (a failed job or pipeline is waived: factory waive pipeline:<id>)")
    return run.append({"type": "dismiss", "source": hit["source"], "finding": hit["id"], "decision": decision, "user_said": st["decisions"][decision]["user_said"]}, actor)


def render(res: dict) -> str:
    lines = [f"refreshed {', '.join(res['refreshed'])}"] if res["refreshed"] else []
    if res["round"] is None:
        return "\n".join(lines + ["no new unresolved thread or failed job at these commits" + (f" ({res['unresolved']} already recorded)" if res["unresolved"] else "")])
    lines.append(f"feedback round {res['round']}: {len(res['findings'])} finding(s); the run is back in build")
    return "\n".join(lines + [f"  {f['id']} ({f['severity']}) {f['source']}: {f['text']}" for f in res["findings"]])


def action_paths(run: Run, repo: str) -> tuple[str, str]:
    return fwd(Path(run.dir) / "feedback" / f"{repo}.raw.json"), fwd(Path(run.dir) / "feedback" / f"{repo}.json")

