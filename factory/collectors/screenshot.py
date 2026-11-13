"""Collector `screenshot`: visual evidence the user asked for (DESIGN §12). Captures, never compares.

`path`: one capture of the page (`capture`, like `visual` without the Figma compare).
`spec`: a Playwright spec with the steps to reach the state (hover, click, fill); the browser CLI runs it with
`--screenshot on`, so every test ends with a screenshot; `page.screenshot({path: testInfo.outputPath("x.png")})`
adds named shots. At HEAD the steps must pass; in BEFORE mode (`check["before"]`, the unfixed base) a failed
step is tolerated as long as images were captured: the bug may be what breaks the steps.
"""

from __future__ import annotations

from factory.collectors import browser as b
from factory.core import Ctx


def collect(check: dict, ctx: Ctx) -> dict:
    state = b.storage_state(check, ctx)
    if isinstance(state, dict):
        return state
    out = b.out_dir(ctx)
    if check.get("spec"):
        return _spec(check, ctx, out, state)
    url = b.full_url(check, ctx)
    if isinstance(url, dict):
        return url
    vp = check.get("viewport") or [1440, 900]
    cap = ["capture", "--url", url, "--out", str(out), "--viewport", f"{vp[0]}x{vp[1]}"]
    for flag, key in (("--selector", "selector"), ("--wait-for", "wait_for")):
        if check.get(key):
            cap += [flag, check[key]]
    if check.get("settle_ms") is not None:
        cap += ["--settle-ms", str(check["settle_ms"])]
    if state:
        cap += ["--storage-state", state]
    res = b.finish(b.run_cli(cap, out), out, ctx)
    res["artifacts"] = [a for a in res["artifacts"] if a.lower().endswith(".png")]
    if res["status"] == "pass" and not res["artifacts"]:
        return b._error("capture reported pass without a png")
    return res


def _spec(check: dict, ctx: Ctx, out, state) -> dict:
    url = b.base_url(ctx)
    if not url:
        return b._error("repo env has no base_url; cannot run the screenshot spec")
    spec = ctx.run_dir / check["spec"]
    if not spec.is_file():
        return b._error(f"spec not found: {check['spec']}")
    argv = ["run-spec", "--spec", str(spec), "--base-url", url, "--out", str(out), "--screenshot", "on"]
    if state:
        argv += ["--storage-state", state]
    data = b.run_cli(argv, out)
    pngs = sorted(p for p in (out / "artifacts").rglob("*.png")) if (out / "artifacts").is_dir() else []
    arts = b.relativize([str(p) for p in pngs], out, ctx)
    details = dict(data.get("details") or {}, spec_status=data.get("status"))
    if data.get("status") == "error" and not pngs:
        return {"status": "error", "summary": data.get("summary", "spec error"), "artifacts": [], "details": details}
    if not pngs:
        return b._error("spec ran but captured no screenshot", **details)
    if data.get("status") != "pass" and not check.get("before"):
        return {"status": "fail", "summary": f"steps failed at HEAD: {data.get('summary', '')}", "artifacts": arts,
                "details": details}
    note = "" if data.get("status") == "pass" else f" (steps on base: {data.get('summary', '')})"
    return {"status": "pass", "summary": f"{len(pngs)} screenshot(s){note}", "artifacts": arts, "details": details}
