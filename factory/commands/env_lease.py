"""Env slots and per-repo leases (DESIGN section 11): slot math (ports, placeholders, cross-repo references, hand-off
URLs), the workspace env lock and lease allocation (acquire, release, heartbeat, queue, shared holders, legacy file).

Split out of `env.py`, which re-exports every name defined here (callers and tests keep `from .env import ...`).
Process helpers (`pid_alive`, `process_create_time`) stay in `env.py` and are looked up there at call time, so a test
that patches them on `env` still affects the lease code.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import json
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from ..core import (
    FactoryError,
    event,
    factory_home,
    now,
    repo_conf,
    run_dir,
    run_load,
    run_update,
    session_hint,
    workspace_load,
    write_json,
)


def _env_dir(ws: str) -> Path:
    return factory_home() / "env" / ws


def pid_alive(pid: int) -> bool:
    from . import env
    return env.pid_alive(pid)


def process_create_time(pid: int) -> str | None:
    from . import env
    return env.process_create_time(pid)


# ---------- slots: several runs of one workspace on one machine (DESIGN section 11) ----------

PLACEHOLDER_RE = re.compile(r"\{(port\d*|slot)\}")
REF_RE = re.compile(r"\{(base_url|port):([A-Za-z0-9_.\-]+)\}")  # cross-repo: {base_url:<ALIAS>}, {port:<ALIAS>} (first port)
_URL_PORT_RE = re.compile(r"^([a-z][a-z0-9+.\-]*://)([^/?#]*?)(?::(\d+))?([/?#].*)?$", re.I | re.S)
_HOSTPORT_RE = re.compile(r"(?:(https?)://)?([A-Za-z0-9_.\-]+):(\d{2,5})(?!\d)")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def slot_conf(env) -> tuple[int, int]:
    """(`slots`, `port_stride`) of a repo env block; (1, 0) when absent or invalid (`workspace check` rejects those)."""
    env = env if isinstance(env, dict) else {}
    n, stride = env.get("slots", 1), env.get("port_stride", 0)
    return (n if _is_int(n) and n >= 1 else 1), (stride if _is_int(stride) and stride >= 0 else 0)


def slots_configured(env) -> bool:
    return isinstance(env, dict) and ("slots" in env or "port_stride" in env)


def slot_key(alias: str, slot: int = 0) -> str:
    """Name of a slot's pid/log/lease record: the alias for slot 0 (today's names), `<alias>@<k>` otherwise."""
    return alias if not slot else f"{alias}@{slot}"


def run_slot(run: dict | None, alias: str) -> int:
    """Slot the run holds for `alias` (`run.json` env_slots), 0 when none is recorded."""
    slots = (run or {}).get("env_slots")
    v = slots.get(alias) if isinstance(slots, dict) else None
    return v if _is_int(v) and v >= 0 else 0


def url_port(url) -> int | None:
    m = _URL_PORT_RE.match(url) if isinstance(url, str) else None
    return int(m.group(3)) if m and m.group(3) else None


def shift_url_port(url, delta: int):
    """`url` with its explicit port moved by `delta` (unchanged without a port or without a delta)."""
    if not delta or not isinstance(url, str):
        return url
    m = _URL_PORT_RE.match(url)
    if not m or not m.group(3):
        return url
    return f"{m.group(1)}{m.group(2)}:{int(m.group(3)) + delta}{m.group(4) or ''}"


def shift_text_ports(text: str, known: set[int], delta: int) -> str:
    """Every `:<port>` of `text` whose port is in `known` moved by `delta`."""
    if not delta:
        return text
    return re.sub(r":(\d+)(?!\d)", lambda m: f":{int(m.group(1)) + delta}" if int(m.group(1)) in known else m.group(0), text)


def _fill(text: str, ports: list[int], slot: int) -> str:
    def sub(m):
        name = m.group(1)
        if name == "slot":
            return str(slot)
        i = int(name[4:] or 0)
        if i >= len(ports):
            raise FactoryError(f"placeholder {{{name}}} but env.ports has {len(ports)} port(s)")
        return str(ports[i])
    return PLACEHOLDER_RE.sub(sub, text)


def _literal_ports(text: str, base_ports: list[int], delta: int) -> str:
    if not delta or not base_ports:
        return text
    pat = re.compile(r"(?<!\d)(" + "|".join(str(p) for p in sorted(set(base_ports), reverse=True)) + r")(?!\d)")
    return pat.sub(lambda m: str(int(m.group(1)) + delta), text)


def slot_start(start, base_ports: list[int], slot: int, delta: int):
    """The start command of a slot: `{port}`/`{port0}`/`{port1}`/`{slot}` placeholders are substituted when any argument
    has one, otherwise the literal base port numbers of the arguments are moved by the slot's offset."""
    items = [start] if isinstance(start, str) else [str(a) for a in start]
    ports = [p + delta for p in base_ports]
    uses = any(PLACEHOLDER_RE.search(a) for a in items)
    out = [_fill(a, ports, slot) if uses else _literal_ports(a, base_ports, delta) for a in items]
    return out[0] if isinstance(start, str) else out


