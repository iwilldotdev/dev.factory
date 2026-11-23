"""`factory env up|down|check|logs|login|lease`: codified runtime environment (DESIGN §3, §11 env lease)."""

from __future__ import annotations

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
    repo_conf,
    repo_path_for_env,
    run_load,
    target_serve_path,
    check_target,
    hotfix_of,
    run_targets,
    run_update,
    secrets_path,
    workspace_load,
    write_json,
)

from .env_lease import (  # noqa: F401  slot math and per-repo leases live there (re-exported)
    _env_dir,
    PLACEHOLDER_RE,
    REF_RE,
    _URL_PORT_RE,
    _HOSTPORT_RE,
    _is_int,
    slot_conf,
    slots_configured,
    slot_key,
    run_slot,
    url_port,
    shift_url_port,
    shift_text_ports,
    _fill,
    _literal_ports,
    slot_start,
    handoff_origin,
    handoff_rewrite_url,
    ref_aliases,
    _ref_value,
    resolve_refs,
    slot_env,
    _slot_env,
    is_shared,
    LOCK_TIMEOUT_S,
    LOCK_STALE_S,
    LEASE_TTL_S,
    WAITER_STALE_S,
    LEASE_POLL_S,
    FINISHED_PHASES,
    _LOCKS_HELD,
    _file_lock,
    lease_path,
    _lock_path,
    lease_conf,
    _age,
    _hhmm,
    _dur,
    _session_short,
    _finished,
    _ws_dict,
    lease_read,
    _lease_write,
    _holder_alive,
    _stamp_holder,
    lease_stale,
    _clear_holder,
    _prune,
    lease_repos,
    lease_mode,
    default_lease_repos,
    lease_requests,
    _load_state,
    _dump,
    _store,
    LEGACY_MARKER,
    is_legacy_marker,
    _raw_legacy,
    sync_legacy_marker,
    _legacy_holder,
    _ticket,
    _waiters,
    _decide,
    _decide_all,
    _apply,
    _blocker_text,
    lease_try,
    lease_held,
    lease_release_info,
    lease_release,
    _leave_queue,
    _lease_files,
    lease_heartbeat,
    _holder_text,
    _repo_holder_text,
    require_lease,
    _record,
    lease_conflict,
    _lease_row,
    lease_status,
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


def _health_url(workspace: dict, alias: str, slot: int = 0) -> str:
    env = slot_env(workspace, None, alias, slot, refs=False)
    return env.get("health") or base_url(workspace, alias, slot)


def base_url(workspace: dict, alias: str, slot: int = 0) -> str:
    env = slot_env(workspace, None, alias, slot, refs=False)
    if env.get("base_url"):
        return str(env["base_url"]).rstrip("/")
    health = env.get("health")
    if health:
        p = urlparse(health)
        return f"{p.scheme}://{p.netloc}"
    raise FactoryError(f"{alias}: env.base_url or env.health missing")


def _safe_run(run_id: str | None) -> dict | None:
    if not run_id:
        return None
    try:
        return run_load(run_id)
    except (FactoryError, OSError):
        return None


def pid_path(ws: str, alias: str, slot: int = 0) -> Path:
    return _env_dir(ws) / f"{slot_key(alias, slot)}.pid"


def log_path(ws: str, alias: str, slot: int = 0) -> Path:
    return _env_dir(ws) / f"{slot_key(alias, slot)}.log"


def user_slug(email: str) -> str:
    """E-mail lowercased, runs of non-alphanumerics -> `_` (secrets key suffix and storage file name)."""
    return re.sub(r"[^a-z0-9]+", "_", str(email).lower()).strip("_")


def storage_state_path(ws: str, alias: str, user: str | None = None, slot: int = 0) -> Path:
    """`<alias>.storage.json` for the workspace default user, `<alias>.<slug>.storage.json` for an override user. Keyed
    by slot like the pid/log files (`<alias>@<k>...` for slot k > 0; slot 0 keeps today's names): each slot logs in on
    its own origin, so two runs on two slots never overwrite each other's session."""
    key = slot_key(alias, slot)
    if user:
        return _env_dir(ws) / f"{key}.{user_slug(user)}.storage.json"
    return _env_dir(ws) / f"{key}.storage.json"


def login_json_path(ws: str, alias: str, slot: int = 0) -> Path:
    """The login block the browser CLI reads (`--login-json`), per slot: `<alias>.login.json` / `<alias>@<k>.login.json`."""
    return _env_dir(ws) / f"{slot_key(alias, slot)}.login.json"


def run_storage_slot(workspace: dict, run: dict | None, alias: str) -> int:
    """Slot whose storage state a run uses for `alias`: 0 for a shared repo, else the run's slot."""
    return 0 if is_shared(workspace, run, alias) else run_slot(run, repo_conf(workspace, alias)["alias"])


def login_command(ws: str, alias: str, user: str | None) -> str:
    return f"factory env login {ws} {alias}" + (f" --user {user}" if user else "")


def read_record(ws: str, alias: str, slot: int = 0) -> dict | None:
    """Pid record {pid, cwd, head, started, create_time}; a legacy bare pid becomes {pid}."""
    try:
        text = pid_path(ws, alias, slot).read_text(encoding="utf-8").strip()
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


def _read_pid(ws: str, alias: str, slot: int = 0) -> int | None:
    rec = read_record(ws, alias, slot)
    return rec["pid"] if rec else None


def _drop_record(ws: str, alias: str, slot: int = 0) -> None:
    try:
        pid_path(ws, alias, slot).unlink()
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


HTTP_SYS_PROBES = 3
HTTP_SYS_PROBE_GAP_S = 2.0


def is_http_sys(owner: tuple[int, str] | None) -> bool:
    """The listener is the Windows HTTP.sys kernel driver (PID 4 / "System"): IIS Express and friends sit behind it."""
    return bool(IS_WIN and owner and (owner[0] == 4 or str(owner[1]).strip().lower() == "system"))


def reprobe_http_sys(health: str) -> bool:
    """HTTP.sys hides the real process: judge by the answer instead, up to 3 probes 2 s apart."""
    for i in range(HTTP_SYS_PROBES):
        if health_ok(health):
            return True
        if i < HTTP_SYS_PROBES - 1:
            time.sleep(HTTP_SYS_PROBE_GAP_S)
    return False


def http_sys_detail(port) -> str:
    return f"port {port} held by HTTP.sys (likely IIS Express from another process)"


def _parent_pid(pid: int) -> int | None:
    if IS_WIN:
        try:
            import ctypes
            from ctypes import wintypes

            class PE(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_char * 260)]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            snap = k32.CreateToolhelp32Snapshot(2, 0)
            entry = PE()
            entry.dwSize = ctypes.sizeof(PE)
            try:
                ok = k32.Process32First(snap, ctypes.byref(entry))
                while ok:
                    if entry.th32ProcessID == pid:
                        return int(entry.th32ParentProcessID)
                    ok = k32.Process32Next(snap, ctypes.byref(entry))
            finally:
                k32.CloseHandle(snap)
        except (OSError, AttributeError, ValueError):
            pass
        return None
    out = _run(["ps", "-o", "ppid=", "-p", str(pid)]).strip()
    return int(out) if out.isdigit() else None


def is_protected_pid(pid: int) -> bool:
    """The current process or one of its ancestors: factory never kills these, whatever a pid record says."""
    cur, seen = os.getpid(), set()
    while cur and cur not in seen:
        if cur == pid:
            return True
        seen.add(cur)
        cur = _parent_pid(cur) or 0
    return False


