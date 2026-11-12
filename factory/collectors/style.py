"""Collector `style`: computed-style probe of a style map against the running app."""

from __future__ import annotations

from factory.collectors import browser as b
from factory.core import Ctx


def collect(check: dict, ctx: Ctx) -> dict:
    url = b.full_url(check, ctx)
    if isinstance(url, dict):
        return url
    map_file = ctx.run_dir / check["map"]
    if not map_file.is_file():
        return b._error(f"style map not found: {check['map']}")
    out = b.out_dir(ctx)
    argv = ["style-probe", "--url", url, "--map", str(map_file), "--out", str(out)]
    vp = check.get("viewport")
    if vp:
        argv += ["--viewport", f"{vp[0]}x{vp[1]}"]
    if check.get("wait_for"):
        argv += ["--wait-for", check["wait_for"]]
    state = b.storage_state(check, ctx)
    if isinstance(state, dict):
        return state
    if state:
        argv += ["--storage-state", state]
    return b.finish(b.run_cli(argv, out), out, ctx)
