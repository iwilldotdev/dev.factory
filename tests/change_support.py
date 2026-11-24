"""Test helpers: valid, fresh `change.v1` / `pipeline.v1` contracts (and their raw files) in a run dir, no profile needed."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory import contracts, core  # noqa: E402


def _write(run_id: str, kind: str, ident: str, doc: dict, raw_body: bytes) -> Path:
    rd = core.run_dir(run_id)
    safe = contracts.safe_id(ident)
    raw = contracts.raw_path_for(rd, kind, safe, "json")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(raw_body)
    doc["provenance"] = {"profile": "native", "profile_digest": "native", "id": ident,
                         "raw_path": raw.relative_to(rd).as_posix(), "raw_sha256": hashlib.sha256(raw_body).hexdigest(),
                         "ingested_at": "2026-01-01T00:00:00Z"}
    problems = contracts.validate(kind, doc)
    assert not problems, problems
    path = contracts.contract_path(rd, kind, safe)
    core.write_json(path, contracts.order_contract(kind, doc))
    return path


def put_change(run_id: str, repo: str, target: str, *, number: int = 1, source_branch: str = "feature/x",
               target_branch: str | None = None, title: str = "[DEV-1] - Do thing", draft: bool = True,
               head_sha: str = "f" * 40, state: str = "open", description_md: str = "", reviewers: list | None = None,
               threads: list | None = None, pipelines: list | None = None, contract_repo: str | None = None,
               **extra) -> Path:
    """`contracts/change/<repo>-<target>.json` (+ raw) for a change; `extra` overrides or adds contract fields."""
    doc = {"schema": "change.v1", "repo": contract_repo or repo, "number": number, "url": f"https://scm.example/{repo}/changes/{number}",
           "title": title, "description_md": description_md, "source_branch": source_branch,
           "target_branch": target_branch or target, "head_sha": head_sha, "state": state, "draft": draft,
           "approvals": {"known": False, "by": []}, "unknowns": []}
    if reviewers is not None:
        doc["reviewers"] = reviewers
    if threads is not None:
        doc["threads"] = threads
    if pipelines is not None:
        doc["pipelines"] = pipelines
    doc.update(extra)
    ident = f"{repo}-{target}"
    return _write(run_id, "change", ident, doc, f'{{"change": "{ident}", "n": {number}}}\n'.encode("utf-8"))


def put_pipeline(run_id: str, repo: str, target: str, *, id: str = "77", sha: str = "f" * 40, status: str = "success",
                 jobs: list | None = None, **extra) -> Path:
    doc = {"schema": "pipeline.v1", "id": id, "sha": sha, "status": status, "jobs": list(jobs or []), "unknowns": []}
    doc.update(extra)
    ident = f"{repo}-{target}"
    return _write(run_id, "pipeline", ident, doc, f'{{"pipeline": "{ident}", "id": "{id}"}}\n'.encode("utf-8"))


def put_for_plan(run_id: str, plan: dict, **over) -> None:
    """A change contract matching every change of a delivery plan (title, branches, draft, head): `ship verify` passes."""
    for i, c in enumerate(plan["changes"], 1):
        kw = dict(number=i, source_branch=c["source_branch"], target_branch=c["target_branch"], title=c["title"],
                  draft=c["draft"], head_sha=c["head_sha"] or "f" * 40)
        if c.get("reviewers"):
            kw["reviewers"] = list(c["reviewers"])
        kw.update(over)
        put_change(run_id, c["repo"], c["target"], **kw)
