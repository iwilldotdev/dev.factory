"""Check runners: each takes (check, ctx) and returns the outcome fields of an evidence record."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

TYPES = ("cmd",)  # runnable plan check types in this build; `commits` is the implicit commit-format check
RANK = {"commits": 0, "cmd": 1, "http": 2, "browser": 3, "style": 4, "visual": 5}  # cheapest first [L-11]


@dataclass
class Ctx:
    path: Path  # the worktree the check runs in
    mode: str  # verify, reproduce, red or green
    cfg: dict
    repo: dict
    secrets: list[str] = field(default_factory=list)
    branch_point: str = ""


def run(check: dict, ctx: Ctx) -> dict:
    from . import cmd, commits
    return {"cmd": cmd, "commits": commits}[check["type"]].run_check(check, ctx)
