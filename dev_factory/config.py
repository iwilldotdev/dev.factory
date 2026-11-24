"""Load `factory.toml` (repo or workspace), compose member files, apply `local.toml`, resolve secrets."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from . import schema
from .util import FactoryError, fwd

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise FactoryError(f"cannot read {fwd(path)}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise FactoryError(f"{fwd(path)}: invalid TOML: {exc}") from exc


def load_file(path: Path, spec: dict = schema.SPEC) -> dict:
    """Read and validate one file; every unknown key or wrong type is listed with its key path (exit 2)."""
    raw = read_toml(path)
    errs: list[str] = []
    schema.validate(raw, spec, "", errs)
    if spec is schema.SPEC and "schema" not in raw:
        errs.append("schema: required (must be 1)")
    if errs:
        raise FactoryError(f"{fwd(path)}:\n  " + "\n  ".join(errs))
    return raw


def merge(base: dict, over: dict) -> dict:
    """Workspace entry over member entry, key by key; tables merge, lists replace, `check` merges by id."""
    out = copy.deepcopy(base)
    for key, val in over.items():
        if key == "check":
            lst = out.setdefault("check", [])
            where = {c.get("id"): i for i, c in enumerate(lst)}
            for chk in val:
                if chk.get("id") in where:
                    lst[where[chk["id"]]] = copy.deepcopy(chk)
                else:
                    lst.append(copy.deepcopy(chk))
        elif isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def _member_base(member_dir: Path, member: dict) -> dict | None:
    for entry in member.get("repo", []):
        if "path" in entry and (member_dir / entry["path"]).resolve() == member_dir:
            return entry
    return None


def load(root: Path) -> dict:
    """Effective config of the project at `root`: `repo` is the composed list, defaults filled, local applied."""
    root = Path(root).resolve()
    top = load_file(root / "factory.toml")
    local_path = root / ".factory" / "local.toml"
    local = load_file(local_path, schema.LOCAL_SPEC) if local_path.is_file() else {}
    errs: list[str] = []
    keys = set(top.get("secrets", {}).get("keys", []))
    repos, mode = [], "repo"
    for i, entry in enumerate(top.get("repo", [])):
        if "path" not in entry:
            errs.append(f"repo[{i}].path: required")
            continue
        declared = (root / entry["path"]).resolve()
        name = entry.get("name") or declared.name
        where = declared
        if "path" in local.get("repo", {}).get(name, {}):
            where = (root / local["repo"][name]["path"]).resolve()
        eff = entry
        if declared != root:
            mode = "workspace"
            member_file = where / "factory.toml"
            if member_file.is_file():
                member = load_file(member_file)
                keys |= set(member.get("secrets", {}).get("keys", []))
                base = _member_base(where, member)
                if base:
                    eff = merge(base, entry)
        eff = dict(eff)
        eff["path"] = str(where)
        eff.setdefault("name", name)
        repos.append(eff)
    cfg = {k: v for k, v in top.items() if k != "repo"}
    cfg["repo"] = repos
    cfg.setdefault("secrets", {})["keys"] = sorted(keys)
    schema.defaults(cfg, schema.SPEC)
    errs += _apply_local(cfg, local, root)
    errs += semantic(cfg)
    if errs:
        raise FactoryError(f"{fwd(root / 'factory.toml')}:\n  " + "\n  ".join(errs))
    cfg["_meta"] = {"dir": str(root), "mode": mode, "local": fwd(local_path) if local else None}
    return cfg


def _apply_local(cfg: dict, local: dict, root: Path) -> list[str]:
    errs: list[str] = []
    if "worktree_root" in local:
        cfg["worktree_root"] = str((root / local["worktree_root"]).resolve())
    by_name = {r["name"]: r for r in cfg["repo"]}
    for name, over in local.get("repo", {}).items():
        repo = by_name.get(name)
        if repo is None:
            errs.append(f"local.toml: repo.{name}: no such repo (known: {', '.join(sorted(by_name)) or 'none'})")
            continue
        if "env" in over:
            if "env" not in repo:
                errs.append(f"local.toml: repo.{name}.env: the repo declares no [repo.env]")
                continue
            for key, val in over["env"].items():
                repo["env"][key] = {**repo["env"].get(key, {}), **val} if key == "vars" else val
        if "slots" in over:
            repo["slots"] = over["slots"]
    if local.get("checks", {}).get("browser"):
        cfg["checks"].setdefault("browser", {}).update(local["checks"]["browser"])
    return errs


def _local_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and parsed.hostname in LOOPBACK


def semantic(cfg: dict) -> list[str]:
    """Rules a type check cannot see: required keys after composition, uniqueness, loopback origins."""
    errs: list[str] = []
    name = cfg.get("project", {}).get("name")
    if not name:
        errs.append("project.name: required")
    elif not schema.NAME.match(name):
        errs.append(f"project.name: {name!r} must match ^[a-z0-9][a-z0-9-]{{0,62}}$")
    if not cfg["repo"]:
        errs.append("repo: at least one [[repo]] is required")
    names = [r["name"] for r in cfg["repo"]]
    for r in cfg["repo"]:
        here = f"repo[{r['name']}]"
        if not schema.REPO_NAME.match(r["name"]):
            errs.append(f"{here}.name: must match ^[A-Za-z0-9._-]+$")
        if names.count(r["name"]) > 1:
            errs.append(f"{here}.name: duplicate repo name")
        ids = [c.get("id") for c in r.get("check", [])]
        for c in r.get("check", []):
            if not c.get("id") or not c.get("run"):
                errs.append(f"{here}.check: every check needs id and run")
            elif ids.count(c["id"]) > 1:
                errs.append(f"{here}.check: duplicate id {c['id']}")
        for m in r.get("mirror", []):
            if not (m.get("name") and m.get("branch")):
                errs.append(f"{here}.mirror: every entry needs name and branch")
        env = r.get("env")
        if env is not None:
            for key in ("up", "base_url"):
                if not env.get(key):
                    errs.append(f"{here}.env.{key}: required")
            if env.get("base_url") and not _local_url(env["base_url"]):
                errs.append(f"{here}.env.base_url: must be an http(s) URL on a loopback host")
            health = env.get("health", "/")
            if not health.startswith("/") and not _local_url(health):
                errs.append(f"{here}.env.health: must be a path starting with / or a loopback URL")
            for dep in env.get("depends_on", []):
                if dep not in names or dep == r["name"]:
                    errs.append(f"{here}.env.depends_on: {dep!r} is not another repo of this project")
    ship = cfg["ship"]
    if not str(ship.get("title") or "").strip():
        ship["title"] = "[{key}] - {summary}"  # an empty or null title falls back [L-142]
    if ship["ticket"] in ("transition", "both") and not ship.get("transition_to"):
        errs.append('ship.transition_to: required when ship.ticket is "transition" or "both"')
    if any(not (r.get("scm") and r.get("tracker")) for r in ship.get("reviewers", [])):
        errs.append("ship.reviewers: every entry needs scm and tracker identities [L-143]")
    if any(not (t.get("name") and t.get("branch")) for t in cfg.get("hotfix", {}).get("targets", [])):
        errs.append("hotfix.targets: every entry needs name and branch")
    return errs


def secrets_file(project_name: str) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dev.factory" / "secrets" / f"{project_name}.env"


def read_secrets_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            values[key.strip()] = val
    return values


def resolve_secrets(cfg: dict, keys: list[str] | None = None) -> tuple[dict[str, str], list[str]]:
    """Values for the named keys (environment first, then the user secrets file) and the names still missing [L-129]."""
    wanted = list(cfg["secrets"]["keys"] if keys is None else keys)
    filed = read_secrets_file(secrets_file(cfg["project"]["name"]))
    values = {k: os.environ.get(k) or filed.get(k) for k in wanted}
    return {k: v for k, v in values.items() if v}, [k for k, v in values.items() if not v]


def worktree_root(cfg: dict) -> Path:
    if cfg.get("worktree_root"):
        return Path(cfg["worktree_root"])
    if os.name == "nt":
        cache = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "dev.factory" / "worktrees" / cfg["project"]["name"]
