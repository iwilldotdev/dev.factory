from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from factory.commands import gate

WS = {"commit": {"header_regex": r"^\[[^\]]+\] - .+", "forbidden_trailers": []}}


def run(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class InheritedCommitsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.remote = root / "remote.git"
        self.repo = root / "repo"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        run(self.repo, "remote", "add", "origin", str(self.remote))
        run(self.repo, "commit", "--allow-empty", "-m", "[X-1] - base")
        run(self.repo, "push", "origin", "main")

    def commit(self, msg):
        run(self.repo, "commit", "--allow-empty", "-m", msg)

    def check(self, branch="feature/own"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            errors = gate.commits_errors(self.repo, "main", WS, "R", branch)
        return errors, out.getvalue()

    def test_commit_on_other_remote_branch_is_skipped(self):
        run(self.repo, "checkout", "-b", "feature/other")
        self.commit("bad inherited")
        run(self.repo, "push", "origin", "feature/other")
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("[X-2] - mine")
        errors, out = self.check()
        self.assertEqual(errors, [])
        self.assertIn("1 inherited commits on other remote branches skipped", out)

    def test_local_only_commit_is_checked(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("bad local")
        errors, out = self.check()
        self.assertEqual(len(errors), 1)
        self.assertNotIn("skipped", out)

    def test_commit_on_own_remote_branch_is_checked(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("bad pushed")
        run(self.repo, "push", "origin", "feature/own")
        errors, _ = self.check()
        self.assertEqual(len(errors), 1)

    def test_without_remotes_behaviour_is_unchanged(self):
        run(self.repo, "remote", "remove", "origin")
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("bad local")
        errors, out = self.check()
        self.assertEqual(len(errors), 1)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