def tree_kill_order(table: list[dict], root: int, protected=frozenset()) -> list[int]:
    """pids to kill for the process tree under `root`, leaf-first (root last). `table` rows: {pid, ppid, created}
    (`created` comparable). A process is a child only when it was created AFTER its parent: a stale ParentProcessId
    whose pid was reused by an unrelated process never links it. Protected pids are never returned and never
    descended through."""
    by_pid = {r["pid"]: r for r in table}
    if root not in by_pid or root in protected:
        return []
    kids: dict[int, list[int]] = {}
    for r in table:
        parent = by_pid.get(r["ppid"])
        if parent is not None and r["pid"] != r["ppid"] and r["created"] > parent["created"]:
            kids.setdefault(r["ppid"], []).append(r["pid"])
    order, seen, queue = [], {root}, [root]
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for k in kids.get(cur, []):
            if k not in seen and k not in protected:
                seen.add(k)
                queue.append(k)
    return order[::-1]


def _win_process_table() -> list[dict]:
    ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
          "@{n='C';e={if($_.CreationDate){$_.CreationDate.ToFileTimeUtc()}else{0}}} | ConvertTo-Json -Compress")
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(res.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"pid": int(r["ProcessId"]), "ppid": int(r["ParentProcessId"] or 0), "created": int(r["C"] or 0)}
            for r in data if isinstance(r, dict) and r.get("ProcessId") is not None]


def _protected_pids() -> set[int]:
    cur, seen = os.getpid(), set()
    while cur and cur not in seen:
        seen.add(cur)
        cur = _parent_pid(cur) or 0
    return seen


def kill_tree(pid: int) -> None:
    if is_protected_pid(pid):
        return
    if IS_WIN:
        order = tree_kill_order(_win_process_table(), pid, _protected_pids()) or [pid]
        for victim in order:  # leaf first; no `taskkill /T`: it follows stale parent links to unrelated processes
            subprocess.run(["taskkill", "/F", "/PID", str(victim)], capture_output=True)
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an answer: the probe never follows it (the redirect target may be a login host that is down)."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
CHECK_HEALTH_TIMEOUT_S = 20.0   # `env check` probe budget


def health_status(url: str, timeout: float = 3.0) -> int | None:
    """HTTP status of `url` (redirects are not followed), None when nothing answers."""
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def health_ok(url: str, timeout: float = 3.0) -> bool:
    """Any answer below 500 (2xx, 3xx without following it, 4xx) means the server is up."""
    status = health_status(url, timeout)
    return status is not None and status < 500


def env_fail_line(alias: str, kind: str, detail: str) -> str:
    """Machine-readable line: `ENV-FAIL repo=<alias> kind=<...> detail=<first line, <=200 chars>`."""
    first = (str(detail).strip().splitlines() or [""])[0][:200]
    return f"ENV-FAIL repo={alias} kind={kind} detail={first}"


# ---------- up / down / check ----------

def _cwd_for(workspace: dict, run: dict | None, alias: str, target: str | None = None) -> Path:
    repo = repo_conf(workspace, alias)
    base = (target_serve_path(run, workspace, alias, target) if run and target else None) \
        or (repo_path_for_env(run, workspace, alias) if run else Path(repo["path"]))
    return (base / ((repo.get("env") or {}).get("cwd") or ".")).resolve()


def _up_one(workspace: dict, run: dict | None, alias: str, seen: set[str], out: list[str],
            target: str | None = None) -> bool:
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
    require_lease(workspace, run["id"] if run else None, f"env up {alias}", alias)
    for dep in env.get("depends_on") or []:
        if not _up_one(workspace, run, dep, seen, out, target):
            return False

    shared = is_shared(workspace, run, alias)
    slot = 0 if shared else run_slot(run, alias)
    env = slot_env(workspace, run, alias, slot)  # ports, health, base_url and start of this run's slot
    health = _health_url(workspace, alias, slot)
    if shared:  # the run does not change this repo: it uses the running one and never starts, stops or restarts it
        if health_ok(health):
            out.append(f"{alias}: up (shared) {health}")
            return True
        detail = f"shared repo {alias} not up; ask its holder or start it with env up"
        out.append(f"{alias}: {detail}")
        out.append(env_fail_line(alias, "lease", detail))
        return False
    cwd = _cwd_for(workspace, run, alias, target)
    head = _head_of(cwd) if cwd.is_dir() else None
    # record check -> port checks -> Popen -> pid write under the env lock: two concurrent ups cannot both start
    with _file_lock(_lock_path(ws)):
        status, proc = _start_locked(env, ws, alias, health, cwd, head, out, slot, target, run["id"] if run else None)
    if status != "started":
        return status == "up"
    log = log_path(ws, alias, slot)

    deadline = time.time() + float(env.get("startup_timeout_s") or 120)
    while time.time() < deadline:
        if health_ok(health):
            out.append(f"{alias}: up (pid {proc.pid}) {health}")
            return True
        if proc.poll() is not None:
            out.append(f"{alias}: process exited with code {proc.returncode} before becoming healthy")
            out.append(env_fail_line(alias, "start", f"process exited with code {proc.returncode} before becoming healthy"))
            break
        time.sleep(0.5)
    else:
        out.append(f"{alias}: not healthy after {env.get('startup_timeout_s') or 120}s ({health})")
        out.append(env_fail_line(alias, "timeout", f"not healthy after {env.get('startup_timeout_s') or 120}s ({health})"))
    out.append(f"--- last 40 log lines ({log}) ---\n{tail(log)}")
    return False


def _http_sys_healthy(env: dict, health: str) -> bool:
    """Our recorded process sits behind HTTP.sys (PID 4 owns the port): its health answer is the only signal."""
    for port in env.get("ports") or []:
        if port_in_use(int(port)) and is_http_sys(port_owner(int(port))):
            return reprobe_http_sys(health)
    return False


