"""Ready-to-use dev.mcp calls for files the CLI is missing.

The CLI never calls the network. When a gate or command needs a remote file under
`runs/<id>/`, it prints the exact dev.mcp call (tool + args, `save_to` absolute with
forward slashes, `overwrite: true`) so an agent runs it as-is and the payload never
passes through the model. Unknown values stay as `<placeholders>`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import abs_path, dedupe_fetch, fetch_item, node_file, repo_conf, run_dir

PNG_SIG = b"\x89PNG\r\n\x1a\n"
FILE_KEY = "<fileKey>"


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save(run_id: str, rel: str) -> str:
    return abs_path(run_dir(run_id) / rel)


# ---------- jira ----------

def issue_fetch(run_id: str, key: str) -> dict:
    return fetch_item("jira_get_issue", issueIdOrKey=key, save_to=_save(run_id, f"remote/issue-{key}.json"), overwrite=True)


def intake_fetches(run: dict) -> list[dict]:
    rid = run["id"]
    return [issue_fetch(rid, t["key"]) for t in run.get("tickets", [])
            if not (run_dir(rid) / "remote" / f"issue-{t['key']}.json").is_file()]


# ---------- figma ----------

def _unwrap_mr(data: Any) -> Any:
    if isinstance(data, dict) and isinstance(data.get("merge_request"), dict):
        return data["merge_request"]
    return data


def known_file_keys(run_id: str) -> list[str]:
    """fileKeys already seen in acs checks, figma/meta*.json, or saved payloads."""
    keys: list[str] = []
    rd = run_dir(run_id)
    acs = _json(rd / "acs.json")
    for chk in (acs or {}).get("checks", []) if isinstance(acs, dict) else []:
        if isinstance(chk, dict) and chk.get("fileKey"):
            keys.append(str(chk["fileKey"]))
    fdir = rd / "figma"
    if fdir.is_dir():
        for p in sorted(fdir.glob("*.json")):
            data = _json(p)
            if p.name == "meta.json" and isinstance(data, dict):
                files = data.get("files") if isinstance(data.get("files"), dict) else data
                if data.get("fileKey"):
                    keys.append(str(data["fileKey"]))
                keys += [k for k, v in files.items() if isinstance(v, dict)]
            elif isinstance(data, dict) and data.get("fileKey"):
                keys.append(str(data["fileKey"]))
    return list(dict.fromkeys(keys))


def _node_keys(run_id: str) -> dict[str, str]:
    acs = _json(run_dir(run_id) / "acs.json")
    out: dict[str, str] = {}
    for chk in (acs or {}).get("checks", []) if isinstance(acs, dict) else []:
        if isinstance(chk, dict) and chk.get("node") and chk.get("fileKey"):
            out[chk["node"]] = str(chk["fileKey"])
    return out


def figma_fetches(run_id: str) -> list[dict]:
    """Calls for every missing/invalid piece of the Figma pack of the run's acs.json."""
    rd = run_dir(run_id)
    acs = _json(rd / "acs.json")
    nodes: list[str] = []
    if isinstance(acs, dict):
        for chk in acs.get("checks", []):
            if isinstance(chk, dict) and chk.get("node") and chk["node"] not in nodes:
                nodes.append(chk["node"])
    keys = known_file_keys(run_id)
    per_node = _node_keys(run_id)
    default_key = keys[0] if len(keys) == 1 else FILE_KEY
    fdir = rd / "figma"
    out: list[dict] = []
    for node in nodes:
        key = per_node.get(node, default_key)
        stem = node_file(node)
        jpath, ppath = fdir / f"{stem}.json", fdir / f"{stem}.png"
        if _json(jpath) is None:
            out.append(fetch_item("figma_get_design_context", fileKey=key, nodeIds=node, includeScreenshot=False,
                                  save_to=_save(run_id, f"figma/{stem}.json"), overwrite=True))
        try:
            png_ok = ppath.read_bytes()[:8] == PNG_SIG
        except OSError:
            png_ok = False
        if not png_ok:
            out.append(fetch_item("figma_fetch_screenshot", fileKey=key, nodeIds=node, scale=1, format="png",
                                  save_to=_save(run_id, f"figma/{stem}.png"), overwrite=True))
    if nodes:
        meta_keys = keys or [FILE_KEY]
        have = {p.name for p in fdir.glob("meta*.json")} if fdir.is_dir() else set()
        if not have or not _meta_ok(fdir, set(keys)):
            for i, key in enumerate(meta_keys):
                name = "meta.json" if len(meta_keys) == 1 else f"meta-{key}.json"
                out.append(fetch_item("figma_get_file_meta", fileKey=key, save_to=_save(run_id, f"figma/{name}"),
                                      overwrite=True))
    return dedupe_fetch(out)


