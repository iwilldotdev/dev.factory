"""Harness adapters written by `factory init --harness <id>`: generated from the installed CLI, at most 3 files and 100 lines each [L-160, L-171].
The prose lives in `adapters/*.md` (package data); `adapters.py` owns the file handling and `hook.py` the optional host hook."""
from __future__ import annotations

import os
from pathlib import Path

from . import __version__, adapters, hook
from .adapters import BEGIN, END  # noqa: F401  (the markers are part of this module's interface)
from .util import FactoryError

IDS = ("agents-md", "claude-code", "none")
FILES = {"agents-md": "AGENTS.md", "claude-code": "CLAUDE.md"}  # a host that does not read AGENTS.md gets its own file (decision 13)
SCOPES = ("project", "user")
HOME_DIR, HOME_ENV = ".claude", "CLAUDE_CONFIG_DIR"  # the host's folder inside a project, and its user home (the variable moves it)
SECTION = {"agents-md": "agents.md", "claude-code": "host.md"}  # the text of the managed block of each host file
OWNED = {"claude-code": (("skills/factory/SKILL.md", "skill.md"), ("agents/factory-worker.md", "worker.md"))}  # whole files, 3 per harness at most


def text(name: str) -> str:
    return (Path(__file__).parent / "adapters" / name).read_text(encoding="utf-8").replace("{version}", __version__)


def agents_md_section() -> str:
    return text(SECTION["agents-md"])


def home() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / HOME_DIR)


def write(root: Path, harness: str, scope: str = "project", hooks: bool = False) -> list[str]:
    """Write the adapter for `harness`: under `root` (scope project) or in the user's host home (scope user).
    Files it does not own stay untouched; the ones it changes keep a backup [L-160]."""
    if harness == "none":
        return []
    if harness not in FILES:
        raise FactoryError(f"unknown harness {harness!r}; available: {', '.join(IDS)}")
    if scope not in SCOPES:
        raise FactoryError(f"unknown scope {scope!r}; available: {', '.join(SCOPES)}")
    if (scope == "user" or hooks) and harness not in OWNED:
        raise FactoryError(f"--harness {harness} has no user scope and no hooks: it only writes {FILES[harness]} in the project")
    base = home() if scope == "user" else root / HOME_DIR  # the host's folder; its instructions file sits in it for the user and beside it in a project
    done = [adapters.put_block((base if scope == "user" else root) / FILES[harness], text(SECTION[harness]))]
    for rel, src in OWNED.get(harness, ()):
        done.append(adapters.put_owned(base / rel, text(src)))
    if hooks:
        done.append(adapters.merge_hook(base / "settings.json", "PreToolUse", hook.ENTRY, "factory hook "))
    return done
