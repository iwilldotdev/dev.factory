"""Collector `visual`: capture the running app, then pixel-compare against the design frame image."""

from __future__ import annotations

import json

from factory import design, pngcrop
from factory.collectors import browser as b
from factory.core import Ctx


def collect(check: dict, ctx: Ctx) -> dict:
    url = b.full_url(check, ctx)
    if isinstance(url, dict):
        return url
    node = design.check_node(check)
    frame = design.read_frame(ctx.run_dir, node) if node else None
    image = design.frame_image(ctx.run_dir, frame)
    if image is None:
        return b._error(f"design image missing for frame {node!r}: no design_frame contract with an existing frame.image "
                        "(factory design check)")
    adapter = b.adapter_args(check, ctx)
    if isinstance(adapter, dict):
        return adapter
    out = b.out_dir(ctx)
    vp = check.get("viewport") or [1440, 900]
    budget = b.Budget(b.check_cap(check, ctx))

    cap = ["capture", "--url", url, "--out", str(out), "--viewport", f"{vp[0]}x{vp[1]}", *budget.cli_args(check)]
    for flag, key in (("--selector", "selector"), ("--wait-for", "wait_for")):
        if check.get(key):
            cap += [flag, check[key]]
    if check.get("settle_ms") is not None:
        cap += ["--settle-ms", str(check["settle_ms"])]
    cap += adapter
    state = b.storage_state(check, ctx)
    if isinstance(state, dict):
        return state
    if state:
        cap += ["--storage-state", state]
    captured = b.run_cli(cap, out, budget.remaining())
    if captured["status"] != "pass":
        return b.finish(captured, out, ctx)

    image = pngcrop.cropped_design(ctx.run_dir, frame, out) or image
    cmp = ["compare", "--design", str(image), "--app", str(out / "app.png"), "--out", str(out)]
    if check.get("max_diff_ratio") is not None:
        cmp += ["--max-diff-ratio", str(check["max_diff_ratio"])]
    if check.get("masks"):
        masks_file = out / "masks.json"
        masks_file.write_text(json.dumps(check["masks"]), encoding="utf-8")
        cmp += ["--masks", str(masks_file)]
    if check.get("copies"):
        cmp += ["--copies", "|".join(check["copies"])]
    if check.get("fit"):
        cmp += ["--fit"]
    return b.finish(b.run_cli(cmp, out, budget.remaining()), out, ctx)
