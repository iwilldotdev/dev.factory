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


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started [L-43]."""
    if WINDOWS:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if WINDOWS:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float) -> Result:
    """Run argv with stdin closed, output to temp files (no pipe holds), killed as a tree on timeout [L-42, L-44]."""
    exe = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
    kwargs: dict = {}
    if WINDOWS:
        kwargs["creationflags"] = 0x00000200 | 0x08000000  # new process group, no console window
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        try:
            proc = subprocess.Popen([exe, *argv[1:]], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                    stdout=fout, stderr=ferr, **kwargs)
        except OSError as exc:
            raise FactoryError(f"cannot start {argv[0]!r}: {exc}") from exc
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
            fh.seek(0)
            return fh.read(_CAP)
        out = grab(fout)
        return Result(None if timed_out else proc.returncode, out.decode("utf-8", "replace"),
                      grab(ferr).decode("utf-8", "replace"), secs, timed_out, out)
