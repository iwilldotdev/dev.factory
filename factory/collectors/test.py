"""`test` collector: run a shell command in the repo worktree (DESIGN §4)."""

from __future__ import annotations

import os
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


def temp_worktree(repo: Path, ref: str) -> Path:
    """Detached worktree of `repo` at `ref` in a fresh temp dir."""
    base = Path(tempfile.mkdtemp(prefix="factory-wt-"))
    target = base / "wt"
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(target), ref],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    link_deps(repo, target)
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
    return {
        "status": "pass" if ok else "fail",
        "summary": f"exit {code} (expected {expect})",
        "artifacts": artifacts,
        "details": {"cmd": cmd, "exit_code": code, "output_tail": output[-TAIL:]},
    }
