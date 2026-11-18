"""MR description lists unresolved minor findings under `## Follow-ups`."""

from __future__ import annotations

import unittest

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

    def test_section_omitted_without_findings(self):
        self.ready()
        self.assertNotIn("Follow-ups", self.description())


if __name__ == "__main__":
    unittest.main()
