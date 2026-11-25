"""review record and gate review: kinds, bindings, refusals, the loop budget, follow-ups and the reviewer identity."""
import json
import unittest

from tests.helpers import lesson
from tests.m3_support import M3Case


LOOPS = "\n[autonomy]\nmax_loops = 2\n"


class RecordTests(M3Case):
    def setUp(self):
        super().setUp()
        self.ready()

    def test_a_valid_review_is_stored_bound_to_the_full_sha_and_the_right_hash(self):
        self.record("code")
        self.record("qa")
        code, qa = self.reviews()
        lk = [e for e in self.events(self.run_dir) if e["type"] == "plan_locked"][-1]
        self.assertEqual((code["kind"], code["round"], code["verdict"], code["repos"], code["bound"]),
                         ("code", 1, "APPROVE", {"web": self.head()}, {"core_hash": lk["core_hash"]}))
        self.assertEqual((qa["kind"], qa["bound"]), ("qa", {"plan_hash": lk["plan_hash"]}))
        self.assertEqual(len(code["hash"]), 64)
        self.assertEqual(self.gate("review")[0], 0)
        self.assertEqual(self.record("code", "CHANGES", findings=[{"id": "F1", "severity": "minor", "text": "name it"}]).count("round 2"), 1)

    @lesson("L-107")
    def test_a_sha_prefix_of_seven_is_stored_full_and_a_bad_repos_shape_is_refused_with_the_shape(self):
        """L-107: `repos` takes `{repo: sha}` with a prefix of 7 or more hex characters; any other shape is refused with an example."""
        self.record("code", repos={"web": self.head()[:7].upper()})
        self.assertEqual(self.reviews()[0]["repos"], {"web": self.head()})
        for bad in (["web"], {"web": self.head()[:6]}, {"web": 1234567}, {"web": "zzzzzzzz"}, {}):
            msg = self.record("code", code=2, repos=bad)
            self.assertIn('e.g. {"web": "3f9c2ab"}', msg)
        self.assertEqual(len(self.reviews()), 1)

    @lesson("L-23")
    def test_approve_with_gaps_not_checked_or_a_blocker_or_major_finding_is_refused(self):
        """L-23: `review record` refuses APPROVE with gap > 0, not_checked > 0, or any blocker or major finding."""
        for comp in ({"done": 2, "gap": 1, "not_checked": 0}, {"done": 2, "gap": 0, "not_checked": 1}):
            self.assertIn("gap or not_checked", self.record("code", code=2, completeness=comp))
        for sev in ("blocker", "major"):
            self.assertIn("blocker or major finding", self.record("code", code=2, findings=[{"id": "F1", "severity": sev, "text": "wrong"}]))
        self.record("code", findings=[{"id": "F1", "severity": "minor", "text": "naming"}])
        self.assertEqual(len(self.reviews()), 1)

    @lesson("L-22")
    def test_a_qa_review_needs_an_exercises_entry_per_ac_and_cannot_approve_a_check_that_cannot_fail(self):
        """L-22: `qa` is refused without `exercises` for every AC, and `exercises: false` cannot be approved."""
        self.assertIn("exercises", self.record("qa", code=2, exercises=None))
        self.assertIn("AC-1 has no entry", self.record("qa", code=2, exercises=[]))
        self.assertIn("exercises: false", self.record("qa", code=2, exercises=[{"ac": "AC-1", "exercises": False, "note": "asserts nothing"}]))
        self.assertIn("not an AC", self.record("qa", code=2, exercises=[{"ac": "AC-9", "exercises": True}]))
        self.record("qa", "CHANGES", exercises=[{"ac": "AC-1", "exercises": False, "note": "asserts nothing"}],
                    findings=[{"id": "F1", "severity": "major", "text": "CHK-1 cannot fail", "ac": "AC-1"}])
        self.assertEqual([r["verdict"] for r in self.reviews()], ["CHANGES"])

    @lesson("L-39")
    def test_a_code_review_needs_a_changed_values_list_and_an_explicit_empty_one_is_accepted(self):
        """L-39: a code review is refused without `changed_values`; `[]` says none and is accepted."""
        self.assertIn("changed_values", self.record("code", code=2, changed_values=None))
        self.assertIn("changed_values", self.record("code", code=2, changed_values=[{"file": "a.ts:1", "what": "5s -> 20s"}]))
        self.record("code", changed_values=[{"file": "a.ts:1", "what": "duration 5s -> 20s", "why": "the ticket asks for 20s"}])
        self.record("code", changed_values=[])
        self.assertEqual([r["changed_values"] for r in self.reviews()][1], [])

    def test_shape_problems_are_listed_together_and_nothing_is_written(self):
        msg = self.record("code", code=2, verdict="MAYBE", reviewer="", findings=[{"id": "F1", "severity": "huge", "text": ""}], bogus=1)
        for part in ("unknown key 'bogus'", "verdict: must be APPROVE or CHANGES", "reviewer", "severity", "findings[0].text"):
            self.assertIn(part, msg)
        self.assertEqual(self.reviews(), [])
        self.assertIn("at least one finding", self.record("code", "CHANGES", code=2))

    def test_a_review_needs_a_passing_verify_and_a_valid_kind(self):
        self.commit("extra.txt", "x\n")
        self.assertIn("passing verify first", self.record("code", code=2))
        self.ok("verify")
        self.assertIn("invalid review kind", self.fails("review", "record", "--kind", "ux", "--file", str(self.write_review(self.review_input()))))
        self.assertIn("review example", self.fails("review", "record", "--kind", "code", "--file", str(self.write_review([1]))))
        self.assertIn("is not valid JSON", self.fails("review", "record", "--kind", "code", "--file", str(self.write("bad.json", "{", root=self.base))))
        self.assertEqual(json.loads(self.ok("review", "example"))["verdict"], "APPROVE")

    def test_the_reviewer_must_differ_from_every_actor_that_built_or_verified(self):
        """The reviewer name and the --actor hint are compared with the actors on evidence and worktree events."""
        self.assertIn("also built or verified", self.record("code", code=2, reviewer="Builder-1"))
        self.assertIn("also built or verified", self.record("code", "APPROVE", "--actor", "builder-1", code=2))
        self.record("code", "APPROVE", "--actor", "reviewer-9")
        self.assertEqual(self.reviews()[0]["independent_of"], ["builder-1"])


