"""`factory workspace init|import|check` (DESIGN §2.1)."""

from __future__ import annotations

import json
from pathlib import Path

from ..core import (SCHEMA_VERSION, FactoryError, GateFailed, read_json,
                    workspace_load, workspace_path, write_json)

DEFAULT_AUTONOMY = {"ship": "ask", "max_build_loops": 3, "max_review_rounds": 2, "max_replans": 2}


def _skeleton(name: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "name": name,
        "worktree_root": "",
        "repos": [],
        "ship": {"targets": [], "draft_default": True},
        "commit": {"header_regex": None, "forbidden_trailers": []},
        "autonomy": dict(DEFAULT_AUTONOMY),
        "limits": {"max_diff_ratio": 0.03, "max_tolerance_px": 2},
        "rules": [],
        "epics": [],
    }


def _out(args, payload: dict, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(text)


def cmd_init(args) -> int:
    path = workspace_path(args.name)
    if path.exists():
        raise FactoryError(f"workspace {args.name} already exists: {path}")
    write_json(path, _skeleton(args.name))
    _out(args, {"workspace": args.name, "path": str(path)}, f"created {path}")
    return 0


def convert_profile(name: str, profile: dict) -> dict:
    ws = _skeleton(name)
    repos = profile.get("repos") or []
    if isinstance(repos, dict):
        repos = [dict(v, alias=k) for k, v in repos.items()]
    for idx, repo in enumerate(repos, start=1):
        if not isinstance(repo, dict) or not repo.get("alias"):
            raise FactoryError(f"project.json repos[{idx - 1}] needs an alias")
        ws["repos"].append({
            "alias": repo["alias"],
            "name": repo.get("name") or repo["alias"],
            "order": repo.get("order", idx),
            "gitlab_project": repo.get("gitlab_project"),
            "path": "",
            "quality": {},
            "env": {},
        })
    if isinstance(profile.get("ship"), dict):
        ws["ship"].update(profile["ship"])
    if isinstance(profile.get("commit"), dict):
        ws["commit"].update(profile["commit"])
    if isinstance(profile.get("epics"), list):
        ws["epics"] = profile["epics"]
    return ws


def cmd_import(args) -> int:
    src = Path(args.src)
    profile = read_json(src)
    if not isinstance(profile, dict):
        raise FactoryError(f"{src}: expected a JSON object")
    path = workspace_path(args.name)
    if path.exists() and not args.force:
        raise FactoryError(f"workspace {args.name} already exists (use --force)")
    ws = convert_profile(args.name, profile)
    write_json(path, ws)
    _out(args, {"workspace": args.name, "path": str(path), "repos": len(ws["repos"])},
         f"imported {len(ws['repos'])} repo(s) into {path}\n"
         f"fill in worktree_root and per-repo path/quality/env, then run: factory workspace check {args.name}")
    return 0


BRANCH_PLACEHOLDERS = {"key", "slug", "target", "target_upper", "package"}


def hotfix_config(ws: dict) -> dict | None:
    """ship.hotfix, or the default (homolog + mirror pack) when a target named homolog exists."""
    ship = ws.get("ship") or {}
    if isinstance(ship.get("hotfix"), dict):
        return ship["hotfix"]
    if "homolog" in (ship.get("targets") or []):
        return {"base": "homolog", "mirror": ["pack"]}
    return None


def hotfix_config_errors(ws: dict) -> list[str]:
    import string
    errors = []
    ship = ws.get("ship") or {}
    hf = ship.get("hotfix")
    if hf is not None:
        if not isinstance(hf, dict) or not hf.get("base") or not isinstance(hf.get("mirror", []), list):
            errors.append("ship.hotfix must be {base: <target>, mirror: [<target>, ...]}")
        else:
            targets = ship.get("targets") or []
            for t in [hf["base"], *hf.get("mirror", [])]:
                if t not in targets:
                    errors.append(f"ship.hotfix target {t!r} is not in ship.targets")
            if hf["base"] in hf.get("mirror", []):
                errors.append("ship.hotfix.mirror must not contain the base target")
    fmt = ws.get("branch_format")
    if fmt is not None:
        if not isinstance(fmt, dict):
            errors.append("branch_format must be an object {feature, bug, hotfix}")
        else:
            for key, text in fmt.items():
                if key not in ("feature", "bug", "hotfix"):
                    errors.append(f"branch_format.{key}: unknown key (feature, bug, hotfix)")
                    continue
                try:
                    names = {n for _, n, _, _ in string.Formatter().parse(str(text)) if n}
                except ValueError as err:
                    errors.append(f"branch_format.{key}: {err}")
                    continue
                bad = names - BRANCH_PLACEHOLDERS
                if bad:
                    errors.append(f"branch_format.{key}: unknown placeholder(s) {sorted(bad)}")
    return errors


def orchestration_config_errors(ws: dict) -> list[str]:
    """Optional `orchestration` (parallel sessions) and workspace-level `env` (lease) blocks."""
    errors: list[str] = []
    orch = ws.get("orchestration")
    if orch is not None:
        if not isinstance(orch, dict):
            errors.append("orchestration must be an object {sessions, per}")
        else:
            unknown = set(orch) - {"sessions", "per"}
            if unknown:
                errors.append(f"orchestration: unknown key(s) {sorted(unknown)} (sessions, per)")
            if not isinstance(orch.get("sessions", False), bool):
                errors.append("orchestration.sessions must be true or false")
            if orch.get("per", "ticket") not in ("ticket", "run"):
                errors.append("orchestration.per must be ticket or run")
    env = ws.get("env")
    if env is not None:
        if not isinstance(env, dict):
            errors.append("env must be an object {lease, lease_ttl_s}")
        else:
            unknown = set(env) - {"lease", "lease_ttl_s"}
            if unknown:
                errors.append(f"env: unknown key(s) {sorted(unknown)} (lease, lease_ttl_s)")
            if not isinstance(env.get("lease", False), bool):
                errors.append("env.lease must be true or false")
            ttl = env.get("lease_ttl_s", 900)
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 60:
                errors.append("env.lease_ttl_s must be an integer >= 60")
    return errors


def check_workspace(ws: dict) -> list[str]:
    missing: list[str] = []
    root = ws.get("worktree_root")
    if not root:
        missing.append("worktree_root is empty")
    elif not Path(root).parent.exists():
        missing.append(f"worktree_root parent does not exist: {root}")
    if not ws.get("repos"):
        missing.append("no repos defined")
    seen = set()
    for repo in ws.get("repos", []):
        alias = repo.get("alias") or "?"
        if alias in seen:
            missing.append(f"duplicate repo alias {alias}")
        seen.add(alias)
        path = repo.get("path")
        if not path:
            missing.append(f"{alias}: path is missing")
        elif not (Path(path) / ".git").exists():
            missing.append(f"{alias}: path is not a git repository: {path}")
        if not repo.get("quality"):
            missing.append(f"{alias}: quality commands are missing")
        if "ui" in repo and not isinstance(repo["ui"], bool):
            missing.append(f"{alias}: ui must be true or false (UI repo: visual-evidence question)")
        env = repo.get("env") or {}
        if not env:
            missing.append(f"{alias}: env block is missing")
        else:
            for key in ("start", "health", "base_url"):
                if not env.get(key):
                    missing.append(f"{alias}: env.{key} is missing")
    autonomy = ws.get("autonomy") or {}
    if autonomy.get("ship", "ask") not in ("ask", "draft-auto"):
        missing.append("autonomy.ship must be ask or draft-auto")
    missing.extend(hotfix_config_errors(ws))
    missing.extend(orchestration_config_errors(ws))
    for rule in ws.get("rules", []):
        if not rule.get("id") or not isinstance(rule.get("check"), dict):
            missing.append(f"rule needs id and check: {rule}")
    return missing


def cmd_check(args) -> int:
    ws = workspace_load(args.name)
    missing = check_workspace(ws)
    if missing:
        raise GateFailed(missing)
    _out(args, {"workspace": args.name, "ok": True}, f"OK workspace {args.name}")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("workspace", help="create, import and validate workspace.json")
    ws_sub = parser.add_subparsers(dest="ws_cmd", required=True)
    p = ws_sub.add_parser("init", help="create an empty workspace")
    p.add_argument("name")
    p.set_defaults(func=cmd_init)
    p = ws_sub.add_parser("import", help="convert a dev.harness .dev/project.json")
    p.add_argument("name")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_import)
    p = ws_sub.add_parser("check", help="report missing configuration")
    p.add_argument("name")
    p.set_defaults(func=cmd_check)
