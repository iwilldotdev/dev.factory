"""Collector `style`: computed-style probe of a style map against the running app."""

from __future__ import annotations

import json

from factory import design
from factory.collectors import browser as b
from factory.core import Ctx


def _approx_map(map_file, check: dict, ctx: Ctx, out):
    """For an approximate design frame: a copy of the map in the evidence dir whose entries carry the approx tolerances
    (px: max(tolerance_px, limits.approx.px); color distance; font size). Exact frames use the map as is."""
    node = design.check_node(check)
    frame = design.read_frame(ctx.run_dir, node) if node else None
    if not frame or frame.get("fidelity") != "approx":
        return map_file
    lim = design.approx_limits(ctx.workspace)
    try:
        data = json.loads(map_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return map_file
    for e in data.get("entries") or []:
        if isinstance(e, dict):
            e["tolerance_px"] = max(e.get("tolerance_px") if design.is_num(e.get("tolerance_px")) else 1, lim["px"])
            e["color_delta_e"] = lim["color_delta_e"]
            e["font_size_px"] = lim["font_size_px"]
    out.mkdir(parents=True, exist_ok=True)
    derived = out / "style-map-approx.json"
    derived.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return derived


def collect(check: dict, ctx: Ctx) -> dict:
    url = b.full_url(check, ctx)
    if isinstance(url, dict):
        return url
    map_file = ctx.run_dir / check["map"]
    if not map_file.is_file():
        return b._error(f"style map not found: {check['map']}")
    adapter = b.adapter_args(check, ctx)
    if isinstance(adapter, dict):
        return adapter
    out = b.out_dir(ctx)
    map_file = _approx_map(map_file, check, ctx, out)
    budget = b.Budget(b.check_cap(check, ctx))
    argv = ["style-probe", "--url", url, "--map", str(map_file), "--out", str(out), *adapter, *budget.cli_args(check)]
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
    return b.finish(b.run_cli(argv, out, budget.cap), out, ctx)
