"""`factory env`: one lock per repo, factory-started process groups, health, `depends_on`, and the runtime binding a check needs
[L-13, L-37, L-64 to L-71, L-73, L-74, L-84, L-85]."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import envdata, envproc as ep, git, localhttp, proc
from .run import Run
from .util import FactoryError, fwd, now_iso, redact

RUNTIME = ("http", "browser", "visual", "style", "screenshot")  # check types that call a served env


class EnvError(FactoryError):
    """An environment failure (exit 3): `kind` is files, lock, port, start, health, data, warmup, login, stale or restore."""

    def __init__(self, kind: str, message: str):
        super().__init__(message, 3)
        self.kind = kind


def repo_of(project, name: str) -> dict:
    for r in project.cfg["repo"]:
        if r["name"] == name:
            return r
    raise FactoryError(f"unknown repo {name!r} (repos: {', '.join(r['name'] for r in project.cfg['repo'])})")


def env_of(project, name: str) -> dict:
    rc = repo_of(project, name)
    if not rc.get("env"):
        raise FactoryError(f"repo {name} has no [repo.env] in factory.toml, so it cannot be served")
    return rc["env"]


def closure(project, name: str, seen: tuple[str, ...] = ()) -> list[str]:
    """`depends_on` repos first, then the repo itself; a cycle is a config error."""
    if name in seen:
        raise FactoryError(f"depends_on cycle: {' -> '.join(seen + (name,))}")
    out: list[str] = []
    for dep in env_of(project, name).get("depends_on", []):
        out += [d for d in closure(project, dep, seen + (name,)) if d not in out]
    return out + [name]


def target(project, st: dict, name: str, override: Path | None = None) -> tuple[Path, bool]:
    """Where a repo is served from: the run worktree when the run has one, else its main path (the caller warns) [L-71]."""
    if override is not None:
        return override, False
    w = git.current(st, name) if name in st["repos"] else None
    if w and Path(w["path"]).is_dir():
        return Path(w["path"]), False
    return Path(repo_of(project, name)["path"]), True


def heads_of(project, st: dict, repo: str, override: Path | None = None) -> dict[str, str]:
    """HEAD of every repo a runtime check of `repo` exercises (itself and what it depends on) [L-12]."""
    return {n: git.head(target(project, st, n, override if n == repo else None)[0]) for n in closure(project, repo)}


def health_url(env: dict) -> str:
    h = env.get("health", "/")
    return h if not h.startswith("/") else localhttp.join(env["base_url"], h)


def _tail(project, repo: str, secrets: list[str], n: int = 15) -> str:
    try:
        text = ep.log_path(project, repo).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return redact("\n".join(text.splitlines()[-n:]), secrets)


def _fail(project, run: Run, repo: str, kind: str, why: str, actor: str | None) -> EnvError:
    run.append({"type": "env", "repo": repo, "action": "fail", "kind": kind, "reason": why[:600]}, actor)
    return EnvError(kind, f"env {repo} failed ({kind}): {why}")


def stop(project, run: Run, repo: str, actor: str | None = None) -> str:
    """Stop exactly the recorded process group, and only when its create time still matches the record [L-67, L-68]."""
    rec = ep.read_lock(project, repo)
    if rec is None:
        return f"{repo}: no env lock; nothing to stop"
    if rec.get("pending") and ep.holder_alive(rec):
        return f"{repo}: another factory command is starting it (pid {rec['pid']}); not stopping it"
    if not ep.holder_alive(rec):
        live = proc.create_time(rec["pid"]) if isinstance(rec.get("pid"), int) else None
        ep.release(project, repo)
        note = f" (pid {rec['pid']} now belongs to another process: its create time differs, so it was not killed)" if live else ""
        return f"{repo}: removed a stale lock{note}"
    if rec.get("run") != run.id:
        raise EnvError("lock", f"env {repo} is held by {ep.describe(rec)}; only that run stops it")
    env = env_of(project, repo)
    if env.get("down"):
        proc.run(env["down"], cwd=Path(rec["cwd"]), env=envdata.child_env(project, repo_of(project, repo), run.id, env["base_url"]),
                 timeout_s=60, shell=True)
    proc.kill_pid_tree(rec["pid"])
    ep.release(project, repo)
    run.append({"type": "env", "repo": repo, "action": "down", "pid": rec["pid"], "cwd": rec["cwd"], "sha": rec.get("sha")}, actor)
    return f"{repo}: stopped pid {rec['pid']} (its process group)"


def _wait_free(project, repo: str, run_id: str, wait_s: float) -> dict | None:
    """The live holder of the lock after waiting up to `wait_s` for another run's holder to leave; None when it is free [L-75]."""
    end = time.monotonic() + wait_s
    while True:
        rec = ep.read_lock(project, repo)
        if rec is None or not ep.holder_alive(rec):
            return None
        if (rec.get("run") == run_id and not rec.get("pending")) or time.monotonic() >= end:
            return rec
        time.sleep(0.5)


