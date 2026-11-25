"""Plan lock drift, `plan amend` and the loop budget."""
import json
import sys
import unittest

from dev_factory.run import Run
from tests.helpers import lesson
from tests.m2_support import M2Case


class DriftTests(M2Case):
    def setUp(self):
        super().setUp()
        self.run_dir = self.prepare()

    def change_check(self, **over):
        plan = self.read_plan(self.run_dir)
        plan["checks"][0].update(over)
        self.write_plan(self.run_dir, plan)

    @lesson("L-132", "L-58")
    def test_declared_check_files_are_locked_and_undeclared_files_are_ignored(self):
        """L-132, L-58: only declared spec and fixture files join the lock; other files in the run directory do not."""
        run = self.run_dir
        (run / "checks").mkdir()
        (run / "checks" / "data.txt").write_text("one")
        self.change_check(files=["checks/data.txt"])
        self.ok("plan", "amend", "--reason", "declare the fixture")
        lock = [e for e in self.events(run) if e["type"] == "plan_locked"][-1]
        self.assertEqual(list(lock["files"]), ["checks/data.txt"])
        (run / "checks" / "other.txt").write_text("noise")
        (run / "artifacts").mkdir(exist_ok=True)
        (run / "artifacts" / "out.txt").write_text("output")
        self.assertEqual(self.cli("gate", "plan")[0], 0)
        (run / "checks" / "data.txt").write_text("two")
        code, out, _ = self.cli("gate", "plan", "--json")
        self.assertEqual(code, 1)
        self.assertIn("checks/data.txt changed after lock", out)

    @lesson("L-131")
    def test_a_proof_relevant_config_change_fails_with_config_changed_after_plan(self):
        """L-131: the lock hashes commands and caps; editing them fails the gates until an amend."""
        self.fix()
        self.write("factory.toml", self.TOML + "\n[checks]\ncmd_timeout_s = 9999\n")
        self.assertIn("config changed after plan", self.failing("verify"))
        self.ok("plan", "amend", "--reason", "new default cap")
        self.assertEqual(self.gate("plan")[0], 0)

    def test_an_amend_that_only_changes_standing_checks_is_upkeep_and_spends_no_loop(self):
        self.write("factory.toml", self.TOML + '\n[[repo.check]]\nid = "lint"\nrun = "python -c pass"\n')
        out = self.ok("plan", "amend", "--reason", "standing lint already fails at the branch point")
        self.assertIn("web:lint", out)
        self.assertIn("loops used: 0/3", self.ok("status"))

    def test_a_checks_only_amend_that_closes_open_minor_findings_spends_no_loop(self):
        self.run_dir.joinpath("notes").mkdir(exist_ok=True)
        Run(self.proj / ".factory" / "runs", "ABC-1").append({"type": "review", "kind": "qa", "round": 1, "verdict": "APPROVE", "reviewer": "r",
                                                               "repos": {}, "findings": [{"id": "F1", "severity": "minor", "text": "the check is weak"}],
                                                               "resolved": [], "completeness": {}, "bound": {}, "lock_seq": 1, "independent_of": []})
        self.change_check(expect_pattern="checks passed: VALUE=2$")
        self.assertIn("not an open minor finding", self.fails("plan", "amend", "--reason", "x", "--resolves", "1:F9"))
        self.assertIn("closes 1:F1, no loop spent", self.ok("plan", "amend", "--reason", "stronger check", "--resolves", "1:F1"))
        self.assertIn("loops used: 0/3", self.ok("status"))

    def test_a_standing_check_runs_with_its_vars_like_a_ci_job(self):
        self.fix()
        probe = "import os,sys; sys.exit(0 if os.environ.get('FACTORY_T') == 'on' else 3)"
        self.write("factory.toml", self.TOML + f'\n[[repo.check]]\nid = "env"\nrun = {json.dumps(f"{sys.executable} -c " + json.dumps(probe))}\nvars = {{ FACTORY_T = "on" }}\n')
        self.ok("plan", "amend", "--reason", "a standing check with vars")
        self.assertIn("web:env", self.ok("verify"))
        self.assertIn("pass", next(e for e in self.events(self.run_dir) if e.get("check") == "web:env")["result"])

    def test_machine_facts_in_local_toml_do_not_move_the_lock(self):
        self.write(".factory/local.toml", f'worktree_root = {json.dumps(str(self.base / "elsewhere"))}\n')
        self.assertEqual(self.gate("plan")[0], 0)

    def test_a_contract_acquired_after_the_lock_is_drift(self):
        self.ingest_ticket(self.run_dir, title="A different title")
        self.assertIn("gate intake has not passed", self.failing("plan"))
        self.ok("gate", "intake")
        self.assertIn("a contract was acquired or replaced after the plan lock", self.failing("plan"))


