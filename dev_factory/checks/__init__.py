"""Check runners: each takes (check, ctx) and returns the outcome fields of an evidence record."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

TYPES = ("cmd", "http", "browser", "visual", "style", "screenshot")  # runnable plan check types; `commits` is the implicit commit-format check
RUNTIME = ("http", "browser", "visual", "style", "screenshot")  # types that call a served env
RUNNER = ("browser", "visual", "style", "screenshot")  # types that run in the bundled browser runner (and can share one browser)
RANK = {"commits": 0, "cmd": 1, "http": 2, "screenshot": 3, "browser": 3, "style": 4, "visual": 5}  # cheapest first [L-11]


@dataclass
class Ctx:
    path: Path  # the worktree the check runs in
    mode: str  # verify, reproduce, red or green
    cfg: dict
    repo: dict
    secrets: list[str] = field(default_factory=list)
    branch_point: str = ""
    project: object = None  # the Project (design contracts, env state files)
    run: object = None  # the Run
    base_url: str | None = None
    state_file: Path | None = None  # login state of the repo's env, when it has a login_spec
    session: dict | None = None  # headers of an `http` check with `session`; in memory only [L-52]
    user: str | None = None  # plan login_user
    tmp: Path | None = None  # scratch directory of this attempt; its files become artifacts


def run(check: dict, ctx: Ctx) -> dict:
    from . import browser, cmd, commits, http, screenshot, style, visual
    return {"cmd": cmd, "commits": commits, "http": http, "browser": browser, "visual": visual, "style": style,
            "screenshot": screenshot}[check["type"]].run_check(check, ctx)


def build_args(check: dict, ctx: Ctx) -> list[str]:
    """Runner argv of a check that runs in the bundled browser runner (also the entry of a batch manifest)."""
    from . import browser, screenshot, style, visual
    return {"browser": browser, "visual": visual, "style": style, "screenshot": screenshot}[check["type"]].build(check, ctx)
