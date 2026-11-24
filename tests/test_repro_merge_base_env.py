"""`prove --repro` after the fix is committed (a check-only replan): the runtime repro serves the merge-base from a temporary
worktree, switching the run's env and restoring it, instead of demanding a run worktree at the merge-base."""

from __future__ import annotations

import socket
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from real_support import cli, commit_file, core, sh  # noqa: E402
from test_bug_flow import BugCase  # noqa: E402

from factory.commands import env as envmod  # noqa: E402
from factory.commands import prove  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(port: int, path: str = "/mod.py") -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.read().decode()


class MovedHead(BugCase):
    def setUp(self) -> None:
        super().setUp()
        self.port = free_port()
        self.ws["repos"][0]["env"] = {
            "start": [sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"], "cwd": ".",
            "ports": [self.port], "health": f"http://127.0.0.1:{self.port}/", "base_url": f"http://127.0.0.1:{self.port}",
            "startup_timeout_s": 20}
        self.save_ws()
        self.write_acs(
            [{"id": "CHK-1", "type": "api", "repo": "React", "path": "/mod.py", "expect": {"body_contains": "v = 2"}}],
            [{"id": "AC-1", "ticket": "BUG-1", "text": "v is 2", "kind": "bug", "checks": ["CHK-1"]}])
        self.addCleanup(self.stop_env)
        self.mb = sh("git", "merge-base", "HEAD", "origin/main", cwd=self.wt)

    def stop_env(self):
        try:
            envmod.down(core.workspace_load("ws"), "React", self.rid)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass

    def repro(self, *extra):
        rc, out = cli("prove", self.rid, "--repro", *extra)
        ev = core.read_json(core.run_dir(self.rid) / "evidence" / "REPRO-CHK-1.json")
        return rc, out, ev

    def record(self):
        return envmod.read_record("ws", "React", core.run_load(self.rid).get("env_slots", {}).get("React", 0))

    def worktrees(self):
        return [l for l in sh("git", "worktree", "list", cwd=self.repo_dirs["React"]).splitlines() if l.strip()]

    def test_fix_already_committed_serves_the_merge_base_then_restores_the_env(self):
        self.ok("env", "up", "ws", "React", "--run", self.rid)
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")  # the build: HEAD is past the merge-base
        self.assertEqual(fetch(self.port).strip(), "v = 2")  # (the running server serves the worktree files)
        rc, out, ev = self.repro()  # no --assume-env: the CLI serves the merge-base itself
        self.assertEqual(rc, 0, out)
        self.assertEqual(ev["status"], "reproduced", ev["summary"])
        self.assertEqual(ev["git_sha"], self.mb)
        self.assertIn("serving the merge-base from a temporary worktree", out)
        self.assertIn("env restored", out)
        rec = self.record()
        self.assertEqual(envmod.record_state(rec), "same")  # the env of the run is back, on the run worktree
        self.assertTrue(envmod._same_path(rec["cwd"], self.wt))
        self.assertEqual(rec["head"], core.git_head(self.wt))
        self.assertEqual(fetch(self.port).strip(), "v = 2")
        self.assertEqual(len(self.worktrees()), 2, self.worktrees())  # main + run worktree: the temporary one is gone

    def test_no_env_before_means_no_env_after(self):
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        rc, out, ev = self.repro()
        self.assertEqual((rc, ev["status"]), (0, "reproduced"), out)
        self.assertIsNone(self.record())
        self.assertEqual(len(self.worktrees()), 2)

    def test_the_worktree_is_removed_when_the_env_cannot_start(self):
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        self.ws["repos"][0]["env"]["start"] = [sys.executable, "-c", "import sys; sys.exit(3)"]
        self.save_ws()
        rc, out, ev = self.repro()
        self.assertEqual(rc, 1, out)
        self.assertEqual(ev["status"], "error")
        self.assertIn("could not serve", ev["summary"])
        self.assertEqual(len(self.worktrees()), 2)

    def test_an_env_that_is_not_the_runs_is_never_touched(self):
        self.ok("env", "up", "ws", "React")  # started without the run: serves the main checkout
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        before = self.record()
        rc, out, ev = self.repro()
        self.assertEqual(ev["status"], "error", out)
        self.assertIn("cannot be switched", ev["summary"])
        self.assertEqual(self.record()["pid"], before["pid"])
        self.assertEqual(envmod.record_state(self.record()), "same")

    def test_head_at_the_merge_base_keeps_the_assume_env_path(self):
        self.assertEqual(prove.moved_repro_repos(core.run_load(self.rid), core.workspace_load("ws"),
                                                 [{"type": "api", "repo": "React"}], []), {})
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        self.assertEqual(prove.moved_repro_repos(core.run_load(self.rid), core.workspace_load("ws"),
                                                 [{"type": "api", "repo": "React"}], []), {"React": self.mb})
        self.assertEqual(prove.moved_repro_repos(core.run_load(self.rid), core.workspace_load("ws"),
                                                 [{"type": "test", "repo": "React"}], []), {})  # local checks never need it


    # ---- independent review of the Phase 4 branch: a failed restore is never a success ----

    def test_a_failed_restore_is_an_env_failure_not_a_success(self):
        self.ok("env", "up", "ws", "React", "--run", self.rid)
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        real_up, calls = envmod.up, []

        def flaky_up(*a, **kw):
            calls.append(kw.get("serve_path"))
            if len(calls) >= 2:  # the second `up` is the restore
                return False, ["ENV-FAIL repo=React kind=start detail=simulated"]
            return real_up(*a, **kw)

        with mock.patch.object(envmod, "up", flaky_up):
            rc, out, ev = self.repro()
        self.assertEqual(rc, prove.EXIT_ENV, out)
        self.assertIn("ENV-FAIL repo=React kind=start detail=env not restored", out)
        self.assertEqual(ev["status"], "reproduced")  # the evidence already written stays as it is
        self.assertIsNone(self.record())  # (and the message tells the user the env is down)
        self.assertEqual(len(self.worktrees()), 2)

    def test_a_restore_that_works_keeps_exit_zero(self):
        self.ok("env", "up", "ws", "React", "--run", self.rid)
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        rc, out, _ = self.repro()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("env not restored", out)

    def test_the_cleanup_is_registered_before_the_worktree_is_created(self):
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        dropped = []

        def interrupted(repo, ref, setup=None, env_vars=None, files=None, on_target=None):
            on_target(Path("target-before-creation"))  # as the real function does, before `git worktree add`
            raise KeyboardInterrupt

        with mock.patch.object(prove, "temp_worktree", interrupted),                 mock.patch.object(prove, "drop_worktree", lambda repo, t: dropped.append(t)):
            with self.assertRaises(KeyboardInterrupt):
                cli("prove", self.rid, "--repro")
        self.assertEqual(dropped, [Path("target-before-creation")])


class ServeFromRestore(unittest.TestCase):
    """`serve_from` with the env primitives stubbed: what it reports and which dependencies it stops again."""

    WS = {"name": "ws", "repos": [{"alias": "Web", "env": {"depends_on": ["Api"]}}, {"alias": "Api", "env": {}}]}
    RUN = {"id": "r1"}

    def serve(self, before, after=None, up_ok=True, down_error=None):
        """`before`/`after`: record state of each alias ("same", "gone", "unknown") before the serve and once it serves."""
        said, downs = [], []
        phase = {"now": before}

        def fake_down(ws, alias, run_id=None, force=False, slot=None):
            downs.append(alias)
            if down_error:
                raise core.FactoryError(down_error)
            return f"{alias} stopped"

        calls = []

        def fake_up(*a, **kw):
            calls.append(kw.get("serve_path"))
            if len(calls) == 1:
                phase["now"] = after or before  # the serve started the env (and its dependencies)
                return True, []
            return up_ok, ["ENV-FAIL repo=Web kind=start detail=nope"]

        with mock.patch.object(envmod, "repo_conf", lambda ws, a: next(r for r in ws["repos"] if r["alias"] == a)),                 mock.patch.object(envmod, "is_shared", lambda *a: False),                 mock.patch.object(envmod, "run_slot", lambda *a: 0),                 mock.patch.object(envmod, "read_record", lambda ws, alias, slot: {"alias": alias}),                 mock.patch.object(envmod, "record_state", lambda rec: phase["now"].get(rec["alias"], "gone") if rec else "gone"),                 mock.patch.object(envmod, "restart_blocker", lambda *a: None),                 mock.patch.object(envmod, "down", fake_down), mock.patch.object(envmod, "up", fake_up):
            with envmod.serve_from(self.WS, self.RUN, "Web", Path("tmp"), said.append) as outcome:
                pass
        return outcome, said, downs

    def test_dependencies_started_by_the_serve_are_stopped_again(self):
        outcome, _, downs = self.serve({}, {"Web": "same", "Api": "same"})
        self.assertFalse(outcome.restore_failed)
        self.assertEqual(downs, ["Web", "Api"])

    def test_a_dependency_that_ran_before_is_left_running(self):
        _, _, downs = self.serve({"Api": "same"}, {"Web": "same", "Api": "same"})
        self.assertEqual(downs, ["Web"])

    def test_nothing_stopped_means_no_env_not_restored_message(self):
        outcome, said, _ = self.serve({"Web": "unknown"}, down_error="lease held elsewhere")
        self.assertFalse(outcome.restore_failed)  # an "unknown" identity record was never stopped by us
        self.assertFalse([t for t in said if "env not restored" in t], said)

    def test_a_running_env_that_cannot_be_started_again_is_reported(self):
        outcome, said, _ = self.serve({"Web": "same"}, up_ok=False)
        self.assertTrue(outcome.restore_failed)
        self.assertIn("nope", outcome.detail)
        self.assertTrue([t for t in said if "env not restored" in t], said)


class Worktree(BugCase):
    def test_temp_base_worktree_gets_the_files_from_main(self):
        main = self.repo_dirs["React"]
        with open(main / ".git" / "info" / "exclude", "a", encoding="utf-8") as fh:
            fh.write(".env" + chr(10))
        (main / ".env").write_text("TOKEN=abc", encoding="utf-8")
        self.ws["repos"][0]["env"] = {"files_from_main": [".env"]}
        self.save_ws()
        run, ws = core.run_load(self.rid), core.workspace_load("ws")
        mb = sh("git", "merge-base", "HEAD", "origin/main", cwd=self.wt)
        tmp = prove._base_worktree(run, ws, "React", mb)
        try:
            self.assertEqual((tmp / ".env").read_text(encoding="utf-8"), "TOKEN=abc")
            self.assertEqual(core.git_head(tmp), mb)
        finally:
            prove.drop_worktree(main, tmp)


if __name__ == "__main__":
    unittest.main()
