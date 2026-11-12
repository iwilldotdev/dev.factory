"""`test` collector: run a shell command in the repo worktree (DESIGN §4)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT_S = 900
TAIL = 4000


def run_shell(cmd: str, cwd: Path, timeout: int = DEFAULT_TIMEOUT_S, env_vars: dict | None = None) -> tuple[int, str]:
    """Run `cmd` through the shell. Returns (exit_code, combined output). 124 on timeout."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (env_vars or {}).items()})
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as err:
        out = err.stdout if isinstance(err.stdout, str) else ""
        return 124, f"{out}\n[timeout after {timeout}s]"
    return proc.returncode, (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")


def _rm_error(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def link_deps(src: Path, dst: Path) -> None:
    """Best effort: expose src/node_modules inside a temp worktree."""
    nm = Path(src) / "node_modules"
    target = Path(dst) / "node_modules"
    if not nm.is_dir() or target.exists():
        return
    try:
        os.symlink(nm, target, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(nm)],
                           capture_output=True, text=True)


def unlink_deps(dst: Path) -> None:
    target = Path(dst) / "node_modules"
    if target.is_symlink():
        target.unlink()
    elif target.exists() and os.name == "nt" and target.is_dir():
        try:
            os.rmdir(target)  # junction: removes the link only
        except OSError:
            pass


def setup_command(workspace: dict | None, alias: str | None) -> str | None:
    """`quality.setup` of the repo: the restore command every worktree gets before checks run."""
    for repo in (workspace or {}).get("repos", []):
        if alias and alias in (repo.get("alias"), repo.get("name")):
            return (repo.get("quality") or {}).get("setup") or None
    return None


def prepare_worktree(src: Path, dst: Path, setup: str | None = None, env_vars: dict | None = None) -> None:
    """Identical preparation for every worktree (run, temp base, red, green): `setup` when the repo
    declares one (no node_modules link then: a restore through a junction would rewrite the main repo),
    else link src/node_modules. Raises RuntimeError when setup fails."""
    if setup:
        code, out = run_shell(setup, Path(dst), DEFAULT_TIMEOUT_S, env_vars)
        if code != 0:
            raise RuntimeError(f"setup `{setup}` failed (exit {code}): {out.strip()[-500:]}")
        return
    link_deps(src, dst)


def temp_worktree(repo: Path, ref: str, setup: str | None = None, env_vars: dict | None = None) -> Path:
    """Detached worktree of `repo` at `ref` in a fresh temp dir, prepared like the run worktree."""
    base = Path(tempfile.mkdtemp(prefix="factory-wt-"))
    target = base / "wt"
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(target), ref],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    try:
        prepare_worktree(repo, target, setup, env_vars)
    except RuntimeError:
        drop_worktree(repo, target)
        raise
    return target


def drop_worktree(repo: Path, target: Path) -> None:
    unlink_deps(target)
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                   capture_output=True, text=True)
    shutil.rmtree(target.parent, ignore_errors=True, onerror=_rm_error)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)


def collect(check: dict, ctx) -> dict:
    if ctx.repo_path is None:
        return {"status": "error", "summary": "test check needs a repo"}
    cmd = check.get("cmd")
    if not cmd:
        return {"status": "error", "summary": "test check has no cmd"}
    cwd = Path(ctx.repo_path) / (check.get("cwd") or ".")
    timeout = int(check.get("timeout_s") or DEFAULT_TIMEOUT_S)
    code, output = run_shell(cmd, cwd, timeout, (ctx.env or {}).get("env_vars"))
    expect = check.get("expect") or "exit0"
    if expect == "exit0":
        ok = code == 0
    elif expect == "nonzero":
        ok = code != 0
    elif isinstance(expect, str) and expect.startswith("exit"):
        ok = code == int(expect[4:])
    else:
        return {"status": "error", "summary": f"unknown expect {expect!r}"}
    artifacts = []
    if ctx.out_dir is not None:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / "output.log").write_text(output, encoding="utf-8")
        artifacts.append(f"evidence/{check['id']}/output.log")
    details = {"cmd": cmd, "exit_code": code, "output_tail": output[-TAIL:]}
    failure = fail_pattern_error(check, code, output)
    if failure:
        return {"status": "error", "summary": failure, "artifacts": artifacts, "details": details}
    return {
        "status": "pass" if ok else "fail",
        "summary": f"exit {code} (expected {expect})",
        "artifacts": artifacts,
        "details": details,
    }


def fail_pattern_error(check: dict, code: int, output: str) -> str | None:
    """A failing exit must show `expect_fail_pattern` (the test name or assertion); otherwise it is a
    setup failure (missing deps, compile error, wrong cwd), not a reproduction."""
    pattern = check.get("expect_fail_pattern")
    if not pattern or code == 0:
        return None
    try:
        found = re.search(pattern, output, re.MULTILINE)
    except re.error as err:
        return f"invalid expect_fail_pattern {pattern!r}: {err}"
    if found:
        return None
    return f"could not reproduce: setup failure (exit {code} without expect_fail_pattern {pattern!r} in the output)"
