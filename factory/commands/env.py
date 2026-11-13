"""`factory env up|down|check|logs|login|lease`: codified runtime environment (DESIGN §3, §11 env lease)."""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from ..core import (ensure_owner, 
    FactoryError,
    event,
    factory_home,
    git,
    now,
    read_json,
    repo_conf,
    repo_path_for_run,
    run_dir,
    run_load,
    run_update,
    secrets_path,
    session_hint,
    workspace_load,
    write_json,
)

IS_WIN = os.name == "nt"


# ---------- config helpers ----------

def capability(workspace: dict, alias: str) -> list[str]:
    """Errors that stop `alias` from being run as an environment. Empty list = capable."""
    errors: list[str] = []
    try:
        repo = repo_conf(workspace, alias)
    except FactoryError as err:
        return [str(err)]
    env = repo.get("env")
    if not isinstance(env, dict) or not env:
        return [f"{alias}: no env block in workspace"]
    if not env.get("start"):
        errors.append(f"{alias}: env.start missing")
    if not (env.get("health") or env.get("base_url")):
        errors.append(f"{alias}: env.health or env.base_url missing")
    for dep in env.get("depends_on") or []:
        try:
            repo_conf(workspace, dep)
        except FactoryError:
            errors.append(f"{alias}: depends_on {dep!r} is not a repo of the workspace")
    return errors


def base_url(workspace: dict, alias: str) -> str:
    env = repo_conf(workspace, alias).get("env") or {}
    if env.get("base_url"):
        return str(env["base_url"]).rstrip("/")
    health = env.get("health")
    if health:
        p = urlparse(health)
        return f"{p.scheme}://{p.netloc}"
    raise FactoryError(f"{alias}: env.base_url or env.health missing")


def _env_dir(ws: str) -> Path:
    return factory_home() / "env" / ws


def pid_path(ws: str, alias: str) -> Path:
    return _env_dir(ws) / f"{alias}.pid"


def log_path(ws: str, alias: str) -> Path:
    return _env_dir(ws) / f"{alias}.log"


def user_slug(email: str) -> str:
    """E-mail lowercased, runs of non-alphanumerics -> `_` (secrets key suffix and storage file name)."""
    return re.sub(r"[^a-z0-9]+", "_", str(email).lower()).strip("_")


def storage_state_path(ws: str, alias: str, user: str | None = None) -> Path:
    """`<alias>.storage.json` for the workspace default user, `<alias>.<slug>.storage.json` for an override user."""
    if user:
        return _env_dir(ws) / f"{alias}.{user_slug(user)}.storage.json"
    return _env_dir(ws) / f"{alias}.storage.json"


def login_command(ws: str, alias: str, user: str | None) -> str:
    return f"factory env login {ws} {alias}" + (f" --user {user}" if user else "")


def read_record(ws: str, alias: str) -> dict | None:
    """Pid record {pid, cwd, head, started, create_time}; a legacy bare pid becomes {pid}."""
    try:
        text = pid_path(ws, alias).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, int) and not isinstance(data, bool):
        return {"pid": data}
    if isinstance(data, dict) and isinstance(data.get("pid"), int):
        return data
    return None


def _read_pid(ws: str, alias: str) -> int | None:
    rec = read_record(ws, alias)
    return rec["pid"] if rec else None


def _drop_record(ws: str, alias: str) -> None:
    try:
        pid_path(ws, alias).unlink()
    except OSError:
        pass


def _same_path(a, b) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _head_of(path: Path) -> str | None:
    try:
        return git(path, "rev-parse", "HEAD")
    except FactoryError:
        return None


