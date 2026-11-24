"""`factory feedback ingest`: unresolved change threads and pipeline failures become review findings (contracts only)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import contracts
from ..core import FactoryError, code_repos, fetch_call, event, file_sha256, now, git_head, read_json, repo_path_for_run, run_dir, run_load, run_save, workspace_load, write_json
from ..fetches import feedback_fetches
from .gate import _unresolved_threads


def thread_findings(doc: dict) -> list[dict]:
    """One finding per unresolved human thread of a `change.v1` contract (source `thread:<id>`)."""
    out = []
    for t in _unresolved_threads(doc.get("threads") or []):
        notes = [n for n in t.get("notes") or [] if not n.get("system")]
        first = notes[0]
        loc = first.get("path") or ""
        if loc and first.get("line"):
            loc += f":{first['line']}"
        reply = f" (+{len(notes) - 1} repl{'y' if len(notes) == 2 else 'ies'})" if len(notes) > 1 else ""
        out.append({"severity": "major", "file": loc,
                    "text": f"{first.get('author') or 'reviewer'}: {str(first.get('body_md', '')).strip()}"[:2000] + reply,
                    "repo": doc.get("repo"), "source": f"thread:{t.get('id', '')}"})
    return out


def pipeline_findings(doc: dict, repo: str | None) -> list[dict]:
    """Failed jobs of a `pipeline.v1` contract (source `job:<id>`), or the pipeline itself when it failed without job detail."""
    out = []
    failed = [j for j in doc.get("jobs") or [] if j.get("status") == "failed"]
    for j in failed:
        out.append({"severity": "major" if j.get("allow_failure") is not True else "minor", "file": "",
                    "text": f"pipeline job failed: {j.get('name', '?')} (stage {j.get('stage', '?')}) {j.get('url', '')}".strip(),
                    "repo": repo, "source": f"job:{j.get('id', j.get('name', ''))}"})
    if not failed and doc.get("status") == "failed":
        out.append({"severity": "major", "file": "", "text": f"pipeline {doc['id']} failed (no failed job details saved)",
                    "repo": repo, "source": f"pipeline:{doc['id']}"})
    return out


def _canonical_source(source) -> str | None:
    """A finding source with the legacy `discussion:<id>` spelling folded into `thread:<id>`."""
    if not isinstance(source, str):
        return None
    return "thread:" + source[len("discussion:"):] if source.startswith("discussion:") else source


def _repo_of_ident(ws: dict, ident: str) -> str | None:
    """Workspace repo alias a contract id (`<alias>-<target>`) belongs to; the longest matching alias wins."""
    aliases = [r["alias"] for r in ws.get("repos", []) if r.get("alias") and (ident == r["alias"] or ident.startswith(r["alias"] + "-"))]
    return max(aliases, key=len) if aliases else None


def _next_round(rdir: Path) -> int:
    nums = [int(m.group(1)) for p in rdir.glob("feedback-*.json") if (m := re.match(r"feedback-(\d+)\.json", p.name))]
    return max(nums, default=0) + 1


def ingest(run_id: str) -> tuple[int, Path | None]:
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
    rd = run_dir(run_id)
    rdir = rd / "reviews"
    seen = set()
    for p in rdir.glob("feedback-*.json"):
        for f in read_json(p).get("findings", []):
            seen.add(_canonical_source(f.get("source")))

    raw: list[dict] = []
    plan_path = rd / "ship-plan.json"
    if not plan_path.is_file():
        raise FactoryError("ship-plan.json missing: feedback is read only from the changes the ship plan lists "
                           "(run the ship plan first)")
    plan = read_json(plan_path)
    unusable: list[str] = []
    for c in plan.get("changes") or []:
        ident = f"{c['repo']}-{c['target']}"
        for kind in ("change", "pipeline"):
            if not contracts.contract_path(rd, kind, contracts.safe_id(ident)).is_file():
                continue  # not fetched yet: `feedback ingest` prints the FETCH hints
            doc, problems = contracts.load_contract(run_id, kind, ident)
            if doc is None or problems:
                unusable += problems or [f"{kind}/{contracts.safe_id(ident)}: unusable"]
            elif kind == "change":
                raw += thread_findings(doc)
            else:
                raw += pipeline_findings(doc, c["repo"])
    if unusable:
        raise FactoryError("feedback contracts are stale or invalid (nothing was ingested; re-fetch and re-ingest them):\n  - "
                           + "\n  - ".join(unusable))
    fresh = [f for f in raw if _canonical_source(f["source"]) not in seen]
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
        print("missing feedback inputs (fetch each as hinted, `factory ingest` it, then run this again):")
        for item in fetch:
            print(f"FETCH {fetch_call(item)}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("feedback", help="ingest change thread / pipeline feedback")
    fsub = p.add_subparsers(dest="feedback_cmd", required=True)
    i = fsub.add_parser("ingest")
    i.add_argument("run")
    i.set_defaults(func=_cmd_ingest)
