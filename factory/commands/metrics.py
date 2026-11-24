"""`factory metrics <run>` and `factory metrics report`: effectiveness numbers per run."""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections import Counter
from pathlib import Path

from ..core import HUMAN_TYPES, valid_session_id, factory_home, read_json, run_dir, run_load, write_json

RUN_MARKER = "factory-run: "


def _ts(text) -> _dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        d = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def _projects(path: Path) -> Path | None:
    """`path` itself when it is a `projects` dir, else its `projects` child."""
    p = path.expanduser()
    if p.name == "projects" and p.is_dir():
        return p
    return p / "projects" if (p / "projects").is_dir() else None


def claude_roots(run: dict | None = None) -> list[Path]:
    """Claude Code `projects` dirs where transcripts may live. Never a glob of the home folder: only the profile of this
    process (`CLAUDE_CONFIG_DIR`), the profile this CLI was installed into (provenance), the profiles the run recorded
    (`run.transcript_roots`), the `FACTORY_TRANSCRIPT_ROOTS` list (profile or `projects` dirs, os.pathsep separated)
    and the default `~/.claude`."""
    from ..dist_info import ROOT, provenance
    cands: list[str] = list((run or {}).get("transcript_roots") or [])
    explicit = [x for x in os.environ.get("FACTORY_TRANSCRIPT_ROOTS", "").split(os.pathsep) if x.strip()]
    cands += explicit
    if not explicit:  # an explicit list replaces discovery (tests and odd setups pin it)
        cands.append(os.environ.get("CLAUDE_CONFIG_DIR", ""))
        if provenance(ROOT):
            cands.append(str(ROOT.parent))  # <profile>/factory-dist
        cands.append(str(Path.home() / ".claude"))
    out: dict[Path, None] = {}
    for c in cands:
        if str(c).strip() and (p := _projects(Path(c))):
            out[p] = None
    return list(out)


def find_transcripts(run: dict) -> list[Path]:
    """Transcripts of the sessions registered on the run (`run start` / `run session` / `run claim`), plus transcripts
    that carry the run marker when at least one session is registered. No registered session: nothing (metrics show
    "unknown" instead of attributing someone else's tokens)."""
    sessions = [s.get("session") if isinstance(s, dict) else s for s in run.get("sessions") or []]
    sessions = [s for s in sessions if s and valid_session_id(s)]
    if not sessions:
        return []
    found: dict[Path, None] = {}
    roots = claude_roots(run)
    for s in sessions:
        for root in roots:
            for hit in root.glob(f"*/{s}.jsonl"):
                found[hit] = None
    created = _ts(run.get("created"))
    since = created.timestamp() if created else 0
    # a parent run (epic / review-batch) also owns the transcripts marked with any of its children
    needles = [(RUN_MARKER + rid).encode() for rid in [run["id"], *(run.get("children") or [])]]
    for root in roots:
        for f in root.glob("*/*.jsonl"):
            if f in found:
                continue
            try:
                if f.stat().st_mtime < since:
                    continue
                data = f.read_bytes()
                if any(n in data for n in needles):
                    found[f] = None
            except OSError:
                continue
    return list(found)


def _records(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _is_human(rec: dict) -> bool:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        return any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip() for b in content)
    return False