class GateTests(M3Case):
    def setUp(self):
        super().setUp()
        self.ready()

    @lesson("L-18")
    def test_a_review_stays_bound_to_the_sha_and_the_plan_it_saw(self):
        """L-18: `gate review` re-checks both: a later commit or a changed plan makes the review stale."""
        self.assertIn("no code review", self.failing("review"))
        self.approve_both()
        self.assertEqual(self.gate("review")[0], 0)
        self.commit("extra.txt", "x\n")
        self.ok("verify")
        text = self.failing("review")
        self.assertIn("the code review (round 1) is stale, HEAD moved", text)
        self.assertIn("the qa review (round 1) is stale, HEAD moved", text)
        self.approve_both()
        self.assertEqual(self.gate("review")[0], 0)
        plan = self.read_plan(self.run_dir)
        plan["acs"][0]["text"] = "VALUE is exactly 2"
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "sharper AC")
        self.ok("verify")
        text = self.failing("review")
        self.assertIn("the code review (round 2) is stale, the plan changed (core_hash)", text)
        self.assertIn("the qa review (round 2) is stale, the plan changed (plan_hash)", text)

    @lesson("L-137")
    def test_a_checks_only_amend_keeps_the_code_review_and_invalidates_the_qa_review(self):
        """L-137: the code review binds to the plan without checks; the qa review binds to the full plan."""
        self.approve_both()
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["expect_pattern"] = "checks passed: VALUE=2$"
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "sharper pattern")
        self.ok("verify")
        text = self.failing("review")
        self.assertNotIn("the code review", text)
        self.assertIn("the qa review (round 1) is stale, the plan changed (plan_hash)", text)
        self.record("qa")
        self.assertEqual(self.gate("review")[0], 0)

    def test_a_changes_verdict_fails_the_gate_and_lists_the_findings(self):
        self.record("code")
        self.record("qa", "CHANGES", findings=[{"id": "F1", "severity": "major", "text": "the check proves nothing"}])
        text = self.failing("review")
        self.assertIn("the qa review (round 1) asked for changes: F1 (major) the check proves nothing", text)
        self.assertNotIn("the code review", text)

    def test_a_later_verify_failure_fails_the_review_gate_too(self):
        self.approve_both()
        (self.tree() / "app.py").write_text("VALUE = 1\n")
        self.assertIn("uncommitted", self.failing("review"))


class NextTests(M3Case):
    def setUp(self):
        super().setUp()
        self.ready()

    def nxt(self):
        return json.loads(self.ok("next", "--json"))

    def test_next_walks_the_review_phase_in_order(self):
        n = self.nxt()
        self.assertEqual((n["phase"], n["role"]), ("review", "reviewer"))
        self.assertIn("factory review record --kind code --file", n["command"])
        self.assertTrue(n["files"][0].endswith("/notes/review-code.json"), "a path the hook lets the reviewer write (M3)")
        from dev_factory import hook
        self.assertIsNone(hook.reason({"tool_input": {"file_path": n["files"][0]}}))
        self.record("code")
        self.assertIn("--kind qa", self.nxt()["command"])
        self.record("qa", "CHANGES", findings=[{"id": "F1", "severity": "minor", "text": "rename it"}])
        n = self.nxt()
        self.assertEqual((n["role"], n["command"]), ("builder", "factory verify"))
        self.assertIn("qa review asked for changes", n["action"])
        self.record("qa")
        self.assertEqual(self.nxt()["command"], "factory gate review")
        self.ok("gate", "review")
        self.assertEqual(self.nxt()["phase"], "ship")

    def test_a_new_commit_returns_the_run_to_build_and_a_stale_review_to_review(self):
        self.approve_both()
        self.ok("gate", "review")
        self.commit("extra.txt", "x\n")
        self.assertEqual(self.nxt()["phase"], "build")
        self.ok("verify")
        self.ok("gate", "verify")
        self.assertEqual(self.nxt()["phase"], "review")


if __name__ == "__main__":
    unittest.main()
