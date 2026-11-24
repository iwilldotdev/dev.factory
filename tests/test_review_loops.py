"""Review loops (one budget with amends) and follow-ups across rounds."""
import unittest

from dev_factory import review
from dev_factory.run import Run
from tests.helpers import lesson
from tests.m3_support import M3Case

LOOPS = "\n[autonomy]\nmax_loops = 2\n"


class LoopTests(M3Case):
    def setUp(self):
        super().setUp()
        self.ready(extra_toml=LOOPS)
        self.major = [{"id": "F1", "severity": "major", "text": "the toast never closes"}]

    @lesson("L-138")
    def test_only_blocker_or_major_rounds_count_and_minor_only_changes_never_do(self):
        """L-138: review rounds count only blocker/major findings."""
        for i in range(3):
            self.record("code", "CHANGES", findings=[{"id": f"F{i}", "severity": "minor", "text": f"naming {i}"}])
        self.assertEqual(self.loops(), [])
        self.record("code", "CHANGES", findings=self.major)
        self.assertEqual([(e["kind"], e["counts"]) for e in self.loops()], [("review", True)])

    @lesson("L-135", "L-136")
    def test_one_budget_covers_amends_and_review_rounds_and_a_decision_exempts_one_round_once(self):
        """L-135, L-136: past the cap a CHANGES round refuses and blocks once; a recorded decision is consumed once and exempts one loop."""
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["expect_pattern"] = "checks passed: VALUE=2$"
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "sharper pattern")
        self.ok("verify")
        self.record("code", "CHANGES", findings=self.major)
        self.assertEqual([e["kind"] for e in self.loops()], ["amend", "review"])
        self.commit("extra.txt", "x\n")
        self.ok("verify")
        for _ in range(2):
            self.assertIn("loop budget exhausted: 2 of 2", self.record("code", "CHANGES", code=1, findings=self.major))
        self.assertEqual(len([e for e in self.events(self.run_dir) if e["type"] == "blocker"]), 1)
        self.assertEqual(len(self.reviews()), 1)
        self.ok("ask", "One more review round?")
        self.ok("answer", "Q-1", "--user-said", "yes, one more")
        self.record("code", "CHANGES", "--decision", "Q-1", findings=self.major)
        self.assertEqual([(e["kind"], e["counts"]) for e in self.loops()], [("amend", True), ("review", True), ("review", False)])
        self.assertEqual(len(self.reviews()), 2)
        self.commit("extra2.txt", "x\n")
        self.ok("verify")
        self.assertIn("already consumed", self.record("code", "CHANGES", "--decision", "Q-1", code=2, findings=self.major))

    def test_two_reviewers_finding_the_same_problem_at_one_commit_spend_one_loop(self):
        self.record("code", "CHANGES", findings=self.major)
        self.record("qa", "CHANGES", findings=self.major, exercises=[{"ac": "AC-1", "exercises": True}])
        self.assertEqual(len(self.loops()), 1)
        self.assertEqual(len(self.reviews()), 2)


class FollowUpTests(M3Case):
    def setUp(self):
        super().setUp()
        self.ready()

    def minors(self):
        return [(f["round"], f["id"]) for f in review.open_minors(Run(self.proj / ".factory" / "runs", "ABC-1").state())]

    @lesson("L-147")
    def test_resolved_entries_are_round_id_and_clear_the_same_id_and_text_in_earlier_rounds_only(self):
        """L-147: a bare id means the previous round; resolving clears the same id and text in earlier rounds, never a different finding."""
        m = {"id": "F1", "severity": "minor", "text": "rename the constant"}
        other = {"id": "F2", "severity": "minor", "text": "fix the typo"}
        self.record("code", "CHANGES", findings=[m, other])
        self.record("code", "CHANGES", findings=[m, {"id": "F1x", "severity": "minor", "text": "a new one"}])
        self.assertEqual(self.minors(), [(1, "F1"), (1, "F2"), (2, "F1x")])
        self.assertIn("names no finding", self.record("code", "CHANGES", code=2, findings=[m], resolved=["F9"]))
        self.assertIn("names no finding", self.record("code", "CHANGES", code=2, findings=[m], resolved=["3:F1"]))
        self.record("code", "CHANGES", findings=[{"id": "F7", "severity": "minor", "text": "later"}], resolved=["2:F1"])
        self.assertEqual(self.minors(), [(1, "F2"), (2, "F1x"), (3, "F7")])

    def test_a_bare_id_means_the_previous_round_and_other_findings_stay_open(self):
        self.record("code", "CHANGES", findings=[{"id": "F1", "severity": "minor", "text": "one"}, {"id": "F2", "severity": "minor", "text": "two"}])
        self.record("code", "APPROVE", resolved=["F1"])
        self.assertEqual(self.reviews()[-1]["resolved"], ["1:F1"])
        self.assertEqual(self.minors(), [(1, "F2")])

    def test_a_reused_id_with_different_text_is_a_different_finding(self):
        self.record("code", "CHANGES", findings=[{"id": "F1", "severity": "minor", "text": "one"}])
        self.record("code", "CHANGES", findings=[{"id": "F1", "severity": "minor", "text": "something else"}], resolved=["1:F1"])
        self.assertEqual(self.minors(), [(2, "F1")])


if __name__ == "__main__":
    unittest.main()