def scan_transcript(path: Path, human: bool) -> dict:
    usage: dict[str, dict] = {}
    agents: Counter = Counter()
    humans = 0
    first = last = None
    for rec in _records(path):
        t = _ts(rec.get("timestamp"))
        if t:
            first = t if first is None or t < first else first
            last = t if last is None or t > last else last
        if human and _is_human(rec):
            humans += 1
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        u = msg.get("usage")
        if isinstance(u, dict):
            usage[msg.get("id") or f"{path.name}:{len(usage)}"] = u  # streamed duplicates: last wins
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                inp = block.get("input") or {}
                agents[f"{inp.get('subagent_type') or 'general-purpose'}/{inp.get('model') or 'default'}"] += 1
    tokens = Counter()
    for u in usage.values():
        tokens["input"] += int(u.get("input_tokens") or 0)
        tokens["output"] += int(u.get("output_tokens") or 0)
        tokens["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        tokens["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
    return {"humans": humans, "agents": agents, "tokens": tokens, "first": first, "last": last}


def _subagent_files(main: Path) -> list[Path]:
    d = main.parent / main.stem / "subagents"
    return sorted(d.rglob("*.jsonl")) if d.is_dir() else []


ESCAPED_SEVERITIES = ("blocker", "major")


def _escaped(run: dict, rd: Path) -> dict:
    """Findings that got past the prove gate: blocker/major findings recorded after it passed (code/qa reviews and
    feedback), each counted once even when later rounds carry it forward. Minor and nit findings are in `all_findings`."""
    t0 = None
    for e in run.get("events", []):
        d = e.get("data") or {}
        if e.get("kind") == "gate_pass" and (d.get("phase") == "prove" or d.get("gate") == "prove"):
            t0 = _ts(e.get("at"))
            break
    result = {"count": 0, "by_severity": {}, "all_findings": 0}
    if t0 is None:
        return result
    sev: Counter = Counter()
    seen: set = set()
    total = 0
    for p in sorted((rd / "reviews").glob("*.json")):
        try:
            rec = read_json(p)
        except Exception:
            continue
        when = _ts(rec.get("recorded") or rec.get("at") or rec.get("created")) or _dt.datetime.fromtimestamp(
            p.stat().st_mtime, _dt.timezone.utc)
        if when > t0:
            for f in rec.get("findings") or []:
                if not isinstance(f, dict):
                    continue
                what = f.get("text") or f.get("id")
                if not what:  # nothing to tell two findings apart but where they are
                    what = (f.get("file"), f.get("line"))
                key = (p.stem.rsplit("-", 1)[0], f.get("file"), what)
                if key in seen:
                    continue
                seen.add(key)
                total += 1
                if f.get("severity") in ESCAPED_SEVERITIES:
                    sev[f["severity"]] += 1
    result["count"] = sum(sev.values())
    result["by_severity"] = dict(sev)
    result["all_findings"] = total
    return result


IDLE_GAP_S = 600  # a stretch of `ship` with no run event for this long is waiting (humans, pipelines), not work


def _union(spans: list[tuple[_dt.datetime, _dt.datetime]]) -> list[tuple[_dt.datetime, _dt.datetime]]:
    out: list[tuple[_dt.datetime, _dt.datetime]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _overlap(s: _dt.datetime, e: _dt.datetime, spans: list[tuple[_dt.datetime, _dt.datetime]]) -> float:
    return sum(max(0.0, (min(e, b) - max(s, a)).total_seconds()) for a, b in spans)


def time_breakdown(run: dict) -> dict:
    """Hours per phase from `phase` events (the last open phase runs up to `updated`), with the time spent blocked
    (open blockers, the `blocked` phase) and the idle time of `ship` taken out of the phases and reported apart."""
    evs = sorted(((_ts(e.get("at")), e) for e in run.get("events", []) if e.get("kind") == "phase"),
                 key=lambda x: x[0] or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc))
    end = _ts(run.get("updated"))
    spans: list[tuple[str, _dt.datetime, _dt.datetime]] = []
    cur = at0 = None
    for at, e in evs:
        if at is None:
            continue
        if cur is not None:
            spans.append((cur, at0, max(at0, at)))
        cur, at0 = (e.get("data") or {}).get("to"), at
    if cur is not None and end and end > at0:
        spans.append((cur, at0, end))
    blocked = [(s, e) for ph, s, e in spans if ph == "blocked"]
    for b in run.get("blockers") or []:
        s = _ts(b.get("at"))
        e = _ts(b.get("resolved_at")) or (end if not b.get("resolved") else None)
        if s and e and e > s:
            blocked.append((s, e))
    blocked = _union(blocked)
    times = sorted(t for t in (_ts(e.get("at")) for e in run.get("events", [])) if t)
    secs: Counter = Counter()
    idle = 0.0
    for ph, s, e in spans:
        if ph == "blocked":
            continue
        secs[ph] += (e - s).total_seconds() - _overlap(s, e, blocked)
        if ph == "ship":
            marks = [s, *[t for t in times if s < t < e], e]
            for a, b in zip(marks, marks[1:]):
                gap = (b - a).total_seconds() - _overlap(a, b, blocked)
                if gap > IDLE_GAP_S:
                    idle += gap
                    secs[ph] -= gap
    return {"phase_hours": {k: round(max(0.0, v) / 3600, 2) for k, v in secs.items()},
            "blocked_hours": round(sum((e - s).total_seconds() for s, e in blocked) / 3600, 2),
            "idle_hours": round(idle / 3600, 2)}


def phase_hours(run: dict) -> dict:
    return time_breakdown(run)["phase_hours"]


def human_types(events: list) -> Counter:
    """`run human` events per type; an event without a type (older runs) or with an unknown one is `other`."""
    out: Counter = Counter()
    for e in events:
        if e.get("kind") == "human":
            t = (e.get("data") or {}).get("type")
            out[t if t in HUMAN_TYPES else "other"] += 1
    return out


def prove_stats(run: dict) -> dict:
    events = run.get("events", [])
    proves = [e for e in events if e.get("kind") == "prove"]
    modes = Counter((e.get("data") or {}).get("mode") or "unknown" for e in proves)
    wall = sum(float((e.get("data") or {}).get("wall_s") or 0) for e in proves)
    entries = sum(1 for e in events if e.get("kind") == "phase" and (e.get("data") or {}).get("to") == "prove")
    return {"invocations": len(proves), "phase_entries": entries, "by_mode": dict(modes), "wall_s": round(wall, 1)}


def check_stats(run: dict, rd: Path) -> dict:
    evs = [e for e in run.get("events", []) if e.get("kind") == "evidence"]
    by_status = Counter((e.get("data") or {}).get("status") or "unknown" for e in evs)
    reruns = Counter((e.get("data") or {}).get("check") for e in evs if (e.get("data") or {}).get("check"))
    rows, flaky = [], 0
    for p in sorted((rd / "evidence").glob("*.json")):
        try:
            rec = read_json(p)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("flaky") or (isinstance(rec.get("details"), dict) and rec["details"].get("flaky")):
            flaky += 1
        if rec.get("duration_s") is not None:
            rows.append({"id": rec.get("id") or rec.get("check") or p.stem, "type": rec.get("type"),
                         "duration_s": rec.get("duration_s")})
    rows.sort(key=lambda r: -float(r["duration_s"] or 0))
    return {"executions": len(evs), "by_status": dict(by_status), "top_slowest": rows[:10],
            "most_rerun": [{"id": k, "count": v} for k, v in reruns.most_common(10)], "flaky": flaky}


def _parent_attribution(run: dict) -> tuple[dict | None, str | None, list[str]]:
    """Child without transcripts: tokens of the parent's messages in its time window that mention it."""
    pid = run.get("parent")
    if not pid:
        return None, None, []
    try:
        parent = run_load(pid)
    except Exception:
        return None, None, []
    lo, hi = _ts(run.get("created")), _ts(run.get("updated"))
    needles = [RUN_MARKER + run["id"], run["id"]]
    total: Counter = Counter()
    used: list[str] = []
    for main in find_transcripts(parent):
        usage: dict[str, dict] = {}
        for rec in _records(main):
            t = _ts(rec.get("timestamp"))
            if t is None or (lo and t < lo) or (hi and t > hi) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            u = msg.get("usage")
            if isinstance(u, dict) and any(n in json.dumps(rec, ensure_ascii=False) for n in needles):
                usage[msg.get("id") or f"{main.name}:{len(usage)}"] = u
        for u in usage.values():
            total["input"] += int(u.get("input_tokens") or 0)
            total["output"] += int(u.get("output_tokens") or 0)
            total["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
            total["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
        if usage:
            used.append(str(main))
    if total:
        return dict(total), None, used
    return None, f"shared with parent {pid}", []


TOKENS_KEPT_NOTE = "kept from the previous report: no session is registered on the run now"


def _kept_tokens(run: dict, rd: Path) -> dict | None:
    """A run with no registered session cannot recompute tokens; a report that already holds token numbers keeps them
    (a refresh at `done` or `close` must not wipe them)."""
    if [s for s in run.get("sessions") or [] if (s.get("session") if isinstance(s, dict) else s)]:
        return None
    try:
        old = read_json(rd / "metrics.json")
    except Exception:
        return None
    tok = old.get("tokens") if isinstance(old, dict) else None
    if isinstance(tok, dict) and any((tok.get("total") or {}).values()):
        return tok
    return None


def compute(run_id: str) -> dict:
    run = run_load(run_id)
    rd = run_dir(run_id)
    events = run.get("events", [])
    created = _ts(run.get("created"))
    end_candidates = [_ts(run.get("updated"))] + [_ts(e.get("at")) for e in events]
    end = max([d for d in end_candidates if d], default=None)
    wall = round((end - created).total_seconds() / 3600, 2) if created and end else 0.0

    humans = 0
    agents: Counter = Counter()
    tokens: Counter = Counter()
    sub_tokens: Counter = Counter()
    firsts, lasts = [], []
    transcripts = find_transcripts(run)
    if run.get("parent"):  # the parent's own session mentions the child marker; it is not the child's transcript
        try:
            ps = {str((x.get("session") if isinstance(x, dict) else x) or "") for x in run_load(run["parent"]).get("sessions") or []}
        except Exception:
            ps = set()
        own = {str((x.get("session") if isinstance(x, dict) else x) or "") for x in run.get("sessions") or []}
        transcripts = [t for t in transcripts if (t.stem not in ps and str(t) not in ps) or t.stem in own or str(t) in own]
    for main in transcripts:
        s = scan_transcript(main, human=True)
        humans += s["humans"]
        agents += s["agents"]
        tokens += s["tokens"]
        firsts.append(s["first"])
        lasts.append(s["last"])
        for sub in _subagent_files(main):
            ss = scan_transcript(sub, human=False)
            agents += ss["agents"]
            sub_tokens += ss["tokens"]
            firsts.append(ss["first"])
            lasts.append(ss["last"])
    firsts = [d for d in firsts if d]
    lasts = [d for d in lasts if d]
    span = round((max(lasts) - min(firsts)).total_seconds() / 3600, 2) if firsts and lasts else None

    gate_fail: Counter = Counter()
    for e in events:
        if e.get("kind") == "gate_fail":
            d = e.get("data") or {}
            gate_fail[d.get("phase") or d.get("gate") or "unknown"] += 1
    review_rounds: Counter = Counter()
    for e in events:
        if e.get("kind") == "review":
            review_rounds[(e.get("data") or {}).get("review_kind") or "unknown"] += 1
    tokens_block = None
    tokens_note = None
    if not transcripts and run.get("parent"):
        attributed, tokens_note, used = _parent_attribution(run)
        if attributed is not None:
            tokens_block = {"main": attributed, "subagents": {}, "total": attributed}
            tokens_note = f"attributed from parent {run['parent']} transcripts"
            transcripts = [Path(u) for u in used]
    kept = _kept_tokens(run, rd) if not transcripts and not tokens_block else None
    if kept:
        tokens_block, tokens_note = kept, TOKENS_KEPT_NOTE
    total = Counter()
    total.update(tokens)
    total.update(sub_tokens)
    metrics = {
        "schemaVersion": 1,
        "run": run_id,
        "phase": run.get("phase"),
        "wall_time_h": wall,
        "transcript_span_h": span,
        "human_messages": humans,
        "human_interventions": sum(1 for e in events if e.get("kind") == "human"),
        "human_by_type": dict(sorted(human_types(events).items())),
        "subagents": dict(sorted(agents.items())),
        "tokens": tokens_block if (tokens_block or tokens_note) else
        {"main": dict(tokens), "subagents": dict(sub_tokens), "total": dict(total)},
        "gate_fail": dict(gate_fail),
        "review_rounds": dict(review_rounds),
        "loops": run.get("loops", {}),
        "escaped_findings": _escaped(run, rd),
        "transcripts": [str(p) for p in transcripts],
        **time_breakdown(run),
        "prove": prove_stats(run),
        "checks": check_stats(run, rd),
    }
    if tokens_note:
        metrics["tokens_note"] = tokens_note
    write_json(rd / "metrics.json", metrics)
    return metrics


def refresh(run_id: str) -> None:
    """Recompute the metrics report when a run reaches a final state (done, report, closed). Never fails the caller."""
    try:
        compute(run_id)
    except Exception:  # metrics are a report, not a gate
        pass


def _out_tokens(m: dict) -> int | str:
    if not m.get("tokens"):
        return "null"
    return m["tokens"]["total"].get("output", 0)


def _summary(m: dict) -> str:
    pr, ck = m.get("prove") or {}, m.get("checks") or {}
    extra = [f"phase_h {m.get('phase_hours')} blocked {m.get('blocked_hours', 0)}h idle(ship) {m.get('idle_hours', 0)}h",
             f"prove {pr.get('invocations', 0)} runs {pr.get('by_mode')} {pr.get('wall_s', 0)}s",
             f"checks {ck.get('executions', 0)} {ck.get('by_status')} flaky {ck.get('flaky', 0)}",
             "slowest " + ", ".join(f"{r['id']} {r['duration_s']}s" for r in ck.get("top_slowest", [])[:5]),
             "rerun " + ", ".join(f"{r['id']}x{r['count']}" for r in ck.get("most_rerun", [])[:5])]
    if m.get("tokens_note"):
        extra.append(f"tokens: {m['tokens_note']}")
    return _summary_base(m) + "".join("\n  " + x for x in extra)


def _summary_base(m: dict) -> str:
    return (
        f"{m['run']}: wall {m['wall_time_h']}h, human msgs {m['human_messages']}, interventions {m['human_interventions']}"
        f"{' ' + str(m['human_by_type']) if m.get('human_by_type') else ''}, "
        f"subagents {sum(m['subagents'].values())}, out tokens {_out_tokens(m)}, "
        f"gate fails {sum(m['gate_fail'].values())}, loops {m['loops']}, escaped {m['escaped_findings']['count']}"
    )


def report(since: str | None) -> list[dict]:
    root = factory_home() / "runs"
    cutoff = _ts(since + "T00:00:00") if since else None
    rows = []
    for p in sorted(root.glob("*/run.json")) if root.is_dir() else []:
        try:
            run = read_json(p)
        except Exception:
            continue
        if cutoff and (_ts(run.get("created")) or cutoff) < cutoff:
            continue
        rows.append(compute(run["id"]))
    return rows


def _table(rows: list[dict]) -> str:
    head = ["run", "phase", "wall_h", "human", "interv", "agents", "out_tok", "gate_fail", "build_loops", "escaped"]
    body = [[
        m["run"], str(m["phase"]), str(m["wall_time_h"]), str(m["human_messages"]), str(m["human_interventions"]),
        str(sum(m["subagents"].values())), str(_out_tokens(m)), str(sum(m["gate_fail"].values())),
        str((m["loops"] or {}).get("build", 0)), str(m["escaped_findings"]["count"]),
    ] for m in rows]
    widths = [max(len(r[i]) for r in [head, *body]) for i in range(len(head))]
    fmt = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    return "\n".join([fmt(head), *[fmt(r) for r in body]])


def _cmd(args) -> int:
    if args.target == "report":
        rows = report(args.since)
        print(json.dumps(rows, indent=2) if getattr(args, "json", False) else (_table(rows) if rows else "no runs"))
        return 0
    m = compute(args.target)
    print(json.dumps(m, indent=2) if getattr(args, "json", False) else _summary(m))
    return 0


def register(sub) -> None:
    p = sub.add_parser("metrics", help="run metrics, or `metrics report` across runs")
    p.add_argument("target", help="run id, or the word 'report'")
    p.add_argument("--since", help="YYYY-MM-DD (report only)")
    p.set_defaults(func=_cmd)
