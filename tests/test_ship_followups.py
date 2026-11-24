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


if __name__ == "__main__":
    unittest.main()
