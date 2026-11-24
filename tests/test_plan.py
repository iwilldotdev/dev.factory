"""plan.json validation, the plan gate and the lock content."""
import json
import unittest

from dev_factory import plan as planmod
from tests.helpers import PY, lesson
from tests.m2_support import M2Case, check_cmd


class PatternTests(unittest.TestCase):
    @lesson("L-04")
    def test_weak_patterns_are_rejected_and_strong_ones_accepted(self):
        """L-04: a pattern under 6 characters or with fewer than 4 literal word characters cannot match just any failure."""
        for weak in (".", ".*", r"\w+", "fail", r"\d{4}\s+", "[a-z]+.*", r"(?i).{6}", r"\.\.\.\.\.\."):
            self.assertIsNotNone(planmod.pattern_problem(weak, "p"), weak)
        for strong in ("AssertionError: VALUE=1", r"expected 120\.00", "(?i)totals? differ", "Error.*overdue"):
            self.assertIsNone(planmod.pattern_problem(strong, "p"), strong)
        self.assertIn("not a valid regular expression", planmod.pattern_problem("(unclosed", "p"))


class PlanGateTests(M2Case):
    def setUp(self):
        super().setUp()
        self.run_dir = self.prepare(lock=False)

    def edit(self, fn):
        plan = self.read_plan(self.run_dir)
        fn(plan)
        self.write_plan(self.run_dir, plan)
        return self.failing("plan")

    @lesson("L-04")
    def test_the_plan_gate_rejects_a_weak_expect_pattern(self):
        """L-04: `gate plan` names the weak pattern."""
        text = self.edit(lambda p: p["checks"][0].update(expect_pattern=".*"))
        self.assertIn("too weak", text)
        self.assertIn("expect_pattern", text)

    @lesson("L-01")
    def test_an_ac_without_a_check_fails_the_plan_gate(self):
        """L-01: every AC needs a check that executes it; nothing is passed from reading code."""
        text = self.edit(lambda p: p["acs"].append({"id": "AC-2", "text": "Another behaviour", "source": "ticket:description"}))
        self.assertIn("plan.acs[AC-2]: no check names it", text)

    def test_every_structural_problem_is_listed_at_once(self):
        def bad(p):
            p["checks"][0]["bogus"] = 1
            p["checks"].append({"id": "CHK-1", "type": "quality", "repo": "api", "ac": ["AC-9"]})
            p["risk"] = "extreme"
        text = self.edit(bad)
        for part in ("unknown key 'bogus'", "duplicate id CHK-1", "quality checks arrive in a later milestone", "'api' is not in plan.repos",
                     "AC-9 is not an AC", "plan.risk"):
            self.assertIn(part, text)

    @lesson("L-31")
    def test_the_plan_kind_must_equal_the_run_kind(self):
        """L-31: a bug plan in a feature run is refused."""
        text = self.edit(lambda p: p.update(kind="bug"))
        self.assertIn("plan.kind is bug but the run kind is feature", text)

    def test_cwd_cannot_leave_the_repo_and_files_must_exist_under_checks(self):
        def bad(p):
            p["checks"][0].update(cwd="../x", files=["checks/missing.txt"], timeout_s=0)
        text = self.edit(bad)
        self.assertIn(".cwd: must be a path inside the repo", text)
        self.assertIn("checks/missing.txt", text)
        self.assertIn("timeout_s", text)

    def test_an_open_question_blocks_the_plan_gate(self):
        self.ok("ask", "Which locale?")
        self.assertIn("question Q-1 is still open", self.failing("plan"))
        self.ok("answer", "Q-1", "--user-said", "pt-BR")
        self.assertEqual(self.gate("plan")[0], 0)

    def test_amend_before_the_first_lock_is_refused(self):
        self.assertIn("no plan lock yet", self.fails("plan", "amend", "--reason", "x"))

    def test_a_missing_plan_names_the_path_and_the_example_command(self):
        (self.run_dir / "plan.json").unlink()
        text = self.failing("plan")
        self.assertIn("plan.json is missing", text)
        self.assertIn("factory plan example", text)
        example = json.loads(self.ok("plan", "example"))
        self.assertEqual((example["schema"], example["kind"]), (1, "feature"))

    def test_the_plan_gate_locks_once_and_the_lock_has_every_field(self):
        self.assertEqual(self.gate("plan")[0], 0)
        self.assertEqual(self.gate("plan")[0], 0)
        locks = [e for e in self.events(self.run_dir) if e["type"] == "plan_locked"]
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        for key in ("plan_hash", "core_hash", "acs", "spec_hashes", "files", "config_hash", "contracts", "branch_points", "seq"):
            self.assertIn(key, lock)
        self.assertEqual(lock["seq"], 1)
        self.assertEqual(sorted(lock["spec_hashes"]), ["CHK-1"])
        self.assertEqual(list(lock["contracts"]), ["contracts/ticket/ABC-1.json"])
        self.assertEqual(len(lock["branch_points"]["web"]), 40)
        self.assertNotEqual(lock["plan_hash"], lock["core_hash"])

    @lesson("L-167")
    def test_hashes_are_computed_over_canonical_json_not_file_layout(self):
        """L-167: re-ordering keys or re-indenting plan.json does not move the lock."""
        self.ok("gate", "plan")
        plan = self.read_plan(self.run_dir)
        reordered = {k: plan[k] for k in reversed(list(plan))}
        (self.run_dir / "plan.json").write_text(json.dumps(reordered, indent=4), encoding="utf-8")
        self.assertEqual(self.gate("plan")[0], 0)

    def test_standing_checks_and_the_commit_check_join_the_effective_checks(self):
        self.write("factory.toml", self.TOML.replace('name = "web"\n', 'name = "web"\ncommit_pattern = "^(feat|fix): .+"\n')
                   + f'\n[[repo.check]]\nid = "lint"\nrun = {json.dumps(check_cmd(1))}\n')
        self.ok("gate", "plan")
        lock = [e for e in self.events(self.run_dir) if e["type"] == "plan_locked"][0]
        self.assertEqual(sorted(lock["spec_hashes"]), ["CHK-1", "web:commits", "web:lint"])


class WorktreeGateTests(M2Case):
    @lesson("L-06")
    def test_a_failing_worktree_setup_blocks_the_plan_and_is_never_a_result(self):
        """L-06: a failing `setup` is an error that blocks, recorded with its exit code and tail."""
        self.write("factory.toml", self.TOML.replace('name = "web"\n', f'name = "web"\nsetup = {json.dumps(f"{PY} -c " + chr(34) + "import sys; print(1234); sys.exit(7)" + chr(34))}\n'))
        run = self.start("ABC-1")
        self.ingest_ticket(run)
        self.ok("gate", "intake")
        code, _, err = self.cli("run", "branch")
        self.assertEqual(code, 1)
        self.assertIn("setup failed in web (exit 7)", err)
        self.write_plan(run, self.default_plan())
        text = self.failing("plan")
        self.assertIn("the setup command failed in the worktree (exit 7)", text)
        self.assertIn("1234", text)
        wt = [e for e in self.events(run) if e["type"] == "worktree"][0]
        self.assertEqual((wt["setup"], wt["setup_exit"]), ("fail", 7))

    def test_a_missing_worktree_is_named_with_the_command_that_creates_it(self):
        run = self.start("ABC-1")
        self.ingest_ticket(run)
        self.ok("gate", "intake")
        self.write_plan(run, self.default_plan())
        self.assertIn("repo web: no run worktree; run factory run branch", self.failing("plan"))


if __name__ == "__main__":
    unittest.main()
