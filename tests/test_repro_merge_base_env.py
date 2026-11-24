"""`prove --repro` after the fix is committed (a check-only replan): the runtime repro serves the merge-base from a temporary
worktree, switching the run's env and restoring it, instead of demanding a run worktree at the merge-base."""

from __future__ import annotations

import socket
import sys
import unittest
import urllib.request
from pathlib import Path

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
