"""Audit follow-ups: plan lock covers every check input (F4), qa human-only needs clean completeness (F6), human
evidence is bound to HEAD (F7), update-description re-checks the proof (F11)."""

from __future__ import annotations

import json
import unittest

from tests.integrity_support import PASS, IntegrityCase, cli, commit_file, core

from factory import integrity
from factory.commands import gate

HUMAN = {"id": "CHK-H", "type": "human", "text": "picker looks right", "reason": "native widget"}
TEST = {"id": "CHK-1", "type": "test", "repo": "React", "cmd": PASS}
HUMAN_FINDING = [{"id": "F1", "severity": "major", "text": "human check not confirmed", "file": "-", "check": "CHK-H"}]


class InputLockTests(IntegrityCase):
    """F4: a spec/script under checks/**/{out,output,...} used to escape the plan lock."""

    def lay_out(self, checks):
        self.start()
        checks = checks(self.rd.as_posix()) if callable(checks) else checks
        for sub, name, text in (("artifacts", "login.json", '{"steps": []}'), ("output", "run_check.py", "assert 1\n")):
            (self.rd / "checks" / sub).mkdir(parents=True, exist_ok=True)
            (self.rd / "checks" / sub / name).write_text(text, encoding="utf-8")
        (self.rd / "specs").mkdir(exist_ok=True)
        (self.rd / "specs" / "login.json").write_text('{"steps": []}', encoding="utf-8")
        (self.rd / "specs" / "run_check.py").write_text("assert 1\n", encoding="utf-8")
        self.acs(checks)

    def browser(self, spec):
        return {"id": "CHK-B", "type": "browser", "repo": "React", "spec": spec}

    def test_spec_in_a_generated_dir_is_rejected_by_the_plan_gate(self):
        self.lay_out([self.browser("checks/artifacts/login.json")])
        self.ok("gate", self.rid, "intake")
        rc, out = self.gate("plan")
        self.assertEqual(rc, 1, out)
        self.assertIn("CHK-B: spec 'checks/artifacts/login.json' is inside a generated dir", out)

    def test_script_in_cmd_inside_a_generated_dir_is_rejected(self):
        self.lay_out(lambda rdir: [{"id": "CHK-1", "type": "test", "repo": "React",
                                    "cmd": f"{PASS} && python {rdir}/checks/output/run_check.py"}])
        self.ok("gate", self.rid, "intake")
        rc, out = self.gate("plan")
        self.assertEqual(rc, 1, out)
        self.assertIn("is inside a generated dir", out)

    def test_run_token_script_in_a_generated_dir_is_rejected(self):
        self.lay_out([{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "python <run>/checks/output/run_check.py"}])
        self.ok("gate", self.rid, "intake")
        rc, out = self.gate("plan")
        self.assertEqual(rc, 1, out)
        self.assertIn("is inside a generated dir", out)

    def test_spec_and_cmd_script_outside_checks_are_locked(self):
        self.lay_out(lambda rdir: [self.browser("specs/login.json"),
                                   {"id": "CHK-1", "type": "test", "repo": "React", "cmd": f"python {rdir}/specs/run_check.py"}])
        files = integrity.plan_files(self.rid)
        self.assertIn("specs/login.json", files)
        self.assertIn("specs/run_check.py", files)
        self.assertNotIn("checks/artifacts/login.json", files)  # generated stays out
        self.assertEqual(integrity.referenced_files(self.rid).count("specs/login.json"), 1)

    def test_lockprobe_case_does_not_escape(self):
        self.lay_out([self.browser("checks/artifacts/login.json"),
                      {"id": "CHK-1", "type": "test", "repo": "React", "cmd": "python <run>/checks/output/run_check.py"}])
        # both inputs are now referenced (locked) AND the plan gate refuses them while they sit in a generated dir
        self.assertIn("checks/artifacts/login.json", integrity.plan_files(self.rid))
        self.assertIn("checks/output/run_check.py", integrity.plan_files(self.rid))


