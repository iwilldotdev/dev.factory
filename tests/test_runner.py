"""The parallel test runner: hard timeout per module, budget, listing from any cwd."""
import re
import subprocess
import sys
import unittest

from tests.helpers import ROOT, lesson, tmpdir

RUNNER = ROOT / "tests" / "run_tests.py"


def run_runner(*args, cwd=None):
    return subprocess.run([sys.executable, "-P", str(RUNNER), *args], cwd=cwd or tmpdir(), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)


def fixture_dir(**modules: str):
    d = tmpdir()
    for name, body in modules.items():
        (d / f"test_{name}.py").write_text(body, encoding="utf-8")
    return d


PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\nunittest.main()\n"
HANGING = "import time\ntime.sleep(60)\n"
FAILING = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): self.fail('boom')\nunittest.main()\n"


class RunnerTests(unittest.TestCase):
    @lesson("L-44")
    def test_a_hanging_module_is_killed_at_the_hard_timeout_and_reported(self):
        """L-44: every test module has a hard timeout; a hang is reported, never waited on."""
        d = fixture_dir(ok=PASSING, hang=HANGING)
        res = run_runner("--tests-dir", str(d), "--timeout", "3", "--out", str(d / "out"))
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertRegex(res.stdout, r"TIMEOUT\s+test_hang")
        self.assertRegex(res.stdout, r"ok\s+test_ok\s+2 tests")
        self.assertLess(float(re.search(r"([\d.]+)s wall", res.stdout).group(1)), 30)

    @lesson("L-174")
    def test_full_output_of_every_module_goes_to_a_file_and_failures_show_their_tail(self):
        """L-174: the runner always writes full output to a file, so a failure keeps its name."""
        d = fixture_dir(bad=FAILING)
        res = run_runner("--tests-dir", str(d), "--out", str(d / "out"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("boom", res.stdout)
        self.assertIn("boom", (d / "out" / "test_bad.log").read_text())
        self.assertIn("FAIL", res.stdout)

    @lesson("L-173")
    def test_the_budget_fails_the_run_and_the_summary_reports_slowest_modules_and_total(self):
        """L-173: the suite has a time budget; the runner prints the slowest modules and the wall time."""
        d = fixture_dir(a=PASSING)
        res = run_runner("--tests-dir", str(d), "--out", str(d / "out"), "--budget", "0.001")
        self.assertEqual(res.returncode, 1)
        self.assertIn("took longer than its", res.stdout)
        res = run_runner("--tests-dir", str(d), "--out", str(d / "out"))
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("slowest:", res.stdout)
        self.assertRegex(res.stdout, r"2 tests in 1 modules, [\d.]+s wall")

    @lesson("L-141")
    def test_the_runner_lists_modules_from_any_cwd(self):
        """L-141: the suite does not depend on the cwd."""
        res = run_runner("--list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("test_structure", res.stdout.split())

    def test_an_empty_selection_is_a_failure(self):
        d = fixture_dir()
        self.assertEqual(run_runner("--tests-dir", str(d), "--out", str(d / "out")).returncode, 1)


if __name__ == "__main__":
    unittest.main()
