"""The mutant proof of a check: a command breaks the behaviour in a throwaway worktree at HEAD, and the check must fail there.
A check that runs against state it left behind (a column an earlier run created) can pass whatever the code does; a recorded
kill shows that it can fail. Only `cmd` and `quality` checks, which run in a worktree, take a mutant."""
from __future__ import annotations

from . import evidence, git, proc
from .util import redact

TYPES = ("cmd", "quality")


def problems(c: dict, here: str) -> list[str]:
    if "mutant" not in c:
        return []
    out = []
    if not (isinstance(c["mutant"], str) and c["mutant"].strip()):
        out.append(f"{here}.mutant: must be a command that breaks the behaviour the check proves (it runs in a throwaway worktree)")
    if c.get("type") not in TYPES:
        out.append(f"{here}.mutant: only a {' or '.join(TYPES)} check takes a mutant")
    return out


def invert(out: dict) -> dict:
    """With the mutant applied, an assertion failure is the good result (killed) and a pass is the bad one (survived).
    A setup, timeout or error outcome is neither: it says nothing about whether the check can fail."""
    if out["result"] == "fail" and out["kind"] == "assert":
        return {**out, "result": "pass", "kind": None, "reason": f"killed: the check fails with the mutant applied ({out['reason']})"[:500]}
    if out["result"] in ("pass", "flaky"):
        return {**out, "result": "fail", "kind": "assert",
                "reason": "survived: the check still passes with the mutant applied, so it cannot fail on the behaviour it proves"}
    return out


def prove(b, c: dict, sha: str, fresh: bool) -> tuple[dict | None, bool, str]:
    """(record, reused, mode) for `Batch.note`: the latest kill at this SHA and spec is reused."""
    if not fresh and evidence.satisfied(b.st, c["id"], "mutant", b.spec(c), sha):
        return None, True, "mutant"
    with git.throwaway(b.project, b.run, b.repos[c["repo"]], sha, "mutant", b.opts.actor) as (tree, setup):
        bad = None
        if setup["setup"] == "fail":
            bad = (setup.get("setup_exit"), setup.get("setup_tail", ""), f"worktree setup failed (exit {setup.get('setup_exit')})")
        else:
            res = proc.run(c["mutant"], cwd=tree, env=proc.child_env(c.get("vars") or {}), timeout_s=c["timeout_s"], shell=True)
            text = redact(res.out + res.err, b.secrets)
            if res.timed_out or res.exit != 0:
                bad = (res.exit, text, f"the mutant command failed (exit {res.exit}); it must apply cleanly")
            elif not git.status_now(tree):
                bad = (res.exit, text, "the mutant command changed nothing in the worktree")
        if bad:
            out = {"cmd": redact(c["mutant"], b.secrets), "exit": bad[0], "duration_s": 0.0, "output": bad[1], "result": "setup", "kind": "setup", "reason": bad[2]}
            return evidence.append(b.run, c, b.spec(c), "mutant", sha, tree, True, out, b.cfg["checks"]["tail_lines"], None, b.opts.actor), False, "mutant"
        return b.run_one(c, "mutant", tree, sha, True), False, "mutant"