def tail(path: Path, n: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


# ---------- processes ----------

def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout


def pid_alive(pid: int) -> bool:
    if IS_WIN:
        out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_create_time(pid: int) -> str | None:
    """Opaque process start stamp, to tell our process from a later one that reused its pid."""
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                    return None
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                k32.CloseHandle(handle)
        except (OSError, AttributeError, ValueError):
            return None
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[19]  # field 22: starttime (ticks since boot)
        except (OSError, IndexError):
            return None
    out = _run(["ps", "-o", "lstart=", "-p", str(pid)]).strip()
    return out or None


def record_state(rec: dict | None) -> str:
    """"gone" (no record or not running), "same" (create time matches), "other" (pid reused) or
    "unknown" (a create time could not be read on either side: identity cannot be verified)."""
    if not rec or not pid_alive(rec["pid"]):
        return "gone"
    if not rec.get("create_time"):
        return "unknown"  # legacy record or unreadable at start: never assume it is ours
    cur = process_create_time(rec["pid"])
    if cur is None:
        return "unknown"
    return "same" if cur == rec["create_time"] else "other"


def record_alive(rec: dict | None) -> bool:
    """The recorded process still runs and is verifiably the same process (create time matches)."""
    return record_state(rec) == "same"


def _unknown_identity(alias: str, pid: int) -> str:
    kill = f"taskkill /T /F /PID {pid}" if IS_WIN else f"kill {pid}"
    return (f"{alias}: pid {pid} is running but its create time cannot be read, so factory cannot tell whether it is "
            f"the process it started; NOT killed. Ask the user to check and stop it (e.g. `{kill}`), then retry.")


def process_name(pid: int) -> str:
    if IS_WIN:
        out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).strip()
        if out.startswith('"'):
            return out.split('","')[0].strip('"')
        return "unknown"
    out = _run(["ps", "-p", str(pid), "-o", "comm="]).strip()
    return out or "unknown"


def port_owner(port: int) -> tuple[int, str] | None:
    """(pid, process name) of the process listening on `port`, or None if not found."""
    if IS_WIN:
        for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].rsplit(":", 1)[-1] == str(port):
                    pid = int(parts[4])
                    return pid, process_name(pid)
        return None
    if shutil.which("lsof"):
        out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"])
        pid, name = None, "unknown"
        for line in out.splitlines():
            if line.startswith("p") and pid is None:
                pid = int(line[1:])
            elif line.startswith("c") and pid is not None and name == "unknown":
                name = line[1:]
        return (pid, name) if pid is not None else None
    return None


