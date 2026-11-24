"""Plan-time dry run: every new runtime check runs once against the served env and must not end in `setup` or `error` [L-86]."""
from __future__ import annotations

import shutil

from . import checks as chk, env as envmod, plan as planmod
from .run import Run
from .util import FactoryError


def reasons(run: Run, project, st: dict, plan: dict, previous: dict | None) -> list[str]:
    """Nothing is recorded: a dry run is a question to the plan, not evidence. Checks whose spec is unchanged since `previous` are skipped."""
    from .verify import build_ctx
    cfg, out, sessions = project.cfg, [], {}
    before = (previous or {}).get("spec_hashes", {})
    todo = [c for c in planmod.effective_checks(plan, cfg) if c["type"] in chk.RUNTIME and c["type"] != "screenshot"
            and planmod.spec_hash(c, run.dir) != before.get(c["id"])]
    for repo in sorted({c["repo"] for c in todo}):
        try:
            problems, info = envmod.binding(project, st, repo)
        except FactoryError as exc:
            out.append(f"{repo}: {exc}")
            continue
        if problems:
            out.append(f"{repo}: the env cannot serve the dry run of its new runtime checks ({'; '.join(problems)}); start it with the env command of this CLI [L-86]")
            continue
        for c in [x for x in todo if x["repo"] == repo]:
            tree = envmod.target(project, st, repo)[0]
            ctx = None
            try:
                ctx = build_ctx(project, run, plan, c, "verify", tree, info, "", [], sessions)
                res = chk.run(c, ctx)
            except FactoryError as exc:
                res = {"result": "setup", "reason": str(exc)}
            if ctx is not None and ctx.tmp is not None:
                shutil.rmtree(ctx.tmp, ignore_errors=True)
            if res["result"] in ("setup", "error", "env", "timeout"):
                out.append(f"{c['id']}: the dry run ended in {res['result']}: {res['reason'][:300]} [L-86]")
    return out
