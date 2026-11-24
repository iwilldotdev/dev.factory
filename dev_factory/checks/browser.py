"""`browser` checks: a declared spec run by the bundled runner, or by the project's own `runner` template; the exit code decides [L-14, L-56, L-59]."""
from __future__ import annotations

import time

from .. import proc
from ..util import fwd, redact
from . import common, ui


def build(check: dict, ctx) -> list[str]:
    return ["spec", "--spec", fwd(ctx.run.dir / check["spec"]), "--expect-timeout-ms", str(check["expect_timeout_ms"])] + ui.base_args(check, ctx)


def run_template(check: dict, ctx, template: str) -> dict:
    """The project's own runner: placeholders are substituted inside argv tokens, no shell; exit 0 is a pass [L-130]."""
    ctx.tmp.mkdir(parents=True, exist_ok=True)
    values = {"spec": fwd(ctx.run.dir / check["spec"]), "base_url": ctx.base_url, "out": fwd(ctx.tmp), "timeout_ms": str(check["timeout_s"] * 1000),
              "storage_state": fwd(ctx.state_file) if ctx.state_file else ""}
    argv = proc.substitute(proc.split_template(template), values)
    start = time.monotonic()
    res = proc.run(argv, cwd=ctx.path, env=ui.env_for(ctx.project, ctx, check), timeout_s=check["timeout_s"])
    text = redact((res.out + "\n" + res.err).replace("\r\n", "\n"), ctx.secrets).strip()
    if res.timed_out:
        result, kind, reason = "timeout", "timeout", f"killed after {check['timeout_s']}s (the whole process tree)"
    else:
        last = text.splitlines()[-1] if text else ""
        result, kind, reason = common.verdict(check, ctx, res.exit == 0, f"exit {res.exit}" + (f": {last}" if last else ""), text)
    return {"cmd": redact(" ".join(argv), ctx.secrets), "exit": res.exit, "duration_s": round(time.monotonic() - start, 2), "output": text,
            "result": result, "kind": kind, "reason": reason, "details": None, "tmp_dir": ctx.tmp}


def run_check(check: dict, ctx) -> dict:
    template = ctx.cfg["checks"].get("browser", {}).get("runner")
    return run_template(check, ctx, template) if template else ui.run(check, ctx, build)
