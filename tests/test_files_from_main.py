"""`env.files_from_main`: gitignored files copied from the main checkout into new worktrees, and the env preflight that
fails when a listed file is still missing in the served path (DESIGN section 3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


def git_dir(path: Path, ignore: str = "") -> Path:
    """A throw-away git repo (the destination worktree), ignoring the given patterns."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    (path / ".gitignore").write_text(ignore, encoding="utf-8")
    return path


def make_junction(link: Path, target: Path) -> bool:
    """A directory link at `link` to `target`: a symlink when allowed, else a Windows junction. False: cannot be made."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return True
        except OSError:
            return False
    return False


class CopyHelper(unittest.TestCase):
    def test_copies_missing_never_overwrites_skips_unsafe(self):
        with tempfile.TemporaryDirectory() as t:
            src, dst = Path(t, "main"), git_dir(Path(t, "wt"), ".env\ncfg/\nkeep\n")
            (src / "cfg").mkdir(parents=True)
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


class Containment(unittest.TestCase):
    """Independent review of the Phase 4 branch: `files_from_main` never writes outside the worktree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.main = self.root / "main"
        self.outside = self.root / "outside"
        self.wt = git_dir(self.root / "wt", ".env\nconfig/\nsecret.txt\n")
        for d in (self.main / "config", self.outside):
            d.mkdir(parents=True)
        (self.main / "config" / ".env").write_text("SECRET=1", encoding="utf-8")
        (self.main / ".env").write_text("SECRET=1", encoding="utf-8")

    def copy(self, files):
        skipped: list[str] = []
        return copy_main_files(self.main, self.wt, files, skipped), skipped

    def test_a_linked_parent_directory_is_never_written_through(self):
        if not make_junction(self.wt / "config", self.outside):
            self.skipTest("cannot create a symlink or junction here")
        copied, skipped = self.copy(["config/.env"])
        self.assertEqual(copied, [])
        self.assertFalse((self.outside / ".env").exists())
        self.assertTrue(skipped and "link" in skipped[0], skipped)

    def test_a_dangling_link_at_the_destination_is_not_written_through(self):
        victim = self.root / "victim.txt"
        try:
            os.symlink(victim, self.wt / ".env")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a file symlink here")
        copied, skipped = self.copy([".env"])
        self.assertEqual(copied, [])
        self.assertFalse(victim.exists())
        self.assertTrue(skipped and "link" in skipped[0], skipped)

    def test_a_symlinked_source_is_refused(self):
        (self.outside / "x").write_text("top secret", encoding="utf-8")
        try:
            os.symlink(self.outside / "x", self.main / "secret.txt")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a file symlink here")
        copied, skipped = self.copy(["secret.txt"])
        self.assertEqual(copied, [])
        self.assertFalse((self.wt / "secret.txt").exists())
        self.assertIn("symlink", skipped[0])

    def test_a_source_below_a_link_to_outside_the_checkout_is_refused(self):
        (self.outside / ".env").write_text("elsewhere", encoding="utf-8")
        if not make_junction(self.main / "linked", self.outside):
            self.skipTest("cannot create a symlink or junction here")
        copied, skipped = self.copy(["linked/.env"])
        self.assertEqual(copied, [])
        self.assertTrue(skipped and "outside the main checkout" in skipped[0], skipped)

    def test_a_directory_source_is_not_a_regular_file(self):
        copied, skipped = self.copy(["config"])
        self.assertEqual(copied, [])
        self.assertIn("must be files", skipped[0])

    def test_a_copy_that_is_not_gitignored_is_removed(self):
        (self.main / "notes.txt").write_text("private", encoding="utf-8")
        (self.main / "config" / "other.txt").write_text("private", encoding="utf-8")
        (self.wt / ".gitignore").write_text(".env\n", encoding="utf-8")  # config/ and notes.txt are not ignored here
        copied, skipped = self.copy(["notes.txt", "config/other.txt", ".env"])
        self.assertEqual(copied, [".env"])
        self.assertFalse((self.wt / "notes.txt").exists())
        self.assertFalse((self.wt / "config").exists())  # the directory this copy created is gone too
        self.assertEqual(sorted(s.split(":")[0] for s in skipped), ["config/other.txt", "notes.txt"])
        self.assertIn("not gitignored in this worktree: not copied", skipped[0])

    def test_a_worktree_that_is_not_a_repository_copies_nothing(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(copy_main_files(self.main, plain, [".env"], []), [])
        self.assertFalse((plain / ".env").exists())

    def test_a_review_worktree_that_ignores_nothing_keeps_no_copy(self):
        # the shape of `run branch --mr`: a `.env` copied there would be one `git add -A` from a commit
        (self.wt / ".gitignore").write_text("", encoding="utf-8")
        copied, skipped = self.copy([".env"])
        self.assertEqual(copied, [])
        self.assertFalse((self.wt / ".env").exists())

    def test_a_directory_entry_is_a_config_error_not_missing_forever(self):
        ws = {"worktree_root": str(self.root), "repos": [{"alias": "A", "path": str(self.main), "quality": {"a": "b"},
                                                           "env": {"start": "s", "health": "h", "base_url": "b",
                                                                   "files_from_main": ["config", ".env"]}}]}
        errs = [e for e in workspace.check_workspace(ws) if "files_from_main" in e]
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("entries must be files", errs[0])
        msgs = envmod.files_missing(ws, None, "A", root=self.root / "empty")
        self.assertEqual(len(msgs), 2)
        self.assertIn("entries must be files", msgs[0])
        self.assertIn("file .env missing", msgs[1])


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
        ignore_env(self.repo_dirs["React"])
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
        ignore_env(self.repo_dirs["React"])
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