class AmendTests(M2Case):
    def setUp(self):
        super().setUp()
        self.run_dir = self.prepare()

    def retarget(self, pattern="checks passed: VALUE=2$"):
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["expect_pattern"] = pattern
        self.write_plan(self.run_dir, plan)

    def locks(self):
        return [e for e in self.events(self.run_dir) if e["type"] == "plan_locked"]

    @lesson("L-134")
    def test_an_amend_records_changed_ids_reason_and_decision_and_counters_come_from_events(self):
        """L-134: each amend event records the changed AC and check ids; the counters are derived from the events."""
        self.retarget()
        out = self.ok("plan", "amend", "--reason", "pattern was too loose")
        self.assertIn("changed checks: CHK-1", out)
        lock = self.locks()[-1]
        self.assertEqual(lock["seq"], 2)
        self.assertEqual(lock["amend"], {"reason": "pattern was too loose", "decision": None, "changed_acs": [],
                                         "changed_checks": ["CHK-1"], "counts_loop": True})
        self.assertEqual([e["counts"] for e in self.events(self.run_dir) if e["type"] == "loop"], [True])
        self.assertIn("loops used: 1/3", self.ok("status"))

    @lesson("L-137")
    def test_a_checks_only_amend_keeps_the_core_hash_and_an_ac_change_moves_it(self):
        """L-137: the code review binds to the plan without checks, so a checks-only amend leaves it valid."""
        before = self.locks()[0]
        self.retarget()
        self.ok("plan", "amend", "--reason", "checks only")
        after = self.locks()[-1]
        self.assertEqual(after["core_hash"], before["core_hash"])
        self.assertNotEqual(after["plan_hash"], before["plan_hash"])
        plan = self.read_plan(self.run_dir)
        plan["acs"][0]["text"] = "VALUE is exactly 2"
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "the AC wording changed")
        last = self.locks()[-1]
        self.assertNotEqual(last["core_hash"], before["core_hash"])
        self.assertEqual(last["amend"]["changed_acs"], ["AC-1"])

    def test_amend_needs_a_reason_a_change_a_valid_plan_and_a_lock(self):
        self.fails("plan", "amend", "--reason", "  ")
        self.assertIn("nothing changed", self.fails("plan", "amend", "--reason", "why not"))
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["expect_pattern"] = "."
        self.write_plan(self.run_dir, plan)
        self.assertIn("not valid yet", self.fails("plan", "amend", "--reason", "weak"))
        self.assertEqual(len(self.locks()), 1)

    def test_an_unrecorded_decision_is_refused(self):
        self.retarget()
        self.assertIn("not recorded", self.fails("plan", "amend", "--reason", "x", "--decision", "Q-5"))


class BudgetTests(M2Case):
    def setUp(self):
        super().setUp()
        self.write("factory.toml", self.TOML + "\n[autonomy]\nmax_loops = 2\n")
        self.run_dir = self.prepare()
        self.events_run = Run(self.proj / ".factory" / "runs", "ABC-1")

    def retarget(self, pattern):
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["expect_pattern"] = pattern
        self.write_plan(self.run_dir, plan)

    @lesson("L-135", "L-136")
    def test_one_budget_covers_amends_and_review_loops_and_a_decision_exempts_one_loop_once(self):
        """L-135, L-136: past the cap the amend refuses and blocks once; a recorded decision is consumed once and exempts one loop."""
        self.events_run.append({"type": "loop", "kind": "review", "decision": None, "counts": True})
        self.retarget("checks passed: VALUE=2$")
        self.ok("plan", "amend", "--reason", "second counted loop")
        self.retarget("checks passed: VALUE=2 *$")
        for _ in range(2):
            self.assertIn("loop budget exhausted: 2 of 2", self.fails("plan", "amend", "--reason", "third", code=1))
        self.assertEqual(len([e for e in self.events(self.run_dir) if e["type"] == "blocker"]), 1)
        self.assertIn("blocked by B-1 (decision)", self.ok("next"))
        self.ok("ask", "Raise the loop cap for this ticket?")
        self.ok("answer", "Q-1", "--user-said", "yes, one more")
        self.ok("plan", "amend", "--reason", "approved by the user", "--decision", "Q-1")
        loops = [e for e in self.events(self.run_dir) if e["type"] == "loop"]
        self.assertEqual([e["counts"] for e in loops], [True, True, False])
        self.assertEqual(loops[-1]["decision"], "Q-1")
        self.assertFalse(any(b["open"] for b in self.events_run.state()["blockers"].values()))
        self.retarget("checks passed: VALUE=2 *")
        self.assertIn("already consumed", self.fails("plan", "amend", "--reason", "reuse", "--decision", "Q-1"))


if __name__ == "__main__":
    unittest.main()
