"""`factory init --import <file>`: convert a v0 workspace file or legacy project file; list what could not map."""
from __future__ import annotations

import os
import shlex

REPO_MAPPED = {"alias", "name", "path", "quality", "env", "order"}
ENV_MAP = {"start": "up", "stop": "down", "health": "health", "base_url": "base_url",
           "warmup": "warmup", "env_vars": "vars", "shareable": "shareable"}


def _argv(value) -> str | None:
    if isinstance(value, list) and value and all(isinstance(a, str) for a in value):
        return shlex.join(value)
    return value if isinstance(value, str) and value else None


def _repo(src: dict, root, commit_pattern, lost: list[str]) -> dict:
    alias = src.get("alias") or src.get("name") or "repo"
    out: dict = {"name": alias}
    if src.get("path"):
        try:
            out["path"] = os.path.relpath(src["path"], root).replace("\\", "/")
        except ValueError:
            out["path"] = str(src["path"]).replace("\\", "/")
            lost.append(f"repo {alias}: path is on another drive, so it stayed absolute; use a relative path or local.toml")
    else:
        out["path"] = f"../{alias}"
        lost.append(f"repo {alias}: the source has no path; edit path in factory.toml")
    lost.append(f"repo {alias}: base branch is not in the source; set base (defaults to main)")
    if commit_pattern:
        out["commit_pattern"] = commit_pattern
    for name, cmd in (src.get("quality") or {}).items():
        if isinstance(cmd, str) and cmd:
            out.setdefault("check", []).append({"id": name, "run": cmd})
        else:
            lost.append(f"repo {alias}: quality.{name} is not a command string")
    env, mapped = src.get("env") or {}, {}
    for key, dest in ENV_MAP.items():
        if env.get(key) not in (None, "", [], {}):
            mapped[dest] = env[key]
    if env.get("files_from_main"):
        out["files_from_main"] = env["files_from_main"]
    seed = env.get("seed") or {}
    for key, dest in (("cmd", "seed"), ("reset", "reset")):
        if _argv(seed.get(key)):
            mapped[dest] = _argv(seed[key])
    pre = [_argv(p.get("cmd")) for p in env.get("preflight") or [] if isinstance(p, dict) and p.get("cmd")]
    if pre:
        mapped["preflight"] = [p for p in pre if p]
    if mapped:
        out["env"] = mapped
    used = set(ENV_MAP) | {"seed", "preflight", "files_from_main"}
    for key in sorted(set(env) - used):
        lost.append(f"repo {alias}: env.{key} has no v1 equivalent (see docs/parity-map.md)")
    if any(isinstance(p, dict) and "request" in p for p in env.get("preflight") or []):
        lost.append(f"repo {alias}: env.preflight request entries are not commands; rewrite them as commands")
    for key in sorted(set(src) - REPO_MAPPED):
        lost.append(f"repo {alias}: {key} was not mapped")
    return out


def convert(src: dict, root, name: str) -> tuple[dict, dict, list[str]]:
    """Return (factory.toml document, local.toml document, unmapped notes)."""
    lost: list[str] = []
    repos = src.get("repos") or []
    if isinstance(repos, dict):
        repos = [dict(v, alias=k) for k, v in repos.items()]
    commit = src.get("commit") or {}
    doc: dict = {"schema": 1, "project": {"name": name},
                 "repo": [_repo(r, root, commit.get("header_regex"), lost) for r in repos if isinstance(r, dict)]}
    ship, auto, lim = src.get("ship") or {}, src.get("autonomy") or {}, src.get("limits") or {}
    if "draft_default" in ship:
        doc.setdefault("ship", {})["draft"] = bool(ship["draft_default"])
    mode = str(ship.get("ticket_mode") or "")
    if mode:
        doc.setdefault("ship", {})["ticket"] = "transition" if "transition" in mode else "comment" if "comment" in mode else "none"
        if "transition" in mode:
            lost.append("ship.ticket_mode transition: set ship.transition_to")
    if auto.get("ship") in ("ask", "draft-auto"):
        doc["autonomy"] = {"ship": auto["ship"]}
    if "max_diff_ratio" in lim:
        doc.setdefault("limits", {})["max_diff_ratio"] = lim["max_diff_ratio"]
    if "max_tolerance_px" in lim:
        doc.setdefault("limits", {})["tolerance_px"] = lim["max_tolerance_px"]
    if isinstance(lim.get("approx"), dict):
        doc.setdefault("limits", {})["approx"] = lim["approx"]
    local = {"worktree_root": src["worktree_root"].replace("\\", "/")} if src.get("worktree_root") else {}
    consumed = {"schemaVersion", "name", "repos", "ship", "commit", "autonomy", "limits", "worktree_root"}
    for key in sorted(set(src) - consumed):
        lost.append(f"{key} was not mapped")
    for key in sorted(set(ship) - {"draft_default", "ticket_mode"}):
        lost.append(f"ship.{key} was not mapped")
    for key in sorted(set(commit) - {"header_regex"}):
        lost.append(f"commit.{key} was not mapped (trailer rules are not part of factory.toml)")
    for key in sorted(set(auto) - {"ship"}):
        lost.append(f"autonomy.{key} was not mapped")
    lost.append("secrets: list the key names under [secrets] keys; values are never imported")
    return doc, local, lost