def _meta_ok(fdir: Path, needed: set[str]) -> bool:
    """True when figma/meta*.json name a revision (lastModified/version) for every needed fileKey."""
    have: set[str] = set()
    for p in fdir.glob("meta*.json"):
        data = _json(p)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            data = data["files"]
        entries = [data] if isinstance(data, dict) and "fileKey" in data else             [{"fileKey": k, **v} for k, v in (data or {}).items() if isinstance(v, dict)] if isinstance(data, dict) else []
        have |= {str(e["fileKey"]) for e in entries if e.get("fileKey") and (e.get("lastModified") or e.get("version"))}
    return bool(have) and needed <= have


# ---------- gitlab ----------

def project_id(ws: dict | None, alias: str) -> str:
    try:
        return str(repo_conf(ws or {}, alias).get("gitlab_project") or "<projectId>")
    except Exception:
        return "<projectId>"


def mr_iid(run_id: str, alias: str, target: str) -> Any:
    data = _unwrap_mr(_json(run_dir(run_id) / "remote" / f"mr-{alias}-{target}.json"))
    return data.get("iid") if isinstance(data, dict) and data.get("iid") else "<iid>"


def mr_fetch(run_id: str, ws: dict | None, alias: str, iid: Any, name: str | None = None, **extra: Any) -> dict:
    return fetch_item("gitlab_get_merge_request", projectId=project_id(ws, alias), mergeRequestIid=iid, **extra,
                      save_to=_save(run_id, f"remote/{name or f'mr-{alias}-{iid}.json'}"), overwrite=True)


def review_intake_fetches(run: dict, ws: dict | None) -> list[dict]:
    rid = run["id"]
    out = intake_fetches(run)
    for mr in run.get("mrs", []):
        if not (run_dir(rid) / "remote" / f"mr-{mr['repo']}-{mr['iid']}.json").is_file():
            out.append(mr_fetch(rid, ws, mr["repo"], mr["iid"]))
    return out


def readback_fetches(run_id: str, ws: dict | None) -> list[dict]:
    """Missing or unreadable ship readbacks (ship verify)."""
    plan = _json(run_dir(run_id) / "ship-plan.json")
    out = []
    for mr in (plan or {}).get("mrs", []) if isinstance(plan, dict) else []:
        rel = mr.get("readback", f"remote/mr-{mr['repo']}-{mr['target']}.json")
        if _json(run_dir(run_id) / rel) is None:
            out.append(fetch_item("gitlab_get_merge_request", projectId=project_id(ws, mr["repo"]),
                                  mergeRequestIid="<iid>", save_to=_save(run_id, rel), overwrite=True))
    return out


def feedback_fetches(run_id: str, ws: dict | None) -> list[dict]:
    """Missing discussions / pipeline / jobs files for every shipped MR."""
    rd = run_dir(run_id)
    plan = _json(rd / "ship-plan.json")
    out: list[dict] = []
    for mr in (plan or {}).get("mrs", []) if isinstance(plan, dict) else []:
        alias, target = mr["repo"], mr["target"]
        tag = f"{alias}-{target}"
        iid = mr_iid(run_id, alias, target)
        disc = rd / "remote" / f"mr-{tag}-discussions.json"
        if _json(disc) is None:
            out.append(mr_fetch(run_id, ws, alias, iid, f"mr-{tag}-discussions.json",
                                includeNotes=True, includePipelines=True))
        pipe_id: Any = "<pipelineId>"
        pipes = _unwrap_pipelines(_json(disc))
        if pipes:
            pipe_id = pipes[0].get("id", pipe_id)
        pid = project_id(ws, alias)
        if _json(rd / "remote" / f"pipeline-{tag}.json") is None:
            out.append(fetch_item("gitlab_get_pipeline", projectId=pid, pipelineId=pipe_id,
                                  save_to=_save(run_id, f"remote/pipeline-{tag}.json"), overwrite=True))
        if _json(rd / "remote" / f"pipeline-{tag}-jobs.json") is None:
            out.append(fetch_item("gitlab_get_pipeline_jobs", projectId=pid, pipelineId=pipe_id,
                                  save_to=_save(run_id, f"remote/pipeline-{tag}-jobs.json"), overwrite=True))
    return out


def _unwrap_pipelines(data: Any) -> list[dict]:
    pipes = data.get("pipelines") if isinstance(data, dict) else None
    return [p for p in pipes if isinstance(p, dict)] if isinstance(pipes, list) else []


# ---------- review start / queue ----------

def start_payload_fetch(ws: dict, alias: str, iid: int, folder: Path) -> dict:
    return fetch_item("gitlab_get_merge_request", projectId=project_id(ws, alias), mergeRequestIid=iid,
                      save_to=abs_path(folder / f"mr-{alias}-{iid}.json"), overwrite=True)


def queue_fetches(ws: dict, path: Path, repo: str | None = None) -> list[dict]:
    repos = [r for r in ws.get("repos", []) if not repo or repo in (r.get("alias"), r.get("name"))]
    out = []
    for r in repos or [{}]:
        dest = path if len(repos) <= 1 else path.with_name(f"{path.stem}-{r.get('alias', 'repo')}{path.suffix}")
        out.append(fetch_item("gitlab_list_merge_requests", projectId=r.get("gitlab_project") or "<projectId>",
                              state="opened", save_to=abs_path(dest), overwrite=True))
    return out