def handoff_origin(success_url_contains) -> tuple[str | None, int | None]:
    """(host, port) of the origin named in `login.success_url_contains` (the fixed URL a legacy login redirects to)."""
    m = _HOSTPORT_RE.search(success_url_contains) if isinstance(success_url_contains, str) else None
    return (m.group(2), int(m.group(3))) if m else (None, None)


def handoff_rewrite_url(url: str, host: str | None, from_port: int, to_port: int) -> str:
    """Hand-off URL rewrite (pure): a URL on `from_port` (and `host`, when given) moves to `to_port`; path, query and
    token are kept. Any other URL is returned unchanged."""
    m = _URL_PORT_RE.match(url) if isinstance(url, str) else None
    if not m or not m.group(3) or int(m.group(3)) != from_port:
        return url
    bare = m.group(2).rsplit("@", 1)[-1]
    if host and bare.lower() != host.lower():
        return url
    return f"{m.group(1)}{m.group(2)}:{to_port}{m.group(4) or ''}"


def ref_aliases(text) -> set[str]:
    """Aliases named by `{base_url:<ALIAS>}` / `{port:<ALIAS>}` placeholders in `text` (a string or a list of them)."""
    items = [text] if isinstance(text, str) else [str(a) for a in text] if isinstance(text, list) else []
    return {m.group(2) for a in items for m in REF_RE.finditer(a)}


def _ref_value(workspace: dict, run: dict | None, kind: str, other: str) -> str:
    repo = repo_conf(workspace, other)  # unknown alias -> FactoryError
    env = slot_env(workspace, run, repo["alias"], refs=False)  # no refs inside: A -> B -> A cannot loop
    if kind == "port":
        ports = env.get("ports") or []
        port = int(ports[0]) if ports else url_port(env.get("base_url")) or url_port(env.get("health"))
        if not port:
            raise FactoryError(f"{{port:{other}}}: {repo['alias']} has no env.ports (nor a port in base_url/health)")
        return str(port)
    if env.get("base_url"):
        return str(env["base_url"]).rstrip("/")
    if env.get("health"):
        p = urlparse(env["health"])
        return f"{p.scheme}://{p.netloc}"
    raise FactoryError(f"{{base_url:{other}}}: {repo['alias']} has no env.base_url or env.health")


def resolve_refs(workspace: dict, run: dict | None, value):
    """`{base_url:<ALIAS>}` / `{port:<ALIAS>}` replaced by the value of that repo in the run's slot for it (slot 0 without
    one). Works on a string or a list of strings; unknown alias -> FactoryError."""
    if isinstance(value, list):
        return [resolve_refs(workspace, run, a) for a in value]
    if not isinstance(value, str) or "{" not in value:
        return value
    return REF_RE.sub(lambda m: _ref_value(workspace, run, m.group(1), m.group(2)), value)


def slot_env(workspace: dict, run: dict | None, alias: str, slot: int | None = None, refs: bool = True) -> dict:
    """Copy of the repo's `env` block as seen by `slot` (default: the run's slot): `ports`, the port of `health` and
    `base_url`, the `start` command, `env_vars` placeholders and the port in `login.success_url_contains` moved by
    slot * port_stride. A repo without `slots`/`port_stride` returns its env block untouched."""
    env = _slot_env(workspace, run, alias, slot)
    if not refs:
        return env
    if env.get("start"):
        env["start"] = resolve_refs(workspace, run, env["start"])
    if isinstance(env.get("env_vars"), dict):
        env["env_vars"] = {key: resolve_refs(workspace, run, v) for key, v in env["env_vars"].items()}
    return env


def _slot_env(workspace: dict, run: dict | None, alias: str, slot: int | None) -> dict:
    repo = repo_conf(workspace, alias)
    env = dict(repo.get("env") or {})
    k = run_slot(run, repo["alias"]) if slot is None else slot
    if not slots_configured(env):
        if k:
            raise FactoryError(f"{repo['alias']}: slot {k} requested but env.slots is not configured")
        return env
    n, stride = slot_conf(env)
    if not 0 <= k < n:
        raise FactoryError(f"{repo['alias']}: slot {k} is out of range (env.slots={n})")
    if k and not stride:
        raise FactoryError(f"{repo['alias']}: env.port_stride is required for slot {k}")
    delta = k * stride
    base_ports = [int(p) for p in env.get("ports") or []]
    ports = [p + delta for p in base_ports]
    known = set(base_ports) | {p for p in (url_port(env.get("health")), url_port(env.get("base_url"))) if p}
    if "ports" in env:
        env["ports"] = ports
    for key in ("health", "base_url"):
        if env.get(key):
            env[key] = shift_url_port(env[key], delta)
    if env.get("start"):
        env["start"] = slot_start(env["start"], base_ports, k, delta)
    if isinstance(env.get("env_vars"), dict):
        env["env_vars"] = {key: (_fill(str(v), ports, k) if PLACEHOLDER_RE.search(str(v)) else v) for key, v in env["env_vars"].items()}
    login = env.get("login")
    if isinstance(login, dict) and isinstance(login.get("success_url_contains"), str):
        hp = handoff_origin(login["success_url_contains"])[1]
        if hp and login.get("handoff_rewrite") is True:
            known.add(hp)
        env["login"] = dict(login, success_url_contains=shift_text_ports(login["success_url_contains"], known, delta))
    return env


