"""`factory metrics <run>` and `factory metrics report`: effectiveness numbers per run."""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter
from pathlib import Path

from ..core import factory_home, read_json, run_dir, run_load, write_json

RUN_MARKER = "factory-run: "


def _ts(text) -> _dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        d = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)


def claude_roots() -> list[Path]:
    """Claude Code `projects` dirs of every profile (~/.claude, ~/.claude-perfil2, ...)."""
    return [p / "projects" for p in sorted(Path.home().glob(".claude*")) if (p / "projects").is_dir()]


def find_transcripts(run: dict) -> list[Path]:
    found: dict[Path, None] = {}
    roots = claude_roots()
    for s in run.get("sessions") or []:
        p = Path(s)
        if p.suffix == ".jsonl" and p.is_file():
            found[p] = None
            continue
        for root in roots:
            for hit in root.glob(f"*/{s}.jsonl"):
                found[hit] = None
    created = _ts(run.get("created"))
    since = created.timestamp() if created else 0
    needle = (RUN_MARKER + run["id"]).encode()
    for root in roots:
        for f in root.glob("*/*.jsonl"):
            if f in found:
                continue
            try:
                if f.stat().st_mtime < since:
                    continue
                if needle in f.read_bytes():
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


def _escaped(run: dict, rd: Path) -> dict:
    t0 = None
    for e in run.get("events", []):
        d = e.get("data") or {}
        if e.get("kind") == "gate_pass" and (d.get("phase") == "prove" or d.get("gate") == "prove"):
            t0 = _ts(e.get("at"))
            break
    result = {"count": 0, "by_severity": {}}
    if t0 is None:
        return result
    sev: Counter = Counter()
    for p in sorted((rd / "reviews").glob("*.json")):
        try:
            rec = read_json(p)
        except Exception:
            continue
        when = _ts(rec.get("recorded") or rec.get("at") or rec.get("created")) or _dt.datetime.fromtimestamp(
            p.stat().st_mtime, _dt.timezone.utc)
        if when > t0:
            for f in rec.get("findings", []):
                sev[f.get("severity", "unknown")] += 1
    result["count"] = sum(sev.values())
    result["by_severity"] = dict(sev)
    return result


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
        "subagents": dict(sorted(agents.items())),
        "tokens": {"main": dict(tokens), "subagents": dict(sub_tokens), "total": dict(total)},
        "gate_fail": dict(gate_fail),
        "loops": run.get("loops", {}),
        "escaped_findings": _escaped(run, rd),
        "transcripts": [str(p) for p in transcripts],
    }
    write_json(rd / "metrics.json", metrics)
    return metrics


def _summary(m: dict) -> str:
    return (
        f"{m['run']}: wall {m['wall_time_h']}h, human msgs {m['human_messages']}, interventions {m['human_interventions']}, "
        f"subagents {sum(m['subagents'].values())}, out tokens {m['tokens']['total'].get('output', 0)}, "
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
        str(sum(m["subagents"].values())), str(m["tokens"]["total"].get("output", 0)), str(sum(m["gate_fail"].values())),
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
