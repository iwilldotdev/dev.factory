"""`factory env up|down|check|logs|login`: codified runtime environment (DESIGN §3)."""

from __future__ import annotations

import json
import os
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

from ..core import (
    FactoryError,
    factory_home,
    git,
    now,
    read_json,
    repo_conf,
    repo_path_for_run,
    run_load,
    secrets_path,
    workspace_load,
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


def storage_state_path(ws: str, alias: str) -> Path:
    return _env_dir(ws) / f"{alias}.storage.json"


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


def record_alive(rec: dict | None) -> bool:
    """The recorded process still runs and is the same process (create time matches)."""
    if not rec or not pid_alive(rec["pid"]):
        return False
    if rec.get("create_time") is None:
        return True  # legacy record: no stamp to compare
    return process_create_time(rec["pid"]) == rec["create_time"]


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
    rec = read_record(ws, alias)
    if rec and record_alive(rec):
        if not _same_path(rec.get("cwd"), cwd) or (rec.get("head") or None) != head:
            out.append(
                f"{alias}: already up (pid {rec['pid']}) for {rec.get('cwd') or '?'} at "
                f"{str(rec.get('head') or '?')[:8]}, but this request is {cwd} at {str(head or '?')[:8]}. "
                f"Run `factory env down {ws} {alias}` first, then up again.")
            return False
        if health_ok(health):
            out.append(f"{alias}: already up (pid {rec['pid']})")
            return True
        kill_tree(rec["pid"])  # stale: ours, alive but unhealthy; restart below
        _drop_record(ws, alias)
    elif rec:
        if pid_alive(rec["pid"]):
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
            return False
    if not env.get("ports") and health_ok(health):
        out.append(f"{alias}: unknown server already answering at {health}, not started by factory. "
                   "Ask the user to stop it (or declare env.ports to see its owner) and retry.")
        return False

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


def up(workspace: dict, alias: str, run: dict | None = None) -> tuple[bool, list[str]]:
    out: list[str] = []
    ok = _up_one(workspace, run, alias, set(), out)
    return ok, out


def down(workspace: dict, alias: str) -> str:
    repo = repo_conf(workspace, alias)
    ws, alias = workspace["name"], repo["alias"]
    rec = read_record(ws, alias)
    if not rec:
        return f"{alias}: no pid file (nothing started by factory)"
    pid = rec["pid"]
    if record_alive(rec):
        kill_tree(pid)
    elif pid_alive(pid):
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
    if not record_alive(rec):
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


def _credentials(ws: str, alias: str) -> tuple[str, str]:
    sec = read_secrets(ws)
    up = alias.upper().replace("-", "_")
    user = sec.get(f"{up}_USER") or sec.get("USER")
    pw = sec.get(f"{up}_PASS") or sec.get("PASS")
    if not user or not pw:
        raise FactoryError(f"secrets/{ws}.env needs {up}_USER and {up}_PASS (or USER and PASS)")
    return user, pw


def factory_root() -> Path:
    return Path(__file__).resolve().parents[2]


def login(workspace: dict, alias: str) -> tuple[bool, str]:
    repo = repo_conf(workspace, alias)
    alias = repo["alias"]
    env = repo.get("env") or {}
    if not env.get("login"):
        raise FactoryError(f"{alias}: env.login missing")
    user, pw = _credentials(workspace["name"], alias)
    cli = factory_root() / "browser" / "dist" / "cli.js"
    if not cli.is_file():
        raise FactoryError(f"browser CLI not built: {cli}")
    node = shutil.which("node")
    if not node:
        raise FactoryError("node not found on PATH")
    out = storage_state_path(workspace["name"], alias)
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
    return ws, run


def _cmd_up(args) -> int:
    ws, run = _load(args)
    ok, lines = up(ws, args.repo, run)
    print("\n".join(lines))
    return 0 if ok else 1


def _cmd_down(args) -> int:
    ws, _ = _load(args)
    print(down(ws, args.repo))
    return 0


def _cmd_check(args) -> int:
    ws, _ = _load(args)
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
    ok, text = login(ws, args.repo)
    if text:
        print(text)
    if ok:
        print(f"storage state: {storage_state_path(ws['name'], repo_conf(ws, args.repo)['alias'])}")
    return 0 if ok else 1


def register(sub) -> None:
    p = sub.add_parser("env", help="start/stop/check the runtime environment of a repo")
    esub = p.add_subparsers(dest="env_cmd", required=True)
    for name, fn in (("up", _cmd_up), ("down", _cmd_down), ("check", _cmd_check), ("logs", _cmd_logs), ("login", _cmd_login)):
        sp = esub.add_parser(name)
        sp.add_argument("workspace")
        sp.add_argument("repo")
        if name in ("up", "check"):
            sp.add_argument("--run", help="use the worktree of this run")
        if name == "logs":
            sp.add_argument("--lines", type=int, default=40)
        sp.set_defaults(func=fn)