def port_in_use(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        fam = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def kill_tree(pid: int) -> None:
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        time.sleep(0.5)
        if not pid_alive(pid):
            return


def health_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as err:
        return err.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _health_url(workspace: dict, alias: str) -> str:
    env = repo_conf(workspace, alias).get("env") or {}
    return env.get("health") or base_url(workspace, alias)


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


def lease_path(ws: str) -> Path:
    return _env_dir(ws) / "lease.json"


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


def lease_read(ws: str) -> dict:
    """{run, session, pid, acquired_at, heartbeat, queue: [{run, session, pid, since, seen}]}; run None = free."""
    try:
        data = json.loads(lease_path(ws).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("run", None)
    if not isinstance(data.get("queue"), list):
        data["queue"] = []
    data["queue"] = [w for w in data["queue"] if isinstance(w, dict) and w.get("run")]
    return data


def _lease_write(ws: str, lease: dict) -> None:
    if not lease.get("run") and not lease.get("queue"):
        try:  # free and nobody waiting: no file (a present lease.json reads as "held" elsewhere)
            lease_path(ws).unlink()
        except OSError:
            pass
        return
    write_json(lease_path(ws), lease)


def lease_stale(lease: dict, ttl: int) -> str | None:
    """Why the held lease may be reclaimed (heartbeat older than the TTL, or the owning run finished), else None."""
    run_id = lease.get("run")
    if not run_id:
        return None
    age = _age(lease.get("heartbeat"))
    if age is None or age > ttl:
        return f"heartbeat {_dur(age)} old > ttl {ttl}s"
    fin = _finished(run_id)
    return f"run {run_id} is {fin}" if fin else None


def _clear_holder(lease: dict) -> None:
    lease.update(run=None, session=None, pid=None, acquired_at=None, heartbeat=None)


def _prune(lease: dict, ttl: int) -> list[tuple[str, str]]:
    """Drop a stale holder and stale/duplicate waiters. Returns [(reclaimed run, reason)]."""
    reclaimed: list[tuple[str, str]] = []
    reason = lease_stale(lease, ttl)
    if reason:
        reclaimed.append((lease["run"], reason))
        _clear_holder(lease)
    keep, seen = [], set()
    for w in lease["queue"]:
        age = _age(w.get("seen"))
        if w["run"] in seen or age is None or age > WAITER_STALE_S or _finished(w["run"]):
            continue
        seen.add(w["run"])
        keep.append(w)
    lease["queue"] = keep
    return reclaimed


def lease_try(ws: str, run_id: str, ttl: int, wait: bool = False) -> dict:
    """One atomic acquire attempt. FIFO: a free lease goes to the head of the queue. With `wait` a refused run
    joins (or refreshes its place in) the queue. Returns {granted, fresh, position, lease, reclaimed}."""
    with _file_lock(_lock_path(ws)):
        lease = lease_read(ws)
        reclaimed = _prune(lease, ttl)
        stamp = now()
        queue = lease["queue"]
        fresh = False
        if lease.get("run") == run_id:
            lease["heartbeat"] = stamp
            granted = True
        elif not lease.get("run") and (not queue or queue[0]["run"] == run_id):
            lease.update(run=run_id, session=session_hint() or None, pid=os.getpid(), acquired_at=stamp, heartbeat=stamp)
            lease["queue"] = [w for w in queue if w["run"] != run_id]
            granted = fresh = True
        else:
            granted = False
            if wait:
                mine = next((w for w in queue if w["run"] == run_id), None)
                if mine:
                    mine.update(seen=stamp, pid=os.getpid())
                else:
                    queue.append({"run": run_id, "session": session_hint() or None, "pid": os.getpid(),
                                  "since": stamp, "seen": stamp})
        position = 0 if granted else next((i + 1 for i, w in enumerate(lease["queue"]) if w["run"] == run_id), None)
        _lease_write(ws, lease)
    return {"granted": granted, "fresh": fresh, "position": position, "lease": lease, "reclaimed": reclaimed}


def lease_release(ws: str, run_id: str, force: bool = False) -> str:
    with _file_lock(_lock_path(ws)):
        lease = lease_read(ws)
        holder = lease.get("run")
        queued = any(w["run"] == run_id for w in lease["queue"])
        lease["queue"] = [w for w in lease["queue"] if w["run"] != run_id]
        if holder and (holder == run_id or force):
            _clear_holder(lease)
            msg = f"lease of {ws} released (was held by {holder})"
        elif queued:
            msg = f"{run_id} left the queue of {ws}; lease still held by {holder or 'nobody'}"
        elif holder:
            raise FactoryError(f"lease of {ws} is held by run {holder}, not {run_id}; "
                               "pass --force to release it anyway")
        else:
            msg = f"lease of {ws} is not held"
        _lease_write(ws, lease)
    return msg


def _leave_queue(ws: str, run_id: str) -> None:
    with _file_lock(_lock_path(ws)):
        lease = lease_read(ws)
        lease["queue"] = [w for w in lease["queue"] if w["run"] != run_id]
        _lease_write(ws, lease)


def lease_heartbeat(ws: str, run_id: str | None) -> bool:
    """Refresh the heartbeat when `run_id` holds the lease. True when it holds it."""
    if not run_id or not lease_path(ws).is_file():
        return False
    with _file_lock(_lock_path(ws)):
        lease = lease_read(ws)
        if lease.get("run") != run_id:
            return False
        lease["heartbeat"] = now()
        _lease_write(ws, lease)
    return True


def _holder_text(lease: dict) -> str:
    if not lease.get("run"):
        return "free"
    return f"held by {lease['run']} since {_hhmm(lease.get('acquired_at'))}"


def require_lease(workspace: dict, run_id: str | None, what: str) -> None:
    """With `env.lease: true`, `run_id` must hold the env lease (its heartbeat is refreshed). No-op otherwise."""
    enabled, _ = lease_conf(workspace)
    if not enabled:
        return
    ws = workspace["name"]
    acquire = f"`factory env lease acquire {ws} --run {run_id or '<run>'} --wait`"
    if not run_id:
        raise FactoryError(f"{what}: env.lease is on for {ws}; pass --run <run> holding the env lease ({acquire})")
    if not lease_heartbeat(ws, run_id):
        raise FactoryError(f"{what}: run {run_id} does not hold the env lease of {ws} "
                           f"({_holder_text(lease_read(ws))}); {acquire} first")


def _record(run_id: str | None, kind: str, **data) -> None:
    """Best-effort event on a run (never fails the env command)."""
    if not run_id:
        return
    try:
        run_update(run_id, lambda r: event(r, kind, **data))
    except (FactoryError, OSError):
        pass


def lease_status(ws: str, ttl: int) -> dict:
    lease = lease_read(ws)
    return {
        "workspace": ws, "holder": lease.get("run"), "session": lease.get("session"), "pid": lease.get("pid"),
        "acquired_at": lease.get("acquired_at"), "heartbeat": lease.get("heartbeat"),
        "age_s": None if not lease.get("run") else _age(lease.get("acquired_at")),
        "heartbeat_age_s": None if not lease.get("run") else _age(lease.get("heartbeat")),
        "ttl_s": ttl, "stale": lease_stale(lease, ttl),
        "queue": [{"run": w["run"], "since": w.get("since"), "wait_s": _age(w.get("since")),
                   "session": w.get("session")} for w in lease["queue"]],
    }


# ---------- up / down / check ----------

def _cwd_for(workspace: dict, run: dict | None, alias: str) -> Path:
    repo = repo_conf(workspace, alias)
    base = repo_path_for_run(run, workspace, alias) if run else Path(repo["path"])
    return (base / ((repo.get("env") or {}).get("cwd") or ".")).resolve()


def _up_one(workspace: dict, run: dict | None, alias: str, seen: set[str], out: list[str]) -> bool:
    if alias in seen:
        return True
    seen.add(alias)
    errs = capability(workspace, alias)
    if errs:
        raise FactoryError("; ".join(errs))
    repo = repo_conf(workspace, alias)
    alias = repo["alias"]
    env = repo["env"]
    ws = workspace["name"]
    for dep in env.get("depends_on") or []:
        if not _up_one(workspace, run, dep, seen, out):
            return False

    health = _health_url(workspace, alias)
    cwd = _cwd_for(workspace, run, alias)
    head = _head_of(cwd) if cwd.is_dir() else None
    # record check -> port checks -> Popen -> pid write under the env lock: two concurrent ups cannot both start
    with _file_lock(_lock_path(ws)):
        status, proc = _start_locked(env, ws, alias, health, cwd, head, out)
    if status != "started":
        return status == "up"
    log = log_path(ws, alias)

    deadline = time.time() + float(env.get("startup_timeout_s") or 120)
    while time.time() < deadline:
        if health_ok(health):
            out.append(f"{alias}: up (pid {proc.pid}) {health}")
            return True
        if proc.poll() is not None:
            out.append(f"{alias}: process exited with code {proc.returncode} before becoming healthy")
            break
        time.sleep(0.5)
    else:
        out.append(f"{alias}: not healthy after {env.get('startup_timeout_s') or 120}s ({health})")
    out.append(f"--- last 40 log lines ({log}) ---\n{tail(log)}")
    return False


def _start_locked(env: dict, ws: str, alias: str, health: str, cwd: Path, head: str | None,
                  out: list[str]) -> tuple[str, subprocess.Popen | None]:
    """("up", None) already running for this request, ("fail", None) refused, ("started", proc)."""
    rec = read_record(ws, alias)
    state = record_state(rec) if rec else "gone"
    if state == "unknown":
        out.append(_unknown_identity(alias, rec["pid"]))
        return "fail", None
    if state == "same":
        if not _same_path(rec.get("cwd"), cwd) or (rec.get("head") or None) != head:
            out.append(
                f"{alias}: already up (pid {rec['pid']}) for {rec.get('cwd') or '?'} at "
                f"{str(rec.get('head') or '?')[:8]}, but this request is {cwd} at {str(head or '?')[:8]}. "
                f"Run `factory env down {ws} {alias}` first, then up again.")
            return "fail", None
        if health_ok(health):
            out.append(f"{alias}: already up (pid {rec['pid']})")
            return "up", None
        kill_tree(rec["pid"])  # stale: ours, alive but unhealthy; restart below
        _drop_record(ws, alias)
    elif rec:
        if state == "other":
            out.append(f"{alias}: pid {rec['pid']} now belongs to another process (create time differs); not touched")
        _drop_record(ws, alias)  # our process is gone
    for port in env.get("ports") or []:
        if port_in_use(int(port)):
            owner = port_owner(int(port))
            who = f"PID {owner[0]} ({owner[1]})" if owner else "unknown process (could not resolve PID)"
            out.append(
                f"{alias}: port {port} is already in use by {who}, not started by factory. "
                f"Ask the user to stop it (e.g. {'taskkill /F /PID ' + str(owner[0]) if owner and IS_WIN else 'kill ' + str(owner[0]) if owner else 'free the port'}) and retry."
            )
            return "fail", None
    if not env.get("ports") and health_ok(health):
        out.append(f"{alias}: unknown server already answering at {health}, not started by factory. "
                   "Ask the user to stop it (or declare env.ports to see its owner) and retry.")
        return "fail", None

    if not cwd.is_dir():
        raise FactoryError(f"{alias}: cwd {cwd} does not exist")
    start = env["start"]
    shell = isinstance(start, str)
    cmd: list[str] | str = start
    if not shell:
        cmd = list(start)
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd[0] = resolved
    proc_env = {**os.environ, **{k: str(v) for k, v in (env.get("env_vars") or {}).items()}}
    _env_dir(ws).mkdir(parents=True, exist_ok=True)
    log = log_path(ws, alias)
    kwargs: dict = {}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    with open(log, "wb") as logf:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=proc_env, stdin=subprocess.DEVNULL,
            stdout=logf, stderr=subprocess.STDOUT, shell=shell, **kwargs,
        )
    pid_path(ws, alias).write_text(json.dumps({
        "pid": proc.pid, "cwd": str(cwd), "head": head, "started": now(),
        "create_time": process_create_time(proc.pid)}), encoding="utf-8")
    return "started", proc


def up(workspace: dict, alias: str, run: dict | None = None) -> tuple[bool, list[str]]:
    require_lease(workspace, run["id"] if run else None, f"env up {alias}")
    out: list[str] = []
    ok = _up_one(workspace, run, alias, set(), out)
    return ok, out


def down(workspace: dict, alias: str, run_id: str | None = None, force: bool = False) -> str:
    """Stop the env of `alias`. Refused when a live env lease is held by another run than `run_id`, unless
    `force`. The stop is recorded on `run_id` (and, when forced, on the holder's run)."""
    repo = repo_conf(workspace, alias)
    ws, alias = workspace["name"], repo["alias"]
    _, ttl = lease_conf(workspace)
    lease = lease_read(ws)
    holder = lease.get("run")
    other = holder if holder and holder != run_id and not lease_stale(lease, ttl) else None
    if other and not force:
        raise FactoryError(f"{alias}: the env lease of {ws} is {_holder_text(lease)}; not stopped. "
                           f"Pass --run <run> if that run is yours, or --force to stop another run's env")
    with _file_lock(_lock_path(ws)):
        msg = _down_locked(ws, alias)
    _record(run_id, "env_down", repo=alias, result=msg, forced=bool(other), lease_holder=holder)
    if other:
        _record(other, "env_down_forced", repo=alias, by=run_id, result=msg)
    return msg + (f" (forced: lease held by {other})" if other else "")


def _down_locked(ws: str, alias: str) -> str:
    rec = read_record(ws, alias)
    if not rec:
        return f"{alias}: no pid file (nothing started by factory)"
    pid = rec["pid"]
    state = record_state(rec)
    if state == "unknown":
        raise FactoryError(_unknown_identity(alias, pid) + " (pid record kept)")
    if state == "same":
        kill_tree(pid)
    elif state == "other":
        _drop_record(ws, alias)
        return (f"{alias}: pid {pid} now belongs to another process (create time differs); "
                "not killed; pid record removed")
    _drop_record(ws, alias)
    return f"{alias}: stopped (pid {pid})"


def binding_error(workspace: dict, alias: str, worktree: Path, head: str | None) -> str | None:
    """None when the env of `alias` was started by `factory env up` from `worktree` at `head` and still runs."""
    repo = repo_conf(workspace, alias)
    ws, alias = workspace["name"], repo["alias"]
    rec = read_record(ws, alias)
    if not rec:
        return (f"env {alias}: not started by `factory env up {ws} {alias} --run <run>` (no pid record); "
                "evidence cannot be bound to the worktree")
    state = record_state(rec)
    if state == "unknown":
        return f"env {alias}: process {rec['pid']} identity cannot be verified (create time unreadable); ask the user"
    if state != "same":
        return f"env {alias}: recorded process {rec['pid']} is not running (or its pid was reused); env up again"
    want = (Path(worktree) / ((repo.get("env") or {}).get("cwd") or ".")).resolve()
    if not _same_path(rec.get("cwd"), want):
        return (f"env {alias}: running env serves {rec.get('cwd') or '?'}, not {want}; "
                f"`factory env down {ws} {alias}` then `env up --run`")
    if head and rec.get("head") != head:
        return (f"env {alias}: started at {str(rec.get('head') or '?')[:8]}, worktree HEAD is {head[:8]}; "
                f"`factory env down {ws} {alias}` then `env up --run`")
    return None


# ---------- login ----------

def read_secrets(ws: str) -> dict[str, str]:
    path = secrets_path(ws)
    if not path.is_file():
        raise FactoryError(f"missing secrets file: {path} (KEY=VALUE lines; <ALIAS>_USER / <ALIAS>_PASS)")
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def _credentials(ws: str, alias: str, user: str | None = None) -> tuple[str, str]:
    """(user, password). With an override `user` the password is `<ALIAS>_PASS_<slug>` / `PASS_<slug>` when
    present, else the workspace default password."""
    sec = read_secrets(ws)
    up = alias.upper().replace("-", "_")
    default_user = sec.get(f"{up}_USER") or sec.get("USER")
    pw = sec.get(f"{up}_PASS") or sec.get("PASS")
    if user:
        slug = user_slug(user)
        pw = sec.get(f"{up}_PASS_{slug}") or sec.get(f"PASS_{slug}") or pw
        if not pw:
            raise FactoryError(f"secrets/{ws}.env needs {up}_PASS (or PASS), or {up}_PASS_{slug}, for {user}")
        return user, pw
    if not default_user or not pw:
        raise FactoryError(f"secrets/{ws}.env needs {up}_USER and {up}_PASS (or USER and PASS)")
    return default_user, pw


def factory_root() -> Path:
    return Path(__file__).resolve().parents[2]


def login(workspace: dict, alias: str, user: str | None = None, run_id: str | None = None) -> tuple[bool, str]:
    require_lease(workspace, run_id, f"env login {alias}")  # the storage state is shared per workspace
    repo = repo_conf(workspace, alias)
    alias = repo["alias"]
    env = repo.get("env") or {}
    if not env.get("login"):
        raise FactoryError(f"{alias}: env.login missing")
    override = user
    user, pw = _credentials(workspace["name"], alias, override)
    cli = factory_root() / "browser" / "dist" / "cli.js"
    if not cli.is_file():
        raise FactoryError(f"browser CLI not built: {cli}")
    node = shutil.which("node")
    if not node:
        raise FactoryError("node not found on PATH")
    out = storage_state_path(workspace["name"], alias, override)
    out.parent.mkdir(parents=True, exist_ok=True)
    cfg = out.with_name(f"{alias}.login.json")  # the CLI reads --login-json as a file
    cfg.write_text(json.dumps(env["login"]), encoding="utf-8")
    cmd = [node, str(cli), "login", "--base-url", base_url(workspace, alias), "--login-json", str(cfg), "--out", str(out)]
    # credentials go through the environment, never argv (process listings show argv)
    proc_env = {**os.environ, "FACTORY_LOGIN_USER": user, "FACTORY_LOGIN_PASS": pw}
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=180, env=proc_env)
    text = (proc.stdout + proc.stderr).replace(pw, "***").replace(user, "***")
    return proc.returncode == 0, text.strip()[-2000:]