def _up_one(project, run: Run, name: str, path: Path, restart: bool, wait_s: float, user: str | None, actor: str | None) -> list[str]:
    rc, env = repo_of(project, name), env_of(project, name)
    sha, url, notes = git.head(path), health_url(env), []
    secrets = envdata.secret_values(project, rc)
    rec = _wait_free(project, name, run.id, wait_s)
    if rec is not None:
        mine = rec.get("run") == run.id and not rec.get("pending")
        if not mine:
            raise _fail(project, run, name, "lock", f"held by {ep.describe(rec)}; stop that run's env or wait with --wait <seconds> [L-75]", actor)
        if rec.get("cwd") == fwd(path) and rec.get("sha") == sha and not restart and localhttp.healthy(url):
            return [f"{name}: already running from {fwd(path)} at {sha[:7]} (pid {rec['pid']})"]
        if not restart:
            raise _fail(project, run, name, "lock", f"it already serves {rec.get('cwd')} at {str(rec.get('sha'))[:7]}; use --restart", actor)
        notes.append(stop(project, run, name, actor))
    elif ep.read_lock(project, name) is not None:
        notes.append(stop(project, run, name, actor))  # a stale lock
    if (problems := ep.copy_main_files(rc, path)):
        raise _fail(project, run, name, "files", "; ".join(problems), actor)
    answers = localhttp.healthy(url, 2)
    owner = ep.port_owner(env["base_url"]) if answers or localhttp.listening(env["base_url"]) else None
    if answers or owner or localhttp.listening(env["base_url"]):
        raise _fail(project, run, name, "port", f"something the factory did not start already answers at {url}"
                    + (f": {owner}" if owner else "") + "; it is never killed, stop it yourself or pick another port in .factory/local.toml [L-70, L-74]", actor)
    me = {"pending": True, "pid": os.getpid(), "create_time": proc.create_time(os.getpid()), "run": run.id}
    if not ep.claim(project, name, me):
        raise _fail(project, run, name, "lock", "another factory command is starting this env at the same time", actor)
    log = ep.log_path(project, name)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"[factory] {now_iso()} starting {name} for run {run.id}: {env['up']} (cwd {fwd(path)})\n", encoding="utf-8")
    try:
        pid = proc.spawn_group(env["up"], cwd=path, env=envdata.child_env(project, rc, run.id, env["base_url"], None, []) | {
            "PYTHONUNBUFFERED": "1", "FORCE_COLOR": "0", "NO_COLOR": "1"}, log=log)
    except FactoryError as exc:
        ep.release(project, name)
        raise _fail(project, run, name, "start", str(exc), actor) from exc
    rec = {"repo": name, "pid": pid, "create_time": proc.create_time(pid), "group": pid, "cwd": fwd(path), "sha": sha, "run": run.id,
           "started": now_iso(), "base_url": env["base_url"], "log": fwd(log)}
    ep.write_lock(project, name, rec)
    run.append({"type": "env", "repo": name, "action": "restart" if restart else "up", **{k: rec[k] for k in ("pid", "create_time", "group", "cwd", "sha", "run")}}, actor)
    problem = _ready(project, rc, env, pid, url, path, run, user, secrets)
    if problem:
        proc.kill_pid_tree(pid)
        ep.release(project, name)
        raise _fail(project, run, name, problem[0], f"{problem[1]}\n{_tail(project, name, secrets)}".strip(), actor)
    return notes + [f"{name}: up from {fwd(path)} at {sha[:7]} (pid {pid}), healthy at {url}"]


def _ready(project, rc: dict, env: dict, pid: int, url: str, path: Path, run: Run, user: str | None, secrets: list[str]) -> tuple[str, str] | None:
    """Wait for health (up to `ready_timeout_s`), then warm up, run the data preflight and log in [L-40, L-50, L-84]."""
    end, pause = time.monotonic() + env["ready_timeout_s"], 0.04
    while not localhttp.healthy(url, 2):
        if proc.create_time(pid) is None:
            return "start", "the process exited before the health URL answered"
        if time.monotonic() >= end:
            return "health", f"{url} did not answer below 500 within ready_timeout_s={env['ready_timeout_s']}"
        time.sleep(pause)
        pause = min(pause * 1.6, 0.25)  # a server that is up in a fraction of a second is noticed in one; a slow one is polled every 0.25 s
    if (why := envdata.warmup(env["base_url"], env.get("warmup", []))):
        return "warmup", why
    if (why := envdata.preflight(project, rc, path, run.id, env["base_url"])):
        return "data", why
    if (why := envdata.login(project, run, rc, path, env["base_url"], user)):
        return "login", why
    return None


