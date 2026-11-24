"""`screenshot` checks: an image artifact only (before on base, after at HEAD); it never satisfies an AC [L-56]."""
from __future__ import annotations

from ..util import fwd
from . import ui


def build(check: dict, ctx) -> list[str]:
    if check.get("spec"):
        return ["screenshot", "--spec", fwd(ctx.run.dir / check["spec"])] + ui.base_args(check, ctx) + ui.page_args(check, ctx)
    return ["screenshot"] + ui.base_args(check, ctx) + ui.page_args(check, ctx)


def run_check(check: dict, ctx) -> dict:
    return ui.run(check, ctx, build, judge=False)
