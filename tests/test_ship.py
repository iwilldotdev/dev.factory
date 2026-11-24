"""ship plan: neutral intents, autonomy, the change body, the behind count and --update."""
import sys
import unittest

from dev_factory import shipbody
from tests.helpers import git, lesson
from tests.m3_support import ShipCase

DRAFT_AUTO = '\n[ship]\nreviewers = [{ scm = "ana.lima", tracker = "5f1c2e" }]\nticket = "comment"\ntransition_to = "In Review"\n\n[autonomy]\nship = "draft-auto"\n'


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

    def test_the_body_has_the_ac_check_status_table_with_evidence_references(self):
        self.ship_plan()
        text = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        rec = [e for e in self.events(self.run_dir) if e["type"] == "evidence"][-1]
        self.assertIn("| AC | Check | Command | Exit | Result | Evidence |", text)
        row = next(ln for ln in text.splitlines() if ln.startswith("| AC-1 | CHK-1"))
        self.assertIn(f"| 0 | pass | CHK-1 attempt {rec['attempt']} ({rec['hash'][:8]}) |", row)
        self.assertIn("check.py 2", row)
        self.assertIn("checks passed: VALUE=2", text)
        self.assertIn("- AC-1: VALUE is 2", text)
        self.assertIn("- code: round 1, APPROVE by fresh-reviewer", text)
        self.assertEqual([r["result"] for r in self.sp()["rows"]], ["pass"])

    @lesson("L-144")
    def test_the_body_links_nothing_local_and_shows_a_short_trailing_excerpt(self):
        """L-144: the generated body has no run-folder link, and the excerpt is the last 10 output lines."""
        noisy = "import sys\nprint('warning: noise')\n" * 1 + "for i in range(40):\n    print(f'warning {i}')\nprint('checks passed: VALUE=2')\n"
        self.commit_many({"noisy.py": noisy}, "feat: noisy check")
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["run"] = f'"{sys.executable}" noisy.py'
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "noisy check")
        self.ok("verify")
        self.approve_both()
        self.ok("gate", "review")
        self.ship_plan()
        text = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        self.assertIsNone(shipbody.LOCAL_LINK.search(text))
        self.assertNotIn(self.run_dir.as_posix(), text)
        block = text.split("```text")[1].split("```")[0].strip().splitlines()
        self.assertEqual(len(block), 10)
        self.assertEqual(block[-1], "checks passed: VALUE=2")
        for bad in ("](evidence/x)", "](C:/x)", "file:///x", "](artifacts/x)"):
            self.assertTrue(shipbody.LOCAL_LINK.search(bad), bad)

    def test_ship_plan_needs_the_review_gate_to_hold(self):
        self.commit("more.txt", "x\n")
        self.assertIn("review gate to hold", self.fails("ship", "plan"))
        self.assertFalse((self.run_dir / "ship" / "plan.json").exists())

    @lesson("L-146")
    def test_unresolved_minors_from_every_round_are_listed_once_and_capped_at_20(self):
        """L-146: minors of round 2 stay in the body after a clean round 3, de-duplicated, with a cap and a count."""
        minors = [{"id": f"F{i}", "severity": "minor", "file": f"a.py:{i}", "text": f"rename thing {i}"} for i in range(1, 24)]
        self.record("code", "CHANGES", findings=minors)
        self.record("code", "APPROVE", resolved=["2:F3"])
        self.record("qa", "CHANGES", findings=[{"id": "F1", "severity": "minor", "file": "a.py:1", "text": "rename thing 1"}])
        self.record("qa", "APPROVE")
        self.ok("gate", "review")
        self.ship_plan()
        text = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        self.assertIn("## Follow-ups: 22 unresolved minor finding(s)", text)
        self.assertEqual(text.count("rename thing 1 "), 1, "the same minor from two reviews is listed once")
        self.assertNotIn("rename thing 3 (", text)
        self.assertEqual(len([ln for ln in text.splitlines() if "(round" in ln and ln.startswith("- a.py")]), 20)
        self.assertIn("- and 2 more", text)

    def tree_branch(self) -> str:
        return git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.tree())


if __name__ == "__main__":
    unittest.main()
