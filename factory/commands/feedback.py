"""`factory feedback ingest`: MR discussions and pipeline failures become review findings."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core import code_repos, fetch_call, event, file_sha256, now, git_head, read_json, repo_path_for_run, run_dir, run_load, run_save, workspace_load, write_json
from ..fetches import feedback_fetches


def _is_resolved(disc: dict) -> bool:
    if disc.get("resolved") is True:
        return True
    notes = [n for n in disc.get("notes", []) if not n.get("system")]
    resolvable = [n for n in notes if n.get("resolvable")]
    if resolvable:
        return all(n.get("resolved") for n in resolvable)
    return False


def _discussions(data) -> list:
    """Discussions list, or flat notes from a saved gitlab_get_merge_request (includeNotes) as one-note threads."""
    if not isinstance(data, dict):
        return data
    if isinstance(data.get("discussions"), list):
        return data["discussions"]
    if isinstance(data.get("notes"), list):
        return [{"id": n.get("id", ""), "notes": [n]} for n in data["notes"] if isinstance(n, dict)]
    return []


def discussion_findings(data, repo: str | None) -> list[dict]:
    out = []
    discs = _discussions(data)
    for d in discs or []:
        if not isinstance(d, dict) or _is_resolved(d):
            continue
        notes = [n for n in d.get("notes", []) if not n.get("system")]
        if not notes:
            continue
        first = notes[0]
        pos = first.get("position") or {}
        loc = pos.get("new_path") or pos.get("old_path") or ""
        if loc and (pos.get("new_line") or pos.get("old_line")):
            loc += f":{pos.get('new_line') or pos.get('old_line')}"
        author = (first.get("author") or {}).get("username", "reviewer")
        out.append({
            "severity": "major",
            "file": loc,
            "text": f"{author}: {str(first.get('body', '')).strip()}"[:2000] + (f" (+{len(notes) - 1} repl{'y' if len(notes) == 2 else 'ies'})" if len(notes) > 1 else ""),
            "repo": repo,
            "source": f"discussion:{d.get('id', '')}",
        })
    return out


def pipeline_findings(data: dict, repo: str | None, name: str) -> list[dict]:
    out = []
    pipe = data.get("pipeline", data) if isinstance(data, dict) else {}
    jobs = data.get("jobs") or pipe.get("jobs") or []
    failed = [j for j in jobs if isinstance(j, dict) and j.get("status") == "failed"]
    for j in failed:
        out.append({
            "severity": "major" if j.get("allow_failure") is not True else "minor",
            "file": "",
            "text": f"pipeline job failed: {j.get('name', '?')} (stage {j.get('stage', '?')}) {j.get('web_url', '')}".strip(),
            "repo": repo,
            "source": f"job:{j.get('id', j.get('name', ''))}",
        })
    if not failed and pipe.get("status") == "failed":
        out.append({"severity": "major", "file": "", "text": f"pipeline {pipe.get('id', name)} failed (no failed job details saved)",
                    "repo": repo, "source": f"pipeline:{pipe.get('id', name)}"})
    return out


def _repo_from(name: str) -> str | None:
    m = re.match(r"(?:mr|pipeline)-(.+?)(?:-[^-]+)?(?:-discussions)?\.json$", name)
    return m.group(1) if m and not m.group(1).isdigit() else None


def _next_round(rdir: Path) -> int:
    nums = [int(m.group(1)) for p in rdir.glob("feedback-*.json") if (m := re.match(r"feedback-(\d+)\.json", p.name))]
    return max(nums, default=0) + 1


def ingest(run_id: str) -> tuple[int, Path | None]:
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
    rd = run_dir(run_id)
    remote = rd / "remote"
    rdir = rd / "reviews"
    seen = set()
    for p in rdir.glob("feedback-*.json"):
        for f in read_json(p).get("findings", []):
            seen.add(f.get("source"))

    raw: list[dict] = []
    for p in sorted(remote.glob("mr-*-discussions.json")):
        raw += discussion_findings(read_json(p), _repo_from(p.name))
    for p in sorted(remote.glob("pipeline-*.json")):
        if p.name.endswith("-jobs.json"):
            continue
        data = read_json(p)
        jobs_file = p.with_name(f"{p.stem}-jobs.json")  # raw gitlab_get_pipeline_jobs save
        if jobs_file.is_file() and isinstance(data, dict) and "jobs" not in data:
            data = {"pipeline": data, "jobs": read_json(jobs_file)}
        raw += pipeline_findings(data, _repo_from(p.name), p.stem)
    fresh = [f for f in raw if f["source"] not in seen]
    if not fresh:
        return 0, None

    findings = []
    for i, f in enumerate(fresh, 1):
        findings.append({"id": f"F{i}", **f})
    shas = {}
    for alias in code_repos(run):
        try:
            shas[alias] = git_head(repo_path_for_run(run, workspace, alias))
        except Exception:
            pass
    rnd = _next_round(rdir)
    record = {
        "schemaVersion": 1, "kind": "feedback", "round": rnd, "verdict": "CHANGES",
        "reviewer": "feedback-ingest", "recorded": now(),
        "repos": shas, "findings": findings,
        "completeness": {"done": 0, "gap": len(findings), "not_checked": 0},
    }
    path = rdir / f"feedback-{rnd}.json"
    write_json(path, record)
    run["phase"] = "build"
    run.setdefault("loops", {"build": 0, "review": 0})
    run["loops"]["review"] = run["loops"].get("review", 0) + 1
    event(run, "review", review_kind="feedback", round=rnd, findings=len(findings), digest=file_sha256(path))
    event(run, "phase", to="build", reason="feedback")
    run_save(run)
    return len(findings), path


def _cmd_ingest(args) -> int:
    run = run_load(args.run)
    fetch = feedback_fetches(args.run, workspace_load(run["workspace"]))  # computed before ingest changes nothing on disk
    n, path = ingest(args.run)
    if getattr(args, "json", False):
        print(json.dumps({"findings": n, "record": str(path) if path else None, "fetch": fetch}))
        return 0
    if n:
        print(f"{n} finding(s) -> {path}; phase set to build")
    else:
        print("no new unresolved threads or failed jobs")
    if fetch:
        print("missing feedback inputs (run these dev.mcp calls as-is, then ingest again):")
        for item in fetch:
            print(f"FETCH {fetch_call(item)}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("feedback", help="ingest MR/pipeline feedback")
    fsub = p.add_subparsers(dest="feedback_cmd", required=True)
    i = fsub.add_parser("ingest")
    i.add_argument("run")
    i.set_defaults(func=_cmd_ingest)
