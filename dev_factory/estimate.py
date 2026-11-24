"""`verify --estimate`: per check `reuse` or `run`, with the median duration of its earlier runs. Nothing is run or recorded [L-189]."""
from __future__ import annotations

import statistics

from . import evidence, git, human, review, verify


def _durations(project, run, check_id: str) -> list[float]:
    """Durations of earlier verify attempts: this run first, then the other runs of the project (standing checks repeat across runs)."""
    for r in [run] + [project.run(i) for i in project.run_ids() if i != run.id]:
        found = [e["duration_s"] for e in r.read() if e.get("type") == "evidence" and e.get("check") == check_id and e.get("mode") == "verify"
                 and e.get("duration_s") is not None and e.get("result") != "skipped"]
        if found:
            return found
    return []


def compute(project, run, opts) -> dict:
    b = verify.Batch(project, run, opts)
    heads, rows = {}, []
    for c in verify.order(b.st, b.lock, b.chosen()):
        if c["repo"] not in heads:
            path = b.tree(c["repo"])
            heads[c["repo"]] = (git.head(path), bool(git.dirty(path)))
        sha, dirty = heads[c["repo"]]
        if c["type"] == "human":
            done = human.confirmation(b.st, c, b.spec(c), sha, review.heads_of(b.st, b.plan["repos"]))
            rows.append({"check": c["id"], "type": "human", "action": "reuse" if done else "ask", "median_s": 0.0, "history": 0})
            continue
        reuse = not opts.fresh and not dirty and not (opts.scoped and c.get("scoped")) \
            and evidence.satisfied(b.st, c["id"], "verify", b.spec(c), sha, None, verify.current_heads(project, b.st, c))
        past = [] if reuse else _durations(project, run, c["id"])
        rows.append({"check": c["id"], "type": c["type"], "action": "reuse" if reuse else "run",
                     "median_s": round(statistics.median(past), 2) if past else None, "history": len(past)})
    todo = [r for r in rows if r["action"] == "run"]
    return {"checks": rows, "run": len(todo), "reuse": sum(1 for r in rows if r["action"] == "reuse"),
            "estimate_s": round(sum(r["median_s"] or 0 for r in todo), 1), "unknown": [r["check"] for r in todo if r["median_s"] is None]}


def render(s: dict) -> str:
    lines = [f"{r['check']:<14} {r['type']:<9} {r['action']:<6}" + (f" about {r['median_s']}s (median of {r['history']})" if r["action"] == "run" and r["median_s"] is not None
             else " no earlier run to judge by" if r["action"] == "run" else "") for r in s["checks"]]
    unknown = f"; {len(s['unknown'])} check(s) have no history: {', '.join(s['unknown'])}" if s["unknown"] else ""
    return "\n".join(lines + [f"estimate: {s['run']} to run (about {s['estimate_s']}s), {s['reuse']} reused{unknown}"])