def is_shared(workspace: dict, run: dict | None, alias: str) -> bool:
    """With `env.lease` on, a run with no worktree for a `shareable` repo uses it through a shared lease."""
    if not run or not lease_conf(workspace)[0]:
        return False
    repo = repo_conf(workspace, alias)
    if (repo.get("env") or {}).get("shareable") is not True:
        return False
    return not ((run.get("repos") or {}).get(repo["alias"]) or {}).get("worktree")


# ---------- lock + lease (DESIGN §11) ----------

LOCK_TIMEOUT_S = 15.0
LOCK_STALE_S = 120.0     # a lock file older than this was left by a dead process
LEASE_TTL_S = 900        # default `env.lease_ttl_s`
WAITER_STALE_S = 120.0   # a queued waiter that has not polled for this long leaves the queue
LEASE_POLL_S = 2.0
FINISHED_PHASES = ("done", "blocked")
_LOCKS_HELD: set[str] = set()  # lock paths this process holds (re-entrant: `up` -> lease check, deps)


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = LOCK_TIMEOUT_S):
    """Exclusive cross-process lock: `path` created with O_CREAT|O_EXCL, removed on exit. Re-entrant in-process;
    a lock file older than LOCK_STALE_S is reclaimed."""
    key = os.path.normcase(os.path.abspath(str(path)))
    if key in _LOCKS_HELD:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_S:
                    path.unlink()
                    continue
            except OSError:
                pass
        except PermissionError:
            pass  # Windows: the lock file is being deleted by its owner
        else:
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            break
        if time.monotonic() > deadline:
            raise FactoryError(f"env lock {path} is held by another process; retry, or delete it if stale")
        time.sleep(0.02)
    _LOCKS_HELD.add(key)
    try:
        yield
    finally:
        _LOCKS_HELD.discard(key)
        try:
            path.unlink()
        except OSError:
            pass


def lease_path(ws: str, key: str | None = None) -> Path:
    """`env/<ws>/<alias>[@<k>]/lease.json` (one per repo slot); without `key` the legacy workspace-level
    `env/<ws>/lease.json`, still honoured as a lease on every repo."""
    return _env_dir(ws) / "lease.json" if key is None else _env_dir(ws) / key / "lease.json"


def _lock_path(ws: str) -> Path:
    return _env_dir(ws) / "env.lock"


def lease_conf(workspace: dict) -> tuple[bool, int]:
    """(`env.lease` on, `env.lease_ttl_s`) from the workspace-level `env` block."""
    env = workspace.get("env") if isinstance(workspace.get("env"), dict) else {}
    ttl = env.get("lease_ttl_s", LEASE_TTL_S)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
        ttl = LEASE_TTL_S
    return env.get("lease") is True, int(ttl)


def _age(stamp) -> float | None:
    try:
        at = _dt.datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (_dt.datetime.now(_dt.timezone.utc) - at).total_seconds())


def _hhmm(stamp) -> str:
    try:
        at = _dt.datetime.fromisoformat(str(stamp))
    except ValueError:
        return "?"
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    return at.astimezone().strftime("%H:%M")


def _dur(seconds) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _session_short(hint) -> str:
    if not isinstance(hint, dict):
        return "-"
    for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_AGENT_ID", "CLAUDE_CODE_AGENT_ID"):
        if hint.get(k):
            return str(hint[k])[:8]
    return "-"


def _finished(run_id) -> str | None:
    """"done"/"blocked" when the run is finished, "missing" when it has no run.json; None otherwise (or unreadable)."""
    if not run_id:
        return None
    if not (run_dir(str(run_id)) / "run.json").is_file():
        return "missing"
    try:
        phase = run_load(str(run_id)).get("phase")
    except (FactoryError, OSError):
        return None
    return phase if phase in FINISHED_PHASES else None


def _ws_dict(workspace) -> dict:
    return workspace_load(workspace) if isinstance(workspace, str) else workspace


