"""The only network code of the CLI: requests to a loopback origin declared in the config, redirects never followed [L-73, L-82]."""
from __future__ import annotations

import http.client
import socket
import time
from urllib.parse import urlsplit

from .util import FactoryError

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def split(url: str) -> tuple[str, int, str, bool]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.hostname not in LOOPBACK:
        raise FactoryError(f"{url!r} is not a loopback http(s) origin; the CLI only talks to the env origin declared in factory.toml [L-82]")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), path, parts.scheme == "https"


def join(base_url: str, path: str) -> str:
    """base_url + path, always on the same origin: a path with `//` or a scheme would change the origin."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise FactoryError(f"path {path!r} must start with a single / so that it stays on the env origin [L-82]")
    return base_url.rstrip("/") + path


def request(method: str, url: str, headers: dict | None = None, body: bytes | None = None, timeout: float = 30) -> tuple[int, dict, bytes]:
    """(status, lower-cased headers, body). Redirects are returned as they are. Raises OSError when nothing answers."""
    host, port, path, secure = split(url)
    cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    conn = cls(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers={"Connection": "close", **(headers or {})})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read(8 << 20)
    except http.client.HTTPException as exc:
        raise OSError(str(exc)) from exc
    finally:
        conn.close()


def port_free(host: str, port: int) -> bool:
    """True when nothing listens on the loopback port. Binding answers at once, where connecting to a closed port can take
    a second on Windows. SO_REUSEADDR only on POSIX: on Windows it would let a bind succeed on a busy port."""
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE") is False:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def listening(url: str) -> bool:
    return not port_free(*split(url)[:2])


def healthy(url: str, timeout: float = 5) -> bool:
    """Healthy means a status below 500 with no redirect followed (a 302 to a login page is healthy) [L-73]."""
    if not listening(url):
        return False
    try:
        return request("GET", url, timeout=timeout)[0] < 500
    except OSError:
        return False


def wait_until(check, timeout_s: float, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout_s
    while True:
        if check():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(step)
