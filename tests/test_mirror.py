"""Hotfix runs: the mirror worktrees, `verify --mirror`, the per-target ticket titles and the same-diff gate."""
import json
import unittest
from pathlib import Path

from tests.helpers import git, lesson
from tests.m6_support import HotfixCase


class MirrorTests(HotfixCase):
    @lesson("L-93")
    def test_mirror_branches_carry_the_target_and_a_collision_is_refused_before_the_worktree_is_added(self):
        """L-93: the mirror branch is {kind}/{key}-{target}; an existing branch of that name stops `mirror` before `git worktree add`."""
        self.hotfix()
        git("branch", "hotfix/ABC-1-old", cwd=self.proj)
        err = self.fails("mirror", "web", "--to", "old")
        self.assertIn("must not collide", err)
        self.assertEqual([e for e in self.events(self.run_dir) if e["type"] == "mirror"], [])
        git("branch", "-D", "hotfix/ABC-1-old", cwd=self.proj)
        self.ok("mirror", "web", "--to", "old")
        m = self.mirror_event()
        self.assertEqual((m["branch"], m["result"], len(m["commits"])), ("hotfix/ABC-1-old", "applied", 1))
        self.assertEqual(Path(m["path"] + "/app.py").read_text(), "VALUE = 2\n")
        self.assertIn("already mirrored", self.fails("mirror", "web", "--to", "old"))

    def test_a_mirror_needs_the_review_gate_and_a_known_target(self):
        self.hotfix()
        self.assertIn("no mirror target 'nope'", self.fails("mirror", "web", "--to", "nope"))
        self.commit("notes.txt", "later" + chr(10), "chore: later")  # a new commit makes both reviews stale
        self.assertIn("review gate has to pass", self.fails("mirror", "web", "--to", "old"))

    def test_a_cherry_pick_conflict_is_a_tooling_blocker_and_the_mirror_resumes_after_the_fix(self):
        self.hotfix()
        git("checkout", "-q", "rel-old", cwd=self.proj)
        (self.proj / "app.py").write_text("VALUE = 7" + chr(10), newline=chr(10))
        git("add", "app.py", cwd=self.proj)
        git("commit", "-q", "-m", "chore: diverge", cwd=self.proj)
        git("checkout", "-q", "main", cwd=self.proj)
        self.fails("mirror", "web", "--to", "old", code=1)
        m = self.mirror_event()
        self.assertEqual(m["result"], "conflict")
        blocker = [e for e in self.events(self.run_dir) if e["type"] == "blocker"][-1]
        self.assertEqual(blocker["kind"], "tooling")
        self.assertIn("app.py", blocker["reason"])
        self.assertIn("is not finished", self.fails("mirror", "web", "--to", "old"))
        tree = Path(m["path"])
        (tree / "app.py").write_text("VALUE = 2" + chr(10), newline=chr(10))  # the builder resolves it by hand
        git("add", "app.py", cwd=tree)
        git("-c", "core.editor=true", "cherry-pick", "--continue", cwd=tree)
        self.ok("mirror", "web", "--to", "old")
        self.assertEqual(self.mirror_event()["result"], "applied")

    @lesson("L-100")
    def test_each_mirror_change_is_titled_with_its_own_target_ticket_key(self):
        """L-100: the ship plan holds one change per mirror, titled with that target's ticket key, not the first ticket's."""
        self.hotfix(("old", "older"), runtime=True)
        self.ok("mirror", "web", "--to", "old", "--ticket", "ABC-11")
        self.ok("mirror", "web", "--to", "older", "--ticket", "ABC-12")
        self.ok("ship", "plan")
        intents = [i for i in self.sp()["intents"] if i["intent"] == "mirror_change_request"]
        self.assertEqual([(i["target"], i["title"].split("]")[0]) for i in intents], [("old", "[ABC-11"), ("older", "[ABC-12")])
        self.assertTrue(self.sp()["intents"][1]["title"].startswith("[ABC-1]"))
        self.assertEqual([i["target_branch"] for i in intents], ["rel-old", "rel-older"])

    @lesson("L-95")
    def test_gate_ship_needs_an_equal_mirror_diff_or_a_user_decision_naming_the_files(self):
        """L-95: a mirror that moved a test to another file shows as divergence; only a user decision naming the file accepts it."""
        self.hotfix()
        self.ok("mirror", "web", "--to", "old")
        self.ok("verify", "--mirror")
        self.ok("ship", "plan")
        self.assertNotIn("mirror diff differs", self.reasons("ship"))
        tree = Path(self.mirror_event()["path"])
        self.commit("tests/moved_test.py", "assert VALUE == 2\n", "test: moved", tree=tree)
        self.ok("verify", "--mirror")
        self.ok("ship", "plan")
        text = self.reasons("ship")
        self.assertIn("tests/moved_test.py (content)", text)
        self.assertIn("factory mirror diff", text)
        self.ok("decide", "--user-said", "accept the extra tests/moved_test.py on the old branch")
        self.fails("mirror", "diff", "web", "--to", "old", "--decision", "D-1", code=1)
        self.assertNotIn("mirror diff differs", self.reasons("ship"))
        self.commit("tests/more.py", "x = 1\n", "test: more", tree=tree)
        self.ok("verify", "--mirror")
        self.ok("ship", "plan")
        self.assertIn("tests/more.py", self.reasons("ship"))

    def test_the_decision_has_to_name_every_divergent_file(self):
        self.hotfix()
        self.ok("mirror", "web", "--to", "old")
        self.commit("tests/extra.py", "x = 1\n", "test: extra", tree=Path(self.mirror_event()["path"]))
        self.ok("decide", "--user-said", "fine, go ahead")
        self.assertIn("does not name: tests/extra.py", self.fails("mirror", "diff", "web", "--to", "old", "--decision", "D-1"))

    @lesson("L-41", "L-99", "L-98")
    def test_verify_mirror_runs_every_mirror_check_runtime_included_from_the_mirror_tree_per_target(self):
        """L-41, L-99, L-98: `verify --mirror` serves the runtime check from the mirror worktree; a failure on one target does not cancel the
        other target's checks; an env the factory did not start is never stopped."""
        self.hotfix(("old", "older"), runtime=True)
        self.ok("mirror", "web", "--to", "old")
        self.ok("mirror", "web", "--to", "older")
        self.commit("app.py", "VALUE = 3\n", "fix: wrong", tree=Path(self.mirror_event("old")["path"]))
        got, out, err = self.cli("verify", "--mirror", "--json")
        self.assertEqual(got, 1, out + err)
        res = {r["check"]: r["result"] for r in json.loads(out)["results"]}
        self.assertEqual(res, {"CHK-1@old": "fail", "CHK-2@old": "pass", "CHK-1@older": "pass", "CHK-2@older": "pass"})
        served = [e for e in self.evidence(self.run_dir, check="CHK-2") if e.get("target") == "older"][-1]["served"]["web"]["path"]
        self.assertEqual(served, self.mirror_event("older")["path"])
        self.kill_envs()
        foreign = self.foreign_server(self.port)
        self.assertEqual(self.cli("verify", "--mirror", "--fresh")[0], 3)
        self.assertIsNone(foreign.poll())


if __name__ == "__main__":
    unittest.main()
