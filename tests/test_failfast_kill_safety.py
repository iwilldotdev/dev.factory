"""Factory never kills a process it cannot prove it started: current process/ancestors, reused pids, legacy records."""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import prove  # noqa: E402


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class SafetyCase(FactoryCase):
    def setUp(self):
        super().setUp()
        self.web = make_repo(self.tmp / "web").resolve()
        self.ws = {"schemaVersion": 1, "name": "ws", "env": {"lease": True, "lease_ttl_s": 900},
                   "repos": [{"alias": "Web", "name": "web", "path": str(self.web), "env": {
                       "start": [sys.executable, "-c", "pass"], "cwd": ".", "health": "http://127.0.0.1:1/",
                       "startup_timeout_s": 2}}]}
        self.write("workspaces/ws/workspace.json", self.ws)
        self.make_run("r1", phase="prove", repos={"Web": {"worktree": str(self.web), "branch": "b"}})
        self.assertTrue(envmod.lease_try("ws", "r1", 900)["granted"])
        self.victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(self.victim.wait)  # cleanups run last-in first-out: kill, then wait
        self.addCleanup(self.victim.kill)

    def record(self, pid, **over):
        rec = {"pid": pid, "cwd": str(self.web), "head": "0" * 40, "started": core.now(),
               "create_time": envmod.process_create_time(pid)}
        rec.update(over)
        for k in [k for k, v in rec.items() if v is None]:
            del rec[k]
        self.write("env/ws/Web.pid", rec)

    def assert_alive(self, pid):
        self.assertTrue(envmod.pid_alive(pid))


