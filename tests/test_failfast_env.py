"""Fail-fast env preflight: `env check` exit codes, inference from cwd, HTTP.sys port owner, lease liveness."""

import contextlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.commands import env as envmod  # noqa: E402


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Redirector(http.server.BaseHTTPRequestHandler):
    """302 to a host nothing listens on: following it would make a healthy app look down."""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/login")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class EnvCase(FactoryCase):
    lease = False

    def setUp(self):
        super().setUp()
        self.web = make_repo(self.tmp / "web").resolve()
        self.port = free_port()
        self.rebuild_ws()
        self.make_run("r1", phase="prove", repos={"Web": {"worktree": str(self.web), "branch": "b"}})

    def rebuild_ws(self, **env_over):
        env = {"start": [sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"], "cwd": ".",
               "health": f"http://127.0.0.1:{self.port}/", "ports": [self.port], "startup_timeout_s": 30}
        env.update(env_over)
        self.ws = {"schemaVersion": 1, "name": "ws", "env": {"lease": self.lease, "lease_ttl_s": 900},
                   "repos": [{"alias": "Web", "name": "web", "path": str(self.web), "env": env}]}
        self.write("workspaces/ws/workspace.json", self.ws)

    def serve(self, handler, port=None):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port or self.port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def own_record(self, head):
        """A pid record for a live helper process (never the test process: factory refuses to touch that)."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(child.wait)  # LIFO: kill first
        self.addCleanup(child.kill)
        pid = child.pid
        rec = {"pid": pid, "cwd": str(self.web), "head": head, "started": core.now(),
               "create_time": envmod.process_create_time(pid)}
        self.write("env/ws/Web.pid", rec)


class CheckExitCodeTests(EnvCase):
    def test_302_is_healthy_and_redirect_is_not_followed(self):
        self.serve(Redirector)
        self.assertTrue(envmod.health_ok(f"http://127.0.0.1:{self.port}/"))
        rc, out = cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 0, out)
        self.assertIn("healthy", out)
        self.assertNotIn("ENV-FAIL", out)

    def test_repo_is_optional_without_a_run(self):
        self.serve(Redirector)
        rc, out = cli("env", "check", "ws")
        self.assertEqual(rc, 0, out)
        self.assertIn("Web: healthy", out)

    def test_unhealthy_exit_1(self):
        rc, out = cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"ENV-FAIL repo=Web kind=unhealthy detail=", out)

    def test_5xx_is_unhealthy(self):
        class Boom(Redirector):
            def do_GET(self):
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.rebuild_ws(ports=[])  # no declared port: a failing answer is just "unhealthy"
        self.serve(Boom)
        rc, out = cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 1, out)
        self.assertIn("kind=unhealthy", out)

    def test_stale_exit_3_when_head_differs_from_worktree(self):
        self.serve(Redirector)
        self.own_record("0" * 40)
        rc, out = cli("env", "check", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 3, out)
        self.assertIn("ENV-FAIL repo=Web kind=stale detail=Web: started at 00000000, worktree HEAD is", out)

    def test_matching_record_is_healthy(self):
        self.serve(Redirector)
        self.own_record(core.git_head(self.web))
        rc, out = cli("env", "check", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 0, out)

    def test_port_conflict_exit_4_when_not_started_by_factory(self):
        self.serve(Redirector)  # healthy, but no pid record: not ours
        rc, out = cli("env", "check", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 4, out)
        self.assertIn("ENV-FAIL repo=Web kind=port", out)

    def test_port_held_by_other_process_and_unhealthy_exit_4(self):
        with socket.socket() as s:  # listens, never answers HTTP
            s.bind(("127.0.0.1", self.port))
            s.listen(1)
            with mock.patch.object(envmod, "health_status", return_value=None):
                rc, out = cli("env", "check", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 4, out)
        self.assertIn("kind=port", out)

    def test_run_and_repo_inferred_from_cwd(self):
        self.serve(Redirector)
        self.own_record("0" * 40)
        sub = self.web / "src"
        sub.mkdir()
        old = os.getcwd()
        os.chdir(sub)
        self.addCleanup(os.chdir, old)
        rc, out = cli("env", "check", "ws")
        self.assertEqual(rc, 3, out)
        self.assertIn("ENV-FAIL repo=Web kind=stale", out)

    def test_usage_for_repo_without_env(self):
        self.ws["repos"][0]["env"] = {}
        self.write("workspaces/ws/workspace.json", self.ws)
        rc, out = cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 2, out)
        self.assertIn("kind=usage", out)
        self.assertEqual(cli("env", "check", "ws", "Nope")[0], 2)

    def test_probe_budget_is_20s(self):
        seen = []
        with mock.patch.object(envmod, "health_status", side_effect=lambda url, timeout=3.0: seen.append(timeout)):
            cli("env", "check", "ws", "Web")
        self.assertEqual(seen[0], 20.0)


class LeaseConflictTests(EnvCase):
    lease = True

    def test_lease_held_by_other_run_exit_4(self):
        self.make_run("r2", phase="build")
        self.assertTrue(envmod.lease_try("ws", "r2", 900)["granted"])
        rc, out = cli("env", "check", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 4, out)
        self.assertIn("ENV-FAIL repo=Web kind=lease", out)


class AutoRestartTests(EnvCase):
    def test_auto_restart_replaces_a_stale_env(self):
        rc, out = cli("env", "up", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.addCleanup(envmod.down, self.ws, "Web")
        first = envmod.read_record("ws", "Web")
        (self.web / "b.txt").write_text("2")
        subprocess.run(["git", "-C", str(self.web), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.web), "commit", "-qm", "c2"], check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        rc, out = cli("env", "check", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 3, out)
        rc, out = cli("env", "check", "ws", "Web", "--run", "r1", "--auto-restart")
        self.assertEqual(rc, 0, out)
        second = envmod.read_record("ws", "Web")
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertEqual(second["head"], core.git_head(self.web))


class HttpSysTests(EnvCase):
    def patched(self, **extra):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(envmod, "IS_WIN", True))
        stack.enter_context(mock.patch.object(envmod, "port_in_use", return_value=True))
        stack.enter_context(mock.patch.object(envmod, "port_owner", return_value=(4, "System")))
        stack.enter_context(mock.patch.object(envmod, "HTTP_SYS_PROBE_GAP_S", 0))
        for k, v in extra.items():
            stack.enter_context(mock.patch.object(envmod, k, **v))
        self.addCleanup(stack.close)

    def test_pid_4_not_ours_reports_http_sys_instead_of_unknown_process(self):
        self.patched(health_ok={"return_value": False})
        ok, lines = envmod.up(self.ws, "Web")
        self.assertFalse(ok)
        text = "\n".join(lines)
        self.assertIn(f"ENV-FAIL repo=Web kind=port detail=port {self.port} held by HTTP.sys "
                      "(likely IIS Express from another process)", text)
        self.assertNotIn("unknown process", text)
        self.assertNotIn("taskkill", text)  # never invite killing the System process

    def test_pid_4_reprobes_three_times_then_accepts_our_healthy_record(self):
        self.own_record(core.git_head(self.web))
        cwd = str(self.web)
        probes = mock.Mock(side_effect=[False, False, True])  # first look, probe 1, probe 2 (healthy)
        self.patched(health_ok={"side_effect": probes})
        with mock.patch.object(envmod, "_cwd_for", return_value=Path(cwd)):
            ok, lines = envmod.up(self.ws, "Web")
        self.assertTrue(ok, lines)
        self.assertIn("already up", "\n".join(lines))
        self.assertEqual(probes.call_count, 3)

    def test_check_reports_http_sys_port_conflict(self):
        self.patched(health_status={"return_value": None})
        rc, out = cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 4, out)
        self.assertIn("held by HTTP.sys", out)


class LeaseLivenessTests(EnvCase):
    lease = True

    def old_lease(self, **extra):
        stamp = "2000-01-01T00:00:00+00:00"
        self.write("env/ws/lease.json", {"run": "r1", "acquired_at": stamp, "heartbeat": stamp, "queue": [], **extra})
        return envmod.lease_read("ws")

    def test_live_holder_pid_is_not_reclaimed(self):
        lease = self.old_lease(pid=os.getpid())
        self.assertIsNone(envmod.lease_stale(lease, 900))
        lease = self.old_lease(pid=os.getpid(), host=socket.gethostname(),
                               pid_create=envmod.process_create_time(os.getpid()))
        self.assertIsNone(envmod.lease_stale(lease, 900))
        self.make_run("r2", phase="build")
        self.assertFalse(envmod.lease_try("ws", "r2", 900)["granted"])

    def test_dead_or_recycled_holder_is_reclaimed(self):
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        self.assertIn("ttl", envmod.lease_stale(self.old_lease(pid=done.pid), 900))
        self.assertIn("ttl", envmod.lease_stale(self.old_lease(pid=os.getpid(), pid_create="not-this-process"), 900))
        self.assertIn("ttl", envmod.lease_stale(self.old_lease(pid=os.getpid(), host="another-machine"), 900))

    def test_finished_run_is_reclaimed_even_with_a_live_pid(self):
        self.make_run("r1", phase="done")
        self.assertIn("done", envmod.lease_stale(self.old_lease(pid=os.getpid()), 900))

    def test_heartbeat_records_the_beating_process(self):
        self.old_lease(pid=1)
        self.assertTrue(envmod.lease_heartbeat("ws", "r1"))
        lease = envmod.lease_read("ws")
        self.assertEqual(lease["pid"], os.getpid())
        self.assertTrue(lease["pid_create"] == envmod.process_create_time(os.getpid()))


class ReleaseStopsEnvTests(EnvCase):
    lease = True

    def start_env(self):
        self.assertEqual(cli("env", "lease", "acquire", "ws", "--run", "r1")[0], 0)
        rc, out = cli("env", "up", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.addCleanup(lambda: envmod.read_record("ws", "Web") and envmod._down_locked("ws", "Web"))
        return envmod.read_record("ws", "Web")["pid"]

    def test_release_runs_env_down(self):
        pid = self.start_env()
        rc, out = cli("env", "lease", "release", "ws", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.assertIn("stopped", out)
        self.assertIsNone(envmod.read_record("ws", "Web"))
        for _ in range(40):
            if not envmod.pid_alive(pid):
                break
            time.sleep(0.25)
        self.assertFalse(envmod.pid_alive(pid))
        self.assertIsNone(envmod.lease_read("ws").get("run"))

    def test_keep_env_leaves_it_running(self):
        pid = self.start_env()
        rc, out = cli("env", "lease", "release", "ws", "--run", "r1", "--keep-env")
        self.assertEqual(rc, 0, out)
        self.assertEqual(envmod.read_record("ws", "Web")["pid"], pid)
        self.assertTrue(envmod.pid_alive(pid))
        self.assertIsNone(envmod.lease_read("ws").get("run"))


if __name__ == "__main__":
    unittest.main()