# ---------- CLI ----------

def _load(args) -> tuple[dict, dict | None]:
    ws = workspace_load(args.workspace)
    run = run_load(args.run) if getattr(args, "run", None) else None
    if run:
        ensure_owner(run)
    return ws, run


def _cmd_up(args) -> int:
    ws, run = _load(args)
    ok, lines = up(ws, args.repo, run)
    print("\n".join(lines))
    return 0 if ok else 1


def _cmd_down(args) -> int:
    ws, _ = _load(args)
    print(down(ws, args.repo, getattr(args, "run", None), getattr(args, "force", False)))
    return 0


def _cmd_check(args) -> int:
    ws, _ = _load(args)
    if getattr(args, "run", None):
        try:
            lease_heartbeat(ws["name"], args.run)
        except FactoryError:
            pass
    errs = capability(ws, args.repo)
    if errs:
        print("\n".join(errs))
        return 1
    health = _health_url(ws, args.repo)
    ok = health_ok(health)
    print(f"{args.repo}: {'healthy' if ok else 'NOT healthy'} {health}")
    return 0 if ok else 1


def _cmd_logs(args) -> int:
    ws, _ = _load(args)
    alias = repo_conf(ws, args.repo)["alias"]
    print(tail(log_path(ws["name"], alias), args.lines))
    return 0


