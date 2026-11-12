"""`red-green` collector: test must fail before the fix and pass with it (DESIGN §4)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .test import DEFAULT_TIMEOUT_S, TAIL, drop_worktree, run_shell, temp_worktree

TEST_RE = re.compile(r"(^|[/\\._-])(tests?|specs?|__tests__)([/\\._-]|$)|tests?\.", re.IGNORECASE)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout.strip() or proc.stderr.strip()


def collect(check: dict, ctx) -> dict:
    repo = ctx.repo_path
    if repo is None or not check.get("cmd"):
        return {"status": "error", "summary": "red-green needs repo and cmd"}
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
    details: dict = {"fix_commit": fix, "parent": parent, "test_files": files}
    red_wt = green_wt = None
    try:
        red_wt = temp_worktree(repo, parent)
        code, out = _git(red_wt, "checkout", fix, "--", *files)
        if code:
            return {"status": "error", "summary": f"cannot bring tests into red worktree: {out}", "details": details}
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
