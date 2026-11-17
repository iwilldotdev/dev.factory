"""`factory wait`: bounded waiting for a file, a command or a URL (replaces hand-rolled `sleep` loops).

Exit 0 when satisfied, 1 on timeout, 2 on usage error. Prints one line.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..core import FactoryError

DEFAULT_TIMEOUT = 120.0
DEFAULT_POLL = 1.0
MAX_TIMEOUT = 600.0  # a wait is bounded: a longer one is a hung process, not a wait
MIN_POLL = 0.2


def _file_ok(path: Path, contains: str | None) -> bool:
    try:
        if not path.is_file():
            return False
        if contains is None:
            return True
        return contains in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _cmd_ok(cmd: str, want: int, remaining: float) -> bool:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=max(1.0, min(remaining, 60.0)))
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == want


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # report 3xx as-is
        return None


def _url_status(url: str, remaining: float) -> int:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=max(1.0, min(remaining, 10.0))) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as err:
        return int(err.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _status_ok(code: int, want: str) -> bool:
    if want.endswith("xx") and want[:1].isdigit():
        return code // 100 == int(want[0])
    return want.isdigit() and code == int(want)


def _valid_status(want: str) -> bool:
    return want.isdigit() or (len(want) == 3 and want[0].isdigit() and want.endswith("xx"))


def _cmd(args) -> int:
    modes = [m for m in ("file", "cmd", "url") if getattr(args, m) is not None]
    if len(modes) != 1:
        raise FactoryError("factory wait: give exactly one of --file, --cmd, --url")
    mode = modes[0]
    if args.timeout <= 0 or args.poll <= 0:
        raise FactoryError("factory wait: --timeout and --poll must be > 0")
    args.timeout = min(args.timeout, MAX_TIMEOUT)
    args.poll = max(args.poll, MIN_POLL)
    if mode == "url" and not _valid_status(args.status):
        raise FactoryError("factory wait: --status must be like 2xx, 3xx or 200")
    start = time.monotonic()
    deadline = start + args.timeout
    detail = ""
    while True:
        remaining = deadline - time.monotonic()
        if mode == "file":
            ok = _file_ok(Path(args.file), args.contains)
            detail = f"file {args.file}" + (f" contains {args.contains!r}" if args.contains is not None else "")
        elif mode == "cmd":
            ok = _cmd_ok(args.cmd, args.until_exit, remaining)
            detail = f"cmd exit {args.until_exit}: {args.cmd}"
        else:
            code = _url_status(args.url, remaining)
            ok = _status_ok(code, args.status)
            detail = f"url {args.url} status {args.status} (last {code})"
        elapsed = time.monotonic() - start
        if ok:
            print(f"WAIT-OK after {elapsed:.1f}s: {detail}")
            return 0
        if time.monotonic() + args.poll > deadline:
            print(f"WAIT-TIMEOUT after {elapsed:.1f}s: {detail}")
            return 1
        time.sleep(args.poll)


def register(sub) -> None:
    p = sub.add_parser("wait", help="wait for a file, a command exit or a URL status (bounded; replaces sleep loops)")
    p.add_argument("--file", help="wait until this file exists")
    p.add_argument("--contains", help="with --file: until it also contains this text")
    p.add_argument("--cmd", help="shell command to poll")
    p.add_argument("--until-exit", dest="until_exit", type=int, default=0, help="with --cmd: wanted exit code (default 0)")
    p.add_argument("--url", help="URL to poll (redirects are not followed)")
    p.add_argument("--status", default="2xx", help="with --url: 2xx, 3xx or an exact code (default 2xx)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds (default 120, capped at 600)")
    p.add_argument("--poll", type=float, default=DEFAULT_POLL, help="seconds between polls (default 1, minimum 0.2)")
    p.set_defaults(func=_cmd)
