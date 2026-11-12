"""Shared primitives for the factory CLI. Python >= 3.10, stdlib only.

Every command module and collector imports from here; keep this API stable.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
SCHEMA_VERSION = 1


class FactoryError(Exception):
    """Usage or configuration error (exit 2)."""


class GateFailed(Exception):
    """A gate or check failed (exit 1). `reasons` lists what is missing."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# ---------- paths ----------

def factory_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".factory").expanduser()


def workspace_path(ws: str) -> Path:
    return factory_home() / "workspaces" / ws / "workspace.json"


def run_dir(run_id: str) -> Path:
    return factory_home() / "runs" / run_id


def secrets_path(ws: str) -> Path:
    return factory_home() / "secrets" / f"{ws}.env"


def node_file(node_id: str) -> str:
    """Figma node id '3544:5710' -> '3544-5710' (pack file stem)."""
    return node_id.replace(":", "-")


# ---------- time / json ----------

def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise FactoryError(f"missing file: {path}") from err
    except json.JSONDecodeError as err:
        raise FactoryError(f"invalid JSON in {path}: {err}") from err


def write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---------- workspace ----------

def workspace_load(ws: str) -> dict:
    data = read_json(workspace_path(ws))
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise FactoryError(f"workspace {ws}: schemaVersion must be {SCHEMA_VERSION}")
    return data


def repo_conf(workspace: dict, alias: str) -> dict:
    for repo in workspace.get("repos", []):
        if repo.get("alias") == alias or repo.get("name") == alias:
            return repo
    raise FactoryError(f"repo {alias!r} not in workspace {workspace.get('name')}")


# ---------- runs ----------

def run_load(run_id: str) -> dict:
    data = read_json(run_dir(run_id) / "run.json")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise FactoryError(f"run {run_id}: schemaVersion must be {SCHEMA_VERSION}")
    return data


def run_save(run: dict) -> None:
    run["updated"] = now()
    write_json(run_dir(run["id"]) / "run.json", run)


def event(run: dict, kind: str, **data: Any) -> None:
    """Append an event to a loaded run (caller saves)."""
    run.setdefault("events", []).append({"at": now(), "kind": kind, "data": data})


def repo_path_for_run(run: dict, workspace: dict, alias: str) -> Path:
    """Worktree of the run when recorded, else the repo's main path."""
    rec = run.get("repos", {}).get(alias) or {}
    if rec.get("worktree"):
        return Path(rec["worktree"])
    return Path(repo_conf(workspace, alias)["path"])


# ---------- git ----------

def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise FactoryError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def git_dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain", "--untracked-files=no"))


# ---------- evidence / reviews ----------

@dataclass
class Ctx:
    """What a collector receives besides its check."""

    run: dict
    workspace: dict
    run_dir: Path
    repo_path: Path | None = None
    env: dict = field(default_factory=dict)  # repo env block from workspace
    out_dir: Path | None = None  # evidence/<check-id>/ for artifacts


def write_evidence(run_id: str, check: dict, body: dict, repo_path: Path | None, started: str, duration_s: float) -> dict:
    """Only `prove` calls this. Fills common fields and writes evidence/<check-id>.json."""
    record = {
        "schemaVersion": SCHEMA_VERSION,
        "check_id": check["id"],
        "type": check["type"],
        "status": body.get("status", "error"),
        "repo": check.get("repo"),
        "git_sha": git_head(repo_path) if repo_path else None,
        "dirty": git_dirty(repo_path) if repo_path else False,
        "collector": f"factory/{VERSION} {check['type']}",
        "started": started,
        "duration_s": round(duration_s, 2),
        "summary": body.get("summary", ""),
        "artifacts": body.get("artifacts", []),
        "details": body.get("details", {}),
    }
    if record["status"] not in ("pass", "fail", "error"):
        record["status"] = "error"
    write_json(run_dir(run_id) / "evidence" / f"{check['id']}.json", record)
    return record


def load_evidence(run_id: str, check_id: str) -> dict | None:
    path = run_dir(run_id) / "evidence" / f"{check_id}.json"
    return read_json(path) if path.is_file() else None
