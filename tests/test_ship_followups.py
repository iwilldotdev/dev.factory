"""MR description lists unresolved minor findings under `## Follow-ups`."""

from __future__ import annotations

import unittest

from factory import core
from tests.integrity_support import cli
from tests.test_ship_mr_content import ShipCase


def minor(fid: str, text: str, **extra) -> dict:
    return {"id": fid, "severity": "minor", "category": "engineering", "file": "src/a.txt", "text": text, **extra}


class FollowupTests(ShipCase):
    def description(self) -> str:
        return self.plan()["mrs"][0]["description"]

    def test_minor_findings_from_code_and_qa_listed(self):
        self.to_prove()
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")
        self.assertEqual(self.record("code", findings=[minor("F1", "rename\nthis helper")])[0], 0)
        self.assertEqual(self.record("qa", findings=[minor("Q1", "tidy up copy", file="")])[0], 0)
        self.ok("gate", self.rid, "review")
        desc = self.description()
        self.assertIn("## Follow-ups", desc)
        self.assertIn("- F1 `src/a.txt`: rename this helper", desc)
        self.assertIn("- Q1: tidy up copy", desc)
        self.assertNotIn("file://", desc)

    def test_resolved_flag_and_non_minor_are_skipped(self):
        self.to_prove()
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")
        self.assertEqual(self.record("code", findings=[minor("F1", "done already", resolved=True)])[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        self.assertNotIn("Follow-ups", self.description())

    def round_with(self, kind: str, findings, resolved=None):
        path = self.review_file(kind, "APPROVE", findings)
        rec = core.read_json(path)
        if resolved is not None:
            rec["resolved"] = resolved
        core.write_json(path, rec)
        return cli("review", "record", self.rid, "--kind", kind, "--file", str(path))

    def to_review(self):
        self.to_prove()
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")

    def test_minor_findings_of_earlier_rounds_survive_a_clean_later_round(self):
        self.to_review()
        self.assertEqual(self.round_with("code", [minor("F1", "duplicate warning block"), minor("F2", "pin move has no browser check", file="src/b.txt")])[0], 0)
        self.assertEqual(self.round_with("code", [])[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        desc = self.description()
        self.assertIn("- F1 `src/a.txt`: duplicate warning block", desc)
        self.assertIn("- F2 `src/b.txt`: pin move has no browser check", desc)

    def test_a_later_round_can_mark_earlier_findings_resolved(self):
        self.to_review()
        self.assertEqual(self.round_with("code", [minor("F1", "fixed one"), minor("F2", "still open")])[0], 0)
        self.assertEqual(self.round_with("code", [minor("F3", "new one")], resolved=["F1"])[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        desc = self.description()
        self.assertNotIn("fixed one", desc)
        self.assertIn("- F2 `src/a.txt`: still open", desc)
        self.assertIn("- F3 `src/a.txt`: new one", desc)

    def test_reused_ids_across_rounds_are_told_apart_by_round(self):
        # round 1 F1 and round 2 F1 are different findings; `resolved: ["1:F1"]` in round 3 closes only the first
        self.to_review()
        self.assertEqual(self.round_with("code", [minor("F1", "first finding", file="src/one.txt")])[0], 0)
        self.assertEqual(self.round_with("code", [minor("F1", "second finding", file="src/two.txt")])[0], 0)
        self.assertEqual(self.round_with("code", [], resolved=["1:F1"])[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        desc = self.description()
        self.assertNotIn("first finding", desc)
        self.assertIn("- F1 `src/two.txt`: second finding", desc)

    def test_a_bare_id_means_the_round_right_before(self):
        self.to_review()
        self.assertEqual(self.round_with("code", [minor("F1", "first finding", file="src/one.txt")])[0], 0)
        self.assertEqual(self.round_with("code", [minor("F1", "second finding", file="src/two.txt")])[0], 0)
        self.assertEqual(self.round_with("code", [], resolved=["F1"])[0], 0)  # round 3: only round 2's F1
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        desc = self.description()
        self.assertIn("- F1 `src/one.txt`: first finding", desc)
        self.assertNotIn("second finding", desc)

    def test_a_round_qualified_entry_must_name_an_earlier_round(self):
        self.to_review()
        self.assertEqual(self.round_with("code", [minor("F1", "still open")], resolved=["1:F1", "2:F1", "9:F1"])[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        self.assertIn("still open", self.description())

    def test_same_finding_in_several_rounds_is_listed_once(self):
        self.to_review()
        self.assertEqual(self.round_with("code", [minor("F1", "repeat me")])[0], 0)
        self.assertEqual(self.round_with("code", [minor("F4", "repeat   me")])[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        self.assertEqual(self.description().count("repeat me"), 1)

    def test_long_follow_up_list_is_capped_with_a_count(self):
        self.to_review()
        many = [minor(f"F{i}", f"item number {i}") for i in range(1, 26)]
        self.assertEqual(self.round_with("code", many)[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")
        desc = self.description()
        self.assertIn("item number 20", desc)
        self.assertNotIn("item number 21", desc)
        self.assertIn("and 5 more minor finding(s)", desc)

    def test_resolved_must_be_a_list_of_ids(self):
        self.to_review()
        rc, out = self.round_with("code", [], resolved="F1")
        self.assertEqual(rc, 1)
        self.assertIn("resolved must be a list of finding ids", out)

    def test_section_omitted_without_findings(self):
        self.ready()
        self.assertNotIn("Follow-ups", self.description())


class ShipEventTests(ShipCase):
    def ship_events(self):
        return [e for e in core.run_load(self.rid)["events"] if e["kind"] == "ship"]

    def verified_plan(self):
        import json
        self.reviewed()
        plan = json.loads(self.ok("--json", "ship", "plan", self.rid))
        self.readbacks(plan)
        return plan

    def test_verify_then_gate_ship_records_one_ship_event(self):
        self.verified_plan()
        self.ok("ship", "verify", self.rid)
        self.assertEqual(len(self.ship_events()), 1)
        self.ok("gate", self.rid, "ship")
        self.assertEqual(len(self.ship_events()), 1)

    def test_gate_ship_alone_records_one_event_and_a_verification_after_the_phase_moved_another(self):
        self.verified_plan()
        self.ok("gate", self.rid, "ship")
        self.assertEqual(len(self.ship_events()), 1)
        self.ok("ship", "verify", self.rid)
        self.assertEqual(len(self.ship_events()), 2, "the phase moved on since: a new verification is logged")

    def test_a_new_push_makes_the_next_verification_a_new_event(self):
        from factory.commands import ship
        self.verified_plan()
        self.ok("ship", "verify", self.rid)
        self.assertEqual(len(self.ship_events()), 1)
        first = self.ship_events()[0]["data"]["verified"]
        self.assertTrue(all(v.get("head") for v in first), first)
        # the same MRs, but the source branch moved: a verification of another head is a different fact
        run = core.run_load(self.rid)
        again = [dict(v, head="f" * 40) for v in first]
        self.assertFalse(ship._already_verified(run, again))
        self.assertTrue(ship._already_verified(run, [dict(v) for v in first]))


if __name__ == "__main__":
    unittest.main()
