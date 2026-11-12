"""`red-green` collector: test must fail before the fix and pass with it (DESIGN §4)."""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path

from . import test as test_collector
from .test import DEFAULT_TIMEOUT_S, TAIL, drop_worktree, run_shell, temp_worktree

TEST_RE = re.compile(r"(^|[/\\._-])(tests?|specs?|__tests__)([/\\._-]|$)|tests?\.", re.IGNORECASE)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout.strip() or proc.stderr.strip()


def _inner(check: dict, ctx) -> dict | str:
    """The referenced check (`of`) as a dict, or an error string. Only `test` inner checks are supported."""
    from ..core import read_json
    try:
        data = read_json(Path(ctx.run_dir) / "acs.json")
    except Exception as err:  # noqa: BLE001
        return f"cannot read acs.json: {err}"
    inner = next((c for c in data.get("checks") or [] if c.get("id") == check["of"]), None)
    if inner is None:
        return f"of: unknown check {check['of']}"
    if inner.get("type") != "test":
        return (f"of: {check['of']} is {inner.get('type')}; only test inner checks are supported "
                "(browser/api would need the env started from the temp worktree)")
    return inner


def _run_inner(inner: dict, ctx, wt: Path) -> tuple[str, int, str]:
    sub = dataclasses.replace(ctx, repo_path=wt, out_dir=None)
    body = test_collector.collect(inner, sub)
    d = body.get("details") or {}
    return body["status"], d.get("exit_code", -1), d.get("output_tail", body.get("summary", ""))


def collect(check: dict, ctx) -> dict:
    repo = ctx.repo_path
    inner = None
    if check.get("of"):
        inner = _inner(check, ctx)
        if isinstance(inner, str):
            return {"status": "error", "summary": inner}
    if repo is None or not (check.get("cmd") or inner):
        return {"status": "error", "summary": "red-green needs repo and cmd (or of)"}
    spec = (check.get("fix_commit") or "HEAD") + "^{commit}"
    code, fix = _git(repo, "rev-parse", "--verify", spec)
    if code:
        return {"status": "error", "summary": f"cannot resolve fix_commit: {fix}"}
    code, parent = _git(repo, "rev-parse", "--verify", fix + "~1")
    if code:
        return {"status": "error", "summary": "fix_commit has no parent"}
    files = check.get("test_files")
    if not files:
        _, listing = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", fix)
        files = [f for f in listing.splitlines() if TEST_RE.search(f)]
    if not files:
        return {"status": "error", "summary": f"no test files found in {fix[:8]}; set test_files"}
    timeout = int(check.get("timeout_s") or DEFAULT_TIMEOUT_S)
    env_vars = (ctx.env or {}).get("env_vars")
    details: dict = {"of": check.get("of"), "fix_commit": fix, "parent": parent, "test_files": files}
    red_wt = green_wt = None
    try:
        red_wt = temp_worktree(repo, parent)
        code, out = _git(red_wt, "checkout", fix, "--", *files)
        if code:
            return {"status": "error", "summary": f"cannot bring tests into red worktree: {out}", "details": details}
        if inner:
            red_st, red_code, red_out = _run_inner(inner, ctx, red_wt)
            green_wt = temp_worktree(repo, fix)
            green_st, green_code, green_out = _run_inner(inner, ctx, green_wt)
            if "error" in (red_st, green_st):
                return {"status": "error", "details": details,
                        "summary": f"inner check {inner['id']} could not run (red {red_st}, green {green_st})"}
            red_code = 1 if red_st == "fail" else 0
            green_code = 0 if green_st == "pass" else 1
        else:
            red_code, red_out = run_shell(check["cmd"], red_wt / (check.get("cwd") or "."), timeout, env_vars)
            green_wt = temp_worktree(repo, fix)
            green_code, green_out = run_shell(check["cmd"], green_wt / (check.get("cwd") or "."), timeout, env_vars)
    except RuntimeError as err:
        return {"status": "error", "summary": str(err), "details": details}
    finally:
        for wt in (red_wt, green_wt):
            if wt is not None:
                drop_worktree(repo, wt)
    details.update(red_exit=red_code, green_exit=green_code,
                   red_tail=red_out[-TAIL:], green_tail=green_out[-TAIL:])
    artifacts = []
    if ctx.out_dir is not None:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / "red.log").write_text(red_out, encoding="utf-8")
        (ctx.out_dir / "green.log").write_text(green_out, encoding="utf-8")
        artifacts = [f"evidence/{check['id']}/red.log", f"evidence/{check['id']}/green.log"]
    red_ok, green_ok = red_code != 0, green_code == 0
    if red_ok and green_ok:
        status, summary = "pass", f"red (exit {red_code}) before fix, green after"
    elif not red_ok:
        status, summary = "fail", "test passes without the fix (cannot fail)"
    else:
        status, summary = "fail", f"test still fails with the fix (exit {green_code})"
    return {"status": status, "summary": summary, "artifacts": artifacts, "details": details}
