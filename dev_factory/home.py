"""Workspaces in the user's home (`FACTORY_HOME`, default `~/.factory`): layout, matching a repository, the ticket key."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .util import FactoryError, fwd

LAYOUT = ("scripts", "login", "runs", "worktrees")
TICKET = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
class NoWorkspace(FactoryError):
    """No `--workspace`, no factory.toml upward and no home workspace lists this repository: the setup interview starts."""


SECRETS_HEAD = "# Secret values of this workspace, KEY=value per line. Never commit or paste them in a chat.\n"


def factory_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".factory").resolve()


def is_home(root: Path) -> bool:
    """A workspace folder directly under FACTORY_HOME (its state lives in the folder itself, not in `.factory/`)."""
    return Path(root).resolve().parent == factory_home()


def state_dir(root: Path) -> Path:
    return Path(root) if is_home(root) else Path(root) / ".factory"


def names() -> list[str]:
    home = factory_home()
    return sorted(p.name for p in home.iterdir() if (p / "factory.toml").is_file()) if home.is_dir() else []


def create(name: str) -> Path:
    """Make the workspace folder with its layout; the name is the folder (unique by construction)."""
    from .schema import NAME
    if not NAME.match(name or ""):
        raise FactoryError(f"workspace name {name!r} must match ^[a-z0-9][a-z0-9-]{{0,62}}$")
    ws = factory_home() / name
    for sub in LAYOUT:
        (ws / sub).mkdir(parents=True, exist_ok=True)
    if not (ws / "secrets.env").exists():
        (ws / "secrets.env").write_text(SECRETS_HEAD, encoding="utf-8")
    return ws


def unique_name(base: str) -> str:
    from .project import _slug
    base, taken = _slug(base, lower=True), set(names())
    if base not in taken:
        return base
    return next(f"{base}-{i}" for i in range(2, 1000) if f"{base}-{i}" not in taken)


def norm_remote(url: str) -> str:
    """`git@host:owner/repo.git`, `ssh://git@host/owner/repo` and `https://HOST/owner/repo/` compare equal."""
    url = (url or "").strip()
    if "://" in url:
        parts = urlsplit(url)
        host, path = (parts.hostname or ""), parts.path
    elif re.match(r"^[^/:]+:[^/]", url) and not re.match(r"^[A-Za-z]:[/\\]", url):
        host, path = url.split(":", 1)
        host = host.rsplit("@", 1)[-1]
    else:
        return fwd(url).rstrip("/").lower() if os.name == "nt" else fwd(url).rstrip("/")
    path = path.strip("/")
    path = path[:-4] if path.endswith(".git") else path
    return f"{host.lower()}/{path.strip('/')}"


def clean_url(url: str) -> str:
    """A remote URL as recorded in the config: user names, passwords and tokens of `scheme://` URLs are dropped."""
    if "://" not in url:
        return url
    parts = urlsplit(url)
    host = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{host}{parts.path}"


def save(root: Path, doc: dict) -> None:
    """Write a workspace factory.toml that the schema and the semantic rules accept; the old text stays on refusal."""
    from . import config, schema, tomlw
    errs: list[str] = []
    schema.validate(doc, schema.SPEC, "", errs)
    if errs:
        raise FactoryError("refused, the config would be invalid:\n  " + "\n  ".join(errs))
    path = Path(root) / "factory.toml"
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    path.write_text(tomlw.dumps(doc), encoding="utf-8")
    try:
        config.load(root)
    except FactoryError:
        if old is None:
            path.unlink()
        else:
            path.write_text(old, encoding="utf-8")
        raise


def _same_path(a, b) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def here(start: Path) -> tuple[Path | None, list[str]]:
    """Git toplevel and remote URLs of the repository at `start` (nothing when it is not a repository)."""
    from . import git
    res = git.git(start, "rev-parse", "--show-toplevel", check=False, timeout=30)
    if res.exit != 0 or not res.out.strip():
        return None, []
    top = Path(res.out.strip()).resolve()
    out = git.git(top, "remote", "-v", check=False, timeout=30).out
    return top, sorted({line.split()[1] for line in out.splitlines() if len(line.split()) >= 2})


def _remote_urls(entry: dict) -> list[str]:
    val = entry.get("remote_url") or []
    return [val] if isinstance(val, str) else list(val)


def matches(top: Path | None, remotes: list[str]) -> list[tuple[str, str | None]]:
    """(workspace, repo matched by remote only) for every home workspace that lists this repository."""
    from .config import read_toml
    if top is None:
        return []
    mine = {norm_remote(u) for u in remotes}
    found = []
    for name in names():
        ws = factory_home() / name
        try:
            repos = read_toml(ws / "factory.toml").get("repo", [])
        except FactoryError:
            continue
        for r in repos if isinstance(repos, list) else []:
            if isinstance(r, dict) and r.get("path") and _same_path(ws / r["path"], top):
                found.append((name, None))
                break
            if isinstance(r, dict) and mine & {norm_remote(u) for u in _remote_urls(r)}:
                found.append((name, r.get("name") or Path(r.get("path", "")).name))
                break
    return found


def choose(found: list, ask=None) -> tuple[str, str | None]:
    """One match wins; several are an ambiguity: a prompt in a terminal, else an error that lists them."""
    if len(found) == 1:
        return found[0]
    listed = ", ".join(n for n, _ in found)
    if ask is None:
        raise FactoryError(f"this repository belongs to several workspaces ({listed}): pass --workspace NAME")
    pick = ask(f"This repository belongs to several workspaces ({listed}). Which one? ").strip()
    for item in found:
        if item[0] == pick:
            return item
    raise FactoryError(f"{pick!r} is not one of {listed}; pass --workspace NAME")


def ticket_key(value: str) -> str:
    """A key as given, or the last path segment of a URL that looks like a key; else the whole value."""
    value = (value or "").strip()
    if "://" in value:
        segs = [s for s in urlsplit(value).path.split("/") if TICKET.match(s)]
        if segs:
            return segs[-1]
    return value
