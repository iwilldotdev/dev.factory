"""Run worktrees, branch points, throwaway worktrees and the commit-format check."""
import json
import os
import subprocess
import unittest
from pathlib import Path

from dev_factory import git as gitmod, project
from dev_factory.run import Run
from tests.helpers import PY, git, lesson
from tests.m2_support import M2Case, cmd


class WorktreeTests(M2Case):
    def branch(self, ticket="ABC-1", **ticket_over):
        run = self.start(ticket)
        self.ingest_ticket(run, id=ticket, **ticket_over)
        self.ok("gate", "intake")
        return run, self.ok("run", "branch")

    def branch_of(self, run: Path) -> str:
        return [e for e in self.events(run) if e["type"] == "worktree"][-1]["branch"]

    def test_run_delete_removes_a_clean_worktree_and_a_branch_with_no_commit_of_its_own(self):
        run, _ = self.branch()
        wt, br = self.tree(), self.branch_of(run)
        out = self.ok("run", "delete", "ABC-1", "--yes")
        self.assertIn(f"deleted branch {br}", out)
        self.assertFalse(wt.exists())
        self.assertEqual(git("branch", "--list", br, cwd=self.proj).strip(), "")

    def test_run_delete_keeps_and_names_a_branch_with_commits_and_a_dirty_worktree(self):
        run, _ = self.branch()
        wt, br = self.tree(), self.branch_of(run)
        self.commit("app.py", "VALUE = 2\n")
        out = self.ok("run", "delete", "ABC-1", "--yes")
        self.assertIn(f"kept branch {br}: 1 commit(s) beyond its base", out)
        self.assertFalse(wt.exists())
        run, _ = self.branch("ABC-2")
        wt = self.tree()
        (wt / "scratch.txt").write_text("x")
        out = self.ok("run", "delete", "ABC-2", "--yes")
        self.assertIn("kept worktree", out)
        self.assertIn("1 uncommitted path(s)", out)
        self.assertTrue((wt / "scratch.txt").is_file())

    def test_the_default_location_is_the_user_cache_outside_the_repo(self):
        """Decision 7: worktrees live under the user cache dir, so the main checkout's tools never scan them."""
        run, out = self.branch()
        wt = self.tree()
        self.assertTrue(wt.as_posix().startswith((self.base / "cache" / "dev.factory" / "worktrees" / "demo" / "ABC-1").as_posix()), wt)
        self.assertNotIn(self.proj, wt.parents)
        self.assertTrue((wt / "app.py").is_file())
        self.assertIn(wt.as_posix(), out)

    def test_local_toml_overrides_the_worktree_root(self):
        root = self.base / "my worktrees"
        self.write(".factory/local.toml", f"worktree_root = {json.dumps(str(root))}\n")
        self.branch()
        self.assertEqual(self.tree(), root / "ABC-1" / "web")

    def test_branch_names_come_from_the_kind_the_key_and_the_ticket_title(self):
        self.branch(title="Show overdue invoices!  (v2)")
        self.assertEqual(git("branch", "--show-current", cwd=self.tree()), "feature/ABC-1-show-overdue-invoices-v2")
        self.assertEqual(gitmod.branch_name("{kind}/{key}-{slug}", "bug", "AB 1", "x"), "bug/AB-1-x")
        self.assertEqual(gitmod.branch_name("{key}..{slug}.lock", "feature", "K-1", ""), "K-1-change")

    def test_a_bug_ticket_uses_the_bug_kind_in_the_branch(self):
        self.branch(type="bug")
        self.assertTrue(git("branch", "--show-current", cwd=self.tree()).startswith("bug/ABC-1-"))

    @lesson("L-89")
    def test_the_branch_point_is_recorded_at_creation_and_never_moves(self):
        """L-89: commit checks span branch_point..HEAD, so the branch point is recorded when the worktree is created."""
        run, _ = self.branch()
        base_tip = git("rev-parse", "main", cwd=self.proj)
        ev = [e for e in self.events(run) if e["type"] == "worktree"][0]
        self.assertEqual((ev["branch_point"], ev["throwaway"], ev["setup"]), (base_tip, False, "none"))
        git("commit", "-q", "--allow-empty", "-m", "chore: base moved on", cwd=self.proj)
        self.assertEqual(self.ok("run", "branch").strip(), "every repo already has its worktree")
        self.assertEqual(len([e for e in self.events(run) if e["type"] == "worktree"]), 1)
        self.assertEqual(ev["branch_point"], base_tip)

    def test_each_repo_has_its_own_base_branch(self):
        git("branch", "develop", cwd=self.proj)
        self.write("factory.toml", self.TOML.replace('name = "web"\n', 'name = "web"\nbase = "develop"\n'))
        run, _ = self.branch()
        self.assertEqual([e["branch_point"] for e in self.events(run) if e["type"] == "worktree"], [git("rev-parse", "develop", cwd=self.proj)])

    def test_a_missing_base_branch_is_named(self):
        self.write("factory.toml", self.TOML.replace('name = "web"\n', 'name = "web"\nbase = "nope"\n'))
        run = self.start("ABC-1")
        self.ingest_ticket(run)
        self.ok("gate", "intake")
        self.assertIn("base branch 'nope' of repo web does not exist", self.fails("run", "branch"))

    def test_branching_needs_a_passed_intake_and_known_repos(self):
        self.start("ABC-1")
        self.assertIn("gate intake has not passed", self.fails("run", "branch"))

    @lesson("L-72")
    def test_setup_runs_inside_the_new_worktree_and_the_worktree_is_a_real_directory(self):
        """L-72: a worktree gets its own dependencies from the declared setup command, run in the worktree, never a link."""
        probe = f'"{PY}" -c "import os, sys; print(os.getcwd()); sys.exit(3)"'
        self.write("factory.toml", self.TOML.replace('name = "web"\n', f'name = "web"\nsetup = {json.dumps(probe)}\n'))
        run = self.start("ABC-1")
        self.ingest_ticket(run)
        self.ok("gate", "intake")
        code, _, err = self.cli("run", "branch")
        self.assertEqual(code, 1)
        wt = Path([e for e in self.events(run) if e["type"] == "worktree"][0]["path"])
        self.assertIn(wt.name, err)
        self.assertFalse(wt.is_symlink())
        self.assertNotEqual(wt.resolve(), self.proj.resolve())
        self.write("factory.toml", self.TOML)
        self.assertEqual(self.cli("run", "branch")[0], 0, "a second run branch re-runs the setup of a failed worktree")
        self.assertEqual([e["setup"] for e in self.events(run) if e["type"] == "worktree"], ["fail", "none"])

    @lesson("L-72")
    def test_cleaning_a_throwaway_worktree_never_follows_a_link_out_of_it(self):
        """L-72: cleanup removes links, not what they point at."""
        run = self.start("ABC-1")
        self.ingest_ticket(run)
        proj = project.discover(str(self.proj))
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("precious")
        sha = git("rev-parse", "HEAD", cwd=self.proj)
        with gitmod.throwaway(proj, Run(proj.runs_dir, "ABC-1"), proj.cfg["repo"][0], sha, "t") as (path, setup):
            self.assertEqual(setup["setup"], "none")
            self.assertTrue((path / "app.py").is_file())
            try:
                os.symlink(outside, path / "link", target_is_directory=True)
            except (OSError, NotImplementedError):  # no symlink right on Windows: a junction is the same hazard
                subprocess.run(["cmd", "/c", "mklink", "/J", str(path / "link"), str(outside)], check=True, capture_output=True)
        self.assertFalse(path.exists())
        self.assertEqual((outside / "keep.txt").read_text(), "precious")
        wt = [e for e in self.events(run) if e["type"] == "worktree"]
        self.assertEqual([e["throwaway"] for e in wt], [True])
        self.assertNotIn(str(path), git("worktree", "list", cwd=self.proj))



class SlugTests(unittest.TestCase):
    def test_the_branch_slug_folds_accents_and_cuts_at_a_word_boundary(self):
        title = "Subtarefas no Gantt: subtarefas na árvore filtrada e ordenação"
        self.assertEqual(gitmod.slug(title), "subtarefas-no-gantt-subtarefas-na-arvore")
        self.assertEqual(gitmod.branch_name("{kind}/{key}-{slug}", "feature", "ABC-1", "Ordenação por data"), "feature/ABC-1-ordenacao-por-data")
        self.assertEqual(gitmod.slug("x" * 50), "x" * 40)
        self.assertEqual(gitmod.slug("☃ ☃"), "change")

if __name__ == "__main__":
    unittest.main()