class CurrentProcessTests(SafetyCase):
    def test_current_and_parent_pids_are_protected(self):
        self.assertTrue(envmod.is_protected_pid(os.getpid()))
        self.assertTrue(envmod.is_protected_pid(os.getppid()))
        self.assertFalse(envmod.is_protected_pid(self.victim.pid))

    def test_down_refuses_the_current_process(self):
        self.record(os.getpid())
        with self.assertRaises(core.FactoryError) as ctx:
            envmod.down(self.ws, "Web", "r1")
        self.assertIn("NOT killed", str(ctx.exception))
        self.assertIsNotNone(envmod.read_record("ws", "Web"))

    def test_restart_and_auto_restart_do_not_kill_the_current_process(self):
        self.record(os.getpid())
        with self.assertRaises(core.FactoryError):
            envmod.restart_env(self.ws, core.run_load("r1"), "Web")
        rc, out = cli("env", "check", "ws", "Web", "--run", "r1", "--auto-restart")
        self.assertNotEqual(rc, 0, out)  # reported, alive to say so

    def test_prove_preflight_skips_protected_pid(self):
        self.record(os.getpid())
        self.assertIsNone(envmod.stale_reason(self.ws, core.run_load("r1"), "Web"))
        with mock.patch.object(envmod, "restart_env") as restart:
            prove._preflight_envs(core.run_load("r1"), self.ws, ["Web"], prove.Reporter(False), {})
        restart.assert_not_called()

    def test_lease_release_does_not_kill_the_current_process(self):
        self.record(os.getpid(), cwd=str(envmod._cwd_for(self.ws, core.run_load("r1"), "Web")))
        rc, out = cli("env", "lease", "release", "ws", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.assertIn("NOT killed", out)


class ForeignRecordTests(SafetyCase):
    def check_never_killed(self, **over):
        self.record(self.victim.pid, **over)
        cwd = str(envmod._cwd_for(self.ws, core.run_load("r1"), "Web"))
        self.write("env/ws/Web.pid", {**envmod.read_record("ws", "Web"), "cwd": cwd})
        run = core.run_load("r1")
        try:
            envmod.down(self.ws, "Web", "r1")
        except core.FactoryError:
            pass  # unknown identity: refused, record kept
        self.assert_alive(self.victim.pid)
        with mock.patch.object(envmod, "up", return_value=(False, [])):
            try:
                envmod.restart_env(self.ws, run, "Web")
            except core.FactoryError:
                pass
        self.assert_alive(self.victim.pid)
        cli("env", "lease", "release", "ws", "--run", "r1")
        self.assert_alive(self.victim.pid)
        prove._preflight_envs(run, self.ws, ["Web"], prove.Reporter(False), {})
        self.assert_alive(self.victim.pid)
        self.assertEqual(envmod.record_state(envmod.read_record("ws", "Web")) in ("other", "unknown", "gone"), True)

    def test_reused_pid_with_mismatched_create_time(self):
        self.check_never_killed(create_time="12345")

    def test_legacy_record_without_create_time(self):
        self.check_never_killed(create_time=None)

    def test_unhealthy_up_does_not_kill_a_foreign_pid(self):
        self.record(self.victim.pid, create_time="12345")
        ok, lines = envmod.up(self.ws, "Web", core.run_load("r1"))
        self.assert_alive(self.victim.pid)


class OtherRunEnvTests(SafetyCase):
    """Lease off: a stale env whose record cwd is ANOTHER run's worktree must never be restarted by this run."""

    def setUp(self):
        super().setUp()
        self.ws["env"] = {}
        self.write("workspaces/ws/workspace.json", self.ws)
        self.web2 = make_repo(self.tmp / "web2").resolve()
        self.make_run("r2", phase="prove", repos={"Web": {"worktree": str(self.web2), "branch": "b2"}})

    def test_prove_preflight_does_not_down_another_runs_env(self):
        self.record(self.victim.pid, cwd=str(self.web2))  # run B's env is up
        self.assertEqual(envmod.record_state(envmod.read_record("ws", "Web")), "same")
        self.assertTrue(envmod.can_restart(self.ws, core.run_load("r1")))  # lease off: always true
        stopped: dict = {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(envmod, "restart_env", return_value=(True, [])) as restart:
            prove._preflight_envs(core.run_load("r1"), self.ws, ["Web"], prove.Reporter(False), stopped)
        restart.assert_not_called()
        self.assert_alive(self.victim.pid)
        self.assertIn("kind=stale detail=env serves", out.getvalue())
        self.assertIn("(run r2)", out.getvalue())
        self.assertIn("Web", stopped)
        self.assertIsNotNone(envmod.read_record("ws", "Web"))

    def test_env_check_auto_restart_does_not_down_another_runs_env(self):
        self.record(self.victim.pid, cwd=str(self.web2))
        with mock.patch.object(envmod, "restart_env", return_value=(True, [])) as restart:
            rc, out = cli("env", "check", "ws", "Web", "--run", "r1", "--auto-restart")
        restart.assert_not_called()
        self.assertEqual(rc, envmod.EXIT_STALE, out)
        self.assertIn("kind=stale detail=env serves", out)
        self.assertIn("(run r2)", out)
        self.assert_alive(self.victim.pid)

    def test_own_worktree_with_moved_head_is_still_restarted(self):
        self.record(self.victim.pid, cwd=str(self.web))  # r1's own worktree, started at another HEAD
        with mock.patch.object(envmod, "restart_env", return_value=(True, ["Web: up"])) as restart:
            prove._preflight_envs(core.run_load("r1"), self.ws, ["Web"], prove.Reporter(False), {})
        restart.assert_called_once()

    def test_lease_on_restart_only_while_this_run_holds_it(self):
        self.ws["env"] = {"lease": True, "lease_ttl_s": 900}
        self.write("workspaces/ws/workspace.json", self.ws)
        self.assertEqual(cli("env", "lease", "release", "ws", "--run", "r1")[0], 0)
        self.assertTrue(envmod.lease_try("ws", "r2", 900)["granted"])
        self.record(self.victim.pid, cwd=str(self.web))
        with mock.patch.object(envmod, "restart_env", return_value=(True, [])) as restart:
            prove._preflight_envs(core.run_load("r1"), self.ws, ["Web"], prove.Reporter(False), {})
        restart.assert_not_called()
        self.assert_alive(self.victim.pid)


class KillTreeTests(unittest.TestCase):
    def test_kill_tree_of_current_and_parent_pid_is_a_noop(self):
        with mock.patch.object(envmod.subprocess, "run") as run, mock.patch.object(envmod.os, "killpg", create=True) as kp,                 mock.patch.object(envmod, "_win_process_table", return_value=[]) as tbl:
            envmod.kill_tree(os.getppid())
            envmod.kill_tree(os.getpid())
        run.assert_not_called()
        kp.assert_not_called()
        tbl.assert_not_called()

    def test_tree_selection_walks_descendants_leaf_first(self):
        table = [{"pid": 10, "ppid": 1, "created": 100}, {"pid": 11, "ppid": 10, "created": 110},
                 {"pid": 12, "ppid": 11, "created": 120}, {"pid": 13, "ppid": 10, "created": 115},
                 {"pid": 99, "ppid": 1, "created": 50}]
        order = envmod.tree_kill_order(table, 10)
        self.assertEqual(order[-1], 10)
        self.assertEqual(set(order), {10, 11, 12, 13})
        self.assertLess(order.index(12), order.index(11))

    def test_reused_parent_pid_does_not_link_an_older_unrelated_process(self):
        # pid 20 died; its pid was reused by 10's... here 30 still records ppid=10 but was created BEFORE pid 10 started
        table = [{"pid": 10, "ppid": 1, "created": 500}, {"pid": 30, "ppid": 10, "created": 100},
                 {"pid": 31, "ppid": 30, "created": 150}, {"pid": 11, "ppid": 10, "created": 510}]
        self.assertEqual(set(envmod.tree_kill_order(table, 10)), {10, 11})

    def test_protected_pids_are_never_selected_nor_descended(self):
        table = [{"pid": 10, "ppid": 1, "created": 100}, {"pid": 11, "ppid": 10, "created": 110},
                 {"pid": 12, "ppid": 11, "created": 120}]
        self.assertEqual(envmod.tree_kill_order(table, 10, frozenset({11})), [10])
        self.assertEqual(envmod.tree_kill_order(table, 10, frozenset({10})), [])

    def test_windows_kill_uses_explicit_pids_not_taskkill_tree(self):
        table = [{"pid": 10, "ppid": 1, "created": 100}, {"pid": 11, "ppid": 10, "created": 110}]
        with mock.patch.object(envmod, "IS_WIN", True), mock.patch.object(envmod, "is_protected_pid", return_value=False),                 mock.patch.object(envmod, "_win_process_table", return_value=table),                 mock.patch.object(envmod, "_protected_pids", return_value={1}),                 mock.patch.object(envmod.subprocess, "run") as run:
            envmod.kill_tree(10)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls, [["taskkill", "/F", "/PID", "11"], ["taskkill", "/F", "/PID", "10"]])


if __name__ == "__main__":
    unittest.main()