def _start_locked(env: dict, ws: str, alias: str, health: str, cwd: Path, head: str | None,
                  out: list[str], slot: int = 0, target: str | None = None,
                  run_id: str | None = None) -> tuple[str, subprocess.Popen | None]:
    """("up", None) already running for this request, ("fail", None) refused, ("started", proc). `env` is the slot's
    env block (see `slot_env`)."""
    rec = read_record(ws, alias, slot)
    state = record_state(rec) if rec else "gone"
    if state == "unknown":
        out.append(_unknown_identity(alias, rec["pid"]))
        return "fail", None
    if state == "same":
        if rec.get("env_digest") and rec["env_digest"] != env_digest(env):
            out.append(env_fail_line(alias, "stale", "running env serves another resolved env (start/env_vars/ports)"))
            out.append(f"{alias}: already up (pid {rec['pid']}) with another resolved env (start/env_vars/ports differ). "
                       f"Run `factory env down {ws} {alias}` first, then up again.")
            return "fail", None
        if not _same_path(rec.get("cwd"), cwd) or ((rec.get("head") or None) != head and env.get("hot_reload") is not True):
            out.append(env_fail_line(alias, "stale", f"running for {rec.get('cwd') or '?'} at {str(rec.get('head') or '?')[:8]}, "
                                     f"request is {cwd} at {str(head or '?')[:8]}"))
            out.append(
                f"{alias}: already up (pid {rec['pid']}) for {rec.get('cwd') or '?'} at "
                f"{str(rec.get('head') or '?')[:8]}, but this request is {cwd} at {str(head or '?')[:8]}. "
                f"Run `factory env down {ws} {alias}` first, then up again.")
            return "fail", None
        if health_ok(health) or _http_sys_healthy(env, health):
            out.append(f"{alias}: already up (pid {rec['pid']})")
            return "up", None
        if is_protected_pid(rec["pid"]):
            out.append(f"{alias}: pid {rec['pid']} is the current process or its ancestor; NOT killed")
            out.append(env_fail_line(alias, "stale", f"pid {rec['pid']} is not killable (current process or ancestor)"))
            return "fail", None
        kill_tree(rec["pid"])  # stale: ours, alive but unhealthy; restart below
        _drop_record(ws, alias, slot)
    elif rec:
        if state == "other":
            out.append(f"{alias}: pid {rec['pid']} now belongs to another process (create time differs); not touched")
        _drop_record(ws, alias, slot)  # our process is gone
    for port in env.get("ports") or []:
        if port_in_use(int(port)):
            owner = port_owner(int(port))
            if is_http_sys(owner):
                out.append(f"{alias}: {http_sys_detail(port)}; not started by factory, not touched.")
                out.append(env_fail_line(alias, "port", http_sys_detail(port)))
                return "fail", None
            who = f"PID {owner[0]} ({owner[1]})" if owner else "unknown process (could not resolve PID)"
            out.append(
                f"{alias}: port {port} is already in use by {who}, not started by factory. "
                f"Ask the user to stop it (e.g. {'taskkill /F /PID ' + str(owner[0]) if owner and IS_WIN else 'kill ' + str(owner[0]) if owner else 'free the port'}) and retry."
            )
            out.append(env_fail_line(alias, "port", f"port {port} in use by {who}, not started by factory"))
            return "fail", None
    if not env.get("ports") and health_ok(health):
        out.append(f"{alias}: unknown server already answering at {health}, not started by factory. "
                   "Ask the user to stop it (or declare env.ports to see its owner) and retry.")
        out.append(env_fail_line(alias, "port", f"unknown server already answering at {health}, not started by factory"))
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
    if slots_configured(env):
        proc_env["FACTORY_SLOT"] = str(slot)
    _env_dir(ws).mkdir(parents=True, exist_ok=True)
    log = log_path(ws, alias, slot)
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
    record = {"pid": proc.pid, "cwd": str(cwd), "head": head, "started": now(),
              "create_time": process_create_time(proc.pid), "env_digest": env_digest(env)}
    if target:
        record["target"] = target
    if run_id:  # who started it: a hotfix run only switches targets of envs it started itself
        record["run"] = run_id
    pid_path(ws, alias, slot).write_text(json.dumps(record), encoding="utf-8")
    return "started", proc


def up(workspace: dict, alias: str, run: dict | None = None, target: str | None = None) -> tuple[bool, list[str]]:
    """Start the env of `alias` for `run`; `target` (a hotfix mirror) serves that target's worktree and is recorded."""
    require_lease(workspace, run["id"] if run else None, f"env up {alias}", alias)
    out: list[str] = []
    target = check_target(run, target) if run else None
    ok = _up_one(workspace, run, alias, set(), out, target)
    return ok, out


def down(workspace: dict, alias: str, run_id: str | None = None, force: bool = False, slot: int | None = None) -> str:
    """Stop the env of `alias` (the run's slot, else `slot`, else 0). Refused when a live env lease on that slot is held
    by another run than `run_id` (or the repo is shared with other runs), unless `force`; a run that holds the repo only
    shared never stops it. The stop is recorded on `run_id` (and, when forced, on the holder's run)."""
    repo = repo_conf(workspace, alias)
    ws, alias = workspace["name"], repo["alias"]
    run = _safe_run(run_id)
    if slot is None:
        slot = run_slot(run, alias)
    if is_shared(workspace, run, alias) and not force:
        raise FactoryError(f"{alias}: shared repo, this run does not own its process; not stopped. "
                           "Its exclusive holder stops it, or pass --force")
    _, ttl = lease_conf(workspace)
    holders: list[str] = []
    keys: list[str | None] = [slot_key(alias, slot)]
    if lease_path(ws).is_file():
        keys.insert(0, None)
    for key in keys:
        lease = lease_read(ws, key)
        if lease.get("run") and not lease_stale(lease, ttl):
            holders.append(lease["run"])
        if key == alias:  # slot 0 is also in use while other runs share the repo
            holders.extend(e["run"] for e in lease["shared"] if not lease_stale(e, ttl))
    holder = next((h for h in holders if h != run_id), None)
    other = holder
    if other and not force:
        raise FactoryError(f"{alias}: the env lease of {ws} is {_repo_holder_text(ws, alias)}; not stopped. "
                           f"Pass --run <run> if that run is yours, or --force to stop another run's env")
    with _file_lock(_lock_path(ws)):
        msg = _down_locked(ws, alias, slot)
    _record(run_id, "env_down", repo=alias, result=msg, forced=bool(other), lease_holder=holder)
    if other:
        _record(other, "env_down_forced", repo=alias, by=run_id, result=msg)
    return msg + (f" (forced: lease held by {other})" if other else "")


def _down_locked(ws: str, alias: str, slot: int = 0) -> str:
    rec = read_record(ws, alias, slot)
    if not rec:
        return f"{alias}: no pid file (nothing started by factory)"
    pid = rec["pid"]
    state = record_state(rec)
    if state == "unknown":
        raise FactoryError(_unknown_identity(alias, pid) + " (pid record kept)")
    if state == "same":
        if is_protected_pid(pid):
            raise FactoryError(f"{alias}: pid {pid} is the current process or its ancestor; NOT killed (pid record kept)")
        kill_tree(pid)
    elif state == "other":
        _drop_record(ws, alias, slot)
        return (f"{alias}: pid {pid} now belongs to another process (create time differs); "
                "not killed; pid record removed")
    _drop_record(ws, alias, slot)
    return f"{alias}: stopped (pid {pid})"


# ---------- deterministic test data: seed / reset / preflight (DESIGN section 14) ----------

DATA_TIMEOUT_S = 600


def ticket_digits(run: dict | None) -> str:
    key = str((((run or {}).get("tickets") or [{}])[0] or {}).get("key") or "")
    return "".join(re.findall(r"\d+", key))


def data_prefix(run: dict | None) -> str:
    """Prefix every record the checks create must carry, so a reset can delete exactly those: UAT<ticket digits>."""
    return f"UAT{ticket_digits(run)}"


def _data_env(workspace: dict, run: dict | None, alias: str) -> dict[str, str]:
    env = {**os.environ}
    env.update({k: str(v) for k, v in (slot_env(workspace, run, alias).get("env_vars") or {}).items()})
    first = ((run or {}).get("tickets") or [{}])[0] or {}
    env.update({"FACTORY_RUN": str((run or {}).get("id") or ""), "FACTORY_TICKET": str(first.get("key") or ""),
                "FACTORY_DATA_PREFIX": data_prefix(run)})
    return env


def _run_argv(workspace: dict, run: dict | None, alias: str, argv: list, cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), env=_data_env(workspace, run, alias), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=DATA_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124, f"[timeout after {DATA_TIMEOUT_S}s]"
    except OSError as err:
        return 127, str(err)
    return proc.returncode, ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()


def seed_conf(workspace: dict, alias: str) -> dict:
    seed = (repo_conf(workspace, alias).get("env") or {}).get("seed")
    return seed if isinstance(seed, dict) else {}


