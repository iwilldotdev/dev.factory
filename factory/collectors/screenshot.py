"""Collector `screenshot`: visual evidence the user asked for (DESIGN §12). Captures, never compares.

`path`: one capture of the page (`capture`, like `visual` without the design compare).
`spec`: a Playwright spec with the steps to reach the state (hover, click, fill); the browser CLI runs it with
`--screenshot on`, so every test ends with a screenshot; `page.screenshot({path: testInfo.outputPath("x.png")})`
adds named shots. At HEAD the steps must pass; in BEFORE mode (`collect_before`, the unfixed base; never a key of
the check, so acs.json cannot ask for it) a failed step is tolerated as long as images were captured: the bug may
be what breaks the steps.
"""

from __future__ import annotations

import shutil

from factory.collectors import browser as b
from factory.core import Ctx

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def collect(check: dict, ctx: Ctx) -> dict:
    return _collect(check, ctx, before=False)


def collect_before(check: dict, ctx: Ctx) -> dict:
    return _collect(check, ctx, before=True)


def png_artifacts_error(run_dir, artifacts) -> str | None:
    """At least one listed artifact must exist under the run dir and be a real PNG (stale/foreign files do not count)."""
    from pathlib import Path
    for a in artifacts or []:
        p = Path(a) if Path(a).is_absolute() else Path(run_dir) / a
        try:
            if str(a).lower().endswith(".png") and p.is_file() and p.read_bytes()[:8] == PNG_MAGIC:
                return None
        except OSError:
            continue
    return "no listed artifact is an existing PNG image"


def _collect(check: dict, ctx: Ctx, before: bool) -> dict:
    state = b.storage_state(check, ctx)
    if isinstance(state, dict):
        return state
    out = b.out_dir(ctx)
    if check.get("spec"):
        return _spec(check, ctx, out, state, before)
    url = b.full_url(check, ctx)
    if isinstance(url, dict):
        return url
    vp = check.get("viewport") or [1440, 900]
    budget = b.Budget(b.check_cap(check, ctx))
    cap = ["capture", "--url", url, "--out", str(out), "--viewport", f"{vp[0]}x{vp[1]}", *budget.cli_args(check)]
    for flag, key in (("--selector", "selector"), ("--wait-for", "wait_for")):
        if check.get(key):
            cap += [flag, check[key]]
    if check.get("settle_ms") is not None:
        cap += ["--settle-ms", str(check["settle_ms"])]
    if state:
        cap += ["--storage-state", state]
    res = b.finish(b.run_cli(cap, out, budget.cap), out, ctx)
    res["artifacts"] = [a for a in res["artifacts"] if a.lower().endswith(".png")]
    if res["status"] == "pass" and not res["artifacts"]:
        return b._error("capture reported pass without a png")
    return res


def _spec(check: dict, ctx: Ctx, out, state, before: bool) -> dict:
    url = b.base_url(ctx)
    if not url:
        return b._error("repo env has no base_url; cannot run the screenshot spec")
    spec = ctx.run_dir / check["spec"]
    if not spec.is_file():
        return b._error(f"spec not found: {check['spec']}")
    shutil.rmtree(out / "artifacts", ignore_errors=True)  # stale images from an earlier run must not pass as new
    budget = b.Budget(b.check_cap(check, ctx))
    argv = ["run-spec", "--spec", str(spec), "--base-url", url, "--out", str(out), "--screenshot", "on",
            *budget.cli_args(check)]
    if state:
        argv += ["--storage-state", state]
    data = b.run_cli(argv, out, budget.cap)
    pngs = sorted(p for p in (out / "artifacts").rglob("*.png")) if (out / "artifacts").is_dir() else []
    arts = b.relativize([str(p) for p in pngs], out, ctx)
    details = dict(data.get("details") or {}, spec_status=data.get("status"))
    if data.get("status") == "error" and not pngs:
        return {"status": "error", "summary": data.get("summary", "spec error"), "artifacts": [], "details": details}
    if not pngs:
        return b._error("spec ran but captured no screenshot", **details)
    if data.get("status") != "pass" and not before:
        return {"status": "fail", "summary": f"steps failed at HEAD: {data.get('summary', '')}", "artifacts": arts,
                "details": details}
    note = "" if data.get("status") == "pass" else f" (steps on base: {data.get('summary', '')})"
    return {"status": "pass", "summary": f"{len(pngs)} screenshot(s){note}", "artifacts": arts, "details": details}