class HumanOnlyCompletenessTests(IntegrityCase):
    """F6: a qa CHANGES made only of human findings is waiting-on-human only when the review is complete."""

    def setUp(self) -> None:
        super().setUp()
        self.proved([TEST, HUMAN])
        self.assertEqual(self.record("code")[0], 0)

    def stays_changes(self, comp):
        self.assertEqual(self.record("qa", "CHANGES", HUMAN_FINDING, comp)[0], 0)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1, out)
        run = core.run_load(self.rid)
        self.assertEqual((run["phase"], run["loops"]["review"]), ("build", 1), out)
        self.assertNotIn("human check pending", out)

    def test_human_only_with_gap_stays_changes(self):
        self.stays_changes({"done": 1, "gap": 1, "not_checked": 0})

    def test_human_only_with_not_checked_stays_changes(self):
        self.stays_changes({"done": 1, "gap": 0, "not_checked": 2})

    def test_human_only_with_clean_completeness_still_waits(self):
        self.assertEqual(self.record("qa", "CHANGES", HUMAN_FINDING, {"done": 2, "gap": 0, "not_checked": 0})[0], 0)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("CHK-H: human check pending", out)
        self.assertEqual(core.run_load(self.rid)["loops"]["review"], 0)

    def test_review_status_directly(self):
        ws = core.workspace_load("ws")
        self.assertEqual(self.record("qa", "CHANGES", HUMAN_FINDING, {"done": 1, "gap": 0, "not_checked": 1})[0], 0)
        _, changes = gate.review_status(self.rid, core.run_load(self.rid), ws)
        self.assertTrue(any("verdict CHANGES" in c for c in changes), changes)


class HumanEvidenceBindingTests(IntegrityCase):
    """F7: human pass evidence counts only while it is intact, passing, and bound to the current clean HEAD."""

    def setUp(self) -> None:
        super().setUp()
        self.proved([TEST, HUMAN])

    def verify(self):
        self.ok("run", "verify-human", self.rid, "CHK-H", "--note", "ok", "--by", "user", "--user-said", "fine")

    def pending(self) -> list[str]:
        return [c["id"] for c in gate.human_pending(self.rid, core.run_load(self.rid))]

    def test_fresh_confirmation_is_not_pending(self):
        self.assertEqual(self.pending(), ["CHK-H"])
        self.verify()
        self.assertEqual(self.pending(), [])

    def test_failed_human_evidence_is_pending(self):
        chk = dict(HUMAN, repo="React")
        record = core.write_evidence(self.rid, chk, {"status": "fail", "summary": "no"}, self.wt, core.now(), 0.0)

        def mutate(run):
            core.evidence_event(run, record)
        core.run_update(self.rid, mutate)
        self.assertIsNone(integrity.evidence_error(core.run_load(self.rid), "CHK-H"))  # intact, but not a pass
        self.assertEqual(self.pending(), ["CHK-H"])

    def test_forged_human_evidence_is_pending(self):
        self.verify()
        path = self.rd / "evidence" / "CHK-H.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["summary"] = "forged"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.pending(), ["CHK-H"])

    def test_stale_human_evidence_after_a_new_commit_is_pending(self):
        self.verify()
        self.assertEqual(self.pending(), [])
        commit_file(self.wt, "src/later.txt", "later\n", "[Area] - later")
        self.assertEqual(self.pending(), ["CHK-H"])

    def test_dirty_worktree_makes_it_pending(self):
        self.verify()
        (self.wt / "src").mkdir(exist_ok=True)
        (self.wt / "src" / "scratch.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(self.pending(), ["CHK-H"])


class UpdateDescriptionProofTests(IntegrityCase):
    """F11: `ship update-description` refuses the same stale/invalid proof `ship plan` refuses."""

    def planned(self):
        self.reviewed()
        plan = json.loads(self.ok("--json", "ship", "plan", self.rid))
        self.readbacks(plan)
        return plan

    def test_fresh_proof_is_accepted(self):
        self.planned()
        self.assertEqual(cli("ship", "update-description", self.rid)[0], 0)

    def test_tampered_evidence_is_refused(self):
        self.planned()
        path = self.rd / "evidence" / "CHK-1.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"pass"', '"fail"', 1), encoding="utf-8")
        rc, out = cli("ship", "update-description", self.rid)
        self.assertEqual(rc, 1, out)
        self.assertIn("CHK-1", out)

    def test_commit_after_prove_is_refused(self):
        self.planned()
        commit_file(self.wt, "src/more.txt", "more\n", "[Area] - more")
        rc, out = cli("ship", "update-description", self.rid)
        self.assertEqual(rc, 1, out)
        self.assertIn("stale", out)


if __name__ == "__main__":
    unittest.main()