def data_run(workspace: dict, run: dict | None, alias: str, which: str) -> tuple[int, str]:
    """Run the repo's `env.seed.cmd` (which="cmd") or `env.seed.reset` in its worktree with FACTORY_RUN, FACTORY_TICKET and
    FACTORY_DATA_PREFIX set; records an `env_seed`/`env_reset` event with the exit code."""
    repo = repo_conf(workspace, alias)
    alias = repo["alias"]
    seed = seed_conf(workspace, alias)
    argv = seed.get(which)
    if not argv:
        raise FactoryError(f"{alias}: env.seed.{which} is not configured")
    base = repo_path_for_env(run, workspace, alias) if run else Path(repo["path"])
    code, text = _run_argv(workspace, run, alias, argv, (base / (seed.get("cwd") or ".")).resolve())
    _record((run or {}).get("id"), "env_seed" if which == "cmd" else "env_reset", repo=alias, exit=code)
    return code, text


def run_preflights(workspace: dict, run: dict | None, alias: str, target: str | None = None) -> list[tuple[str, int, str]]:
    """Failed `env.preflight` entries of the repo: [(name, exit, output)]. Empty when all pass."""
    repo = repo_conf(workspace, alias)
    out = []
    for item in (repo.get("env") or {}).get("preflight") or []:
        code, text = _run_argv(workspace, run, repo["alias"], item["cmd"], _cwd_for(workspace, run, repo["alias"], target))
        if code != 0:
            out.append((str(item.get("name") or "preflight"), code, text))
    return out


def hot_reload(repo: dict) -> bool:
    """`env.hot_reload: true`: the dev server (Vite HMR, watch mode) follows commits in its worktree, so a HEAD that
    moved since `env up` is not stale (a different cwd still is)."""
    return (repo.get("env") or {}).get("hot_reload") is True


