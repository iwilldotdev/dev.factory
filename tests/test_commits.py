"""The commit-format check of a repo: header pattern over branch_point..HEAD and forbidden trailers."""
import json
import unittest

from tests.helpers import git, lesson
from tests.m2_support import M2Case


class CommitCheckTests(M2Case):
    PATTERN = "^(feat|fix): .+"

    def setUp(self):
        super().setUp()
        self.write("factory.toml", self.TOML.replace('name = "web"\n', f'name = "web"\ncommit_pattern = {json.dumps(self.PATTERN)}\n'
                                                     'commit_forbidden_trailers = ["Co-Authored-By"]\n'))
        self.run_dir = self.prepare()

    def commits(self):
        self.cli("verify", "--json", "--no-retry", "--check", "web:commits")
        rec = self.evidence(self.run_dir, check="web:commits")[-1]
        return rec["result"], rec["reason"]

    @lesson("L-89")
    def test_only_commits_after_the_branch_point_are_checked(self):
        """L-89: the base's own history (here `chore: init`) is never judged against the run's commit pattern."""
        self.commit("a.txt", "1", "feat: add a")
        self.assertEqual(self.commits()[0], "pass")
        self.commit("b.txt", "2", "tweak b")
        result, reason = self.commits()
        self.assertEqual(result, "fail")
        self.assertIn("header 'tweak b' does not match commit_pattern", reason)
        self.assertNotIn("chore: init", reason)

    @lesson("L-90")
    def test_a_merge_that_only_integrates_the_base_skips_the_header_but_never_the_trailer_check(self):
        """L-90: base-integration merges skip the header check; a forbidden trailer still fails, even on such a merge."""
        self.commit("a.txt", "1", "feat: add a")
        git("commit", "-q", "--allow-empty", "-m", "anything the base likes", cwd=self.proj)
        tree = self.tree()
        git("merge", "--no-ff", "-m", "Merge branch main into the feature branch", "main", cwd=tree)
        self.assertEqual(self.commits()[0], "pass")
        git("commit", "-q", "--allow-empty", "-m", "anything else the base likes", cwd=self.proj)
        git("merge", "--no-ff", "-m", "Merge main again\n\nCo-Authored-By: Someone <a@example.com>", "main", cwd=tree)
        result, reason = self.commits()
        self.assertEqual(result, "fail")
        self.assertIn("carries the forbidden trailer Co-Authored-By", reason)
        self.assertNotIn("does not match commit_pattern", reason)

    def test_a_forbidden_trailer_on_an_ordinary_commit_fails(self):
        self.commit("a.txt", "1", "feat: add a\n\nCo-authored-by: Bot <b@example.com>")
        self.assertIn("forbidden trailer", self.commits()[1])

    def test_the_commit_check_joins_the_lock_so_a_pattern_change_is_drift(self):
        self.write("factory.toml", self.TOML.replace('name = "web"\n', 'name = "web"\ncommit_pattern = ".*"\n'))
        self.assertIn("config changed after plan", self.failing("plan"))


if __name__ == "__main__":
    unittest.main()
