"""ship plan: neutral intents, autonomy, the change body, the behind count and --update."""
import json
import sys
import unittest

from dev_factory import shipbody
from tests.helpers import git, lesson
from tests.m3_support import ShipCase

DRAFT_AUTO = '\n[ship]\nreviewers = [{ scm = "ana.lima", tracker = "5f1c2e" }]\nticket = "comment"\ntransition_to = "In Review"\n\n[autonomy]\nship = "draft-auto"\n'


GOOD = "ABC-1 (https://tracker/ABC-1): overdue invoices are listed." + 2 * chr(10) + "## How it was tested" + chr(10) + "A unit test of the list." + chr(10)


class PlanTests(ShipCase):
    def setUp(self):
        super().setUp()
        self.to_ship()

    def intents(self, sp=None):
        return [(i["intent"], i["needs_user"]) for i in (sp or self.sp())["intents"]]

    def test_the_default_plan_asks_the_user_about_every_intent_and_writes_the_files(self):
        sp = self.ship_plan()
        self.assertEqual(self.intents(sp), [("push", True), ("change_request", True), ("readback", True)])
        self.assertEqual((sp["mode"], sp["draft"], sp["ticket"]), ("ask", True, "ABC-1"))
        push, change, readback = sp["intents"]
        self.assertEqual((push["branch"], push["sha"], push["remote"]), (self.tree_branch(), self.head(), "origin"))
        self.assertEqual((change["action"], change["source_branch"], change["target_branch"], change["body_file"]),
                         ("create", self.tree_branch(), "main", (self.run_dir / "ship" / "body.md").as_posix()))
        self.assertIn("factory ingest change --raw", readback["ingest"])
        (ev,) = self.ship_events("ship_planned")
        self.assertEqual((ev["heads"], ev["update"]), ({"web": self.head()}, False))
        self.assertEqual(len(ev["plan_sha256"]), 64)

    @lesson("L-142")
    def test_the_title_is_key_dash_summary_and_never_doubles_the_key(self):
        """L-142: the default title is `[key] - summary`, and an empty title template falls back to it."""
        self.assertEqual(self.ship_plan()["title"], "[ABC-1] - Show overdue invoices")
        self.write("factory.toml", self.TOML + '\n[ship]\ntitle = ""\n')
        self.assertEqual(self.ship_plan()["title"], "[ABC-1] - Show overdue invoices")
        self.write("factory.toml", self.TOML + '\n[ship]\ntitle = "{key}: {summary}"\n')
        self.assertEqual(self.ship_plan()["title"], "ABC-1: Show overdue invoices")

    @lesson("L-144")
    def test_the_body_is_the_reviewers_summary_and_the_evidence_table_stays_in_the_briefing(self):
        """L-144: the change body is the human summary as written; ids, evidence and run paths are refused, the AC x check rows go to the user."""
        for bad, why in (("", "missing or empty"), ("Overdue invoices are listed.", "does not name the ticket ABC-1"),
                         ("ABC-1: see CHK-1 attempt 2.", "bookkeeping"), ("ABC-1: [log](artifacts/x.txt)", "inside the run folder"),
                         ("ABC-1: run factory verify again.", "bookkeeping")):
            self.write_summary(bad)
            self.assertIn(why, self.fails("ship", "plan"))
        self.write_summary(GOOD)
        sp = self.ship_plan()
        text = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        self.assertEqual(text, GOOD)
        self.assertEqual([(r["ac"], r["check"], r["result"]) for r in sp["rows"]], [("AC-1", "CHK-1", "pass")])
        briefing = json.loads(self.ok("next", "--json"))["briefing"]
        self.assertTrue(any(ln.strip().startswith("AC-1 x CHK-1: pass") for ln in briefing), briefing)
        for bad in ("](evidence/x)", "](C:/x)", "file:///x", "](artifacts/x)"):
            self.assertTrue(shipbody.LOCAL_LINK.search(bad), bad)

    def test_ship_plan_needs_the_review_gate_to_hold(self):
        self.commit("more.txt", "x\n")
        self.assertIn("review gate to hold", self.fails("ship", "plan"))
        self.assertFalse((self.run_dir / "ship" / "plan.json").exists())

    @lesson("L-146")
    def test_a_delivery_ships_finished_open_minors_block_the_gate_unless_out_of_scope(self):
        """L-146: a minor finding of the delivery is fixed before it ships; one the reviewer marks out of scope stays in the run, never in the change."""
        self.record("code", "CHANGES", findings=[{"id": "F1", "severity": "minor", "file": "a.py:1", "text": "rename thing 1"},
                                                  {"id": "F2", "severity": "minor", "text": "an old helper elsewhere", "scope": "out"}])
        self.record("code", "APPROVE")
        self.assertIn("1 minor finding(s) of the delivery are open: 2:F1 rename thing 1", self.fails("gate", "review", code=1))
        self.assertEqual(len([e for e in self.events(self.run_dir) if e["type"] == "loop"]), 0, "a minor round spends no loop")
        self.assertIn("scope", self.record("code", "APPROVE", code=2, findings=[{"id": "F9", "severity": "minor", "text": "x", "scope": "later"}]))
        self.record("code", "APPROVE", resolved=["2:F1"])
        self.ok("gate", "review")
        self.ship_plan()
        self.assertNotIn("old helper", (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8"))
        briefing = json.loads(self.ok("next", "--json"))["briefing"]
        self.assertIn("  findings outside this delivery (kept in the run, not in the change): 1", briefing)

    def test_resolving_a_finding_that_carries_an_earlier_one_closes_the_chain(self):
        self.record("code", "CHANGES", findings=[{"id": "U1", "severity": "minor", "text": "the name is too narrow"}])
        self.record("code", "CHANGES", findings=[{"id": "U1", "severity": "minor", "text": "still narrow, better", "carries": "2:U1"}])
        self.assertIn("carries", self.record("code", "APPROVE", code=2, findings=[{"id": "U9", "severity": "minor", "text": "x", "carries": "1:U7"}]))
        self.record("code", "APPROVE", resolved=["3:U1"])  # rounds 2 and 3: the fixture recorded round 1
        self.record("qa", "APPROVE")
        self.assertEqual(self.gate("review")[0], 0)


    def tree_branch(self) -> str:
        return git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.tree())


if __name__ == "__main__":
    unittest.main()