def env_digest(env: dict) -> str:
    """sha256 of what a started process depends on besides its cwd/HEAD: the resolved `start` command, `env_vars`
    (after `{base_url:ALIAS}`/`{port:ALIAS}` and slot placeholders) and `ports` of the slot env (`slot_env`)."""
    import hashlib
    env_vars = env.get("env_vars") if isinstance(env.get("env_vars"), dict) else {}
    body = {"start": env.get("start"), "env_vars": {k: str(v) for k, v in env_vars.items()},
            "ports": [int(p) for p in env.get("ports") or []]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def env_digest_error(workspace: dict, run: dict | None, alias: str, rec: dict, slot: int) -> str | None:
    """The pid record carries `env_digest` and the run's slot env now resolves to another one (a referenced repo moved
    slot, a port/start/env_vars edit): the process does not serve this run's env. Records without it: None."""
    want = rec.get("env_digest")
    if not want:
        return None
    try:
        current = env_digest(slot_env(workspace, run, alias, slot))
    except FactoryError as err:
        return f"slot env cannot be resolved ({err})"
    if current != want:
        return ("serves another resolved env (start/env_vars/ports differ from the run's slot env, e.g. a "
                "{base_url:ALIAS} slot moved)")
    return None


def binding_error(workspace: dict, alias: str, worktree: Path, head: str | None, run: dict | None = None,
                  strict: bool = False) -> str | None:
    """None when the env of `alias` (in the slot of `run`, if any) was started by `factory env up` from `worktree` at
    `head` (with the run's resolved slot env, when the record carries `env_digest`) and still runs. A repo the run only
    shares is judged by its process alone: it serves another run's code. `strict` (`prove --final`) ignores
    `env.hot_reload`: final evidence must come from a process started at HEAD."""
    repo = repo_conf(workspace, alias)
    ws, alias = workspace["name"], repo["alias"]
    shared = is_shared(workspace, run, alias)
    slot = 0 if shared else run_slot(run, alias)
    rec = read_record(ws, alias, slot)
    if not rec:
        return (f"env {alias}: not started by `factory env up {ws} {alias} --run <run>` (no pid record); "
                "evidence cannot be bound to the worktree")
    state = record_state(rec)
    if state == "unknown":
        return f"env {alias}: process {rec['pid']} identity cannot be verified (create time unreadable); ask the user"
    if state != "same":
        return f"env {alias}: recorded process {rec['pid']} is not running (or its pid was reused); env up again"
    if shared:
        return None
    want = (Path(worktree) / ((repo.get("env") or {}).get("cwd") or ".")).resolve()
    if not _same_path(rec.get("cwd"), want):
        return (f"env {alias}: running env serves {rec.get('cwd') or '?'}, not {want}; "
                f"`factory env down {ws} {alias}` then `env up --run`")
    drift = env_digest_error(workspace, run, alias, rec, slot)
    if drift:
        return f"env {alias}: running env {drift}; `factory env down {ws} {alias}` then `env up --run`"
    if head and rec.get("head") != head and (strict or not hot_reload(repo)):
        return (f"env {alias}: started at {str(rec.get('head') or '?')[:8]}, worktree HEAD is {head[:8]}; "
                f"`factory env down {ws} {alias}` then `env up --run`")
    return None


def hot_reload_drift(workspace: dict, alias: str, head: str | None, run: dict | None = None) -> bool:
    """The env of `alias` is served only thanks to `env.hot_reload`: its process was started at another HEAD than
    `head` (evidence then records `details.hot_reload: true`, refused by `gate prove`)."""
    repo = repo_conf(workspace, alias)
    if not hot_reload(repo) or not head or is_shared(workspace, run, repo["alias"]):
        return False
    rec = read_record(workspace["name"], repo["alias"], run_slot(run, repo["alias"]))
    return bool(rec) and rec.get("head") != head


def shared_heads(workspace: dict, run: dict | None) -> dict:
    """{alias: HEAD of the serving process's cwd} of every repo the run uses shared (the runtime repos it needs that it
    has no worktree for): runtime evidence then depends on that code, which is another run's (DESIGN section 11)."""
    if not run or not lease_conf(workspace)[0]:
        return {}
    out: dict = {}
    for alias in default_lease_repos(workspace, run):
        try:
            if not is_shared(workspace, run, alias):
                continue
        except FactoryError:
            continue
        rec = read_record(workspace["name"], alias, 0)
        cwd = (rec or {}).get("cwd")
        out[alias] = _head_of(Path(cwd)) if cwd and Path(cwd).is_dir() else None
    return out


# ---------- preflight (env check, prove) ----------

EXIT_UNHEALTHY, EXIT_USAGE, EXIT_STALE, EXIT_CONFLICT = 1, 2, 3, 4


def stale_reason(workspace: dict, run: dict | None, alias: str, strict: bool = False,
                 target: str | None = None) -> str | None:
    """Why a running env of ours no longer serves the run worktree (other cwd, another resolved slot env, or started at
    another HEAD; `strict` = `prove --final`: a HEAD change is stale even with `env.hot_reload`). `target`: the hotfix
    mirror it must serve; absent, the target the record says it was started for (so a restart keeps it)."""
    if not run or is_shared(workspace, run, alias):
        return None  # a shared repo serves its holder's code: this run never restarts it
    repo = repo_conf(workspace, alias)
    rec = read_record(workspace["name"], repo["alias"], run_slot(run, repo["alias"]))
    if not rec or record_state(rec) != "same":
        return None
    if is_protected_pid(rec["pid"]):
        return None  # not a process factory may restart; the binding check reports the mismatch
    cwd = _cwd_for(workspace, run, repo["alias"], target or rec.get("target"))
    if not _same_path(rec.get("cwd"), cwd):
        return f"serves {rec.get('cwd') or '?'}, run worktree is {cwd}"
    try:
        drift = env_digest_error(workspace, run, repo["alias"], rec, run_slot(run, repo["alias"]))
    except FactoryError:
        drift = None
    if drift and drift.startswith("serves"):
        return drift
    head = _head_of(cwd) if cwd.is_dir() else None
    if head and rec.get("head") != head and (strict or not hot_reload(repo)):
        return f"started at {str(rec.get('head') or '?')[:8]}, worktree HEAD is {head[:8]}"
    return None


def _run_worktrees(run: dict) -> list[str]:
    out: list[str] = []
    for rec in (run.get("repos") or {}).values():
        if not isinstance(rec, dict):
            continue
        if rec.get("worktree"):
            out.append(str(rec["worktree"]))
        wts = rec.get("worktrees")
        if isinstance(wts, dict):
            out.extend(str(v) for v in wts.values() if v)
    return out


def _serves_a_target_of(workspace: dict, run: dict, alias: str, rec: dict) -> bool:
    """The record's cwd is what one of the run's targets resolves to for `alias` (a mirror worktree, a `target_paths`
    directory, or the plain resolution of a dependency without a worktree). Geometry only: who started it is
    `_started_for_target`."""
    try:
        return any(_same_path(rec.get("cwd"), _cwd_for(workspace, run, alias, t)) for t in run_targets(run))
    except FactoryError:
        return False


def _started_for_target(workspace: dict, run: dict, alias: str, rec: dict) -> bool:
    """The record is a hotfix env of `run`, so switching targets may stop it: (a) its cwd is the run's own worktree of
    `alias` for one of its targets, or (b) the record names this run as the one that started it (`run` field) and its cwd
    is what one of the run's targets resolves to (a `target_paths` directory, the main checkout of a dependency). A record
    without a run id, or another run's, served from outside this run's worktrees is someone else's, whatever it serves."""
    if not hotfix_of(run):
        return False
    rrec = (run.get("repos") or {}).get(alias) or {}
    own = [rrec.get("worktree"), *(rrec.get("worktrees") or {}).values()] if isinstance(rrec, dict) else []
    sub = ((repo_conf(workspace, alias).get("env") or {}).get("cwd") or ".")
    for wt in own:
        if wt and _same_path(rec.get("cwd"), (Path(wt) / sub).resolve()):
            return True
    return rec.get("run") == run.get("id") and _serves_a_target_of(workspace, run, alias, rec)


def deps_first(workspace: dict, aliases: list[str]) -> list[str]:
    """`aliases` ordered so that every repo comes after the `depends_on` repos it needs (stable for unrelated ones)."""
    wanted, seen, out = set(aliases), set(), []

    def visit(alias: str) -> None:
        if alias in seen:
            return
        seen.add(alias)
        try:
            deps = (repo_conf(workspace, alias).get("env") or {}).get("depends_on") or []
            for dep in deps:
                visit(repo_conf(workspace, dep)["alias"])
        except FactoryError:
            pass
        if alias in wanted:
            out.append(alias)

    for alias in aliases:
        visit(alias)
    return out


def restart_blocker(workspace: dict, run: dict, alias: str) -> str | None:
    """Why factory must NOT restart this stale env for `run`: it serves a worktree that is not the run's own (another
    run, or a hand-started checkout), so env down would kill someone else's process. None when the record's cwd is
    inside this run's worktrees (HEAD moved) and the run may restart (it holds the lease when env.lease is on)."""
    if is_shared(workspace, run, alias):
        return f"shared repo {repo_conf(workspace, alias)['alias']}: only its exclusive holder restarts it"
    rec = read_record(workspace["name"], repo_conf(workspace, alias)["alias"], run_slot(run, repo_conf(workspace, alias)["alias"]))
    if rec:
        cwd = rec.get("cwd")
        here = os.path.normcase(os.path.abspath(str(cwd))) if cwd else None
        own = [os.path.normcase(os.path.abspath(w)) for w in _run_worktrees(run)]
        if _started_for_target(workspace, run, repo_conf(workspace, alias)["alias"], rec):
            pass  # a hotfix env of this run (e.g. a dependency served from `target_paths`): the run's own
        elif not here or not any(here == w or here.startswith(w.rstrip("\\/") + os.sep) for w in own):
            other = find_run_for_cwd(workspace["name"], Path(cwd)) if cwd else None
            return f"env serves {cwd or '?'} (run {other[0]['id'] if other else 'none'})"
    if not can_restart(workspace, run, alias):
        enabled, _ = lease_conf(workspace)
        return f"env lease of {workspace['name']} is not held by run {run['id']}" if enabled else "run may not restart"
    return None


def env_state(workspace: dict, run: dict | None, alias: str, probe_timeout: float = CHECK_HEALTH_TIMEOUT_S,
              target: str | None = None) -> dict:
    """One repo's preflight: {code, kind, detail, health}. code 0 healthy, 1 unhealthy, 2 usage (bad env block),
    3 stale (our process serves another cwd/HEAD than the run worktree), 4 port or lease conflict."""
    repo = repo_conf(workspace, alias)
    alias, ws, env = repo["alias"], workspace["name"], repo.get("env") or {}
    errs = capability(workspace, alias)
    if errs:
        return {"code": EXIT_USAGE, "kind": "usage", "detail": "; ".join(errs)}
    slot = 0 if is_shared(workspace, run, alias) else run_slot(run, alias)
    try:
        env = slot_env(workspace, run, alias, slot)
    except FactoryError as err:
        return {"code": EXIT_USAGE, "kind": "usage", "detail": str(err)}
    if run:
        conflict = lease_conflict(workspace, run, alias)
        if conflict:
            return {"code": EXIT_CONFLICT, "kind": "lease", "detail": conflict}
    health = _health_url(workspace, alias, slot)
    status = health_status(health, probe_timeout)
    healthy = status is not None and status < 500
    out = {"health": health}
    stale = stale_reason(workspace, run, alias, target=target)
    if stale:
        return {**out, "code": EXIT_STALE, "kind": "stale", "detail": f"{alias}: {stale}"}
    rec = read_record(ws, alias, slot)
    ours = record_state(rec) == "same" if rec else False
    ports = [int(p) for p in env.get("ports") or []]
    busy = next((p for p in ports if port_in_use(p)), None)
    if healthy and (ours or not run or busy is None):
        return {**out, "code": 0, "kind": "healthy", "detail": f"{health} -> {status}"}
    if healthy:  # answering, but not the process factory started
        owner = port_owner(busy)
        who = http_sys_detail(busy) if is_http_sys(owner) else \
            f"port {busy} held by " + (f"PID {owner[0]} ({owner[1]})" if owner else "an unknown process")
        return {**out, "code": EXIT_CONFLICT, "kind": "port",
                "detail": f"{who}, healthy but not started by `factory env up` (evidence cannot bind to it)"}
    if ours:
        return {**out, "code": EXIT_UNHEALTHY, "kind": "unhealthy",
                "detail": f"pid {rec['pid']} runs but {health} -> {status if status is not None else 'no answer'}"}
    if busy is not None:
        owner = port_owner(busy)
        if is_http_sys(owner):
            if reprobe_http_sys(health):
                return {**out, "code": 0, "kind": "healthy", "detail": f"{health} (behind HTTP.sys)"}
            return {**out, "code": EXIT_CONFLICT, "kind": "port", "detail": http_sys_detail(busy)}
        who = f"PID {owner[0]} ({owner[1]})" if owner else "an unknown process"
        return {**out, "code": EXIT_CONFLICT, "kind": "port",
                "detail": f"port {busy} in use by {who}, not started by factory; {health} -> "
                          f"{status if status is not None else 'no answer'}"}
    return {**out, "code": EXIT_UNHEALTHY, "kind": "unhealthy",
            "detail": f"{health} -> {status if status is not None else 'no answer'} (no env started for {alias})"}


def restart_env(workspace: dict, run: dict, alias: str, target: str | None = None) -> tuple[bool, list[str]]:
    """env down + up for `run` (the run must hold the lease when env.lease is on), for the same hotfix target:
    `target`, else the one the running env was started for."""
    alias = repo_conf(workspace, alias)["alias"]
    require_lease(workspace, run["id"], f"env restart {alias}", alias)
    rec = read_record(workspace["name"], alias, run_slot(run, alias))
    target = target or (rec or {}).get("target")
    lines = [down(workspace, alias, run["id"])]
    ok, up_lines = up(workspace, alias, run, target)
    return ok, lines + up_lines


def can_restart(workspace: dict, run: dict, alias: str | None = None) -> bool:
    """The run may restart its env: it holds the lease (of `alias`), or the workspace has no lease."""
    enabled, _ = lease_conf(workspace)
    return (not enabled) or lease_heartbeat(workspace["name"], run["id"], repo_conf(workspace, alias)["alias"] if alias else None)


def find_run_for_cwd(ws_name: str, cwd: Path) -> tuple[dict, str] | None:
    """(run, alias) of the unfinished run of `ws_name` whose repo worktree contains `cwd` (deepest worktree wins)."""
    here = os.path.normcase(os.path.abspath(str(cwd)))
    best: tuple[int, dict, str] | None = None
    root = factory_home() / "runs"
    if not root.is_dir():
        return None
    for p in sorted(root.glob("*/run.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("workspace") != ws_name or data.get("phase") in FINISHED_PHASES:
            continue
        for alias, rec in (data.get("repos") or {}).items():
            wt = rec.get("worktree") if isinstance(rec, dict) else None
            if not wt:
                continue
            base = os.path.normcase(os.path.abspath(str(wt)))
            if here == base or here.startswith(base.rstrip("\\/") + os.sep):
                if best is None or len(base) > best[0]:
                    best = (len(base), data, alias)
    return (best[1], best[2]) if best else None


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


def login_config(workspace: dict, run: dict | None, alias: str, slot: int | None = None) -> dict:
    """The login block for the browser CLI in the run's slot: `success_url_contains` with the slot's port, and, for a slot
    above 0 with `handoff_rewrite: true`, `handoff: {host, from_port, to_port}` so the CLI redirects the hand-off URL
    (e.g. the fixed legacy redirect `http://localhost:3300/portal/coming?token=...`) to the slot's port."""
    base = (repo_conf(workspace, alias).get("env") or {}).get("login") or {}
    k = run_slot(run, repo_conf(workspace, alias)["alias"]) if slot is None else slot
    cfg = dict(slot_env(workspace, run, alias, k).get("login") or {})
    if k and base.get("handoff_rewrite") is True:
        host, port = handoff_origin(base.get("success_url_contains"))
        stride = slot_conf(repo_conf(workspace, alias).get("env"))[1]
        if port:
            cfg["handoff"] = {"host": host, "from_port": port, "to_port": port + k * stride}
    return cfg


def login(workspace: dict, alias: str, user: str | None = None, run_id: str | None = None) -> tuple[bool, str]:
    require_lease(workspace, run_id, f"env login {alias}", alias)  # the storage state is per repo slot
    repo = repo_conf(workspace, alias)
    alias = repo["alias"]
    env = repo.get("env") or {}
    if not env.get("login"):
        raise FactoryError(f"{alias}: env.login missing")
    run = _safe_run(run_id)
    slot = 0 if is_shared(workspace, run, alias) else run_slot(run, alias)
    override = user
    user, pw = _credentials(workspace["name"], alias, override)
    cli = factory_root() / "browser" / "dist" / "cli.js"
    if not cli.is_file():
        raise FactoryError(f"browser CLI not built: {cli}")
    node = shutil.which("node")
    if not node:
        raise FactoryError("node not found on PATH")
    out = storage_state_path(workspace["name"], alias, override, slot)
    out.parent.mkdir(parents=True, exist_ok=True)
    cfg = login_json_path(workspace["name"], alias, slot)  # the CLI reads --login-json as a file
    cfg.write_text(json.dumps(login_config(workspace, run, alias, slot)), encoding="utf-8")
    cmd = [node, str(cli), "login", "--base-url", base_url(workspace, alias, slot), "--login-json", str(cfg), "--out", str(out)]
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


def _arg_target(args, run: dict | None) -> str | None:
    """`--target` of env up/check/login/down: a mirror of the hotfix run, None for absent or the base target."""
    target = getattr(args, "target", None)
    if target and not run:
        raise FactoryError("--target needs --run (a hotfix run)")
    return check_target(run, target) if run else None


def _cmd_up(args) -> int:
    ws, run = _load(args)
    ok, lines = up(ws, args.repo, run, _arg_target(args, run))
    print("\n".join(lines))
    return 0 if ok else 1


def _cmd_down(args) -> int:
    ws, run = _load(args)
    target = _arg_target(args, run)
    print(down(ws, args.repo, getattr(args, "run", None), getattr(args, "force", False), getattr(args, "slot", None)))
    if target:
        for line in _down_target_deps(ws, run, repo_conf(ws, args.repo)["alias"], target):
            print(line)
    return 0


def _down_target_deps(ws: dict, run: dict, alias: str, target: str) -> list[str]:
    """`env down --run --target`: also stop the `depends_on` closure of `alias` that this run started for `target` (its
    record carries the target and this run's id), so the next `env up` can serve another target.
    A dependency another run holds (lease, shared) or one started for another target is left running."""
    lines: list[str] = []
    todo, seen = [alias], {alias}
    while todo:
        for dep in (repo_conf(ws, todo.pop()).get("env") or {}).get("depends_on") or []:
            dep = repo_conf(ws, dep)["alias"]
            if dep in seen:
                continue
            seen.add(dep)
            todo.append(dep)
            slot = run_slot(run, dep)
            rec = read_record(ws["name"], dep, slot)
            if not rec or rec.get("target") != target or not _serves_a_target_of(ws, run, dep, rec):
                continue
            if rec.get("run") != run["id"]:  # same directory, started by another run (or by hand): never stopped
                lines.append(f"{dep}: left running (run {rec.get('run') or 'none'})")
                continue
            try:
                lines.append(down(ws, dep, run["id"], slot=slot))
            except FactoryError as err:
                lines.append(f"{dep}: left running ({err})")
    return lines


def _cmd_check(args) -> int:
    """Fail-fast preflight. Exit 0 healthy, 1 unhealthy, 2 usage, 3 stale, 4 port or lease conflict; each
    non-zero repo prints `ENV-FAIL repo=<alias> kind=<...> detail=<...>`."""
    ws = workspace_load(args.workspace)
    run, inferred_alias = None, None
    if getattr(args, "run", None):
        run = run_load(args.run)
        ensure_owner(run)
    else:
        found = find_run_for_cwd(ws["name"], Path.cwd())
        if found:
            run, inferred_alias = found
    _arg_target(args, run)
    target = getattr(args, "target", None) if run else None
    if run:
        try:
            lease_heartbeat(ws["name"], run["id"])
        except FactoryError:
            pass
    if args.repo:
        aliases = [repo_conf(ws, args.repo)["alias"]]
    elif inferred_alias:
        aliases = [inferred_alias]
    elif run:
        aliases = [a for a in run.get("repos") or {} if not capability(ws, a)] or list(run.get("repos") or {})
    else:
        aliases = [r["alias"] for r in ws.get("repos", []) if isinstance(r.get("env"), dict) and r["env"]]
    if not aliases:
        raise FactoryError("env check: no repo to check (pass <repo>, or --run, or run inside a run worktree)")
    worst = 0
    for alias in aliases:
        st = env_state(ws, run, alias, target=target)
        if st["code"] == EXIT_STALE and getattr(args, "auto_restart", False):
            if not run:
                raise FactoryError("env check --auto-restart needs --run (or a cwd inside a run worktree)")
            ensure_owner(run)
            blocked = _restart_deps(ws, run, alias, target) if hotfix_of(run) else None
            blocked = blocked or restart_blocker(ws, run, alias)
            if blocked:
                print(f"{alias}: NOT restarted ({blocked})")
                print(env_fail_line(alias, "stale", blocked))
                worst = max(worst, st["code"])
                continue
            print(f"{alias}: stale ({st['detail']}); restarting (env down + up)")
            ok, lines = restart_env(ws, run, alias, target)
            print("\n".join(lines))
            st = env_state(ws, run, alias, target=target) if ok else {"code": EXIT_UNHEALTHY, "kind": "start",
                                                      "detail": f"{alias}: restart failed", "health": ""}
        if st["code"] == 0:
            print(f"{alias}: healthy {st.get('health', '')}{_served_target(ws, run, alias)}")
            for name, code, text in run_preflights(ws, run, alias, target):
                detail = f"{name} exited {code}" + (f": {text.splitlines()[-1]}" if text else "")
                print(f"{alias}: preflight {name} FAILED ({detail})")
                print(f"ENV-FAIL repo={alias} kind=preflight detail={name}")
                st = {"code": EXIT_UNHEALTHY}
        else:
            print(f"{alias}: NOT healthy ({st['kind']}) {st['detail']}")
            print(env_fail_line(alias, st["kind"], st["detail"]))
        worst = max(worst, st["code"])
    return worst


def _restart_deps(ws: dict, run: dict, alias: str, target: str | None) -> str | None:
    """`env check --auto-restart` of a hotfix run: before `alias`, restart its stale `depends_on` closure (dependencies
    first) that this run started, so `alias` comes back up onto them. A stale dependency that is not the run's own is not
    touched; its blocker is returned and `alias` is left as it is."""
    closure = deps_first(ws, dep_aliases(ws, alias))
    for dep in closure:
        st = env_state(ws, run, dep, target=target)
        if st["code"] != EXIT_STALE:
            continue
        blocked = restart_blocker(ws, run, dep)
        if blocked:
            print(f"{dep}: NOT restarted ({blocked})")
            print(env_fail_line(dep, "stale", blocked))
            return f"dependency {dep}: {blocked}"
        print(f"{dep}: stale ({st['detail']}); restarting (env down + up)")
        ok, lines = restart_env(ws, run, dep, target)
        print("\n".join(lines))
        if not ok:
            return f"dependency {dep}: restart failed"
    return None


def dep_aliases(ws: dict, alias: str) -> list[str]:
    """The `depends_on` closure of `alias` (itself excluded)."""
    out, todo = [], [alias]
    while todo:
        for dep in (repo_conf(ws, todo.pop()).get("env") or {}).get("depends_on") or []:
            dep = repo_conf(ws, dep)["alias"]
            if dep != alias and dep not in out:
                out.append(dep)
                todo.append(dep)
    return out


def _served_target(ws: dict, run: dict | None, alias: str) -> str:
    """` (target <t>)` when the running env of `alias` was started for a hotfix mirror."""
    if not run or is_shared(ws, run, alias):
        return ""
    rec = read_record(ws["name"], alias, run_slot(run, alias))
    return f" (target {rec['target']})" if rec and rec.get("target") else ""


def _cmd_data(args) -> int:
    ws, run = _load(args)
    alias = repo_conf(ws, args.repo)["alias"]
    which = "cmd" if args.env_cmd == "seed" else "reset"
    require_lease(ws, args.run, f"env {args.env_cmd} {alias}", alias)
    try:
        code, text = data_run(ws, run, alias, which)
    except FactoryError as err:
        print(env_fail_line(alias, "seed", str(err)))
        return 1
    if text:
        print(text)
    if code != 0:
        print(env_fail_line(alias, "seed", f"env.seed.{which} exited {code}"))
        return 1
    print(f"{alias}: {'seed' if which == 'cmd' else 'reset-data'} ok (prefix {data_prefix(run)})")
    return 0


def _cmd_logs(args) -> int:
    ws, run = _load(args)
    alias = repo_conf(ws, args.repo)["alias"]
    slot = getattr(args, "slot", None)
    print(tail(log_path(ws["name"], alias, run_slot(run, alias) if slot is None else slot), args.lines))
    return 0


def _cmd_login(args) -> int:
    ws, run = _load(args)
    user = getattr(args, "user", None)
    _arg_target(args, run)
    ok, text = login(ws, args.repo, user, getattr(args, "run", None))
    if text:
        print(text)
    if ok:
        alias = repo_conf(ws, args.repo)["alias"]
        print(f"storage state: {storage_state_path(ws['name'], alias, user, run_storage_slot(ws, run, alias))}")
    return 0 if ok else 1


def _print_status(st: dict) -> None:
    ws = st["workspace"]
    rows = list(st["repos"])
    if st.get("legacy"):
        rows.insert(0, dict(st["legacy"], key="workspace"))
    for row in rows:
        label = f"{ws}/{row['key']}" if row["key"] != "workspace" else f"{ws} (workspace lease, every repo)"
        if not row["holder"]:
            if not row["shared"]:
                print(f"lease {label}: free")
        else:
            line = (f"lease {label}: held by {row['holder']} (session {_session_short(row['session'])}, pid {row['pid']}) "
                    f"since {_hhmm(row['acquired_at'])}, age {_dur(row['age_s'])}, "
                    f"heartbeat {_dur(row['heartbeat_age_s'])} ago (ttl {row['ttl_s']}s)")
            print(line + (f" STALE: {row['stale']}" if row["stale"] else ""))
        if row["shared"]:
            print(f"lease {label}: shared by " + ", ".join(
                f"{e['run']} (heartbeat {_dur(e['heartbeat_age_s'])} ago{', STALE: ' + e['stale'] if e['stale'] else ''})"
                for e in row["shared"]))
    for i, w in enumerate(st["queue"], 1):
        wants = ", ".join(a if m == "exclusive" else f"{a} (shared)" for a, m in w["repos"].items())
        print(f"  {i}. {w['run']} waiting {_dur(w['wait_s'])} for {wants} (session {_session_short(w['session'])})")


def _stop_run_envs(workspace: dict, run: dict, only: set[str] | None = None) -> list[str]:
    """`env down` for the envs of `run` that factory started from its worktrees (releasing the lease frees the port).
    Shared repos belong to their exclusive holder and are never stopped here."""
    lines: list[str] = []
    for alias in list(run.get("repos") or {}) + [a for a in (run.get("env_slots") or {}) if a not in (run.get("repos") or {})]:
        try:
            if capability(workspace, alias) or (only is not None and alias not in only) or is_shared(workspace, run, alias):
                continue
            slot = run_slot(run, alias)
            rec = read_record(workspace["name"], alias, slot)
            if not rec or not _same_path(rec.get("cwd"), _cwd_for(workspace, run, alias, rec.get("target"))):
                continue  # not started from this run's worktree
            lines.append(down(workspace, alias, run["id"], slot=slot))
        except FactoryError as err:
            lines.append(f"{alias}: env not stopped: {err}")
    return lines


def _flat(values) -> list[str] | None:
    out = [v for group in values or [] for v in (group if isinstance(group, list) else [group])]
    return out or None


def _record_slots(run_id: str, slots: dict[str, int]) -> None:
    def put(run):
        run.setdefault("env_slots", {}).update(slots)
    run_update(run_id, put)


def _forget_slots(run_id: str, aliases: list[str]) -> None:
    def drop(run):
        for a in aliases:
            (run.get("env_slots") or {}).pop(a, None)
        if "env_slots" in run and not run["env_slots"]:
            del run["env_slots"]
    run_update(run_id, drop)


def _cmd_lease(args) -> int:
    workspace = workspace_load(args.workspace)
    ws = workspace["name"]
    _, ttl = lease_conf(workspace)
    repos = _flat(getattr(args, "repo", None))
    if args.action == "status":
        st = lease_status(workspace, ttl, repos)
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
        held = lease_held(workspace, args.run)
        if not getattr(args, "keep_env", False):
            mine = set(held["exclusive"]) if not held["legacy"] else None
            if held["legacy"] or mine:
                chosen = {repo_conf(workspace, r)["alias"] for r in repos} if repos else None
                only = mine if chosen is None else (chosen if mine is None else chosen & mine)
                for line in _stop_run_envs(workspace, run, only):
                    print(line)
        info = lease_release_info(workspace, args.run, args.force, repos)
        if info["released"]:
            _forget_slots(args.run, info["released"])
        _record(args.run, "env_lease", action="release", workspace=ws, result=info["message"], repos=info["released"])
        print(json.dumps({"released": True, "message": info["message"]}) if args.json else info["message"])
        return 0
    reqs = lease_requests(workspace, run, repos)
    deadline = time.monotonic() + args.timeout if args.timeout is not None else None
    last = None
    try:
        while True:
            res = lease_try(workspace, args.run, ttl, wait=args.wait, reqs=reqs)
            for old, reason in res["reclaimed"]:
                print(f"reclaimed stale lease of {old}: {reason}")
                _record(old, "env_lease", action="reclaimed", workspace=ws, by=args.run, reason=reason)
            if res["granted"]:
                _record_slots(args.run, res["slots"])
                if res["fresh"]:
                    _record(args.run, "env_lease", action="acquire", workspace=ws, slots=res["slots"], shared=res["shared"])
                if args.json:
                    print(json.dumps({"granted": True, "run": args.run, "slots": res["slots"], "shared": res["shared"],
                                      "leases": res["leases"]}, indent=2))
                else:
                    what = ", ".join(f"{slot_key(a, k)}{' (shared)' if a in res['shared'] else ''}" for a, k in res["slots"].items())
                    print(f"lease {ws}: acquired by {args.run} [{what}]" + ("" if res["fresh"] else " (already held; heartbeat refreshed)"))
                return 0
            line = "; ".join(res["blockers"]) or "queued behind a run ahead"
            if not args.wait:
                if args.json:
                    print(json.dumps({"granted": False, "run": args.run, "blockers": res["blockers"]}, indent=2))
                else:
                    print(f"lease {ws}: {line}; not acquired (use --wait to queue)")
                return 1
            line = f"waiting: position {res['position']} in queue for {ws}, {line}"
            if line != last and not args.json:
                print(line, flush=True)
                last = line
            if deadline is not None and time.monotonic() > deadline:
                _leave_queue(workspace, args.run)
                if args.json:
                    print(json.dumps({"granted": False, "run": args.run, "timeout": True, "blockers": res["blockers"]}, indent=2))
                else:
                    print(f"lease {ws}: timed out after {args.timeout:g}s; left the queue")
                return 1
            time.sleep(max(0.05, args.poll))
    except KeyboardInterrupt:
        _leave_queue(workspace, args.run)
        raise


def register(sub) -> None:
    p = sub.add_parser("env", help="start/stop/check the runtime environment of a repo")
    esub = p.add_subparsers(dest="env_cmd", required=True)
    for name, fn in (("up", _cmd_up), ("down", _cmd_down), ("check", _cmd_check), ("logs", _cmd_logs), ("login", _cmd_login)):
        sp = esub.add_parser(name)
        sp.add_argument("workspace")
        if name == "check":
            sp.add_argument("repo", nargs="?", help="default: every repo of the run (the run is inferred from the cwd worktree)")
        else:
            sp.add_argument("repo")
        if name == "check":
            sp.add_argument("--auto-restart", action="store_true", help="stale env: env down + up for the run (needs the lease)")
        if name in ("up", "check"):
            sp.add_argument("--run", help="use the worktree of this run (with env.lease on, up needs the run to hold the lease)")
        if name in ("up", "check"):
            sp.add_argument("--target", help="hotfix run: serve/check this mirror target's worktree "
                            "(repos without a worktree for it keep the run's; `target_paths` of the repo applies)")
        if name == "down":
            sp.add_argument("--target", help="hotfix run (with --run): after stopping the repo, also stop its depends_on "
                            "repos this run started for this target, so `env up` can serve another target; "
                            "deps held by another run or started for another target stay up")
        if name == "login":
            sp.add_argument("--target", help="hotfix run: only validated (login uses the running env); accepted for symmetry")
        if name == "down":
            sp.add_argument("--run", help="the run stopping its env (must hold the env lease when one is held)")
            sp.add_argument("--force", action="store_true", help="stop even though another run holds the env lease")
            sp.add_argument("--slot", type=int, help="slot to stop (default: the run's slot, else 0)")
        if name == "login":
            sp.add_argument("--user", help="log in as this e-mail instead of the workspace default user")
            sp.add_argument("--run", help="the run logging in (must hold the env lease when env.lease is on)")
        if name == "logs":
            sp.add_argument("--lines", type=int, default=40)
            sp.add_argument("--run", help="show the log of this run's slot")
            sp.add_argument("--slot", type=int, help="slot whose log to show (default: the run's slot, else 0)")
        sp.set_defaults(func=fn)
    for name in ("seed", "reset-data"):
        sp = esub.add_parser(name, help="run env.seed.cmd" if name == "seed"
                             else "run env.seed.reset (idempotent cleanup by FACTORY_DATA_PREFIX)")
        sp.add_argument("workspace")
        sp.add_argument("repo")
        sp.add_argument("--run", required=True, help="run whose ticket gives FACTORY_DATA_PREFIX (UAT<ticket digits>)")
        sp.set_defaults(func=_cmd_data)
    lp = esub.add_parser("lease", help="per-repo env leases (slots, shared repos): acquire/release/status")
    lp.add_argument("action", choices=("acquire", "release", "status"))
    lp.add_argument("workspace")
    lp.add_argument("--run", help="run that acquires/releases the lease")
    lp.add_argument("--repo", nargs="+", action="append", metavar="ALIAS",
                    help="repos to lease (default: the run's repos plus their env.depends_on closure)")
    lp.add_argument("--wait", action="store_true", help="acquire: queue (FIFO) and poll until granted")
    lp.add_argument("--timeout", type=float, help="acquire --wait: give up after this many seconds")
    lp.add_argument("--poll", type=float, default=LEASE_POLL_S, help="acquire --wait: poll interval in seconds")
    lp.add_argument("--force", action="store_true", help="release: drop a lease held by another run")
    lp.add_argument("--keep-env", action="store_true", help="release: do not stop the envs the run started")
    lp.add_argument("--json", action="store_true")
    lp.set_defaults(func=_cmd_lease)
