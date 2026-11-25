"""Fixtures for the runtime tests: a local fake server as the env, a fake browser runner, free ports and cleanup of every process."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from unittest import mock

from dev_factory import proc
from tests.helpers import PY, git
from tests.m2_support import M2Case, fixture_repo

HERE = Path(__file__).resolve().parent
SERVER = (HERE / "fake_server.py").as_posix()
RUNNER = (HERE / "fake_runner.py").as_posix()


_NEXT = [20000 + (os.getpid() * 7919) % 30000]


def free_port() -> int:
    """A free port from a window chosen by the process id, so test modules that run at the same time never draw the same port."""
    for _ in range(300):
        port, _NEXT[0] = _NEXT[0], 20000 + (_NEXT[0] - 20000 + 1) % 30000
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError("no free port found")


_PORTS: dict[str, int] = {}


def stable_port(label: str) -> int:
    """A free port chosen once per test process, so a project tree built for one test can be copied back for the next."""
    if label not in _PORTS:
        _PORTS[label] = free_port()
    return _PORTS[label]


def env_toml(port: int, extra: str = "", name: str = "web", up_extra: str = "") -> str:
    up = json.dumps(f'"{PY}" "{SERVER}" {port}')
    return (f'\n[repo.env]\nup = {up}\nhealth = "/health"\nbase_url = "http://127.0.0.1:{port}"\nready_timeout_s = 20\n{up_extra}{extra}')


def png(w: int, h: int, color=(200, 30, 30)) -> bytes:
    """A solid-color PNG written with stdlib zlib, so design fixtures need no image library."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class M4Case(M2Case):
    """A single repo `web` whose env is the fake server on a free port; `.factory/local.toml` points the runner at the fake one."""

    def setUp(self):
        super().setUp()
        self.port = stable_port("web")
        self.runner_log = self.base / "runner.log"
        self.local_toml = self.proj / ".factory" / "local.toml"
        patcher = mock.patch.dict(os.environ, {"FAKE_RUNNER_LOG": str(self.runner_log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.kill_envs)
        self.configure()

    def configure(self, extra_env: str = "", extra: str = "", runner: bool = True, repo_extra: str = "", health: str = "/health",
                  ready: int = 20) -> None:
        env = env_toml(self.port, extra_env).replace('health = "/health"', f'health = "{health}"').replace("ready_timeout_s = 20", f"ready_timeout_s = {ready}")
        self.write("factory.toml", self.TOML + repo_extra + env + extra)
        if runner:
            self.write(".factory/local.toml", f'[checks.browser]\nbundled = "{RUNNER}"\n')

    def kill_envs(self) -> None:
        for lock in (self.proj / ".factory" / "env").glob("*.lock") if (self.proj / ".factory" / "env").is_dir() else []:
            try:
                pid = json.loads(lock.read_text(encoding="utf-8")).get("pid")
            except (OSError, ValueError):
                continue
            if isinstance(pid, int) and pid != os.getpid():
                proc.kill_pid_tree(pid)
        for child in proc._CHILDREN:
            child.poll()

    def runner_calls(self) -> list[dict]:
        if not self.runner_log.is_file():
            return []
        return [json.loads(line) for line in self.runner_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def results(self, table: dict) -> None:
        """What the fake runner answers per command from now on."""
        patcher = mock.patch.dict(os.environ, {"FAKE_RUNNER_RESULTS": json.dumps(table)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def branch(self, kind: str = "feature", ticket: str = "ABC-1") -> Path:
        self.cached("branch", lambda: self._branch(kind, ticket), kind, ticket)
        return self.proj / ".factory" / "runs" / ticket

    def _branch(self, kind: str, ticket: str) -> Path:
        run = self.start(ticket)
        self.ingest_ticket(run, id=ticket, type=kind)
        self.ok("gate", "intake")
        self.ok("run", "branch")
        return run

    def up(self, *flags: str, repo: str = "web") -> str:
        return self.ok("env", "up", repo, *flags)

    def get(self, path: str, port: int | None = None) -> tuple[int, dict, bytes]:
        from dev_factory import localhttp
        return localhttp.request("GET", f"http://127.0.0.1:{port or self.port}{path}")

    def lock_of(self, repo: str = "web") -> dict:
        return json.loads((self.proj / ".factory" / "env" / f"{repo}.lock").read_text(encoding="utf-8"))

    def foreign_server(self, port: int) -> subprocess.Popen:
        """A server the factory did not start."""
        # the base interpreter: a Windows venv python.exe is a launcher whose child owns the port, so its pid is not the owner
        py = getattr(sys, "_base_executable", None) or PY
        p = subprocess.Popen([py, SERVER, str(port)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.kill_pid_tree(p.pid), p.wait(timeout=10)))
        return p

    def commit_ignore(self, rel: str) -> None:
        (self.proj / ".gitignore").write_text(f".factory/\n{rel}\n", newline="\n")
        git("add", ".gitignore", cwd=self.proj)
        git("commit", "-q", "-m", "chore: ignore", cwd=self.proj)


class WorkspaceCase(M4Case):
    """A workspace `shop` over two member repos, `api` and `web`; web's env depends on api's."""

    def setUp(self):
        from tests.helpers import FactoryCase
        FactoryCase.setUp(self)
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ports = {"api": stable_port("api"), "web": stable_port("web")}
        self.port = self.ports["web"]
        self.runner_log = self.base / "runner.log"
        patcher = mock.patch.dict(os.environ, {"FAKE_RUNNER_LOG": str(self.runner_log)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.kill_envs)
        shutil.rmtree(self.proj)
        self.proj = self.base / "shop"
        for name in ("api", "web"):
            shutil.copytree(fixture_repo(), self.base / name)
            (self.base / name / ".gitignore").write_text(".factory/\n", newline="\n")
            self.write("factory.toml", f'schema = 1\n[project]\nname = "{name}"\n[[repo]]\npath = "."\nname = "{name}"\n'
                       + env_toml(self.ports[name], "", name), root=self.base / name)
            git("add", "-A", cwd=self.base / name)
            git("commit", "-q", "-m", "chore: member config", cwd=self.base / name)
        self.write("factory.toml", 'schema = 1\n[project]\nname = "shop"\n[[repo]]\npath = "../api"\n[[repo]]\npath = "../web"\n'
                   '[repo.env]\ndepends_on = ["api"]\n')
        self.write(".factory/local.toml", f'[checks.browser]\nbundled = "{RUNNER}"\n')
        self.write(".gitignore", ".factory/\n")

    def default_plan(self, kind: str = "feature") -> dict:
        plan = super().default_plan(kind)
        plan["repos"] = ["api", "web"]
        plan["checks"][0].update(repo="api")
        return plan

    def _branch(self, kind: str, ticket: str) -> Path:
        run = self.start(ticket, "--repos", "api,web")
        self.ingest_ticket(run, id=ticket, type=kind)
        self.ok("gate", "intake")
        self.ok("run", "branch")
        return run
