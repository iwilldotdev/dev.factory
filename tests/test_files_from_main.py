"""`env.files_from_main`: gitignored files copied from the main checkout into new worktrees, and the env preflight that
fails when a listed file is still missing in the served path (DESIGN section 3)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from real_support import RealCase, cli, core  # noqa: E402
from review_support import ReviewCase  # noqa: E402
from test_hotfix_flow import HotfixCase  # noqa: E402

from factory.collectors.test import copy_main_files, safe_rel  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import workspace  # noqa: E402


def ignore_env(repo: Path) -> None:
    """Gitignore `.env` for the main checkout and every worktree (info/exclude is shared), as a real repo does."""
    with open(repo / ".git" / "info" / "exclude", "a", encoding="utf-8") as fh:
        fh.write(".env" + chr(10))


class CopyHelper(unittest.TestCase):
    def test_copies_missing_never_overwrites_skips_unsafe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            src, dst = Path(t, "main"), Path(t, "wt")
            (src / "cfg").mkdir(parents=True)
            dst.mkdir()
            (src / ".env").write_text("A=1")
            (src / "cfg" / "local.json").write_text("{}")
            (dst / "keep").write_text("worktree")
            (src / "keep").write_text("main")
            got = copy_main_files(src, dst, [".env", "cfg/local.json", "keep", "absent", "../x", "/abs", "C:/x"])
            self.assertEqual(got, [".env", "cfg/local.json"])
            self.assertEqual((dst / "keep").read_text(), "worktree")
            self.assertEqual((dst / ".env").read_text(), "A=1")
            self.assertFalse((dst / ".env").is_symlink())  # a copy, not a link

    def test_safe_rel(self):
        for ok in (".env", "a/b.txt", "a\\b"):
            self.assertTrue(safe_rel(ok), ok)
        for bad in ("", "..", "../x", "a/../b", "/etc/x", "\\x", "C:\\x", "d:x", 3, None):
            self.assertFalse(safe_rel(bad), bad)


class Validation(unittest.TestCase):
    def errors(self, files):
        ws = {"worktree_root": "x", "repos": [{"alias": "A", "path": "x", "quality": {"a": "b"},
                                               "env": {"start": "s", "health": "h", "base_url": "b", "files_from_main": files}}]}
        return [e for e in workspace.check_workspace(ws) if "files_from_main" in e]

    def test_workspace_check(self):
        self.assertEqual(self.errors([".env", "cfg/x.json"]), [])
        self.assertTrue(self.errors(".env"))
        self.assertTrue(self.errors(["../secret"]))
        self.assertTrue(self.errors(["/etc/passwd"]))
        self.assertTrue(self.errors(["C:\\x"]))
        self.assertTrue(self.errors([3]))


class RunBranch(RealCase):
    repos = ("React",)

    def setUp(self):
        super().setUp()
        self.ws["repos"][0]["env"] = {"files_from_main": [".env", "missing.local"]}
        self.save_ws()
        (self.repo_dirs["React"] / ".env").write_text("TOKEN=abc", encoding="utf-8")

    def test_branch_copies_and_reports(self):
        self.ok("run", "start", "ws", "DEV-1")
        out = json.loads(self.ok("--json", "run", "branch", "ws-dev-1", "React", "--base", "main"))
        wt = Path(out["worktree"])
        self.assertEqual((wt / ".env").read_text(encoding="utf-8"), "TOKEN=abc")
        self.assertEqual(out["copied_from_main"], [".env"])
        self.assertFalse((wt / "missing.local").exists())

    def test_text_output_names_the_copy(self):
        self.ok("run", "start", "ws", "DEV-2")
        out = self.ok("run", "branch", "ws-dev-2", "React", "--base", "main")
        self.assertIn("copied from main checkout: .env", out)

    def test_no_key_no_copy(self):
        self.ws["repos"][0]["env"] = {}
        self.save_ws()
        self.ok("run", "start", "ws", "DEV-3")
        out = json.loads(self.ok("--json", "run", "branch", "ws-dev-3", "React", "--base", "main"))
        self.assertNotIn("copied_from_main", out)
        self.assertFalse((Path(out["worktree"]) / ".env").exists())


class Mirror(HotfixCase):
    def test_mirror_worktree_gets_the_file(self):
        self.ws["repos"][0]["env"] = {"files_from_main": [".env"]}
        self.save_ws()
        ignore_env(self.repo_dirs["React"])
        (self.repo_dirs["React"] / ".env").write_text("TOKEN=abc", encoding="utf-8")
        self.through_build()
        out = self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        self.assertIn("copied from main checkout: .env", out)
        mirror = Path(core.run_load(self.rid)["repos"]["React"]["worktrees"]["pack"])
        self.assertEqual((mirror / ".env").read_text(encoding="utf-8"), "TOKEN=abc")


class ReviewWorktree(ReviewCase):
    def test_review_worktree_gets_the_file(self):
        for conf in self.ws["repos"]:
            if conf["alias"] == "React":
                conf["env"] = {"files_from_main": [".env"]}
        self.save_ws()
        (self.repo_dirs["React"] / ".env").write_text("TOKEN=abc", encoding="utf-8")
        rid = self.start_single()
        out = self.ok("run", "branch", rid, "React", "--mr", "669")
        self.assertIn("copied from main checkout: .env", out)
        wt = Path(core.run_load(rid)["repos"]["React"]["worktree"])
        self.assertEqual((wt / ".env").read_text(encoding="utf-8"), "TOKEN=abc")


class Preflight(RealCase):
    repos = ("React",)

    def setUp(self):
        super().setUp()
        self.ws["repos"][0]["env"] = {"start": [sys.executable, "-c", "pass"], "health": "http://127.0.0.1:9/",
                                      "files_from_main": [".env"]}
        self.save_ws()
        self.ok("run", "start", "ws", "DEV-1")
        self.ok("run", "branch", "ws-dev-1", "React", "--base", "main")  # no .env in main: nothing copied
        self.run_ = core.run_load("ws-dev-1")
        self.wt = Path(self.run_["repos"]["React"]["worktree"])

    def test_files_missing_names_file_and_worktree(self):
        msgs = envmod.files_missing(core.workspace_load("ws"), self.run_, "React")
        self.assertEqual(msgs, [f"file .env missing in worktree {self.wt}"])
        (self.wt / ".env").write_text("x", encoding="utf-8")
        self.assertEqual(envmod.files_missing(core.workspace_load("ws"), self.run_, "React"), [])

    def test_env_up_refuses_before_starting(self):
        rc, out = cli("env", "up", "ws", "React", "--run", "ws-dev-1")
        self.assertEqual(rc, 1, out)
        self.assertIn("file .env missing in worktree", out)
        self.assertIn("ENV-FAIL repo=React kind=files", out)
        self.assertIsNone(envmod.read_record("ws", "React"))  # nothing was started


if __name__ == "__main__":
    unittest.main()
