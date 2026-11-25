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
    def ship_waits(self) -> str:
        return "\n".join(self.gate("ship")[1])

    def reviewed(self) -> None:
        self.record("code", "APPROVE")
        self.record("qa", "APPROVE", exercises=BOTH)
        self.ok("gate", "review")

    @lesson("L-24")
    def test_a_human_check_is_deferred_to_the_ship_briefing_and_confirmed_once(self):
        """L-24: verify never runs a human check and no longer waits for it: the run goes through the reviews, the user is asked once
        at the ship briefing, and the ship gate needs their words at the final commits."""
        self.pending()
        got, out, _ = self.cli("verify", "--json")
        self.assertEqual({r["check"]: r["result"] for r in json.loads(out)["results"]}, {"CHK-1": "pass", "CHK-2": "pending"})
        self.assertEqual(self.gate_verify()[0], 0, "a pending human check is deferred, not a verify failure")
        self.reviewed()
        self.ship_plan()
        n = json.loads(self.ok("next", "--json"))
        self.assertIn(f"CHK-2: {TEXT}", n["action"])
        self.assertIn("confirm before you push", n["action"])
        self.assertIn("CHK-2: waits for the user's confirmation at the ship briefing", self.ship_waits())
        self.assertIn("CHK-2", self.confirm())
        rec = self.evidence(self.run_dir, check="CHK-2")[-1]
        self.assertEqual((rec["result"], rec["human_heads"], rec["user_said"]), ("pass", {"web": self.head()}, "yes, it reads Saved in Portuguese"))
        self.assertNotIn("CHK-2", self.ship_waits())
        self.assertEqual(json.loads(self.ok("metrics", "--json"))["human_events"], {"confirm": 1})

    @lesson("L-24")
    def test_a_rejection_is_a_finding_that_spends_a_loop_and_sends_the_run_back_to_build(self):
        """L-24: the user's no is a finding of the delivery: it spends one loop and the builder fixes it before the user is asked again."""
        self.pending()
        self.reviewed()
        self.ship_plan()
        self.assertIn("rejection of CHK-2", self.ok("confirm", "CHK-2", "--rejected", "--user-said", "no, it still says Saved in English"))
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual((n["role"], n["command"]), ("builder", "factory verify"))
        self.assertIn("still says Saved in English", n["action"])
        self.assertEqual([e["kind"] for e in self.events(self.run_dir) if e["type"] == "loop"], ["human"])

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
        self.assertNotIn("CHK-2", self.ship_waits())
        self.commit("extra.txt", "x\n")
        self.ok("verify")
        self.reviewed()
        self.assertIn("CHK-2: waits for the user's confirmation", self.ship_waits())
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
    def test_a_pending_human_check_does_not_hold_the_reviews(self):
        """L-138, L-24: the reviews go on while the user has not been asked; nothing blocks and no loop is spent waiting."""
        self.pending()
        self.record("code", "APPROVE")
        self.record("qa", "APPROVE", exercises=BOTH)
        self.assertEqual(self.gate("review")[0], 0)
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
