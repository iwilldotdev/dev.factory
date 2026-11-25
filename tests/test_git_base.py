"""A run measured against a stale local base: commits merged in from the remote base are not the run's own."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_factory import git


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class RemoteBaseTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        sh(self.dir, "init", "-q", "-b", "pack")
        sh(self.dir, "config", "user.email", "t@t")
        sh(self.dir, "config", "user.name", "t")
        self.commit("[X-1] - base")
        self.bp = sh(self.dir, "rev-parse", "HEAD")
        sh(self.dir, "checkout", "-q", "-b", "feature")
        self.commit("[X-2] - work")
        sh(self.dir, "checkout", "-q", "pack")  # the local pack stays stale; the remote one moves on
        self.commit("upstream fix without the pattern")
        sh(self.dir, "update-ref", "refs/remotes/origin/pack", "HEAD")
        sh(self.dir, "reset", "-q", "--keep", self.bp)
        sh(self.dir, "checkout", "-q", "feature")
        sh(self.dir, "merge", "-q", "--no-ff", "--no-edit", "origin/pack")
        self.repo = {"base": "pack", "remote": "origin"}

    def commit(self, msg):
        self.n = getattr(self, "n", 0) + 1
        name = f"f{self.n}.txt"
        (self.dir / name).write_text(msg, encoding="utf-8")
        sh(self.dir, "add", name)
        sh(self.dir, "commit", "-q", "-m", msg)

    def test_the_remote_base_is_preferred_and_its_commits_are_not_checked(self):
        self.assertEqual(git.base_ref(self.dir, self.repo), "origin/pack")
        problems = git.commit_problems(self.dir, self.bp, git.base_ref(self.dir, self.repo), r"^\[[^\]]+\] - .+", [])
        self.assertEqual(problems, [])
        self.assertTrue(git.commit_problems(self.dir, self.bp, "pack", r"^\[[^\]]+\] - .+", []))  # the stale local base did

    def test_the_baseline_point_moves_to_the_merged_base(self):
        self.assertEqual(git.integration_point(self.dir, self.repo, self.bp), sh(self.dir, "rev-parse", "origin/pack"))

    def test_a_clean_merge_of_the_base_keeps_the_reviewed_change(self):
        merged = sh(self.dir, "rev-parse", "HEAD")
        reviewed = sh(self.dir, "rev-parse", "HEAD^1")
        self.assertTrue(git.clean_merges_only(self.dir, reviewed, merged))
        self.commit("[X-3] - more work")  # a commit of its own is new code to review
        self.assertFalse(git.clean_merges_only(self.dir, reviewed, sh(self.dir, "rev-parse", "HEAD")))

    def test_a_rebase_onto_the_base_keeps_the_reviewed_change_and_a_new_patch_does_not(self):
        reviewed = sh(self.dir, "rev-parse", "HEAD")  # the work plus a merge of origin/pack
        sh(self.dir, "checkout", "-q", "-b", "rebased", "HEAD^1")
        sh(self.dir, "rebase", "-q", "origin/pack")  # same patch on the newer base, no merge commit
        rebased = sh(self.dir, "rev-parse", "HEAD")
        self.assertEqual(sh(self.dir, "rev-list", "--count", "--merges", f"origin/pack..{rebased}"), "0")
        self.assertTrue(git.rebased_only(self.dir, reviewed, rebased, "origin/pack"))
        self.commit("[X-3] - more work")
        self.assertFalse(git.rebased_only(self.dir, reviewed, sh(self.dir, "rev-parse", "HEAD"), "origin/pack"))

    def test_a_fixup_passes_the_header_check_and_autosquash_keeps_the_tree(self):
        sh(self.dir, "checkout", "-q", "-b", "fixups", "HEAD^1")  # the work without the merge
        work = sh(self.dir, "rev-parse", "HEAD")
        (self.dir / "f2.txt").write_text("[X-2] - work, fixed", encoding="utf-8")
        sh(self.dir, "commit", "-q", "-am", "fixup! [X-2] - work")
        before = sh(self.dir, "rev-parse", "HEAD")
        self.assertEqual(git.commit_problems(self.dir, self.bp, "pack", r"^\[[^\]]+\] - .+", []), [])
        self.assertEqual(len(git.fixups(self.dir, "pack")), 1)
        sh(self.dir, "-c", "sequence.editor=:", "rebase", "-q", "-i", "--autosquash", "pack")
        after = sh(self.dir, "rev-parse", "HEAD")
        self.assertEqual(git.fixups(self.dir, "pack"), [])
        self.assertEqual(sh(self.dir, "rev-list", "--count", "pack..HEAD"), "1")
        self.assertTrue(git.same_tree(self.dir, before, after, "pack"))
        self.assertFalse(git.same_tree(self.dir, work, after, "pack"), "the folded fix is a different tree than the unfixed work")


if __name__ == "__main__":
    unittest.main()
