"""Blocker 1: plan files are locked at the plan gate; later gates refuse changes; `run replan` is the way back."""

from __future__ import annotations

import json
import unittest

from tests.integrity_support import IntegrityCase, cli, core


class PlanLockTests(IntegrityCase):
    def test_plan_gate_records_lock_of_acs_checks_design(self):
        self.to_prove()
        lock = core.run_load(self.rid)["plan_lock"]
        self.assertIn("acs.json", lock["files"])
        self.assertIn("checks/note.txt", lock["files"])
        self.assertEqual(len(lock["digest"]), 64)
        self.assertIn("plan_lock", [e["kind"] for e in core.run_load(self.rid)["events"]])

    def test_changed_check_file_fails_prove_gate_listing_it(self):
        self.to_prove()
        self.ok("prove", self.rid)
        (self.rd / "checks" / "note.txt").write_text("loosened\n", encoding="utf-8")
        (self.rd / "design").mkdir(exist_ok=True)
        (self.rd / "design" / "extra.json").write_text("{}", encoding="utf-8")
        rc, out = self.gate("prove")
        self.assertEqual(rc, 1)
        self.assertIn("plan files changed after the plan gate", out)
        self.assertIn("checks/note.txt", out)
        self.assertIn("design/extra.json", out)
        self.assertIn("run replan", out)

    def test_changed_acs_fails_review_ship_plan_verify_and_ship_gate(self):
        plan = self.shipped_until_ship()
        acs = core.read_json(self.rd / "acs.json")
        acs["checks"][0]["cmd"] = "echo loosened"
        core.write_json(self.rd / "acs.json", acs)
        rc, out = cli("ship", "plan", self.rid)
        self.assertEqual(rc, 1)
        self.assertIn("acs.json", out)
        rc, out = cli("ship", "verify", self.rid)
        self.assertEqual(rc, 1)
        self.assertIn("acs.json", out)
        rc, out = self.gate("ship")
        self.assertEqual(rc, 1)
        self.assertIn("acs.json", out)
        self.assertTrue(plan["changes"])

    def shipped_until_ship(self) -> dict:
        self.reviewed()
        plan = json.loads(self.ok("--json", "ship", "plan", self.rid))
        self.readbacks(plan)
        return plan

    def test_review_gate_refuses_changed_diagnosis(self):
        self.proved()
        (self.rd / "diagnosis.md").write_text("## Root cause\nnew\n", encoding="utf-8")
        self.record("code")
        self.record("qa")
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("diagnosis.md", out)

    def test_replan_clears_lock_counts_loop_and_needs_plan_gate_and_fresh_evidence(self):
        self.proved()
        rc, out = cli("run", "replan", self.rid, "check could not fail")
        self.assertEqual(rc, 0, out)
        run = core.run_load(self.rid)
        self.assertEqual(run["phase"], "plan")
        self.assertNotIn("plan_lock", run)
        self.assertEqual(run["loops"]["replan"], 1)
        ev = next(e for e in reversed(run["events"]) if e["kind"] == "replan")
        self.assertEqual(ev["data"]["reason"], "check could not fail")
        self.assertEqual(ev["data"]["from"], "review")
        (self.rd / "checks" / "note.txt").write_text("v2\n", encoding="utf-8")
        self.ok("gate", self.rid, "plan")
        self.assertEqual(core.run_load(self.rid)["plan_lock"]["files"]["checks/note.txt"],
                         core.file_sha256(self.rd / "checks" / "note.txt"))
        self.ok("gate", self.rid, "build")
        rc, out = self.gate("prove")  # evidence from before the new lock does not count
        self.assertEqual(rc, 1)
        self.assertIn("predates the plan lock", out)
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")

    def test_replan_revalidates_acs(self):
        self.proved()
        self.ok("run", "replan", self.rid, "new check")
        self.acs([{"id": "CHK-1", "type": "test", "repo": "React"}])  # no cmd
        rc, out = self.gate("plan")
        self.assertEqual(rc, 1)
        self.assertIn("cmd is required", out)

    def test_replan_refused_before_plan_gate(self):
        self.start()
        rc, out = cli("run", "replan", self.rid, "x")
        self.assertEqual(rc, 2)


class ReproduceLockTests(IntegrityCase):
    def test_reproduce_allows_diagnosis_but_not_check_changes(self):
        self.start("BUG-1", bug=True)
        check = "import sys\nprint('ASSERT broken')\nsys.exit(1)\n"
        (self.wt / "check.py").write_text(check, encoding="utf-8")
        self.acs([{"id": "CHK-1", "type": "api", "repo": "React", "path": "/x"}],
                 [{"id": "AC-1", "ticket": "BUG-1", "text": "t", "kind": "bug", "checks": ["CHK-1"]}])
        self.ws["repos"][0]["env"] = {"start": ["x"], "health": "http://127.0.0.1:1/"}
        self.save_ws()
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        (self.rd / "diagnosis.md").write_text("## Root cause\nx\n## Evidence\ny\n## Ruled out\nz\n", encoding="utf-8")
        rc, out = self.gate("reproduce")
        self.assertNotIn("plan files changed", out)  # diagnosis.md is written in reproduce
        acs = core.read_json(self.rd / "acs.json")
        acs["checks"][0]["path"] = "/y"
        core.write_json(self.rd / "acs.json", acs)
        rc, out = self.gate("reproduce")
        self.assertEqual(rc, 1)
        self.assertIn("plan files changed after the plan gate: acs.json", out)


if __name__ == "__main__":
    unittest.main()
