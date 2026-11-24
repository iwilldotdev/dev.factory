"""`http` checks: one request to the env origin, status and a JSON subset; a failure never quotes the body [L-52, L-82, L-83]."""
from __future__ import annotations

import json
import re
import time

from .. import config, localhttp
from ..util import FactoryError, redact
from . import common

REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def subset(want, got, path: str = "$") -> str | None:
    """The JSON path of the first place where `got` lacks what `want` states, or None when `want` is a subset [L-83]."""
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return path
        return next((p for k, v in want.items() if (p := subset(v, got[k], f"{path}.{k}") if k in got else f"{path}.{k}")), None)
    if isinstance(want, list):
        if not isinstance(got, list) or len(got) < len(want):
            return path
        return next((p for i, v in enumerate(want) if (p := subset(v, got[i], f"{path}[{i}]"))), None)
    same = want == got and isinstance(want, bool) == isinstance(got, bool)
    return None if same else path


def expand(cfg: dict, headers: dict) -> tuple[dict, list[str]]:
    """Resolve `${SECRET}` references in header values at run time; returns the headers and the values to redact."""
    keys = {k for v in headers.values() for k in REF.findall(v)}
    values, missing = config.resolve_secrets(cfg, sorted(keys))
    if missing:
        raise FactoryError(f"secrets not set for the http headers: {', '.join(missing)}")
    return {k: REF.sub(lambda m: values[m.group(1)], v) for k, v in headers.items()}, list(values.values())


def run_check(check: dict, ctx) -> dict:
    shown_cmd = f"{check['method']} {check['path']}"
    fail = lambda result, kind, why: {"cmd": shown_cmd, "exit": None, "duration_s": 0.0, "output": why, "result": result, "kind": kind, "reason": why}  # noqa: E731
    try:
        url = localhttp.join(ctx.base_url, check["path"])
        headers, secret = expand(ctx.cfg, check.get("headers") or {})
    except FactoryError as exc:
        return fail("setup", "setup", str(exc))
    headers.update(ctx.session or {})
    body = None
    if "body" in check:
        body, headers = json.dumps(check["body"]).encode("utf-8"), {"Content-Type": "application/json", **headers}
    start = time.monotonic()
    try:
        status, _, data = localhttp.request(check["method"], url, headers, body, check["timeout_s"])
    except OSError as exc:
        return fail("env", "env", f"cannot reach {ctx.base_url}: {redact(str(exc), secret)}")
    ok, parts = status == check["expect_status"], [f"status {status}" + ("" if status == check["expect_status"] else f", expected {check['expect_status']}")]
    if ok and "expect_json" in check:
        try:
            miss = subset(check["expect_json"], json.loads(data.decode("utf-8")))
        except ValueError:
            miss = "$ (the response is not JSON)"
        if miss:
            ok = False
            parts.append(f"expect_json mismatch at {miss}")
    shown = "; ".join(parts)
    result, kind, reason = common.verdict(check, ctx, ok, shown, data.decode("utf-8", "replace"))
    return {"cmd": shown_cmd, "exit": None, "duration_s": round(time.monotonic() - start, 2), "output": shown, "result": result, "kind": kind, "reason": reason}
