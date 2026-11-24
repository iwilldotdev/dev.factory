"""`visual` checks: the app capture against the design PNG cropped to the node box; the runner renders and diffs [L-51, L-54, L-55, L-56]."""
from __future__ import annotations

from .. import designflow
from ..util import fwd
from . import ui


def build(check: dict, ctx) -> list[str]:
    inp = designflow.visual_inputs(ctx.run, ctx.cfg, check)
    args = ["visual", "--design", fwd(inp["design"]), "--crop", ",".join(str(v) for v in inp["crop"]), "--max-diff-ratio", str(inp["ratio"]),
            "--fidelity", inp["fidelity"]]
    for m in check.get("masks", []):
        args += ["--mask", f"{m['x']},{m['y']},{m['w']},{m['h']}"]
    if check.get("fit"):
        args.append("--fit")
    if not check.get("viewport"):
        args += ["--viewport", f"{inp['size'][0]}x{inp['size'][1]}"]
    return args + ui.base_args(check, ctx) + ui.page_args(check, ctx)


def run_check(check: dict, ctx) -> dict:
    return ui.run(check, ctx, build)
