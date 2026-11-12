"""Major 6: review forging; major 7 (reviews): digests; major 10: loop caps."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tests.integrity_support import IntegrityCase, cli, core

MAJOR = [{"id": "F1", "severity": "major", "text": "breaks X", "file": "a.ts:1", "resolved": True}]


class ReviewForgingTests(IntegrityCase):
    def setUp(self) -> None:
        super().setUp()
        self.proved()

    def test_record_requires_auditor_role(self):
        with mock.patch.dict(os.environ, {"FACTORY_ROLE": "builder"}):
            rc, out = self.record("code")
        self.assertEqual(rc, 2)
        self.assertIn("FACTORY_ROLE=auditor", out)
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_ROLE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.record("code")[0], 2)
        self.assertFalse((self.rd / "reviews" / "code-1.json").exists())

    def test_approve_needs_zero_gap_and_not_checked(self):
        rc, out = self.record("code", completeness={"done": 1, "gap": 1, "not_checked": 0})
        self.assertEqual(rc, 1)
        self.assertIn("completeness gap 1", out)
        rc, out = self.record("code", completeness={"done": 1, "gap": 0, "not_checked": 2})
        self.assertEqual(rc, 1)
        self.assertIn("not_checked 2", out)
        rc, out = self.record("code", completeness={"done": 1})
        self.assertEqual(rc, 1)
        self.assertIn("completeness needs integer", out)
        self.assertEqual(self.record("code", "CHANGES", completeness={"done": 0, "gap": 1, "not_checked": 0})[0], 0)

    def test_qa_needs_ac_coverage_for_every_ac(self):
        path = self.review_file("qa")
        rec = core.read_json(path)
        rec["ac_coverage"] = []
        core.write_json(path, rec)
        rc, out = cli("review", "record", self.rid, "--kind", "qa", "--file", str(path))
        self.assertEqual(rc, 1)
        self.assertIn("AC-1", out)

    def test_resolved_flag_does_not_make_approve_valid(self):
        rc, out = self.record("code", "APPROVE", MAJOR)
        self.assertEqual(rc, 1)
        self.assertIn("resolved", out)

    def test_gate_counts_resolved_major_as_open(self):
        self.assertEqual(self.record("code", "CHANGES", MAJOR)[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("F1 [major]", out)

    def test_hand_edited_review_is_rejected(self):
        self.assertEqual(self.record("code")[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        path = self.rd / "reviews" / "code-1.json"
        rec = core.read_json(path)
        core.write_json(path, dict(rec, reviewer="me"))
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("code review round 1: file does not match the digest", out)

    def test_hand_written_review_round_is_rejected(self):
        self.assertEqual(self.record("code", "CHANGES", MAJOR)[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        forged = dict(core.read_json(self.rd / "reviews" / "code-1.json"), round=2, verdict="APPROVE", findings=[])
        core.write_json(self.rd / "reviews" / "code-2.json", forged)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("code review round 2: no review event", out)
        self.assertEqual(core.run_load(self.rid)["phase"], "review")

    def test_record_event_carries_digest(self):
        self.record("code")
        ev = next(e for e in core.run_load(self.rid)["events"] if e["kind"] == "review")
        self.assertEqual(ev["data"]["digest"], core.file_sha256(self.rd / "reviews" / "code-1.json"))


class LoopCapTests(IntegrityCase):
    def changes_round(self):
        self.assertEqual(self.record("code", "CHANGES", [{"id": "F1", "severity": "blocker", "text": "bad"}])[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        return self.gate("review")

    def test_max_review_rounds_blocks_and_unblock_resumes_build(self):
        self.set_autonomy(max_review_rounds=1, max_build_loops=9)
        self.proved()
        rc, out = self.changes_round()
        self.assertEqual(rc, 1)
        self.assertIn("max_review_rounds 1: run blocked", out)
        run = core.run_load(self.rid)
        self.assertEqual(run["phase"], "blocked")
        self.assertIn("max_review_rounds", run["blockers"][-1]["text"])
        self.assertEqual(cli("run", "unblock", self.rid, str(len(run["blockers"]) - 1))[0], 0)
        self.assertEqual(core.run_load(self.rid)["phase"], "build")

    def test_rounds_below_cap_loop_to_build(self):
        self.set_autonomy(max_review_rounds=2, max_build_loops=9)
        self.proved()
        rc, out = self.changes_round()
        self.assertIn("phase -> build", out)
        self.assertEqual(core.run_load(self.rid)["phase"], "build")

    def test_build_loop_cap_unblock_resumes_build(self):
        self.set_autonomy(max_review_rounds=9, max_build_loops=0)
        self.proved()
        rc, out = self.changes_round()
        self.assertIn("max_build_loops", out)
        run = core.run_load(self.rid)
        self.assertEqual(run["phase"], "blocked")
        self.assertEqual(cli("run", "unblock", self.rid, str(len(run["blockers"]) - 1))[0], 0)
        self.assertEqual(core.run_load(self.rid)["phase"], "build")


if __name__ == "__main__":
    unittest.main()
