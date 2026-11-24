"""Risk rules: the design note of medium and high risk plans, two reviewers for high risk, and guideline rules cited by the code review."""
import json
import unittest

from tests.helpers import lesson
from tests.m3_support import M3Case

NOTE = ("## Approach\nOne query for the whole list.\n\n## Complexity\nLinear in the number of invoices.\n\n## Reuse\nThe existing repository.\n\n"
        "## Architecture risks\nNone: no new layer.\n")
GUIDELINE = '\n[[repo.check]]\nid = "no-raw-sql"\nkind = "guideline"\ntext = "Queries go through the repository layer"\n'


class DesignNoteTests(M3Case):
    def planned(self, risk: str) -> None:
        plan = self.default_plan()
        plan["risk"] = risk
        self.run_dir = self.prepare(plan=plan, lock=False)

    @lesson("L-176")
    def test_a_medium_or_high_risk_plan_needs_the_design_note_before_it_locks(self):
        """L-176: `gate plan` requires notes/design.md with Approach, Complexity, Reuse and Architecture risks when risk is medium or high."""
        self.planned("high")
        self.assertIn("risk is high: write", self.failing("plan"))
        self.assertIn("## Approach, ## Complexity, ## Reuse, ## Architecture risks", self.failing("plan"))
        self.write("notes/design.md", "## Approach\nx\n\n## Complexity\n\n## Reuse\ny\n", root=self.run_dir)
        text = self.failing("plan")
        self.assertIn("notes/design.md: ## Complexity is missing or empty", text)
        self.assertIn("notes/design.md: ## Architecture risks is missing or empty", text)
        self.write("notes/design.md", NOTE, root=self.run_dir)
        self.ok("gate", "plan")

    def test_a_low_risk_plan_needs_no_note(self):
        self.planned("low")
        self.ok("gate", "plan")


class ReviewerTests(M3Case):
    def high(self) -> None:
        plan = self.default_plan()
        plan["risk"] = "high"
        self.run_dir = self.prepare(plan=plan, lock=False)
        self.write("notes/design.md", NOTE, root=self.run_dir)
        self.ok("gate", "plan")
        self.fix()
        self.ok("verify", "--actor", "builder-1")
        self.ok("gate", "verify")

    def test_high_risk_needs_two_different_reviewers_for_the_two_review_kinds(self):
        self.high()
        self.record("code", "APPROVE", reviewer="reviewer-a")
        self.record("qa", "APPROVE", reviewer="reviewer-a")
        code, reasons = self.gate("review")
        self.assertEqual(code, 1)
        self.assertIn("risk is high: the code and qa reviews must come from two different reviewers", "\n".join(reasons))
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual(n["phase"], "review")
        self.record("qa", "APPROVE", reviewer="reviewer-b")
        self.assertEqual(self.gate("review")[0], 0)

    def test_next_tells_the_harness_to_use_its_strongest_reviewers_on_high_risk(self):
        self.high()
        self.assertIn("risk is high: the code and qa reviews come from two different reviewers", json.loads(self.ok("next", "--json"))["action"])


class GuidelineTests(M3Case):
    def test_the_code_review_cites_every_guideline_rule_and_cannot_approve_one_the_diff_breaks(self):
        self.ready(extra_toml=GUIDELINE)
        self.assertIn("web:no-raw-sql: Queries go through the repository layer", json.loads(self.ok("next", "--json"))["action"])
        self.assertIn("guidelines: web:no-raw-sql has no entry", self.record("code", "APPROVE", code=2))
        bad = [{"id": "web:no-raw-sql", "followed": False, "note": "a raw query in app.py"}]
        self.assertIn("a guideline that the diff does not follow", self.record("code", "APPROVE", code=2, guidelines=bad))
        self.assertIn("is not a guideline of this plan", self.record("code", "APPROVE", code=2, guidelines=[{"id": "web:other", "followed": True}]))
        self.record("code", "APPROVE", guidelines=[{"id": "web:no-raw-sql", "followed": True, "note": "the repository layer is used"}])
        self.assertEqual(self.reviews()[-1]["guidelines"][0]["followed"], True)

    def test_a_guideline_is_not_a_check_and_needs_text(self):
        self.ready(extra_toml=GUIDELINE)
        self.assertEqual(sorted(e["check"] for e in self.evidence(self.run_dir)), ["CHK-1"])
        self.write("factory.toml", self.TOML + '\n[[repo.check]]\nid = "x"\nkind = "guideline"\n')
        self.assertIn("a guideline needs id and text", self.fails("check", code=2))


if __name__ == "__main__":
    unittest.main()
