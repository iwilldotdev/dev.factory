"""`cmd` checks: classification of exit codes, markers and patterns, caps, tree kill, stdin and output tails."""
import json
import os
import time
import unittest
from unittest import mock

from dev_factory.checks.cmd import classify
from tests.helpers import PY, lesson
from tests.m2_support import M2Case, cmd

CHK = {"id": "CHK-1", "expect_exit": 0, "expect_pattern": None, "timeout_s": 5}
REPRO = {**CHK, "reproduce": {"expect_fail_pattern": "AssertionError: total"}}


class ClassifyTests(unittest.TestCase):
    @lesson("L-02")
    def test_zero_tests_is_setup_never_pass(self):
        """L-02: a run that executed zero tests is `setup`, whatever the exit code says."""
        for text in ("Ran 0 tests in 0.000s\n\nOK", "collected 0 items", "No tests found", "no tests ran in 0.01s", "Tests: 0 total",
                     "0 tests executed", "no specs found"):
            for code in (0, 1, 5):
                self.assertEqual(classify(CHK, code, False, text, "verify")[:2], ("setup", "setup"), (text, code))
        self.assertEqual(classify(CHK, 0, False, "Ran 12 tests in 0.5s\n\nOK", "verify")[0], "pass")

    @lesson("L-03")
    def test_setup_exit_codes_and_markers_are_setup_and_never_a_reproduction(self):
        """L-03: 126/127/9009 and shell or module-not-found text are `setup`; reproduce never counts them."""
        for code in (126, 127, 9009):
            self.assertEqual(classify(CHK, code, False, "", "verify")[0], "setup")
            self.assertEqual(classify(REPRO, code, False, "AssertionError: total", "reproduce")[0], "setup")
        for text in ("sh: foo: command not found", "'foo' is not recognized as an internal or external command", "Error: Cannot find module 'x'",
                     "bash: ./run.sh: No such file or directory", "'foo' não é reconhecido como um comando interno", "comando não encontrado"):
            self.assertEqual(classify(CHK, 1, False, text, "verify")[:2], ("setup", "setup"), text)
            self.assertEqual(classify(REPRO, 1, False, text, "reproduce")[0], "setup", text)
        self.assertEqual(classify(CHK, 127, False, "", "verify", )[0], "setup")
        self.assertEqual(classify({**CHK, "expect_exit": 127}, 127, False, "", "verify")[0], "pass")

    @lesson("L-05")
    def test_a_failure_without_the_expected_text_is_not_a_reproduction(self):
        """L-05: reproduced needs a non-setup failure whose output matches expect_fail_pattern."""
        self.assertEqual(classify(REPRO, 1, False, "AssertionError: total 10 != 12", "reproduce")[0], "reproduced")
        self.assertEqual(classify(REPRO, 1, False, "Traceback: ImportError boom", "reproduce")[0], "not_reproduced")
        self.assertEqual(classify(REPRO, 0, False, "AssertionError: total", "reproduce")[0], "not_reproduced")
        self.assertEqual(classify(REPRO, 1, False, "AssertionError: total", "red")[0], "reproduced")
        self.assertEqual(classify(REPRO, 1, False, "No such file or directory: AssertionError: total", "reproduce")[0], "reproduced")

    @lesson("L-45")
    def test_the_exit_code_decides_first_and_the_pattern_is_secondary(self):
        """L-45: a matching pattern never turns a failing exit into a pass; a missing pattern fails a passing exit."""
        pat = {**CHK, "expect_pattern": "all green"}
        self.assertEqual(classify(pat, 1, False, "all green", "verify")[:2], ("fail", "assert"))
        self.assertEqual(classify(pat, 0, False, "nothing", "verify")[:2], ("fail", "assert"))
        self.assertEqual(classify(pat, 0, False, "all green", "verify")[0], "pass")
        self.assertEqual(classify({**CHK, "expect_exit": 3}, 3, False, "", "verify")[0], "pass")
        self.assertEqual(classify({**CHK, "expect_exit": 3}, 0, False, "", "verify")[0], "fail")

    @lesson("L-42")
    def test_a_timeout_is_its_own_result(self):
        """L-42: a killed check is `timeout`, not a failure of the assertion."""
        self.assertEqual(classify(CHK, None, True, "", "verify")[:2], ("timeout", "timeout"))


