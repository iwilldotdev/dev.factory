"""Fetch lines for files the CLI is missing.

The CLI never calls the network. When a gate or command needs a remote file under
`runs/<id>/`, it prints `{kind, id, raw_path, hint}` (absolute `raw_path` with forward slashes; `hint` is the
profile's acquisition template) so an agent saves the payload there as-is and the payload never passes through
the model. Unknown values stay as `<placeholders>`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import contracts, profiles
from .core import FactoryError, abs_path, dedupe_fetch, repo_conf, run_dir


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------- contracts (provider-neutral) ----------

def needs(kind: str, ident: str, ctx: dict, raw: Path | None = None) -> dict:
    """`{kind, id, raw_path, hint}` for one missing raw payload. `ctx`: `run` (id), `ws` (workspace; picks the profile
    of the kind's role), optional `profile` override, and any hint placeholder (`scm_project`, `number`, ...). The
    hint is the profile's acquisition template (no tool names live in the code); None when no profile is configured.
    `raw` overrides the destination (a file outside any run: a queue list, a shared inbox folder)."""
    prof = None
    ext = contracts.KIND_EXT.get(kind, "json")
    try:
        _, prof = profiles.profile_for(ctx.get("ws") or {}, kind, ctx.get("profile"))
        ext = profiles.kind_ext(prof, kind)
    except FactoryError:
        prof = None
    if raw is None:
        raw = contracts.raw_path_for(run_dir(ctx["run"]), kind, contracts.safe_id(ident), ext)
    values = {"id": ident}  # a ctx `id` (the provider's own id, e.g. a pipeline) wins over the file-name id
    values.update({k: v for k, v in ctx.items() if k not in ("run", "ws", "profile") and v not in (None, "")})
    values.update({"raw_path": abs_path(raw), "raw_dir": abs_path(raw.parent)})
    if kind == "ticket":
        values["key"] = ident
    return {"kind": kind, "id": ident, "raw_path": abs_path(raw),
            "hint": profiles.render_hint(prof, kind, values) if prof else None}


def ticket_raw(run_id: str, key: str) -> Path | None:
    """The raw payload file of a ticket in the run, whatever its extension; None when absent."""
    d = run_dir(run_id) / "raw" / "ticket"
    safe = contracts.safe_id(key)
    if not d.is_dir():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and p.name.startswith(safe + ".") and "." not in p.name[len(safe) + 1:]:
            return p
    return None


def intake_fetches(run: dict, ws: dict | None = None) -> list[dict]:
    """One fetch per ticket whose raw payload is not saved yet (a saved raw without a contract needs `factory ingest`)."""
    rid = run["id"]
    return [needs("ticket", t["key"], {"run": rid, "ws": ws}) for t in run.get("tickets", [])
            if ticket_raw(rid, t["key"]) is None]


# ---------- design ----------

def design_fetches(run_id: str, ws: dict | None = None) -> list[dict]:
    """One `needs()` per missing raw design payload of the run's visual/style checks (image first: the frame contract
    reads its path). Approximate frames have none: they come from `factory design add`. A saved raw without a contract
    needs `factory ingest`, not a fetch."""
    from . import design
    rd = run_dir(run_id)
    checks = design.acs_checks(run_id)
    out: list[dict] = []
    for node in design.frame_nodes(checks):
        if any(design.check_fidelity(c) == "approx" for c in checks if design.check_node(c) == node):
            continue
        ctx = {"run": run_id, "ws": ws}
        file = design.source_file_of(checks, node)
        if file:
            ctx["file"] = file
        safe = contracts.safe_id(node)
        for kind in ("design_image", "design_frame"):
            item = needs(kind, node, ctx)
            ext = Path(item["raw_path"]).suffix.lstrip(".")
            if not contracts.raw_path_for(rd, kind, safe, ext).is_file():
                out.append(item)
    return dedupe_fetch(out)


# ---------- delivery (ship verify, feedback) ----------

def project_id(ws: dict | None, alias: str) -> str:
    try:
        return str(repo_conf(ws or {}, alias).get("scm_project") or "<scm_project>")
    except Exception:
        return "<scm_project>"


def _change_doc(run_id: str, repo: str, target: str) -> dict:
    data = _json(run_dir(run_id) / "contracts" / "change" / f"{contracts.safe_id(f'{repo}-{target}')}.json")
    return data if isinstance(data, dict) else {}


def _latest_pipeline_id(doc: dict) -> Any:
    """Id of the newest pipeline a change contract lists (numeric ids compare numerically), else None."""
    ids = [p.get("id") for p in doc.get("pipelines") or [] if isinstance(p, dict) and p.get("id") is not None]
    if not ids:
        return None
    return max(ids, key=lambda i: (0, int(i)) if str(i).isdigit() else (-1, 0))


def change_fetch(run_id: str, ws: dict | None, repo: str, target: str, number: Any = None) -> dict:
    """Fetch item for the change of (repo, target): its raw path, the profile hint and the `factory ingest` line.
    `number` is the change number when known (a review run); else the one of the saved contract."""
    ident = f"{repo}-{target}"
    doc = _change_doc(run_id, repo, target)
    ctx: dict = {"run": run_id, "ws": ws, "repo": repo, "scm_project": project_id(ws, repo),
                 "number": number if number is not None else doc.get("number")}
    item = needs("change", ident, ctx)
    item["ingest"] = f"factory ingest {run_id} change {item['raw_path']} --id {ident} --repo {repo}"
    return item


def pipeline_fetch(run_id: str, ws: dict | None, repo: str, target: str) -> dict:
    """Fetch item for the pipeline of a change: the pipeline raw plus its jobs raw (`--raw jobs=`)."""
    ident = f"{repo}-{target}"
    pid = _latest_pipeline_id(_change_doc(run_id, repo, target))
    ctx: dict = {"run": run_id, "ws": ws, "repo": repo, "scm_project": project_id(ws, repo),
                "id": pid if pid is not None else "<id>"}
    item = needs("pipeline", ident, ctx)
    jobs = item["raw_path"] + ".jobs"  # where the profile hint saves the jobs payload; `--raw jobs=` names it to ingest
    item["ingest"] = f"factory ingest {run_id} pipeline {item['raw_path']} --id {ident} --repo {repo} --raw jobs={jobs}"
    return item


def _contract_ok(run_id: str, kind: str, ident: str) -> bool:
    doc, problems = contracts.load_contract(run_id, kind, ident)
    return doc is not None and not problems


def readback_fetches(run_id: str, ws: dict | None) -> list[dict]:
    """Change (and, when watched, pipeline) readbacks of the delivery plan whose contract is missing, invalid or stale."""
    plan = _json(run_dir(run_id) / "ship-plan.json")
    out: list[dict] = []
    if not isinstance(plan, dict):
        return out
    watched = {(a.get("repo"), a.get("target")) for a in plan.get("actions") or [] if a.get("type") == "pipeline_watch"}
    for c in plan.get("changes") or []:
        ident = f"{c['repo']}-{c['target']}"
        if not _contract_ok(run_id, "change", ident):
            out.append(change_fetch(run_id, ws, c["repo"], c["target"]))
        if (c["repo"], c["target"]) in watched and not _contract_ok(run_id, "pipeline", ident):
            out.append(pipeline_fetch(run_id, ws, c["repo"], c["target"]))
    return out


def feedback_fetches(run_id: str, ws: dict | None) -> list[dict]:
    """Change and pipeline readbacks (contracts) of every delivered change that are missing, invalid or stale."""
    plan = _json(run_dir(run_id) / "ship-plan.json")
    out: list[dict] = []
    for c in (plan or {}).get("changes", []) if isinstance(plan, dict) else []:
        ident = f"{c['repo']}-{c['target']}"
        if not _contract_ok(run_id, "change", ident):
            out.append(change_fetch(run_id, ws, c["repo"], c["target"]))
        if not _contract_ok(run_id, "pipeline", ident):
            out.append(pipeline_fetch(run_id, ws, c["repo"], c["target"]))
    return out


# ---------- review ----------

def review_intake_fetches(run: dict, ws: dict | None) -> list[dict]:
    """Ticket fetches plus one change fetch per change of a review run whose contract is missing, invalid or stale."""
    rid = run["id"]
    out = intake_fetches(run, ws)
    for ch in run.get("changes", []):
        ident = f"{ch['repo']}-{ch['number']}"
        if not _contract_ok(rid, "change", ident):
            out.append(change_fetch(rid, ws, ch["repo"], str(ch["number"]), ch["number"]))
    return out


def start_change_fetch(ws: dict, alias: str, number: int, folder: Path) -> dict:
    """Fetch item of one change for `review start`: saved in `folder` as `<alias>-<number>.<ext>`."""
    ext = "json"
    try:
        _, prof = profiles.profile_for(ws, "change")
        ext = profiles.kind_ext(prof, "change")
    except FactoryError:
        pass
    ident = f"{alias}-{number}"
    return needs("change", ident, {"ws": ws, "repo": alias, "scm_project": project_id(ws, alias), "number": number},
                 raw=Path(folder) / f"{contracts.safe_id(ident)}.{ext}")


def queue_fetches(ws: dict, path: Path, repo: str | None = None) -> list[dict]:
    """Fetch items of the open changes list(s) of `review queue`: one per matching repo (one file per repo when several)."""
    repos = [r for r in ws.get("repos", []) if not repo or repo in (r.get("alias"), r.get("name"))]
    out = []
    for r in repos or [{}]:
        dest = path if len(repos) <= 1 else path.with_name(f"{path.stem}-{r.get('alias', 'repo')}{path.suffix}")
        ctx = {"ws": ws, "repo": r.get("alias") or "<repo>", "scm_project": r.get("scm_project") or "<scm_project>"}
        out.append(needs("change_list", Path(dest).stem, ctx, raw=Path(dest)))
    return out
