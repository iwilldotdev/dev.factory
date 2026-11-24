"""ship verify and gate ship: the readback against the plan, proof grade, de-duplication, pipelines and the adapter route."""
import json
import unittest

from tests.helpers import lesson
from tests.m3_support import ShipCase

OTHER_SHA = "a" * 40
CONFIRM = ("confirm", "change:42", "--user-said", "yes, that is the change on the board")


class ReadbackTests(ShipCase):
    def setUp(self):
        super().setUp()
        self.to_ship()
        self.ship_plan()

    def confirmed(self, **over):
        self.ingest_change(**over)
        self.ok(*CONFIRM)

    def test_a_matching_confirmed_readback_verifies_and_passes_the_ship_gate(self):
        self.assertIn("no change readback after the ship plan", self.failing("ship"))
        self.confirmed()
        res = self.ship_verify()
        self.assertEqual((res["result"], res["mismatches"], res["recorded"]), ("pass", [], True))
        self.assertEqual(self.gate("ship")[0], 0)
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual((n["phase"], n["kind_of_action"]), ("done", "none"))
        self.assertEqual(self.ship_events("ship_verified")[-1]["heads"], {"web": self.head()})

    @lesson("L-21")
    def test_a_readback_written_by_the_agent_is_proof_only_with_an_adapter_or_the_users_confirmation(self):
        """L-21: a change used as proof needs `source: adapter` or a recorded user confirmation; a re-ingest asks again."""
        self.ingest_change()
        res = self.ship_verify(code=1)
        self.assertIn("change 42 was written by the agent", res["mismatches"][0])
        self.assertIn("factory confirm change:42", res["mismatches"][0])
        self.fails("confirm", "change:42", "--user-said", "  ")
        self.ok(*CONFIRM)
        self.assertEqual(self.ship_verify()["result"], "pass")
        self.ingest_change(title=self.sp()["title"])
        self.assertIn("written by the agent", self.failing("ship"), "a newer ingest of the same change needs a new confirmation")
        self.assertIn("has no change contract 77", self.fails("confirm", "change:77", "--user-said", "x"))
        self.assertIn("e.g. change:42", self.fails("confirm", "42", "--user-said", "x"))

    @lesson("L-153", "L-154")
    def test_the_readback_is_compared_field_by_field_with_the_plan(self):
        """L-153, L-154: title, draft, branches, head and the description table come from the readback, not from a local snapshot."""
        sp = self.sp()
        cases = {
            "title": ({"title": "[ABC-1] - Something else"}, "the change title is"),
            "draft": ({"draft": False}, "the change draft is False, the plan says True"),
            "source": ({"source_branch": "other/branch"}, "the change source branch is 'other/branch'"),
            "target": ({"target_branch": "develop"}, "the change target branch is 'develop'"),
            "head": ({"head_sha": OTHER_SHA}, "the change head sha is"),
            "state": ({"state": "closed"}, "the change is closed, not open"),
        }
        for name, (over, text) in cases.items():
            self.confirmed(**over)
            res = self.ship_verify(code=1)
            self.assertTrue(any(text in m for m in res["mismatches"]), (name, res["mismatches"]))
        edited = sp["rows"][0]
        body = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        self.confirmed(description_md=body.replace(f"| {edited['ac']} | {edited['check']} |", "| edited by hand |"))
        res = self.ship_verify(code=1)
        self.assertIn("lacks the AC x check x status table rows: AC-1 x CHK-1 pass", res["mismatches"][0])
        self.confirmed()
        self.assertEqual(self.ship_verify()["result"], "pass")

    @lesson("L-144")
    def test_a_description_with_a_local_link_fails_the_readback(self):
        """L-144: `ship verify` fails on `](evidence/`, a drive path or `file://` in the change description."""
        body = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        for link in ("[out](evidence/CHK-1/1/output.txt)", "[out](C:/runs/x.txt)", "see file:///tmp/x"):
            self.confirmed(description_md=body + "\n" + link)
            self.assertIn("links to a local path", "\n".join(self.ship_verify(code=1)["mismatches"]), link)

    @lesson("L-153")
    def test_a_readback_from_before_the_plan_does_not_count(self):
        """L-153: only a readback saved after the ship plan is compared; an older one is not read as the action's result."""
        self.confirmed()
        self.assertEqual(self.ship_verify()["result"], "pass")
        self.commit("more.txt", "x\n", "fix: follow up")
        self.ok("verify")
        self.approve_both()
        self.ok("gate", "review")
        self.ship_plan("--update")
        self.assertIn("no change readback after the ship plan", self.ship_verify(code=1)["mismatches"][0])

    @lesson("L-151")
    def test_an_identical_verified_state_is_recorded_once_and_a_new_head_records_again(self):
        """L-151: `ship verify` de-duplicates an identical state but records a new one when the head or the result changed."""
        self.confirmed()
        self.ship_verify()
        again = self.ship_verify()
        self.assertEqual((again["result"], again["recorded"]), ("pass", False))
        self.assertEqual(self.gate("ship")[0], 0)
        self.assertEqual(len(self.ship_events("ship_verified")), 1, "the gate does not write a second ship event")
        self.confirmed(draft=False)
        self.assertEqual(self.ship_verify(code=1)["recorded"], True)
        self.assertEqual(self.ship_verify(code=1)["recorded"], False)
        self.commit("more.txt", "x\n", "fix: follow up")
        self.ok("verify")
        self.approve_both()
        self.ok("gate", "review")
        self.ship_plan("--update")
        self.ingest_change(head_sha=self.head())
        self.ok(*CONFIRM)
        self.assertEqual(self.ship_verify()["recorded"], True)
        self.assertEqual([e["heads"]["web"] for e in self.ship_events("ship_verified")][-1], self.head())
        self.assertEqual(len(self.ship_events("ship_verified")), 3)

    def test_the_ship_gate_goes_stale_with_a_new_commit_a_plan_change_or_an_edited_ship_plan(self):
        self.confirmed()
        self.ship_verify()
        self.ok("gate", "ship")
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "done")
        path = self.run_dir / "ship" / "plan.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"draft": true', '"draft": false'), encoding="utf-8")
        self.assertIn("edited after it was planned", self.failing("ship"))
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "ship")
        self.ship_plan("--update")
        self.commit("more.txt", "x\n", "fix: follow up")
        text = self.failing("ship")
        self.assertIn("verify record", text)
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "build")

    def test_ship_verify_needs_a_plan(self):
        (self.run_dir / "ship" / "plan.json").unlink()
        self.assertIn("no ship plan", self.fails("ship", "verify", code=2))

    def test_reviewers_of_the_plan_must_be_on_the_change(self):
        self.write("factory.toml", self.TOML + '\n[ship]\nreviewers = [{ scm = "ana.lima", tracker = "5f1c2e" }]\n')
        self.ship_plan()
        self.confirmed(reviewers=[])
        self.assertIn("reviewer ana.lima is not on the change", self.ship_verify(code=1)["mismatches"][0])
        self.confirmed(reviewers=["ana.lima"])
        self.assertEqual(self.ship_verify()["result"], "pass")


if __name__ == "__main__":
    unittest.main()
