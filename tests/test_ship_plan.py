"""ship plan: autonomy, ticket intents, the pipeline intent, the behind count and --update."""
import unittest

from tests.helpers import git, lesson
from tests.m3_support import ShipCase

DRAFT_AUTO = '\n[ship]\nreviewers = [{ scm = "ana.lima", tracker = "5f1c2e" }]\nticket = "comment"\ntransition_to = "In Review"\n\n[autonomy]\nship = "draft-auto"\n'


class AutonomyTests(ShipCase):
    def setUp(self):
        super().setUp()
        self.to_ship(extra_toml=DRAFT_AUTO)

    def test_draft_auto_lets_push_and_a_draft_change_run_but_ticket_writes_always_ask(self):
        sp = self.ship_plan("--ticket", "both")
        got = {i["intent"]: i["needs_user"] for i in sp["intents"]}
        self.assertEqual(got, {"push": False, "change_request": False, "readback": False, "issue_comment": True, "issue_transition": True})
        change = next(i for i in sp["intents"] if i["intent"] == "change_request")
        self.assertEqual(change["reviewers"], ["ana.lima"])
        self.assertEqual(next(i for i in sp["intents"] if i["intent"] == "issue_transition")["to"], "In Review")

    def test_a_non_draft_change_always_asks_even_in_draft_auto(self):
        self.write("factory.toml", self.TOML + DRAFT_AUTO.replace("[ship]\n", "[ship]\ndraft = false\n"))
        sp = self.ship_plan()
        self.assertTrue(next(i for i in sp["intents"] if i["intent"] == "change_request")["needs_user"])

    @lesson("L-145")
    def test_the_ticket_flag_controls_the_ticket_intents_and_none_plans_no_write(self):
        """L-145: `--ticket none|comment|transition|both` chooses the ticket writes, whatever the project default says."""
        for flag, want in (("none", []), ("comment", ["issue_comment"]), ("transition", ["issue_transition"]), ("both", ["issue_comment", "issue_transition"])):
            got = [i["intent"] for i in self.ship_plan("--ticket", flag)["intents"] if i["intent"].startswith("issue_")]
            self.assertEqual(got, want, flag)
        self.assertEqual([i["intent"] for i in self.ship_plan()["intents"] if i["intent"].startswith("issue_")], ["issue_comment"])
        self.assertIn("invalid choice", self.fails("ship", "plan", "--ticket", "everything"))

    def test_a_transition_without_a_configured_target_is_refused(self):
        self.write("factory.toml", self.TOML)
        self.assertIn("ship.transition_to", self.fails("ship", "plan", "--ticket", "transition"))

    def test_the_pipeline_intent_is_opt_in_and_required_with_require_pipeline(self):
        self.assertNotIn("pipeline", [i["intent"] for i in self.ship_plan()["intents"]])
        opt = next(i for i in self.ship_plan("--pipeline")["intents"] if i["intent"] == "pipeline")
        self.assertEqual((opt["optional"], opt["needs_user"]), (True, False))
        self.write("factory.toml", self.TOML + "\n[ship]\nrequire_pipeline = true\n")
        req = next(i for i in self.ship_plan()["intents"] if i["intent"] == "pipeline")
        self.assertEqual(req["optional"], False)


class BehindTests(ShipCase):
    def setUp(self):
        super().setUp()
        self.to_ship()

    @lesson("L-108")
    def test_the_plan_reports_commits_behind_and_says_when_a_fetch_is_needed(self):
        """L-108: the behind count comes from local refs, and the plan names a needed fetch; a moved target refuses without a decision."""
        sp = self.ship_plan()
        self.assertEqual((sp["behind"]["web"]["count"], sp["behind"]["web"]["ref"]), (0, "main"))
        self.assertIn("run git fetch first", sp["behind"]["web"]["note"])
        self.assertIn("0 commit(s) behind main", (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8"))
        (self.proj / "base.txt").write_text("moved\n")
        git("add", "-A", cwd=self.proj)
        git("commit", "-q", "-m", "chore: base moved", cwd=self.proj)
        msg = self.fails("ship", "plan")
        self.assertIn("web is 1 commit(s) behind main", msg)
        self.assertIn("--allow-behind --decision", msg)
        self.assertIn("needs --decision", self.fails("ship", "plan", "--allow-behind"))
        self.ok("ask", "The target moved; ship anyway?")
        self.ok("answer", "Q-1", "--user-said", "yes, ship it")
        sp = self.ship_plan("--allow-behind", "--decision", "Q-1")
        self.assertEqual(sp["behind"]["web"]["count"], 1)
        self.assertEqual(self.ship_events("ship_planned")[-1]["decision"], "Q-1")
        self.assertIn("already consumed", self.fails("ship", "plan", "--allow-behind", "--decision", "Q-1"))

    def test_a_remote_tracking_ref_is_preferred_and_named(self):
        git("update-ref", "refs/remotes/origin/main", git("rev-parse", "main", cwd=self.proj), cwd=self.proj)
        sp = self.ship_plan()
        self.assertEqual(sp["behind"]["web"]["ref"], "origin/main")
        self.assertIn("as last fetched", sp["behind"]["web"]["note"])


class UpdateTests(ShipCase):
    def setUp(self):
        super().setUp()
        self.to_ship()

    def shipped(self):
        self.ship_plan()
        self.ingest_change()
        self.ok("confirm", "change:42", "--user-said", "that is the change")
        self.ship_verify()

    def test_a_change_that_exists_needs_update_and_update_needs_a_change(self):
        self.assertIn("no open change was read back yet", self.fails("ship", "plan", "--update"))
        self.shipped()
        self.assertIn("change 42 already exists", self.fails("ship", "plan"))

    def test_update_replans_the_body_and_head_for_the_existing_change(self):
        self.shipped()
        self.commit("more.txt", "x\n", "fix: follow up")
        self.ok("verify")
        self.approve_both()
        self.ok("gate", "review")
        sp = self.ship_plan("--update")
        self.assertEqual([(i["intent"], i.get("action")) for i in sp["intents"]], [("push", None), ("change_request", "update"), ("readback", None)])
        self.assertEqual(sp["intents"][1]["change_id"], "42")
        self.assertTrue(sp["update"])
        self.assertIn("no change readback after the ship plan", self.failing("ship"))
        self.assertEqual(self.ship_events("ship_planned")[-1]["update"], True)

    def test_update_adds_set_draft_when_the_change_is_not_a_draft_but_the_plan_says_so(self):
        self.ship_plan()
        self.ingest_change(draft=False)
        self.ok("confirm", "change:42", "--user-said", "yes")
        sp = self.ship_plan("--update")
        self.assertEqual([i["draft"] for i in sp["intents"] if i["intent"] == "set_draft"], [True])


if __name__ == "__main__":
    unittest.main()
