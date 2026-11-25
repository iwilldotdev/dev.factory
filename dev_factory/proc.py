"""Child processes: closed stdin, English locale, wall-clock cap with tree kill, command templates."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .util import FactoryError

WINDOWS = sys.platform == "win32"
LOCALE_ENV = {"LC_ALL": "C", "LANG": "C", "LANGUAGE": "en", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CAP = 4 << 20


@dataclass
class Result:
    exit: int | None
    out: str
    err: str
    secs: float
    timed_out: bool
    raw: bytes = b""  # stdout exactly as the child wrote it


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def split_template(template: str) -> list[str]:
    """Split into argv first (POSIX rules on every OS), expanding a leading ~; no shell is involved [L-130]."""
    try:
        argv = shlex.split(template, posix=True)
    except ValueError as exc:
        raise FactoryError(f"cannot split command template {template!r}: {exc}") from exc
    if not argv:
        raise FactoryError("empty command template")
    return [os.path.expanduser(a) if a.startswith(("~/", "~\\")) or a == "~" else a for a in argv]


def substitute(argv: list[str], values: dict[str, str]) -> list[str]:
    """Substitute inside each token; values are never re-parsed. An unknown placeholder is an error."""
    def one(token: str) -> str:
        def sub(m):
            if m.group(1) not in values:
                raise FactoryError(f"placeholder {{{m.group(1)}}} has no value here")
            return values[m.group(1)]
        return _PLACEHOLDER.sub(sub, token)
    return [one(t) for t in argv]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(extra or {})
    env.update(LOCALE_ENV)  # last: nothing can bring a localized runner back [L-45]
    return env


def kill_pid_tree(pid: int) -> None:
    """Kill a process and everything it started: the process group on POSIX, the tree on Windows [L-43, L-67]."""
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started [L-43]."""
    kill_pid_tree(proc.pid)
    try:
        proc.kill()
    except OSError:
        pass


def create_time(pid: int) -> float | None:
    """Process creation time in epoch seconds, or None when the process is gone, a zombie, or unreadable [L-68]."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if code.value != 259 or not ctypes.windll.kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                return None
            return ((times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) - 116444736000000000) / 1e7
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        boot = next(float(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime"))
        return boot + int(stat[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError, StopIteration):
        pass
    res = run(["ps", "-o", "etimes=", "-p", str(pid)], cwd=Path.cwd(), env=child_env(), timeout_s=10)  # no /proc: elapsed seconds
    return time.time() - int(res.out.strip()) if res.exit == 0 and res.out.strip().isdigit() else None


def pid_alive(pid: int) -> bool:
    return create_time(pid) is not None


_CHILDREN: list[subprocess.Popen] = []


def spawn_group(line: str, *, cwd: Path, env: dict[str, str], log: Path) -> int:
    """Start a shell command line in its own process group (hidden console on Windows) with stdout and stderr appended
    to `log` and stdin closed; the caller owns the pid and stops it with kill_pid_tree [L-66, L-67]."""
    kwargs: dict = {"creationflags": 0x00000200 | 0x08000000} if WINDOWS else {"start_new_session": True}
    try:
        with open(log, "ab") as fh:
            child = subprocess.Popen(line, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, shell=True, **kwargs)
    except OSError as exc:
        raise FactoryError(f"cannot start {line!r}: {exc}") from exc
    _CHILDREN[:] = [c for c in _CHILDREN if c.poll() is None] + [child]  # reaped on exit, so no zombie keeps a pid "alive"
    return child.pid


def run(argv: list[str] | str, *, cwd: Path, env: dict[str, str], timeout_s: float, shell: bool = False) -> Result:
    """Run argv (or, with shell=True, one command line for the platform shell) with stdin closed, output to temp
    files (no pipe holds), killed as a tree on timeout [L-42, L-44]."""
    if shell:
        argv0, line = str(argv), str(argv)
    else:
        argv0, line = argv[0], [shutil.which(argv[0], path=env.get("PATH")) or argv[0], *argv[1:]]
    kwargs: dict = {}
    if WINDOWS:
        kwargs["creationflags"] = 0x00000200 | 0x08000000  # new process group, no console window
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        try:
            proc = subprocess.Popen(line, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=fout, stderr=ferr,
                                    shell=shell, **kwargs)
        except OSError as exc:
            raise FactoryError(f"cannot start {argv0!r}: {exc}") from exc
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        secs = time.monotonic() - start

        def grab(fh) -> bytes:
            n = fh.seek(0, 2)
            fh.seek(0)
            if n <= _CAP:
                return fh.read()
            head = fh.read(_CAP // 2)  # keep both ends: the summary of a verbose run is at the end
            fh.seek(n - _CAP // 2)
            return head + b"\n[... output truncated by the factory ...]\n" + fh.read()
        out = grab(fout)
        return Result(None if timed_out else proc.returncode, out.decode("utf-8", "replace"),
                      grab(ferr).decode("utf-8", "replace"), secs, timed_out, out)