def _cmd_login(args) -> int:
    ws, _ = _load(args)
    user = getattr(args, "user", None)
    ok, text = login(ws, args.repo, user, getattr(args, "run", None))
    if text:
        print(text)
    if ok:
        print(f"storage state: {storage_state_path(ws['name'], repo_conf(ws, args.repo)['alias'], user)}")
    return 0 if ok else 1


def _print_status(st: dict) -> None:
    ws = st["workspace"]
    if not st["holder"]:
        print(f"lease {ws}: free")
    else:
        line = (f"lease {ws}: held by {st['holder']} (session {_session_short(st['session'])}, pid {st['pid']}) "
                f"since {_hhmm(st['acquired_at'])}, age {_dur(st['age_s'])}, "
                f"heartbeat {_dur(st['heartbeat_age_s'])} ago (ttl {st['ttl_s']}s)")
        print(line + (f" STALE: {st['stale']}" if st["stale"] else ""))
    for i, w in enumerate(st["queue"], 1):
        print(f"  {i}. {w['run']} waiting {_dur(w['wait_s'])} (session {_session_short(w['session'])})")


def _cmd_lease(args) -> int:
    workspace = workspace_load(args.workspace)
    ws = workspace["name"]
    _, ttl = lease_conf(workspace)
    if args.action == "status":
        st = lease_status(ws, ttl)
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            _print_status(st)
        return 0
    if not args.run:
        raise FactoryError(f"env lease {args.action} needs --run <run>")
    run = run_load(args.run)
    ensure_owner(run)
    if run.get("workspace") != ws:
        raise FactoryError(f"run {args.run} belongs to workspace {run.get('workspace')!r}, not {ws!r}")
    if args.action == "release":
        msg = lease_release(ws, args.run, args.force)
        _record(args.run, "env_lease", action="release", workspace=ws, result=msg)
        print(json.dumps({"released": True, "message": msg}) if args.json else msg)
        return 0
    deadline = time.monotonic() + args.timeout if args.timeout is not None else None
    last = None
    try:
        while True:
            res = lease_try(ws, args.run, ttl, wait=args.wait)
            for old, reason in res["reclaimed"]:
                print(f"reclaimed stale lease of {old}: {reason}")
                _record(old, "env_lease", action="reclaimed", workspace=ws, by=args.run, reason=reason)
            lease = res["lease"]
            if res["granted"]:
                if res["fresh"]:
                    _record(args.run, "env_lease", action="acquire", workspace=ws)
                if args.json:
                    print(json.dumps({"granted": True, "run": args.run, "lease": lease}, indent=2))
                else:
                    print(f"lease {ws}: acquired by {args.run}" + ("" if res["fresh"] else " (already held; heartbeat refreshed)"))
                return 0
            if lease.get("run"):
                line = f"held by {lease['run']} since {_hhmm(lease.get('acquired_at'))}"
            else:
                line = f"free, but {lease['queue'][0]['run']} is ahead in the queue"
            if not args.wait:
                if args.json:
                    print(json.dumps({"granted": False, "run": args.run, "lease": lease}, indent=2))
                else:
                    print(f"lease {ws}: {line}; not acquired (use --wait to queue)")
                return 1
            line = f"waiting: position {res['position']} in queue for {ws}, {line}"
            if line != last and not args.json:
                print(line, flush=True)
                last = line
            if deadline is not None and time.monotonic() > deadline:
                _leave_queue(ws, args.run)
                if args.json:
                    print(json.dumps({"granted": False, "run": args.run, "timeout": True, "lease": lease}, indent=2))
                else:
                    print(f"lease {ws}: timed out after {args.timeout:g}s; left the queue")
                return 1
            time.sleep(max(0.05, args.poll))
    except KeyboardInterrupt:
        _leave_queue(ws, args.run)
        raise


