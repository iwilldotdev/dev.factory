"""Plumbing shared by the checks that run in the bundled browser runner: argv, one outcome, one record [L-51, L-56, L-57]."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import envdata
from ..util import FactoryError, fwd, redact
from . import common, runner


def base_args(check: dict, ctx) -> list[str]:
    a = ["--base-url", ctx.base_url]
    if ctx.state_file:
        a += ["--storage-state", fwd(ctx.state_file)]
    if check.get("viewport"):
        a += ["--viewport", f"{check['viewport'][0]}x{check['viewport'][1]}"]
    return a


def page_args(check: dict, ctx) -> list[str]:
    """The page to open and how to reach its state: a `state_spec` file, inline `actions`, `wait_for` and `settle_ms` [L-51]."""
    a = ["--url", check["url"]] if check.get("url") else []
    if check.get("state_spec"):
        a += ["--state-spec", fwd(ctx.run.dir / check["state_spec"])]
    if check.get("actions"):
        ctx.tmp.mkdir(parents=True, exist_ok=True)
        path = ctx.tmp / "actions.json"
        path.write_text(json.dumps({"actions": check["actions"]}), encoding="utf-8")
        a += ["--state-spec", fwd(path)]
    for key, flag in (("wait_for", "--wait-for"), ("selector", "--selector"), ("settle_ms", "--settle-ms")):
        if check.get(key) is not None:
            a += [flag, str(check[key])]
    return a


def finish(check: dict, ctx, o: runner.Outcome, shown: str, judge: bool = True) -> dict:
    """The record fields of one runner outcome; `details` (diff ratio, style results) ride along [L-55, L-56]."""
    result, kind, reason = runner.classify(o, check["timeout_s"])
    text = redact(o.text, ctx.secrets)
    res = o.result or {}
    if result in ("pass", "fail") and judge:
        result, kind, reason = common.verdict(check, ctx, result == "pass", reason or res.get("summary", ""), text)
    elif result == "pass" and ctx.mode in ("reproduce", "red"):
        result, reason = "reproduced", "captured: " + reason
    details = res.get("details")
    if check["type"] == "visual" and isinstance(details, dict):
        details["masks"] = check.get("masks", [])  # the reasons stay in evidence [DESIGN section 9]
    lines = [res.get("summary", "")] + [f"{r.get('selector')} {r.get('property')}: expected {r.get('expected')!r}, got {r.get('actual')!r}"
                                         for r in (details or {}).get("results", []) if isinstance(r, dict) and not r.get("ok", True)]
    output = "\n".join(x for x in [*lines, text.strip()] if x)
    return {"cmd": shown, "exit": o.code, "duration_s": round(o.secs, 2), "output": redact(output, ctx.secrets), "result": result, "kind": kind,
            "reason": redact(reason, ctx.secrets), "details": details if isinstance(details, dict) else None, "tmp_dir": ctx.tmp}


def env_for(project, ctx, check: dict) -> dict:
    return envdata.child_env(project, ctx.repo, ctx.run.id, ctx.base_url, ctx.user) | {"FACTORY_EXPECT_TIMEOUT_MS": str(check.get("expect_timeout_ms", ""))}


def shown_of(check: dict) -> str:
    return f"{check['type']} {check.get('url') or check.get('spec') or check.get('map') or ''}".strip()


def broken(check: dict, exc: Exception, result: str = "error", kind: str = "internal") -> dict:
    return {"cmd": shown_of(check), "exit": None, "duration_s": 0.0, "output": str(exc), "result": result, "kind": kind, "reason": str(exc)}


def run(check: dict, ctx, build, judge: bool = True) -> dict:
    """Build the argv, run the bundled runner once, classify."""
    try:
        argv = build(check, ctx)
        o = runner.invoke(ctx.cfg, argv, ctx.tmp, check["timeout_s"], env_for(ctx.project, ctx, check))
    except runner.RunnerMissing as exc:
        return broken(check, exc, "setup", "setup")
    except FactoryError as exc:
        return broken(check, exc)
    return finish(check, ctx, o, shown_of(check), judge)


def move_artifacts(src: Path, dst: Path) -> list[str]:
    """Move a scratch directory's files into the attempt's artifact folder; returns their relative names."""
    names = []
    for p in sorted(src.rglob("*")):
        if p.is_file():
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(target))
            names.append(p.relative_to(src).as_posix())
    shutil.rmtree(src, ignore_errors=True)
    return names
