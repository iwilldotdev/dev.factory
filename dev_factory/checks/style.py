"""`style` checks: computed styles of the selectors of a style map against facts of the frame, with the tolerances of its fidelity [L-54, L-187]."""
from __future__ import annotations

from .. import designflow
from ..util import fwd
from . import ui


def build(check: dict, ctx) -> list[str]:
    inp = designflow.style_inputs(ctx.run, ctx.cfg, check)
    t = inp["tol"]
    return (["style", "--map", fwd(ctx.run.dir / check["map"]), "--px", str(t["px"]), "--delta-e", str(t["delta_e"]), "--font-px", str(t["font_px"]),
             "--fidelity", inp["fidelity"]] + ui.base_args(check, ctx) + ui.page_args(check, ctx))


def run_check(check: dict, ctx) -> dict:
    return ui.run(check, ctx, build)
