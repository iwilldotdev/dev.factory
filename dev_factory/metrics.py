"""`factory metrics`: derived from events.jsonl on demand, never stored [L-178, L-179, L-180, L-181]. Tokens come only from an
optional `[metrics] tokens` adapter, as `tokens` events keyed by session [L-182, L-183]; the core never reads a transcript."""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone

from . import proc
from .run import Run
from .util import FactoryError

NEXT = {"intake": "plan", "reproduce": "build", "verify": "review", "review": "ship", "ship": "feedback", "feedback": "done"}
IDLE_S = 1800  # a gap longer than this with no open blocker is idle time, not work
SESSION = re.compile(r"^[A-Za-z0-9._-]{1,80}$")  # no `*`, no path: a session id never names a file [L-182]


def _t(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _key(f: dict) -> str:
    return f"{f.get('file', '')}|{' '.join(str(f.get('text', '')).lower().split())}"


def compute(events: list[dict], bug: bool = False, idle_s: int = IDLE_S) -> dict:
    """Time per phase (reproduce apart), verify time, gate failures, loops, blocked and idle hours, human events, escaped findings, tokens."""
    phase, prev, open_blockers = "intake", None, set()
    phases: dict[str, float] = {}
    blocked, idle, verify_s, repro_s = 0.0, 0.0, 0.0, 0.0
    fails: dict[str, int] = {}
    loops = {"amend": 0, "review": 0, "feedback": 0, "exempt": 0}
    human: dict[str, int] = {}
    reviews, first_pass, sessions = [], None, {}
    for i, ev in enumerate(events):
        t = _t(ev["ts"])
        if prev is not None:  # the gap since the previous event belongs to the state before this event
            if open_blockers:
                blocked += t - prev
            elif t - prev > idle_s:
                idle += t - prev
            else:
                phases[phase] = phases.get(phase, 0.0) + t - prev
        prev, kind = t, ev.get("type")
        if kind == "gate":
            if ev.get("result") == "fail":
                fails[ev["name"]] = fails.get(ev["name"], 0) + 1
            elif ev["name"] == "plan":
                phase = "reproduce" if bug else "build"
            elif ev["name"] in NEXT:
                phase = NEXT[ev["name"]]
            if ev["name"] == "verify" and ev.get("result") == "pass" and first_pass is None:
                first_pass = i
        elif kind == "retype":
            phase = "intake"
        elif kind == "blocker":
            open_blockers.add(ev["id"])
        elif kind == "unblock":
            open_blockers.discard(ev.get("blocker"))
        elif kind == "evidence":
            if ev.get("mode") == "verify":
                verify_s += ev.get("duration_s") or 0
            elif ev.get("mode") == "reproduce":
                repro_s += ev.get("duration_s") or 0
            if first_pass is None and ev.get("mode") == "verify" and ev.get("result") == "pass":
                first_pass = i
        elif kind == "loop":
            loops[ev["kind"] if ev.get("counts") else "exempt"] += 1
        elif kind in ("human", "answer", "decision"):
            label = ev.get("kind") if kind == "human" else kind
            human[label] = human.get(label, 0) + 1
        elif kind == "confirm":
            human["confirm"] = human.get("confirm", 0) + 1
        elif kind == "tokens":
            if (ev.get("input") or 0) + (ev.get("output") or 0) > 0:  # an empty report never replaces real numbers [L-183]
                sessions[ev["session"]] = {"input": ev.get("input") or 0, "output": ev.get("output") or 0}
        elif kind == "review":
            reviews.append((i, ev))
            if ev.get("kind") == "feedback":
                phase = "build"  # the findings of the change send the run back to build
    escaped = {_key(f) for i, ev in reviews if first_pass is not None and i > first_pass for f in ev.get("findings", [])
               if f.get("severity") in ("blocker", "major")}
    wall = _t(events[-1]["ts"]) - _t(events[0]["ts"]) if events else 0.0
    return {"wall_s": round(wall), "phases_s": {k: round(v) for k, v in phases.items()}, "verify_s": round(verify_s, 1),
            "reproduce_check_s": round(repro_s, 1), "gate_failures": fails, "loops": loops, "blocked_hours": round(blocked / 3600, 2),
            "idle_hours": round(idle / 3600, 2), "human_events": human, "escaped_findings": len(escaped),
            "tokens": {"sessions": sessions, "input": sum(s["input"] for s in sessions.values()), "output": sum(s["output"] for s in sessions.values())}}


def render(run_id: str, m: dict) -> str:
    phases = ", ".join(f"{k} {v}s" for k, v in m["phases_s"].items()) or "none"
    lines = [f"run {run_id}: wall {m['wall_s']}s, blocked {m['blocked_hours']}h, idle {m['idle_hours']}h", f"time per phase: {phases}",
             f"check time: verify {m['verify_s']}s, reproduce {m['reproduce_check_s']}s",
             f"gate failures: {m['gate_failures'] or 'none'}",
             f"loops: amend {m['loops']['amend']}, review {m['loops']['review']}, feedback {m['loops']['feedback']}, exempt by decision {m['loops']['exempt']}",
             f"human events: {m['human_events'] or 'none'}", f"escaped blocker/major findings: {m['escaped_findings']}"]
    t = m["tokens"]
    return "\n".join(lines + ([f"tokens: input {t['input']}, output {t['output']} over {len(t['sessions'])} session(s)"] if t["sessions"] else []))


def report(project, since: str | None) -> dict:
    """`metrics --all`: every run of the project, from its events; `since` (YYYY-MM-DD) keeps runs started on or after that day [L-179]."""
    from . import gates
    if since and not re.match(r"^\d{4}-\d\d-\d\d$", since):
        raise FactoryError(f"--since {since!r}: use a date such as 2026-11-01")
    rows = []
    for rid in project.run_ids():
        run = project.run(rid)
        events = run.read()
        if not events or (since and events[0]["ts"][:10] < since):
            continue
        st = run.state()
        rows.append({"run": rid, "ticket": st["ticket"], "kind": gates.effective_kind(run, st), "started": events[0]["ts"],
                     "closed": bool(st["closed"]), **compute(events, gates.is_bug(run, st), project.cfg["metrics"]["idle_minutes"] * 60)})
    phases: dict[str, float] = {}
    for r in rows:
        for k, v in r["phases_s"].items():
            phases[k] = phases.get(k, 0) + v
    total = {"runs": len(rows), "median_wall_s": round(statistics.median([r["wall_s"] for r in rows])) if rows else 0, "phases_s": phases,
             "blocked_hours": round(sum(r["blocked_hours"] for r in rows), 2), "idle_hours": round(sum(r["idle_hours"] for r in rows), 2),
             "gate_failures": sum(sum(r["gate_failures"].values()) for r in rows), "loops": sum(sum(r["loops"].values()) for r in rows),
             "escaped_findings": sum(r["escaped_findings"] for r in rows), "tokens": sum(r["tokens"]["input"] + r["tokens"]["output"] for r in rows)}
    return {"since": since, "runs": rows, "totals": total}


def render_all(rep: dict) -> str:
    t = rep["totals"]
    lines = [f"{r['run']:<22} wall {r['wall_s']:>7}s  blocked {r['blocked_hours']}h  idle {r['idle_hours']}h  loops {sum(r['loops'].values())}  "
             f"gate failures {sum(r['gate_failures'].values())}  escaped {r['escaped_findings']}" for r in rep["runs"]] or ["no runs"]
    phases = ", ".join(f"{k} {v}s" for k, v in t["phases_s"].items()) or "none"
    return "\n".join(lines + [f"{t['runs']} run(s){' since ' + rep['since'] if rep['since'] else ''}: median wall {t['median_wall_s']}s; time per phase: {phases}",
                              f"blocked {t['blocked_hours']}h, idle {t['idle_hours']}h, gate failures {t['gate_failures']}, loops {t['loops']}, "
                              f"escaped findings {t['escaped_findings']}, tokens {t['tokens']}"])


def record_tokens(run: Run, cfg: dict, project_dir, session: str | None, actor: str | None) -> dict | None:
    """Ask the `[metrics] tokens` adapter (`{run}` and `{session}` placeholders) for one session's usage on this run and record it as a
    `tokens` event. An empty report records nothing, so real numbers are never replaced by nothing [L-182, L-183]."""
    template = cfg["metrics"].get("tokens")
    if not template:
        raise FactoryError("no [metrics] tokens adapter is configured; the core never reads transcripts [L-182]")
    if not session or not SESSION.match(session):
        raise FactoryError("--session needs the harness session id (letters, digits, dot, dash and underscore only)")
    res = proc.run(proc.substitute(proc.split_template(template), {"run": run.id, "session": session}), cwd=project_dir, env=proc.child_env(), timeout_s=60)
    if res.timed_out or res.exit != 0:
        raise FactoryError(f"the tokens adapter failed (exit {res.exit}): " + "\n".join(res.err.splitlines()[-5:]))
    try:
        data = json.loads(res.out)
        ok = isinstance(data, dict) and data.get("session") == session and all(isinstance(data.get(k), int) and not isinstance(data.get(k), bool)
                                                                                 and data[k] >= 0 for k in ("input", "output"))
    except ValueError:
        ok = False
    if not ok:
        raise FactoryError('the tokens adapter must print {"session": "<the same id>", "input": <int>, "output": <int>}')
    if data["input"] + data["output"] == 0:
        return None
    return run.append({"type": "tokens", "session": session, "input": data["input"], "output": data["output"]}, actor)
