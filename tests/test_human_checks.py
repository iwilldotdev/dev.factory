"""Human checks: satisfied only by `factory confirm`, bound to the current commits, never alone proving a bug AC, listed in the ship body."""
import json
import unittest

from tests.helpers import lesson
from tests.m3_support import ShipCase

TEXT = "The toast says Saved in the user's language"
BOTH = [{"ac": "AC-1", "exercises": True, "note": "CHK-1 asserts the value"}, {"ac": "AC-2", "exercises": True, "note": "the user confirmed the copy"}]


class HumanCase(ShipCase):
    def plan(self, kind: str = "feature") -> dict:
        p = self.default_plan(kind)
        p["acs"].append({"id": "AC-2", "text": "The toast copy is right", "source": "ticket:description", **({"reproduce": False, "reason": "copy"} if kind == "bug" else {})})
        p["checks"].append({"id": "CHK-2", "type": "human", "repo": "web", "ac": ["AC-2"], "text": TEXT, "reason": "copy depends on the locale"})
        return p

    def pending(self) -> None:
        """A locked plan with one human check, a fix commit and a verify that passed everything a command can check."""
        self.run_dir = self.proj / ".factory" / "runs" / "ABC-1"
        self.cached("pending", self._pending)

    def _pending(self) -> None:
        self.prepare(plan=self.plan())
        self.fix()
        self.ok("verify", "--actor", "builder-1")

    def gate_verify(self) -> tuple[int, str]:
        code, reasons = self.gate("verify")
        return code, "\n".join(reasons)

    def confirm(self, said: str = "yes, it reads Saved in Portuguese") -> str:
        return self.ok("confirm", "CHK-2", "--user-said", said)


class ConfirmTests(HumanCase):
    @lesson("L-24")
    def test_a_human_check_waits_for_the_user_and_a_confirmation_satisfies_the_verify_gate(self):
        """L-24: verify never runs a human check; it is pending until `factory confirm --user-said` records the user's words at the current commits."""
        self.pending()
        got, out, _ = self.cli("verify", "--json")
        data = json.loads(out)
        self.assertEqual(got, 0, "a pending human check does not fail verify")
        self.assertEqual({r["check"]: r["result"] for r in data["results"]}, {"CHK-1": "pass", "CHK-2": "pending"})
        code, text = self.gate_verify()
        self.assertEqual(code, 1)
        self.assertIn("CHK-2: waits for the user's confirmation: factory confirm CHK-2 --user-said", text)
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual((n["phase"], n["kind_of_action"], n["role"]), ("build", "user", "orchestrator"))
        self.assertTrue(n["command"].startswith("factory confirm CHK-2 --user-said"), n["command"])
        self.assertIn(TEXT, n["action"])
        self.assertIn("CHK-2", self.confirm())
        rec = self.evidence(self.run_dir, check="CHK-2")[-1]
        self.assertEqual((rec["result"], rec["human_heads"], rec["user_said"]), ("pass", {"web": self.head()}, "yes, it reads Saved in Portuguese"))
        self.assertEqual(self.gate_verify()[0], 0)
        self.assertEqual([e["check"] for e in self.events(self.run_dir) if e["type"] == "confirm"], ["CHK-2"])
        self.assertEqual(json.loads(self.ok("metrics", "--json"))["human_events"], {"confirm": 1})

    def test_the_same_words_at_a_new_commit_are_not_a_new_confirmation(self):
        """Review m5: a confirmation reused verbatim at another commit is refused until the user says something now."""
        self.pending()
        self.confirm()
        self.commit("extra.txt", "x\n")
        self.ok("verify")
        self.assertIn("earlier commit", self.fails("confirm", "CHK-2", "--user-said", "yes, it reads Saved in Portuguese"))
        self.confirm("yes, still reads Saved in Portuguese")

    @lesson("L-24")
    def test_a_new_commit_asks_the_user_again(self):
        """L-24: the confirmation is bound to the commits the user saw; a new commit makes the check pending again."""
        self.pending()
        self.confirm()
        self.assertEqual(self.gate_verify()[0], 0)
        self.commit("extra.txt", "x\n")
        self.ok("verify")
        code, text = self.gate_verify()
        self.assertEqual(code, 1)
        self.assertIn("CHK-2: waits for the user's confirmation", text)
        self.assertIn("pending", self.ok("verify"))

    def test_a_confirmation_needs_the_users_words_a_human_check_and_a_clean_worktree(self):
        self.pending()
        self.assertIn("user's own words", self.fails("confirm", "CHK-2", "--user-said", "  "))
        self.assertIn("is not a human check", self.fails("confirm", "CHK-1", "--user-said", "ok"))
        (self.tree() / "dirty.txt").write_text("x\n")
        self.assertIn("commit the worktree first", self.fails("confirm", "CHK-2", "--user-said", "ok"))
        self.assertEqual([e for e in self.events(self.run_dir) if e["type"] == "confirm"], [])


class RuleTests(HumanCase):
    @lesson("L-24")
    def test_a_human_check_never_alone_proves_a_bug_ac(self):
        """L-24: in a bug plan an AC whose only checks are human is refused at the plan gate."""
        self.prepare("bug", plan=self.plan("bug"), lock=False)
        self.assertIn("a human check never alone proves a bug AC", self.failing("plan"))

    def test_a_human_check_cannot_reproduce_and_needs_its_text(self):
        plan = self.plan()
        plan["checks"][1].update(text="", reproduce={"expect_fail_pattern": "AssertionError: VALUE=1"})
        self.prepare(plan=plan, lock=False)
        text = self.failing("plan")
        self.assertIn(".text: required", text)
        self.assertIn("a human check cannot reproduce a bug", text)

    @lesson("L-138", "L-24")
    def test_a_pending_human_check_never_spends_a_review_loop(self):
        """L-138, L-24: while the user has not confirmed, a review is refused before it can be recorded, so no loop is spent and nothing blocks."""
        self.pending()
        for kind in ("code", "qa"):
            self.assertIn("a passing verify first", self.record(kind, "CHANGES", code=2, findings=[{"id": "F1", "severity": "major", "text": "toast"}]))
        self.assertEqual(self.loops(), [])
        self.assertEqual([e for e in self.events(self.run_dir) if e["type"] == "blocker"], [])


class ShipTests(HumanCase):
    @lesson("L-24")
    def test_the_briefing_lists_the_human_checks_with_the_users_words(self):
        """L-24: the Gate S briefing says what the user confirmed, in their words; the change body stays the reviewer's summary."""
        self.pending()
        self.confirm("yes, Saved in Portuguese")
        self.ok("gate", "verify")
        self.record("code", "APPROVE")
        self.record("qa", "APPROVE", exercises=BOTH)
        self.ok("gate", "review")
        self.ok("ship", "plan")
        body = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        self.assertNotIn("CHK-2", body)  # the user's confirmation is for the briefing, not the reviewer of the change
        n = json.loads(self.ok("next", "--json"))
        self.assertTrue(any("human check CHK-2: the user confirmed (yes, Saved in Portuguese)" in ln for ln in n["briefing"]), n["briefing"])


if __name__ == "__main__":
    unittest.main()
