"""Env lease (DESIGN §11): one env per workspace at a time, FIFO queue, stale reclaim, owner checks on down/up/login."""

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_helpers import FactoryCase  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import prove  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = main(list(argv))
    return rc, out.getvalue()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sub_env():
    return {**os.environ, "PYTHONPATH": str(ROOT)}


class LeaseCase(FactoryCase):
    lease_on = True

    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.ws = {
            "schemaVersion": 1, "name": "ws",
            "env": {"lease": self.lease_on, "lease_ttl_s": 900},
            "repos": [{"alias": "Web", "name": "web", "path": str(self.tmp), "env": {
                "start": [sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"],
                "cwd": ".", "health": f"http://127.0.0.1:{self.port}/", "ports": [self.port],
                "startup_timeout_s": 30, "login": {"url": "/login"}}}],
        }
        self.write("workspaces/ws/workspace.json", self.ws)
        for rid in ("r1", "r2", "r3", "r4"):
            self.make_run(rid, phase="build")

    def holder(self):
        return envmod.lease_read("ws").get("run")

    def try_(self, rid, wait=False):
        return envmod.lease_try("ws", rid, 900, wait=wait)

    def events(self, rid, kind):
        return [e for e in core.run_load(rid)["events"] if e["kind"] == kind]


class AcquireTests(LeaseCase):
    def test_exclusive_between_processes(self):
        procs = [subprocess.Popen([sys.executable, "-m", "factory", "env", "lease", "acquire", "ws", "--run", rid],
                                  cwd=str(ROOT), env=sub_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True) for rid in ("r1", "r2", "r3", "r4")]
        results = [(p.wait(timeout=60), p.stdout.read()) for p in procs]
        for p in procs:
            p.stdout.close()
        winners = [rid for rid, (rc, _) in zip(("r1", "r2", "r3", "r4"), results) if rc == 0]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(self.holder(), winners[0])
        self.assertEqual(len(self.events(winners[0], "env_lease")), 1)

    def test_file_lock_is_exclusive_across_processes(self):
        counter = self.tmp / "counter.txt"
        counter.write_text("0")
        script = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            from factory.commands.env import _file_lock
            p = Path({str(counter)!r})
            for _ in range(40):
                with _file_lock(Path({str(self.tmp / 'c.lock')!r})):
                    n = int(p.read_text())
                    time.sleep(0.001)
                    p.write_text(str(n + 1))
        """)
        procs = [subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT), env=sub_env()) for _ in range(2)]
        self.assertEqual([p.wait(timeout=120) for p in procs], [0, 0])
        self.assertEqual(counter.read_text(), "80")
        self.assertFalse((self.tmp / "c.lock").exists())

    def test_wait_is_granted_after_release(self):
        self.assertTrue(self.try_("r1")["granted"])
        proc = subprocess.Popen([sys.executable, "-m", "factory", "env", "lease", "acquire", "ws", "--run", "r2",
                                 "--wait", "--poll", "0.1", "--timeout", "60"],
                                cwd=str(ROOT), env=sub_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(200):
                if [w["run"] for w in envmod.lease_read("ws")["queue"]] == ["r2"]:
                    break
                time.sleep(0.05)
            self.assertEqual([w["run"] for w in envmod.lease_read("ws")["queue"]], ["r2"])
            envmod.lease_release("ws", "r1")
            rc = proc.wait(timeout=60)
            out = proc.stdout.read()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
        self.assertEqual(rc, 0, out)
        self.assertIn("position 1 in queue", out)
        self.assertIn("held by r1", out)
        self.assertEqual(self.holder(), "r2")
        self.assertEqual(envmod.lease_read("ws")["queue"], [])

    def test_wait_timeout_leaves_queue(self):
        self.try_("r1")
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "r2", "--wait", "--poll", "0.05", "--timeout", "0.2")
        self.assertEqual(rc, 1, out)
        self.assertIn("timed out", out)
        self.assertEqual(envmod.lease_read("ws")["queue"], [])
        self.assertEqual(self.holder(), "r1")

    def test_fifo_order(self):
        self.assertTrue(self.try_("r1")["granted"])
        self.assertEqual(self.try_("r2", wait=True)["position"], 1)
        self.assertEqual(self.try_("r3", wait=True)["position"], 2)
        self.assertFalse(self.try_("r4")["granted"])  # no --wait: refused, not queued
        self.assertEqual([w["run"] for w in envmod.lease_read("ws")["queue"]], ["r2", "r3"])
        envmod.lease_release("ws", "r1")
        self.assertFalse(self.try_("r3", wait=True)["granted"])  # r2 is ahead
        self.assertFalse(self.try_("r4")["granted"])
        self.assertTrue(self.try_("r2", wait=True)["granted"])
        self.assertEqual(self.try_("r3", wait=True)["position"], 1)
        envmod.lease_release("ws", "r2")
        self.assertTrue(self.try_("r3", wait=True)["granted"])

    def test_reacquire_by_holder_refreshes(self):
        self.try_("r1")
        res = self.try_("r1")
        self.assertTrue(res["granted"])
        self.assertFalse(res["fresh"])

    def test_release_free_drops_file(self):
        self.try_("r1")
        self.assertTrue(envmod.lease_path("ws").is_file())
        envmod.lease_release("ws", "r1")
        self.assertFalse(envmod.lease_path("ws").exists())
        self.assertIsNone(self.holder())

    def test_release_by_other_needs_force(self):
        self.try_("r1")
        with self.assertRaises(core.FactoryError):
            envmod.lease_release("ws", "r2")
        rc, out = cli("env", "lease", "release", "ws", "--run", "r2", "--force")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.holder())

    def test_status_json(self):
        self.try_("r1")
        self.try_("r2", wait=True)
        rc, out = cli("env", "lease", "status", "ws", "--json")
        self.assertEqual(rc, 0, out)
        st = json.loads(out)
        self.assertEqual(st["holder"], "r1")
        self.assertEqual([w["run"] for w in st["queue"]], ["r2"])
        self.assertIsNone(st["stale"])
        rc, out = cli("env", "lease", "status", "ws")
        self.assertIn("held by r1", out)
        self.assertIn("1. r2", out)

    def test_run_of_other_workspace_refused(self):
        self.make_run("x1", workspace="other", phase="build")
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "x1")
        self.assertEqual(rc, 2, out)


class StaleTests(LeaseCase):
    def _old_lease(self, rid, age_s):
        old = time.time() - age_s
        import datetime as dt
        stamp = dt.datetime.fromtimestamp(old, dt.timezone.utc).isoformat(timespec="seconds")
        self.write("env/ws/lease.json", {"run": rid, "pid": 1, "acquired_at": stamp, "heartbeat": stamp, "queue": []})

    def test_stale_by_ttl(self):
        self._old_lease("r1", 2000)
        self.assertIn("ttl", envmod.lease_stale(envmod.lease_read("ws"), 900))
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "r2")
        self.assertEqual(rc, 0, out)
        self.assertIn("reclaimed stale lease of r1", out)
        self.assertEqual(self.holder(), "r2")
        self.assertEqual(self.events("r1", "env_lease")[0]["data"]["action"], "reclaimed")

    def test_fresh_within_ttl_not_stale(self):
        self._old_lease("r1", 100)
        self.assertIsNone(envmod.lease_stale(envmod.lease_read("ws"), 900))
        self.assertFalse(self.try_("r2")["granted"])

    def test_stale_by_finished_run(self):
        for phase in ("done", "blocked"):
            with self.subTest(phase=phase):
                envmod.lease_release("ws", "r2", force=True)
                self.make_run("r1", phase="build")
                self.assertTrue(self.try_("r1")["granted"])
                self.make_run("r1", phase=phase)
                res = self.try_("r2")
                self.assertTrue(res["granted"])
                self.assertEqual(res["reclaimed"][0][0], "r1")
                self.assertIn(phase, res["reclaimed"][0][1])

    def test_stale_waiter_leaves_queue(self):
        self.try_("r1")
        self.try_("r2", wait=True)
        lease = envmod.lease_read("ws")
        lease["queue"][0]["seen"] = "2000-01-01T00:00:00+00:00"
        self.write("env/ws/lease.json", lease)
        envmod.lease_release("ws", "r1")
        self.assertTrue(self.try_("r3")["granted"])  # r2 stopped polling: it no longer blocks the queue

    def test_heartbeat(self):
        self._old_lease("r1", 600)
        self.assertFalse(envmod.lease_heartbeat("ws", "r2"))
        self.assertTrue(envmod.lease_heartbeat("ws", "r1"))
        self.assertLess(envmod.lease_status("ws", 900)["heartbeat_age_s"], 60)

    def test_prove_and_check_refresh_heartbeat(self):
        self._old_lease("r1", 600)
        prove._lease_heartbeat(core.run_load("r1"))
        self.assertLess(envmod.lease_status("ws", 900)["heartbeat_age_s"], 60)
        self._old_lease("r1", 600)
        cli("env", "check", "ws", "Web", "--run", "r1")
        self.assertLess(envmod.lease_status("ws", 900)["heartbeat_age_s"], 60)


class OwnerTests(LeaseCase):
    def test_down_refused_for_non_owner(self):
        self.try_("r1")
        with self.assertRaises(core.FactoryError) as ctx:
            envmod.down(self.ws, "Web", "r2")
        self.assertIn("held by r1", str(ctx.exception))
        with self.assertRaises(core.FactoryError):
            envmod.down(self.ws, "Web")
        rc, out = cli("env", "down", "ws", "Web", "--run", "r2")
        self.assertEqual(rc, 2, out)

    def test_down_forced_records_events(self):
        self.try_("r1")
        msg = envmod.down(self.ws, "Web", "r2", force=True)
        self.assertIn("forced", msg)
        self.assertTrue(self.events("r2", "env_down")[0]["data"]["forced"])
        self.assertEqual(self.events("r1", "env_down_forced")[0]["data"]["by"], "r2")
        self.assertEqual(self.holder(), "r1")  # forcing a stop does not take the lease

    def test_down_by_owner(self):
        self.try_("r1")
        rc, out = cli("env", "down", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.events("r1", "env_down")[0]["data"]["forced"])

    def test_up_refused_without_lease(self):
        with self.assertRaises(core.FactoryError) as ctx:
            envmod.up(self.ws, "Web")
        self.assertIn("--run", str(ctx.exception))
        self.try_("r1")
        with self.assertRaises(core.FactoryError) as ctx:
            envmod.up(self.ws, "Web", core.run_load("r2"))
        self.assertIn("does not hold the env lease", str(ctx.exception))
        self.assertFalse(envmod.pid_path("ws", "Web").exists())

    def test_up_with_lease(self):
        self.try_("r1")
        ok, lines = envmod.up(self.ws, "Web", {"id": "r1", "workspace": "ws", "repos": {}})
        self.addCleanup(envmod.down, self.ws, "Web", "r1", True)
        self.assertTrue(ok, lines)
        self.assertFalse(envmod._lock_path("ws").exists())  # lock released after start
        envmod.down(self.ws, "Web", "r1")

    def test_login_refused_without_lease(self):
        with self.assertRaises(core.FactoryError) as ctx:
            envmod.login(self.ws, "Web")
        self.assertIn("lease", str(ctx.exception))
        self.try_("r1")
        rc, out = cli("env", "login", "ws", "Web", "--run", "r2")
        self.assertEqual(rc, 2, out)
        self.assertIn("does not hold the env lease", out)


class LeaseOffTests(LeaseCase):
    lease_on = False

    def test_up_login_not_gated(self):
        envmod.require_lease(self.ws, None, "env up Web")  # no-op
        with self.assertRaises(core.FactoryError) as ctx:
            envmod.login(self.ws, "Web")
        self.assertNotIn("lease", str(ctx.exception))  # fails later, on missing secrets

    def test_down_without_lease_file(self):
        self.assertIn("no pid file", envmod.down(self.ws, "Web"))


if __name__ == "__main__":
    unittest.main()