def register(sub) -> None:
    p = sub.add_parser("env", help="start/stop/check the runtime environment of a repo")
    esub = p.add_subparsers(dest="env_cmd", required=True)
    for name, fn in (("up", _cmd_up), ("down", _cmd_down), ("check", _cmd_check), ("logs", _cmd_logs), ("login", _cmd_login)):
        sp = esub.add_parser(name)
        sp.add_argument("workspace")
        sp.add_argument("repo")
        if name in ("up", "check"):
            sp.add_argument("--run", help="use the worktree of this run (with env.lease on, up needs the run to hold the lease)")
        if name == "down":
            sp.add_argument("--run", help="the run stopping its env (must hold the env lease when one is held)")
            sp.add_argument("--force", action="store_true", help="stop even though another run holds the env lease")
        if name == "login":
            sp.add_argument("--user", help="log in as this e-mail instead of the workspace default user")
            sp.add_argument("--run", help="the run logging in (must hold the env lease when env.lease is on)")
        if name == "logs":
            sp.add_argument("--lines", type=int, default=40)
        sp.set_defaults(func=fn)
    lp = esub.add_parser("lease", help="one env at a time per workspace: acquire/release/status of the env lease")
    lp.add_argument("action", choices=("acquire", "release", "status"))
    lp.add_argument("workspace")
    lp.add_argument("--run", help="run that acquires/releases the lease")
    lp.add_argument("--wait", action="store_true", help="acquire: queue (FIFO) and poll until granted")
    lp.add_argument("--timeout", type=float, help="acquire --wait: give up after this many seconds")
    lp.add_argument("--poll", type=float, default=LEASE_POLL_S, help="acquire --wait: poll interval in seconds")
    lp.add_argument("--force", action="store_true", help="release: drop a lease held by another run")
    lp.add_argument("--json", action="store_true")
    lp.set_defaults(func=_cmd_lease)
