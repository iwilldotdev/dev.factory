"""`factory metrics`: derived from events.jsonl on demand, never stored [L-178, L-179, L-180, L-181]."""
from __future__ import annotations

from datetime import datetime, timezone

NEXT = {"intake": "plan", "reproduce": "build", "verify": "review"}


def _t(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _key(f: dict) -> str:
    return f"{f.get('file', '')}|{' '.join(str(f.get('text', '')).lower().split())}"


def compute(events: list[dict], bug: bool = False) -> dict:
    """Time per phase (reproduce apart), verify time, gate failures, loops, blocked hours, human events, escaped findings."""
    phase, prev, open_blockers = "intake", None, set()
    phases: dict[str, float] = {}
    blocked, verify_s, repro_s = 0.0, 0.0, 0.0
    fails: dict[str, int] = {}
    loops = {"amend": 0, "review": 0, "exempt": 0}
    human: dict[str, int] = {}
    reviews, first_pass = [], None
    for i, ev in enumerate(events):
        t = _t(ev["ts"])
        if prev is not None:  # the gap since the previous event belongs to the state before this event
            if open_blockers:
                blocked += t - prev
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
        elif kind == "review":
            reviews.append((i, ev))
    escaped = {_key(f) for i, ev in reviews if first_pass is not None and i > first_pass for f in ev.get("findings", [])
               if f.get("severity") in ("blocker", "major")}
    wall = _t(events[-1]["ts"]) - _t(events[0]["ts"]) if events else 0.0
    return {"wall_s": round(wall), "phases_s": {k: round(v) for k, v in phases.items()}, "verify_s": round(verify_s, 1),
            "reproduce_check_s": round(repro_s, 1), "gate_failures": fails, "loops": loops, "blocked_hours": round(blocked / 3600, 2),
            "human_events": human, "escaped_findings": len(escaped)}


def render(run_id: str, m: dict) -> str:
    phases = ", ".join(f"{k} {v}s" for k, v in m["phases_s"].items()) or "none"
    lines = [f"run {run_id}: wall {m['wall_s']}s, blocked {m['blocked_hours']}h", f"time per phase: {phases}",
             f"check time: verify {m['verify_s']}s, reproduce {m['reproduce_check_s']}s",
             f"gate failures: {m['gate_failures'] or 'none'}",
             f"loops: amend {m['loops']['amend']}, review {m['loops']['review']}, exempt by decision {m['loops']['exempt']}",
             f"human events: {m['human_events'] or 'none'}", f"escaped blocker/major findings: {m['escaped_findings']}"]
    return "\n".join(lines)
