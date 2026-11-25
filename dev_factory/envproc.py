"""Env helpers without lifecycle: the lock record, link-safe `files_from_main` copies and port ownership [L-64, L-65, L-68, L-74]."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import git, proc, slots
from .util import FactoryError, fwd, write_atomic

TOLERANCE_S = 2.0  # clock ticks and FILETIME rounding: a recorded create time within this of the live one is the same process
CAUSES = {  # one likely cause per failure kind, printed with every env failure
    "files": "a files_from_main entry is missing in the main checkout; create it there or drop it from [repo.env] files_from_main",
    "lock": "another run or command holds this env; wait with --wait <seconds>, or stop that run's env",
    "port": "a process the factory did not start holds the port (named above); stop it, or move base_url to a free port in .factory/local.toml",
    "start": "the up command did not start or exited early; read the log (missing setup or dependencies, wrong cwd, missing .env file)",
    "health": "the health URL did not answer in time; a first start in a fresh worktree can exceed ready_timeout_s, or the health path is wrong",
    "warmup": "a warmup request failed; a first start in a fresh worktree can be slow, so raise its timeout or check the path",
    "data": "the data preflight failed; run factory env seed, or check the preflight command",
    "login": 'the login spec did not reach its done criterion; check the final URL/cookie, or declare "done" in the spec',
    "stale": "the env serves another worktree or head; factory env up --restart",
    "restore": "the run env did not start again after the reproduction; factory env up --restart and read the log",
}
LOG_LINES = 30


def lock_path(project, repo: str) -> Path:
    return project.factory_dir / "env" / f"{slots.key(project, repo)}.lock"


def log_path(project, repo: str) -> Path:
    return project.factory_dir / "env" / f"{slots.key(project, repo)}.log"


def read_lock(project, repo: str) -> dict | None:
    try:
        return json.loads(lock_path(project, repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_lock(project, repo: str, rec: dict) -> None:
    write_atomic(lock_path(project, repo), json.dumps(rec, indent=2, sort_keys=True))


def claim(project, repo: str, owner: dict) -> bool:
    """Create the lock file exclusively; False when another holder's file exists."""
    path = lock_path(project, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(owner, fh)
    return True


def release(project, repo: str) -> None:
    lock_path(project, repo).unlink(missing_ok=True)


def holder_alive(rec: dict | None) -> bool:
    """A recorded holder is live only when its pid exists and its create time still matches the record [L-68]."""
    if not rec or not isinstance(rec.get("pid"), int) or rec.get("create_time") is None:
        return False
    live = proc.create_time(rec["pid"])
    return live is not None and abs(live - rec["create_time"]) <= TOLERANCE_S


def describe(rec: dict) -> str:
    return f"run {rec.get('run')} (pid {rec.get('pid')}, {rec.get('cwd')}, started at {str(rec.get('sha', ''))[:7]})"


def port_owner(url: str) -> str | None:
    """`pid N (name)` of whatever listens on the port of `url`, system pids included; never kills anything [L-74]."""
    from .localhttp import split
    try:
        port = split(url)[1]
    except FactoryError:
        return None
    env, cwd = proc.child_env(), Path.cwd()
    pid, name = None, ""
    if proc.WINDOWS:
        out = proc.run(["netstat", "-ano", "-p", "TCP"], cwd=cwd, env=env, timeout_s=20).out
        found = next((m for m in (re.match(rf"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)", ln) for ln in out.splitlines()) if m), None)
        if found:
            pid = int(found.group(1))
            t = proc.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], cwd=cwd, env=env, timeout_s=20).out.strip()
            name = t.split(",")[0].strip('"') if t.startswith('"') else ""
            name = name or ("System, HTTP.sys" if pid == 4 else "")
    elif shutil.which("ss"):
        res = proc.run(["ss", "-H", "-ltnp", f"sport = :{port}"], cwd=cwd, env=env, timeout_s=20)
        m = re.search(r'users:\(\("([^"]*)",pid=(\d+)', res.out)
        pid, name = (int(m.group(2)), m.group(1)) if m else (None, "")
    elif shutil.which("lsof"):
        out = proc.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], cwd=cwd, env=env, timeout_s=20).out
        pm, nm = re.search(r"^p(\d+)", out, re.M), re.search(r"^c(.+)$", out, re.M)
        pid, name = (int(pm.group(1)), nm.group(1) if nm else "") if pm else (None, "")
    return None if pid is None else f"pid {pid}" + (f" ({name})" if name else "")


def _reparse(path: Path) -> bool:
    """A Windows junction or other reparse point (is_symlink is False for a junction)."""
    try:
        return bool(os.stat(path, follow_symlinks=False).st_file_attributes & 0x400)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def linked(path: Path, root: Path) -> bool:
    """True when `path` or any directory between `root` and it is a link or reparse point."""
    cur = root
    for part in path.relative_to(root).parts:
        cur = cur / part
        if cur.is_symlink() or _reparse(cur):
            return True
    return False


def copy_main_files(repo: dict, tree: Path) -> list[str]:
    """Copy each `files_from_main` file into the worktree without following links, never outside it, and remove a copy
    git would not ignore. Returns the problems, which are env kind `files` [L-64, L-65]."""
    main, problems = Path(repo["path"]).resolve(), []
    if main == tree.resolve():
        return problems
    for rel in repo.get("files_from_main", []):
        pr = Path(rel)
        if pr.is_absolute() or ".." in pr.parts or not rel.strip():
            problems.append(f"{rel}: must be a path inside the repo")
            continue
        src, dst = main / pr, tree / pr
        if not src.is_file() or linked(src, main):
            problems.append(f"{rel}: missing in the main checkout {fwd(main)}, or not a regular file (links are never copied)")
        elif linked(dst, tree) or not dst.resolve().is_relative_to(tree.resolve()):
            problems.append(f"{rel}: the destination goes through a link; refusing to write outside the worktree")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst, follow_symlinks=False)
            if git.git(tree, "check-ignore", "-q", rel, check=False).exit != 0:
                dst.unlink(missing_ok=True)
                problems.append(f"{rel}: git does not ignore it in the worktree, so the copy was removed (add it to .gitignore) [L-65]")
    return problems


def ignored_missing(main: Path, tree: Path) -> list[str]:
    """Top-level folders git ignores in the main checkout that a new worktree lacks (installed dependencies, generated files):
    without a repo `setup`, an env or a check started there may not find its tools."""
    out = git.git(main, "status", "--ignored", "--porcelain", check=False).out
    names = {n.rstrip("/") for ln in out.splitlines() if ln.startswith("!! ") and (n := ln[3:].strip('"')).endswith("/")}
    names.discard(".factory")  # the factory's own folder of a folder workspace
    return sorted(n for n in names if n and "/" not in n and not (tree / n).exists() and not tree.resolve().is_relative_to((main / n).resolve()))
