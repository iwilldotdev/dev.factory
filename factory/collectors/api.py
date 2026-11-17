"""`api` collector: HTTP request against the run's env (DESIGN §4)."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

_MISSING = object()


def jsonpath(data, path: str):
    """Minimal JSONPath: $.a.b, $.items[0].id. Returns _MISSING when absent."""
    if not path.startswith("$"):
        raise ValueError(f"unsupported path {path!r}")
    cur = data
    for key, idx in re.findall(r"\.([^.\[\]]+)|\[(\d+)\]", path[1:]):
        try:
            cur = cur[key] if key else cur[int(idx)]
        except (KeyError, IndexError, TypeError):
            return _MISSING
    return cur


def _request(method: str, url: str, body, headers: dict, timeout: float):
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _request_capped(method: str, url: str, body, headers: dict, cap: float):
    """`_request` under a wall-clock cap (urlopen's own timeout is per socket operation). Raises TimeoutError."""
    box: dict = {}

    def work():
        try:
            box["ok"] = _request(method, url, body, headers, cap)
        except BaseException as err:  # re-raised in the caller
            box["err"] = err

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(cap)
    if t.is_alive():
        raise TimeoutError(f"no complete response within {cap:g}s (check cap)")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def collect(check: dict, ctx) -> dict:
    from factory.collectors.browser import check_cap
    base = check.get("base_url") or (ctx.env or {}).get("base_url")
    if not base:
        return {"status": "error", "summary": "no base_url in repo env block"}
    url = base.rstrip("/") + "/" + str(check.get("path", "")).lstrip("/")
    method = (check.get("method") or "GET").upper()
    cap = check_cap(check, ctx)
    try:
        status, raw = _request_capped(method, url, check.get("body"), check.get("headers") or {}, cap)
    except TimeoutError as err:
        return {"status": "error", "summary": f"api check timed out at {url}: {err}",
                "details": {"timeout": True, "url": url}}
    except (urllib.error.URLError, OSError) as err:
        return {"status": "error", "summary": f"env not reachable at {url}: {err}",
                "details": {"fail_kind": "env", "url": url}}
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        payload = _MISSING
    expect = check.get("expect") or {}
    problems = []
    if "status" in expect and status != expect["status"]:
        problems.append(f"status {status} != {expect['status']}")
    for path, want in (expect.get("json") or {}).items():
        if payload is _MISSING:
            problems.append(f"{path}: response is not JSON")
            continue
        got = jsonpath(payload, path)
        if want == "exists":
            if got is _MISSING:
                problems.append(f"{path}: missing")
        elif want == "absent":
            if got is not _MISSING:
                problems.append(f"{path}: present")
        elif got is _MISSING or got != want:
            problems.append(f"{path}: {None if got is _MISSING else got!r} != {want!r}")
    if expect.get("body_contains") and expect["body_contains"] not in text:
        problems.append(f"body lacks {expect['body_contains']!r}")
    artifacts = []
    if ctx.out_dir is not None:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / "response.txt").write_text(f"HTTP {status}\n{text}", encoding="utf-8")
        artifacts.append(f"evidence/{check['id']}/response.txt")
    env_down = bool(problems) and status in (502, 503, 504) and expect.get("status") != status
    return {
        "status": "fail" if problems else "pass",
        "summary": "; ".join(problems) if problems else f"{method} {check.get('path')} -> {status}",
        "artifacts": artifacts,
        "details": {"url": url, "method": method, "http_status": status, "problems": problems,
                    "body_tail": text[-2000:], **({"fail_kind": "env"} if env_down else {})},
    }