class ProcessTests(M2Case):
    def setUp(self):
        super().setUp()
        self.run_dir = self.prepare()
        self.fix()

    def one(self, plan_check: dict | None = None, *flags: str, code: int | None = None) -> dict:
        """Replace CHK-1 (amend) and run it once with verify --json; returns the single result."""
        plan = self.read_plan(self.run_dir)
        plan["checks"][0] = {"id": "CHK-1", "type": "cmd", "repo": "web", "ac": ["AC-1"], **(plan_check or {})}
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "scenario")
        got, out, err = self.cli("verify", "--json", "--no-retry", *flags)
        if code is not None:
            self.assertEqual(got, code, out + err)
        return json.loads(out)["results"][0]

    @lesson("L-42", "L-43")
    def test_a_check_past_its_cap_is_killed_with_its_whole_process_tree(self):
        """L-42, L-43: the cap kills the shell and every process it started; the result is `timeout`."""
        marker = self.base / "grandchild.txt"
        start = time.monotonic()
        res = self.one({"run": f'"{PY}" spawn.py "{marker.as_posix()}"', "timeout_s": 1}, code=1)
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual((res["result"], res["kind"]), ("timeout", "timeout"), res)
        time.sleep(3.5)
        self.assertFalse(marker.exists(), "the grandchild survived the timeout")
        rec = self.evidence(self.run_dir, result="timeout")[0]
        self.assertIsNone(rec["exit"])
        self.assertEqual([e["attempt"] for e in self.evidence(self.run_dir, check="CHK-1")], [1])

    def test_without_a_check_cap_the_project_default_applies(self):
        self.write("factory.toml", self.TOML + "\n[checks]\ncmd_timeout_s = 1\n")
        self.ok("plan", "amend", "--reason", "default cap")
        res = self.one({"run": cmd("import time; time.sleep(30)")}, code=1)
        self.assertEqual(res["result"], "timeout")
        self.assertIn("killed after 1s", res["reason"])

    @lesson("L-44")
    def test_stdin_is_closed_so_a_check_that_reads_it_gets_end_of_file(self):
        """L-44: a check that waits for input sees EOF at once instead of hanging."""
        res = self.one({"run": cmd("import sys; data = sys.stdin.read(); print('eof', repr(data), 'finished')"), "expect_pattern": "eof '' finished"}, code=0)
        self.assertEqual(res["result"], "pass")

    @lesson("L-03")
    def test_a_missing_command_is_setup_not_a_failure(self):
        """L-03: a command that does not exist is recorded as setup with the setup kind."""
        res = self.one({"run": "factory-test-no-such-command --flag"}, code=1)
        self.assertEqual((res["result"], res["kind"]), ("setup", "setup"))
        self.assertEqual(self.evidence(self.run_dir, result="setup")[0]["kind"], "setup")

    @lesson("L-02")
    def test_zero_tests_in_a_real_run_is_setup(self):
        """L-02: a command that exits 0 after running no tests is setup, so it cannot pass the gate."""
        res = self.one({"run": cmd("print('Ran 0 tests in 0.000s')")}, code=1)
        self.assertEqual((res["result"], res["kind"]), ("setup", "setup"))
        self.assertIn("zero tests", res["reason"])

    def test_the_tail_is_capped_in_lines_and_bytes_and_secrets_are_redacted(self):
        self.write("factory.toml", self.TOML + '\n[secrets]\nkeys = ["DEMO_TOKEN"]\n')
        with mock.patch.dict(os.environ, {"DEMO_TOKEN": "s3cr3t-value"}):
            self.one({"run": cmd("print('token s3cr3t-value'); [print('line', n, 'x' * 150) for n in range(300)]"),
                      "expect_pattern": "line 299"}, code=0)
        rec = self.evidence(self.run_dir, check="CHK-1")[-1]
        self.assertLessEqual(len(rec["tail"].splitlines()), 40)
        self.assertLessEqual(len(rec["tail"].encode("utf-8")), 4096)
        full = (self.run_dir / rec["artifacts"][0]).read_text(encoding="utf-8")
        self.assertIn("line 0 ", full)
        self.assertIn("token ***", full)
        self.assertNotIn("s3cr3t-value", full + json.dumps(rec))

    def test_a_cwd_inside_the_repo_is_honoured(self):
        self.commit("sub/marker.txt", "x", "feat: sub")
        res = self.one({"run": cmd("import os; print('cwd', os.path.basename(os.getcwd()))"), "cwd": "sub", "expect_pattern": "cwd sub"}, code=0)
        self.assertEqual(res["result"], "pass")


if __name__ == "__main__":
    unittest.main()