def up(project, run: Run, repo: str, restart: bool = False, wait_s: float = 0, override: Path | None = None, user: str | None = None,
       actor: str | None = None) -> list[str]:
    """Start `repo` and what it depends on, dependencies first, each from the run worktree when the repo is in the run [L-71]."""
    st, out = run.state(), []
    for name in closure(project, repo):
        path, from_main = target(project, st, name, override if name == repo else None)
        if from_main:
            out.append(f"warning: {name} is not in this run, so it is served from its main path {fwd(path)} on whatever branch is checked out there [L-71]")
        out += _up_one(project, run, name, path, restart, wait_s, user, actor)
    return out


def down(project, run: Run, repo: str | None, actor: str | None = None) -> list[str]:
    names = [repo] if repo else [r["name"] for r in project.cfg["repo"] if r.get("env") and (rec := ep.read_lock(project, r["name"])) and rec.get("run") == run.id]
    return [stop(project, run, n, actor) for n in names] or ["no env of this run is up"]


def status(project) -> list[dict]:
    rows = []
    for r in project.cfg["repo"]:
        if r.get("env"):
            rec = ep.read_lock(project, r["name"])
            state = "down" if rec is None else "up" if ep.holder_alive(rec) else "stale"
            rows.append({"repo": r["name"], "state": state, "base_url": r["env"]["base_url"], "healthy": state == "up" and localhttp.healthy(health_url(r["env"]), 2),
                         **({k: rec.get(k) for k in ("run", "pid", "cwd", "sha")} if rec else {})})
    return rows


def binding(project, st: dict, repo: str, override: Path | None = None) -> tuple[list[str], dict]:
    problems, served, warnings = [], {}, []
    for name in closure(project, repo):
        path, from_main = target(project, st, name, override if name == repo else None)
        rec, sha = ep.read_lock(project, name), git.head(path)
        if from_main:
            warnings.append(f"{name} is served from its main path {fwd(path)}, not from a run worktree [L-71]")
        if rec is None or rec.get("pending") or not ep.holder_alive(rec):
            problems.append(f"{name}: no live env process recorded by the factory")
        elif rec.get("cwd") != fwd(path):
            problems.append(f"{name}: the env serves {rec.get('cwd')}, not {fwd(path)} [L-69]")
        elif rec.get("sha") != sha:
            problems.append(f"{name}: the env process started at {str(rec.get('sha'))[:7]} but HEAD is {sha[:7]} [L-13, L-85]")
        elif not localhttp.healthy(health_url(env_of(project, name)), 3):
            problems.append(f"{name}: the health URL does not answer")
        else:
            served[name] = {"path": fwd(path), "sha": sha, "pid": rec["pid"], "create_time": rec["create_time"]}
    return problems, {"served": served, "heads": heads_of(project, st, repo, override), "warnings": warnings}


def ensure(project, run: Run, repo: str, override: Path | None = None, user: str | None = None, actor: str | None = None) -> dict:
    """The runtime binding: the env lock must show a factory-started process from the checked worktree at its HEAD. Otherwise one
    restart, then an `EnvError` of kind `stale` (no weaker mode exists) [L-13, L-69, L-85]."""
    st = run.state()
    for attempt in (1, 2):
        problems, info = binding(project, st, repo, override)
        if not problems:
            return info
        if attempt == 2:
            raise EnvError("stale", "; ".join(problems))
        up(project, run, repo, restart=True, override=override, user=user, actor=actor)
    raise AssertionError("unreachable")


@contextmanager
def serving(project, run: Run, repo: str, tree: Path, actor: str | None = None):
    """Serve `repo` from another tree (the base worktree of a reproduction) and restore the run env afterwards; a restore
    that fails raises kind `restore`, exit 3 [L-36, L-37]."""
    rec = ep.read_lock(project, repo)
    was_up = bool(rec and ep.holder_alive(rec) and rec.get("run") == run.id)
    if was_up:
        stop(project, run, repo, actor)
    try:
        yield tree
    finally:
        try:
            stop(project, run, repo, actor)
        except FactoryError:
            pass
        if was_up:
            try:
                up(project, run, repo, restart=True, actor=actor)
            except FactoryError as exc:
                raise EnvError("restore", f"could not start the run env of {repo} again after the reproduction: {exc}") from exc


def logs(project, repo: str, tail: int) -> str:
    path = ep.log_path(project, repo)
    if not path.is_file():
        raise FactoryError(f"no env log for {repo} yet (expected {fwd(path)})")
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:])