def lease_read(ws: str, key: str | None = None) -> dict:
    """One lease file: {run, session, pid, acquired_at, heartbeat, queue: [{run, session, pid, since, seen, ticket, req}],
    shared: [{run, session, pid, since, heartbeat}]}; run None = free. `queue` and `shared` live in the slot-0 file of
    a repo; `key` None reads the legacy workspace-level file."""
    try:
        data = json.loads(lease_path(ws, key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if key is None and is_legacy_marker(data):
        return {"run": None, "queue": [], "shared": [], "marker": LEGACY_MARKER}  # per-repo holders are the truth
    data.setdefault("run", None)
    queue, shared = data.get("queue"), data.get("shared")
    data["queue"] = [w for w in queue if isinstance(w, dict) and w.get("run")] if isinstance(queue, list) else []
    data["shared"] = [e for e in shared if isinstance(e, dict) and e.get("run")] if isinstance(shared, list) else []
    return data


def _lease_write(ws: str, lease: dict, key: str | None = None) -> None:
    path = lease_path(ws, key)
    if not lease.get("run") and not lease.get("queue") and not lease.get("shared"):
        try:  # free and nobody waiting: no file (a present lease.json reads as "held" elsewhere)
            path.unlink()
        except OSError:
            pass
        if key is not None:
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return
    write_json(path, lease)


def _holder_alive(lease: dict) -> bool:
    """The process that last refreshed the lease still runs on this machine (a recycled pid does not count when
    its create time was recorded)."""
    pid = lease.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:  # 1 = init, never a factory process
        return False
    if lease.get("host") and lease["host"] != socket.gethostname():
        return False
    if not pid_alive(pid):
        return False
    want = lease.get("pid_create")
    return not want or process_create_time(pid) == want


def _stamp_holder(lease: dict) -> None:
    lease.update(pid=os.getpid(), host=socket.gethostname(), pid_create=process_create_time(os.getpid()))


def lease_stale(lease: dict, ttl: int) -> str | None:
    """Why the held lease (or shared entry) may be reclaimed (heartbeat older than the TTL with no live holder
    process, or the owning run finished), else None."""
    run_id = lease.get("run")
    if not run_id:
        return None
    age = _age(lease.get("heartbeat"))
    if (age is None or age > ttl) and not _holder_alive(lease):
        return f"heartbeat {_dur(age)} old > ttl {ttl}s"
    fin = _finished(run_id)
    return f"run {run_id} is {fin}" if fin else None


def _clear_holder(lease: dict) -> None:
    lease.update(run=None, session=None, pid=None, host=None, pid_create=None, acquired_at=None, heartbeat=None)


def _prune(lease: dict, ttl: int) -> list[tuple[str, str]]:
    """Drop a stale holder, stale shared holders and stale/duplicate waiters. Returns [(reclaimed run, reason)]."""
    reclaimed: list[tuple[str, str]] = []
    reason = lease_stale(lease, ttl)
    if reason:
        reclaimed.append((lease["run"], reason))
        _clear_holder(lease)
    live = []
    for e in lease.get("shared") or []:
        why = lease_stale(e, ttl)
        if why:
            reclaimed.append((e["run"], why))
        else:
            live.append(e)
    lease["shared"] = live
    keep, seen = [], set()
    for w in lease["queue"]:
        age = _age(w.get("seen"))
        if w["run"] in seen or age is None or age > WAITER_STALE_S or _finished(w["run"]):
            continue
        seen.add(w["run"])
        keep.append(w)
    lease["queue"] = keep
    return reclaimed


# ----- repos, requests and allocation (all under the workspace env lock) -----

def lease_repos(workspace: dict) -> list[dict]:
    """Repos with an env block, in `order` (the fixed acquisition order): {alias, order, slots, shareable}."""
    out = []
    for r in workspace.get("repos") or []:
        env = r.get("env")
        if not isinstance(env, dict) or not env or not r.get("alias"):
            continue
        order = r.get("order")
        out.append({"alias": r["alias"], "order": order if _is_int(order) else 10 ** 6, "slots": slot_conf(env)[0],
                    "shareable": env.get("shareable") is True})
    return sorted(out, key=lambda r: (r["order"], r["alias"]))


def lease_mode(workspace: dict, run: dict | None, alias: str) -> str:
    """"shared" for a `shareable` repo the run has no worktree for (it does not change the repo), else "exclusive"."""
    repo = repo_conf(workspace, alias)
    if run and (repo.get("env") or {}).get("shareable") is True and not ((run.get("repos") or {}).get(repo["alias"]) or {}).get("worktree"):
        return "shared"
    return "exclusive"


def default_lease_repos(workspace: dict, run: dict) -> list[str]:
    """Runtime repos the run needs: its own repos that have an env block plus the `env.depends_on` closure. A run
    with none of them (review, parent runs) needs the whole workspace env, like the old workspace lease."""
    known = {r["alias"] for r in lease_repos(workspace)}
    out: list[str] = []
    queue = [a for a in (run.get("repos") or {})]
    while queue:
        try:
            alias = repo_conf(workspace, queue.pop(0))["alias"]
        except FactoryError:
            continue
        if alias in out or alias not in known:
            continue
        out.append(alias)
        queue.extend((repo_conf(workspace, alias).get("env") or {}).get("depends_on") or [])
    return out or sorted(known)


def lease_requests(workspace: dict, run: dict, repos: list[str] | None = None) -> dict[str, str]:
    """{alias: "exclusive"|"shared"} in acquisition order (repo `order`), for `repos` or the run's default repos."""
    wanted = [repo_conf(workspace, r)["alias"] for r in repos] if repos else default_lease_repos(workspace, run)
    order = {r["alias"]: i for i, r in enumerate(lease_repos(workspace))}
    for a in wanted:
        if a not in order:
            raise FactoryError(f"{a}: no env block in workspace, nothing to lease")
    return {a: lease_mode(workspace, run, a) for a in sorted(set(wanted), key=lambda a: order[a])}


def _load_state(workspace: dict) -> dict:
    ws = workspace["name"]
    return {r["alias"]: {**r, "leases": [lease_read(ws, slot_key(r["alias"], k)) for k in range(r["slots"])]}
            for r in lease_repos(workspace)}


def _dump(state: dict) -> dict:
    return {(a, k): json.dumps(l, sort_keys=True) for a, st in state.items() for k, l in enumerate(st["leases"])}


def _store(ws: str, state: dict, before: dict) -> None:
    for (alias, k), text in _dump(state).items():
        if text != before.get((alias, k)):
            _lease_write(ws, state[alias]["leases"][k], slot_key(alias, k))


LEGACY_MARKER = "per-repo leases"


def is_legacy_marker(data) -> bool:
    """The workspace-level lease.json written by this version for older factory versions (see `sync_legacy_marker`)."""
    return isinstance(data, dict) and data.get("marker") == LEGACY_MARKER


def _raw_legacy(ws: str) -> dict | None:
    try:
        data = json.loads(lease_path(ws).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def sync_legacy_marker(ws: str, state: dict) -> None:
    """Older factory versions only know the workspace-level `lease.json`: while any run holds a per-repo lease (slot or
    shared entry), keep a marker there so they see the workspace as held (`marker: "per-repo leases"`, `run` = one of
    the live holders so their staleness rule, which needs a run.json, keeps it; heartbeat and pid of this process).
    Free everywhere -> the marker is removed. A real legacy holder's file is never touched. This version reads the
    marker as free (`lease_read`). Caller holds the env lock."""
    raw = _raw_legacy(ws)
    if raw is not None and not is_legacy_marker(raw):
        return
    holders: list[str] = []
    for st in state.values():
        for lease in st["leases"]:
            if lease.get("run") and lease["run"] not in holders:
                holders.append(lease["run"])
        for e in st["leases"][0]["shared"]:
            if e["run"] not in holders:
                holders.append(e["run"])
    if not holders:
        if raw is not None:
            try:
                lease_path(ws).unlink()
            except OSError:
                pass
        return
    stamp = now()
    keep = raw.get("run") if raw and raw.get("run") in holders else holders[0]
    marker = {"run": keep, "marker": LEGACY_MARKER, "holders": holders, "session": None,
              "acquired_at": (raw or {}).get("acquired_at") or stamp, "heartbeat": stamp, "queue": []}
    _stamp_holder(marker)
    write_json(lease_path(ws), marker)


def _legacy_holder(ws: str, ttl: int, reclaimed: list) -> str | None:
    """Run holding the legacy workspace-level lease.json (it holds every repo); a stale or ownerless one is deleted. The
    marker this version keeps for older versions is not a holder."""
    if not lease_path(ws).is_file() or is_legacy_marker(_raw_legacy(ws)):
        return None
    lease = lease_read(ws)
    reason = lease_stale(lease, ttl) if lease.get("run") else "no holder"
    if reason:
        if lease.get("run"):
            reclaimed.append((lease["run"], f"{reason} (workspace lease)"))
        try:
            lease_path(ws).unlink()
        except OSError:
            pass
        return None
    return lease["run"]


def _ticket(w: dict) -> int:
    return w["ticket"] if _is_int(w.get("ticket")) else 0


def _waiters(state: dict) -> list[dict]:
    """Queued runs of every repo, FIFO by ticket, one entry per run: {run, ticket, req: {alias: mode}, ...}."""
    by_run: dict[str, dict] = {}
    for alias, st in state.items():
        for w in st["leases"][0]["queue"]:
            if w["run"] not in by_run or _ticket(w) < _ticket(by_run[w["run"]]):
                by_run[w["run"]] = {**w, "_alias": alias}
    out = []
    for w in by_run.values():
        req = {a: m for a, m in (w.get("req") or {}).items() if a in state and m in ("exclusive", "shared")} \
            if isinstance(w.get("req"), dict) else {}
        out.append({**w, "req": req or {w["_alias"]: "exclusive"}})
    return sorted(out, key=lambda w: (_ticket(w), w["run"]))


def _decide(state: dict, run_id: str, alias: str, mode: str, legacy: str | None):
    """Where `run_id` holds (or would hold) `alias`: ("excl", slot) | ("shared", 0) | ("legacy", 0); None = blocked.
    An exclusive lease takes the first free slot, slot 0 only while nobody else shares the repo; a shared lease needs
    slot 0 free of an exclusive holder and joins the other sharers."""
    if legacy:
        return ("legacy", 0) if legacy == run_id else None
    st = state[alias]
    leases = st["leases"]
    mine = next((k for k, l in enumerate(leases) if l.get("run") == run_id), None)
    if mine is not None:
        return ("excl", mine)
    shared = leases[0]["shared"]
    joined = any(e["run"] == run_id for e in shared)
    if mode == "shared":
        if joined:
            return ("shared", 0)
        return ("shared", 0) if not leases[0].get("run") and not st.get("writer_waiting") else None
    others = any(e["run"] != run_id for e in shared)
    for k, l in enumerate(leases):
        if not l.get("run") and (k > 0 or not others):
            return ("excl", k)
    return None


def _decide_all(state: dict, run_id: str, reqs: dict, legacy: str | None) -> dict | None:
    out = {}
    for alias, mode in reqs.items():
        dec = _decide(state, run_id, alias, mode, legacy)
        if dec is None:
            return None
        out[alias] = dec
    return out


def _apply(state: dict, run_id: str, decisions: dict, stamp: str, session) -> bool:
    """Record `decisions` in `state`; True when something was newly taken (False: only heartbeats refreshed)."""
    fresh = False
    for alias, (kind, k) in decisions.items():
        leases = state[alias]["leases"]
        if kind == "excl":
            lease = leases[k]
            if lease.get("run") != run_id:
                lease.update(run=run_id, session=session or None, acquired_at=stamp, heartbeat=stamp)
                leases[0]["shared"] = [e for e in leases[0]["shared"] if e["run"] != run_id]
                fresh = True
            lease["heartbeat"] = stamp
            _stamp_holder(lease)
        elif kind == "shared":
            entry = next((e for e in leases[0]["shared"] if e["run"] == run_id), None)
            if entry is None:
                entry = {"run": run_id, "session": session or None, "since": stamp}
                leases[0]["shared"].append(entry)
                fresh = True
            entry["heartbeat"] = stamp
            _stamp_holder(entry)
    return fresh


def _blocker_text(state: dict, alias: str, legacy: str | None, run_id: str) -> str:
    if legacy and legacy != run_id:
        return f"{alias}: held by {legacy} (workspace lease)"
    leases = state[alias]["leases"]
    parts = [f"{slot_key(alias, k)} held by {l['run']} since {_hhmm(l.get('acquired_at'))}"
             for k, l in enumerate(leases) if l.get("run") and l["run"] != run_id]
    if leases[0]["shared"] and any(e["run"] != run_id for e in leases[0]["shared"]):
        parts.append(f"{alias} shared by " + ", ".join(e["run"] for e in leases[0]["shared"] if e["run"] != run_id))
    return "; ".join(parts) or f"{alias}: reserved for a run ahead in the queue"


def lease_try(workspace, run_id: str, ttl: int, wait: bool = False, reqs: dict | None = None) -> dict:
    """One atomic, all-or-nothing acquire attempt of every repo in `reqs` ({alias: mode}, default: the run's repos).
    A refused run holds nothing. FIFO across repos: queued runs ahead of this one reserve what they can get (a run
    that cannot get everything reserves nothing, so no queue can deadlock another). With `wait` a refused run joins
    (or refreshes its place in) the queue of each requested repo. Returns {granted, fresh, position, slots, shared,
    blockers, reclaimed, leases}."""
    workspace = _ws_dict(workspace)
    ws = workspace["name"]
    if reqs is None:
        reqs = lease_requests(workspace, run_load(run_id))
    if not reqs:
        raise FactoryError(f"{ws}: no repo with an env block to lease")
    with _file_lock(_lock_path(ws)):
        state = _load_state(workspace)
        before = _dump(state)
        reclaimed: list[tuple[str, str]] = []
        legacy = _legacy_holder(ws, ttl, reclaimed)
        for alias, st in state.items():
            for k, lease in enumerate(st["leases"]):
                for old, reason in _prune(lease, ttl):
                    reclaimed.append((old, f"{reason} ({slot_key(alias, k)})"))
        stamp = now()
        waiters = _waiters(state)
        idx = next((i for i, w in enumerate(waiters) if w["run"] == run_id), None)
        ahead = [w for w in (waiters if idx is None else waiters[:idx]) if w["run"] != run_id]
        sim = copy.deepcopy(state)
        for w in ahead:
            dec = _decide_all(sim, w["run"], w["req"], legacy)
            if dec is not None:
                _apply(sim, w["run"], dec, stamp, None)
                continue
            for alias, mode in w["req"].items():  # a writer blocked by sharers stops new sharers from joining
                if mode == "exclusive" and _decide(sim, w["run"], alias, mode, legacy) is None:
                    sim[alias]["writer_waiting"] = True
        decisions = _decide_all(sim, run_id, reqs, legacy)
        fresh, position, blockers = False, 0, []
        if decisions is not None:
            fresh = _apply(state, run_id, decisions, stamp, session_hint() or None)
            for st in state.values():
                lease0 = st["leases"][0]
                lease0["queue"] = [w for w in lease0["queue"] if w["run"] != run_id]
            if legacy == run_id:
                old = lease_read(ws)
                old["heartbeat"] = stamp
                _stamp_holder(old)
                _lease_write(ws, old)
        else:
            blockers = [_blocker_text(sim, a, legacy, run_id) for a, m in reqs.items()
                        if _decide(sim, run_id, a, m, legacy) is None]
            if wait:
                ticket = _ticket(waiters[idx]) if idx is not None else time.time_ns()
                for alias, st in state.items():
                    lease0 = st["leases"][0]
                    mine = next((w for w in lease0["queue"] if w["run"] == run_id), None)
                    if alias not in reqs:
                        lease0["queue"] = [w for w in lease0["queue"] if w["run"] != run_id]
                    elif mine:
                        mine.update(seen=stamp, pid=os.getpid(), req=dict(reqs))
                    else:
                        lease0["queue"].append({"run": run_id, "session": session_hint() or None, "pid": os.getpid(),
                                                "since": stamp, "seen": stamp, "ticket": ticket, "req": dict(reqs)})
                position = next((i + 1 for i, w in enumerate(_waiters(state)) if w["run"] == run_id), 0) or None
        _store(ws, state, before)
        sync_legacy_marker(ws, state)
        slots = {a: k for a, (kind, k) in (decisions or {}).items()} if decisions else {}
        return {"granted": decisions is not None, "fresh": fresh, "position": position, "blockers": blockers,
                "slots": slots, "shared": [a for a, (kind, _) in (decisions or {}).items() if kind == "shared"],
                "reclaimed": reclaimed, "legacy": legacy == run_id,
                "leases": {a: state[a]["leases"][k] for a, k in slots.items() if a in state}}


def lease_held(workspace, run_id: str) -> dict:
    """What `run_id` holds now: {"exclusive": {alias: slot}, "shared": [alias], "legacy": bool}."""
    workspace = _ws_dict(workspace)
    ws = workspace["name"]
    out: dict = {"exclusive": {}, "shared": [], "legacy": False}
    out["legacy"] = lease_path(ws).is_file() and lease_read(ws).get("run") == run_id
    for alias, st in _load_state(workspace).items():
        for k, lease in enumerate(st["leases"]):
            if lease.get("run") == run_id:
                out["exclusive"][alias] = k
        if any(e["run"] == run_id for e in st["leases"][0]["shared"]):
            out["shared"].append(alias)
    return out


def lease_release_info(workspace, run_id: str, force: bool = False, repos: list[str] | None = None) -> dict:
    """Release what `run_id` holds on `repos` (default: everything, including the legacy workspace lease) and leave its
    queues. `force` also drops other runs' holds on those repos. Returns {message, released: [alias], queued}."""
    workspace = _ws_dict(workspace)
    ws = workspace["name"]
    with _file_lock(_lock_path(ws)):
        state = _load_state(workspace)
        before = _dump(state)
        chosen = [repo_conf(workspace, r)["alias"] for r in repos] if repos else list(state)
        released: list[tuple[str, str]] = []
        others: list[str] = []
        queued = False
        for alias in chosen:
            if alias not in state:
                continue
            leases = state[alias]["leases"]
            for k, lease in enumerate(leases):
                holder = lease.get("run")
                if holder and (holder == run_id or force):
                    released.append((slot_key(alias, k), holder))
                    _clear_holder(lease)
                elif holder:
                    others.append(f"{slot_key(alias, k)}: {holder}")
            keep = []
            for e in leases[0]["shared"]:
                if e["run"] == run_id or force:
                    released.append((f"{alias} (shared)", e["run"]))
                else:
                    keep.append(e)
            leases[0]["shared"] = keep
        for alias in (chosen if repos else list(state)):
            if alias in state:
                lease0 = state[alias]["leases"][0]
                queued = queued or any(w["run"] == run_id for w in lease0["queue"])
                lease0["queue"] = [w for w in lease0["queue"] if w["run"] != run_id]
        if not repos and lease_path(ws).is_file():
            legacy = lease_read(ws)
            if legacy.get("run") and (legacy["run"] == run_id or force):
                released.append(("workspace", legacy["run"]))
                try:
                    lease_path(ws).unlink()
                except OSError:
                    pass
            elif legacy.get("run"):
                others.append(f"workspace: {legacy['run']}")
        if released:
            msg = f"lease of {ws} released (" + ", ".join(f"{label} was held by {who}" for label, who in released) + ")"
        elif queued:
            msg = f"{run_id} left the queue of {ws}; lease still held by {', '.join(others) or 'nobody'}"
        elif others:
            raise FactoryError(f"lease of {ws} is held by run {others[0].split(': ', 1)[1]}, not {run_id}; "
                               "pass --force to release it anyway")
        else:
            msg = f"lease of {ws} is not held"
        _store(ws, state, before)
        sync_legacy_marker(ws, state)
        return {"message": msg, "released": sorted({label.split(" ")[0].split("@")[0] for label, _ in released}),
                "queued": queued}


def lease_release(workspace, run_id: str, force: bool = False, repos: list[str] | None = None) -> str:
    return lease_release_info(workspace, run_id, force, repos)["message"]


def _leave_queue(workspace, run_id: str) -> None:
    workspace = _ws_dict(workspace)
    ws = workspace["name"]
    with _file_lock(_lock_path(ws)):
        state = _load_state(workspace)
        before = _dump(state)
        for st in state.values():
            lease0 = st["leases"][0]
            lease0["queue"] = [w for w in lease0["queue"] if w["run"] != run_id]
        _store(ws, state, before)


def _lease_files(ws: str, alias: str | None = None) -> list[str | None]:
    """Keys of the lease files that exist: None (legacy workspace file) and `<alias>[@k]` directories."""
    keys: list[str | None] = [None] if lease_path(ws).is_file() else []
    root = _env_dir(ws)
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "lease.json").is_file())
    except OSError:
        names = []
    for name in names:
        if alias is None or name == alias or name.startswith(alias + "@"):
            keys.append(name)
    return keys


def lease_heartbeat(ws: str, run_id: str | None, alias: str | None = None) -> bool:
    """Refresh the heartbeat of every lease `run_id` holds (exclusive slots, shared entries, the legacy workspace
    lease), only on `alias` when given. True when it holds one."""
    if not run_id:
        return False
    keys = _lease_files(ws, alias)
    if not keys:
        return False
    held = False
    with _file_lock(_lock_path(ws)):
        for key in _lease_files(ws, alias):
            lease = lease_read(ws, key)
            touched = False
            if lease.get("run") == run_id:
                lease["heartbeat"] = now()
                _stamp_holder(lease)  # the process that keeps proving is the one whose death frees the lease
                touched = True
            for e in lease["shared"]:
                if e["run"] == run_id:
                    e["heartbeat"] = now()
                    _stamp_holder(e)
                    touched = True
            if touched:
                _lease_write(ws, lease, key)
                held = True
        marker = _raw_legacy(ws) if held else None
        if is_legacy_marker(marker):  # keep the marker older versions read alive while a holder works
            marker["heartbeat"] = now()
            _stamp_holder(marker)
            write_json(lease_path(ws), marker)
    return held


def _holder_text(lease: dict) -> str:
    if not lease.get("run"):
        return "free"
    return f"held by {lease['run']} since {_hhmm(lease.get('acquired_at'))}"


def _repo_holder_text(ws: str, alias: str | None) -> str:
    """Who holds the lease(s) of `alias` (or of the whole workspace): `Web held by r1 since 12:00; Web shared by r2`."""
    parts = []
    for key in _lease_files(ws, alias):
        lease = lease_read(ws, key)
        label = "workspace" if key is None else key
        if lease.get("run"):
            parts.append(f"{label} {_holder_text(lease)}")
        parts.extend(f"{label} shared by {e['run']}" for e in lease["shared"])
    return "; ".join(parts) or "free"


def require_lease(workspace: dict, run_id: str | None, what: str, alias: str | None = None) -> None:
    """With `env.lease: true`, `run_id` must hold the env lease of `alias` (every repo without `alias`); its heartbeat
    is refreshed. No-op otherwise."""
    enabled, _ = lease_conf(workspace)
    if not enabled:
        return
    ws = workspace["name"]
    acquire = f"`factory env lease acquire {ws} --run {run_id or '<run>'} --wait`"
    if not run_id:
        raise FactoryError(f"{what}: env.lease is on for {ws}; pass --run <run> holding the env lease ({acquire})")
    if alias is not None:
        alias = repo_conf(workspace, alias)["alias"]
    if not lease_heartbeat(ws, run_id, alias):
        raise FactoryError(f"{what}: run {run_id} does not hold the env lease of {ws}{f' for {alias}' if alias else ''} "
                           f"({_repo_holder_text(ws, alias)}); {acquire} first")


def _record(run_id: str | None, kind: str, **data) -> None:
    """Best-effort event on a run (never fails the env command)."""
    if not run_id:
        return
    try:
        run_update(run_id, lambda r: event(r, kind, **data))
    except (FactoryError, OSError):
        pass


def lease_conflict(workspace: dict, run: dict, alias: str) -> str | None:
    """Another run holds what `run` needs of `alias` (its slot, the legacy workspace lease, or slot 0 sharers)."""
    enabled, ttl = lease_conf(workspace)
    if not enabled:
        return None
    ws, alias = workspace["name"], repo_conf(workspace, alias)["alias"]
    shared = is_shared(workspace, run, alias)
    keys: list[str | None] = [slot_key(alias, 0 if shared else run_slot(run, alias))]
    if lease_path(ws).is_file():
        keys.insert(0, None)
    for key in keys:
        lease = lease_read(ws, key)
        if lease.get("run") and lease["run"] != run["id"] and not lease_stale(lease, ttl):
            return f"env lease of {ws} is {_repo_holder_text(ws, alias)}"
    if not shared and keys[-1] == alias:  # exclusive slot 0 is blocked by other runs sharing the repo
        lease0 = lease_read(ws, alias)
        if any(e["run"] != run["id"] and not lease_stale(e, ttl) for e in lease0["shared"]):
            return f"env lease of {ws} is {_repo_holder_text(ws, alias)}"
    return None


def _lease_row(key: str, alias: str, slot: int, lease: dict, ttl: int) -> dict:
    held = bool(lease.get("run"))
    return {
        "repo": alias, "slot": slot, "key": key, "holder": lease.get("run"), "session": lease.get("session"),
        "pid": lease.get("pid"), "acquired_at": lease.get("acquired_at"), "heartbeat": lease.get("heartbeat"),
        "age_s": _age(lease.get("acquired_at")) if held else None,
        "heartbeat_age_s": _age(lease.get("heartbeat")) if held else None, "ttl_s": ttl, "stale": lease_stale(lease, ttl),
        "shared": [{"run": e["run"], "since": e.get("since"), "heartbeat_age_s": _age(e.get("heartbeat")),
                    "stale": lease_stale(e, ttl)} for e in lease["shared"]],
    }


def lease_status(workspace, ttl: int, repos: list[str] | None = None) -> dict:
    """{workspace, ttl_s, repos: [row per repo slot], queue: [waiting runs], legacy: row | None}."""
    workspace = _ws_dict(workspace)
    ws = workspace["name"]
    state = _load_state(workspace)
    chosen = {repo_conf(workspace, r)["alias"] for r in repos} if repos else set(state)
    rows = [_lease_row(slot_key(a, k), a, k, l, ttl) for a, st in state.items() if a in chosen for k, l in enumerate(st["leases"])]
    legacy = None
    if lease_path(ws).is_file():
        old = lease_read(ws)
        if old.get("run"):
            legacy = _lease_row("workspace", "*", 0, old, ttl)
    queue = [{"run": w["run"], "since": w.get("since"), "wait_s": _age(w.get("since")), "session": w.get("session"),
              "repos": w["req"]} for w in _waiters(state) if chosen & set(w["req"])]
    return {"workspace": ws, "ttl_s": ttl, "repos": rows, "queue": queue, "legacy": legacy}


def register(sub) -> None:
    """No CLI of its own: `factory env lease` is registered by `env.py` (the CLI loads every commands module)."""
