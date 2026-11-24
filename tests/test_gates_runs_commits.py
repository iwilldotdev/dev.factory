"""gate build commit checks: review-fix origin head, restricted inherited-commit skip, per-target own branches."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory.commands import gate  # noqa: E402

WS = {"commit": {"header_regex": r"^\[[^\]]+\] - .+", "forbidden_trailers": []}}


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class CommitsCase(unittest.TestCase):
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

    def check(self, branch="feature/own", **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            errors = gate.commits_errors(self.repo, "main", WS, "R", branch, **kw)
        return errors, out.getvalue()


class OriginHead(CommitsCase):
    def test_mr_commits_before_the_origin_head_are_not_checked(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("bad pre-existing change commit")
        run(self.repo, "push", "origin", "feature/own")  # the run branch IS the change source branch
        origin = run(self.repo, "rev-parse", "HEAD")
        self.commit("[X-2] - review fix")
        errors, _ = self.check()
        self.assertEqual(len(errors), 1, "without the origin head the change's own bad commit is checked")
        errors, _ = self.check(origin_head=origin)
        self.assertEqual(errors, [])

    def test_new_bad_commit_after_origin_head_is_checked_and_merges_skipped(self):
        run(self.repo, "checkout", "-b", "feature/own")
        origin = run(self.repo, "rev-parse", "HEAD")
        self.commit("bad new commit")
        run(self.repo, "checkout", "-b", "side", "main")
        self.commit("[X-3] - side")
        run(self.repo, "checkout", "feature/own")
        run(self.repo, "merge", "--no-ff", "side", "-m", "Merge branch side")
        errors, _ = self.check(origin_head=origin)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad new commit", errors[0])

    def test_rewritten_history_falls_back_to_base_span(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("[X-2] - one")
        old = run(self.repo, "rev-parse", "HEAD")
        run(self.repo, "reset", "--hard", "main")
        self.commit("bad rewritten")
        errors, out = self.check(origin_head=old)
        self.assertEqual(len(errors), 1)
        self.assertIn("not an ancestor", out)


class BranchPoint(CommitsCase):
    """Only the commits the run created are checked: what the branch already had at `run branch` time is inherited."""

    def stacked_branch(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("chore(deps): old non-conforming commit")
        self.commit('Revert "something old"')
        return run(self.repo, "rev-parse", "HEAD")

    def test_commits_before_the_branch_point_are_not_checked(self):
        point = self.stacked_branch()
        self.commit("[X-2] - mine")
        errors, out = self.check()
        self.assertEqual(len(errors), 2, "without a recorded branch point the old behaviour stands")
        errors, out = self.check(branch_point=point)
        self.assertEqual(errors, [])
        self.assertIn("2 commits already on the branch when the run started are not checked", out)

    def test_run_commits_after_the_branch_point_are_still_checked(self):
        point = self.stacked_branch()
        self.commit("[X-2] - mine")
        self.commit("bad run commit")
        errors, _ = self.check(branch_point=point)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad run commit", errors[0])

    def test_forbidden_trailer_after_the_branch_point_is_still_caught(self):
        point = self.stacked_branch()
        self.commit("[X-2] - mine\n\nCo-Authored-By: Bot")
        ws = {"commit": {"header_regex": WS["commit"]["header_regex"], "forbidden_trailers": ["Co-Authored-By"]}}
        with contextlib.redirect_stdout(io.StringIO()):
            errors = gate.commits_errors(self.repo, "main", ws, "R", "feature/own", branch_point=point)
        self.assertEqual(len(errors), 1)
        self.assertIn("forbidden trailer", errors[0])

    def test_a_branch_point_that_is_no_longer_an_ancestor_falls_back(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("[X-2] - one")
        gone = run(self.repo, "rev-parse", "HEAD")
        run(self.repo, "reset", "--hard", "main")
        self.commit("bad rewritten")
        errors, _ = self.check(branch_point=gone)
        self.assertEqual(len(errors), 1)
        errors, _ = self.check(branch_point="0" * 40)
        self.assertEqual(len(errors), 1)

    def test_merge_commits_that_integrate_the_base_are_not_checked(self):
        run(self.repo, "checkout", "-b", "feature/own")
        point = run(self.repo, "rev-parse", "HEAD")
        self.commit("[X-2] - mine")
        run(self.repo, "checkout", "main")
        self.commit("[X-3] - main moved")
        run(self.repo, "checkout", "feature/own")
        run(self.repo, "merge", "--no-ff", "main", "-m", "Merge branch main into feature/own")
        for kw in ({}, {"branch_point": point}):
            errors, _ = self.check(**kw)
            self.assertEqual(errors, [], kw)

    def test_a_merge_that_integrates_the_base_still_gets_the_forbidden_trailer_check(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("[X-2] - mine")
        run(self.repo, "checkout", "main")
        self.commit("[X-3] - main moved")
        run(self.repo, "checkout", "feature/own")
        run(self.repo, "merge", "--no-ff", "main", "-m", "Merge branch main into feature/own\n\nCo-Authored-By: Some Bot <bot@x.com>")
        ws = {"commit": {"header_regex": WS["commit"]["header_regex"], "forbidden_trailers": ["Co-Authored-By"]}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            errors = gate.commits_errors(self.repo, "main", ws, "R", "feature/own")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("forbidden trailer", errors[0])
        self.assertNotIn("header does not match", errors[0], "the header of a merge that brings the base in is still not checked")
        clean = {"commit": {"header_regex": WS["commit"]["header_regex"], "forbidden_trailers": ["Signed-off-by"]}}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gate.commits_errors(self.repo, "main", clean, "R", "feature/own"), [])


class InheritedScope(CommitsCase):
    def stale_bad_commit_on_own_run(self):
        run(self.repo, "checkout", "-b", "feature/stale")
        self.commit("bad on a stale branch")
        run(self.repo, "push", "origin", "feature/stale")
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("[X-2] - mine")

    def test_unrelated_stale_remote_branch_does_not_excuse_a_commit(self):
        self.stale_bad_commit_on_own_run()
        self.assertEqual(self.check()[0], [], "legacy: any other remote branch excuses")
        errors, out = self.check(allow={"main"})
        self.assertEqual(len(errors), 1)
        self.assertNotIn("skipped", out)

    def test_allowed_dependency_branch_still_excuses(self):
        self.stale_bad_commit_on_own_run()
        errors, out = self.check(allow={"main", "feature/stale"})
        self.assertEqual(errors, [])
        self.assertIn("1 inherited commits", out)

    def test_mirror_target_branch_is_own_not_inherited(self):
        run(self.repo, "checkout", "-b", "feature/own")
        self.commit("[X-2] - base fix")
        run(self.repo, "checkout", "-b", "feature/own-pack")
        self.commit("bad on mirror")
        run(self.repo, "push", "origin", "feature/own-pack")
        run(self.repo, "checkout", "feature/own-pack")
        self.assertEqual(len(self.check(allow=None)[0]), 0, "mirror branch excuses the base check by default")
        errors, _ = self.check(own=("feature/own-pack",))
        self.assertEqual(len(errors), 1)

    def test_inherited_allow_lists_targets_stack_and_open_change_branches(self):
        from factory import core
        import os
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        old = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = str(home)
        self.addCleanup(lambda: os.environ.__setitem__("FACTORY_HOME", old) if old else os.environ.pop("FACTORY_HOME", None))
        ws = {"name": "ws", "repos": [{"alias": "R", "target_branches": {"homolog": "release/h"}}]}
        runrec = {"id": "ws-a-1", "workspace": "ws", "targets": ["homolog", "pack"], "tickets": [],
                  "hotfix": {"base": "homolog", "mirror": ["pack"]},
                  "repos": {"R": {"base": "homolog", "base_branch": "feature/dep"}}}
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from change_support import put_change
        put_change("ws-a-1", "R", "open", number=5, source_branch="feature/open", state="open")
        put_change("ws-a-1", "R", "merged", number=6, source_branch="feature/merged", state="merged", head_sha="c" * 40)
        put_change("ws-a-1", "Other", "open", number=7, source_branch="feature/other-repo", state="open")
        (core.run_dir("ws-a-1") / "raw" / "change" / "R-open.json").write_text("{}", encoding="utf-8")  # stale: ignored
        put_change("ws-a-1", "R", "late", number=8, source_branch="feature/late", state="open")
        self.assertEqual(gate.inherited_allow(runrec, ws, "R"),
                         {"release/h", "pack", "feature/dep", "feature/late"})
        self.assertEqual(gate.merged_change_heads(runrec), ["c" * 40])


if __name__ == "__main__":
    unittest.main()
