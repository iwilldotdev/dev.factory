"""Blocker 4: a reproduction is a real assertion failure, with identical setup on both sides."""

from __future__ import annotations

import unittest

from factory.commands import prove
from tests.integrity_support import PY, IntegrityCase, cli, commit_file, core, sh

# exit 3 + "NO SETUP" when quality.setup did not run; exit 1 + "ASSERT" when the bug is present
CHECK = ("import os, sys, mod\n"
         "if not os.path.exists('ready.txt'):\n    print('NO SETUP')\n    sys.exit(3)\n"
         "if mod.v != 2:\n    print('ASSERT v == 2')\n    sys.exit(1)\n")
SETUP = f'{PY} -c "open(\'ready.txt\', \'w\').write(\'ok\')"'
DIAGNOSIS = "## Root cause\nx\n\n## Evidence\ny\n\n## Ruled out\nz\n"


class ReproCase(IntegrityCase):
    def setUp(self) -> None:
        super().setUp()
        repo = self.repo_dirs["React"]
        commit_file(repo, "mod.py", "v = 1\n")
        commit_file(repo, "check.py", CHECK)
        commit_file(repo, ".gitignore", "ready.txt\n")
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
        self.start("BUG-1", bug=True)

    def bug_acs(self, pattern="ASSERT v", rg_pattern=None):
        chk1 = {"id": "CHK-1", "type": "test", "repo": "React", "cmd": f"{PY} check.py"}
        if pattern:
            chk1["expect_fail_pattern"] = pattern
        chk2 = {"id": "CHK-2", "type": "red-green", "repo": "React", "of": "CHK-1", "test_files": ["check.py"]}
        if rg_pattern:
            chk2["expect_fail_pattern"] = rg_pattern
        self.acs([chk1, chk2], [{"id": "AC-1", "ticket": "BUG-1", "text": "v", "kind": "bug", "checks": ["CHK-1", "CHK-2"]}])

    def with_setup(self, setup=SETUP):
        self.ws["repos"][0]["quality"] = {"setup": setup}
        self.save_ws()

    def repro(self) -> dict:
        cli("prove", self.rid, "--repro")
        return core.read_json(self.rd / "evidence" / "REPRO-CHK-1.json")


class PlanRequiresPatternTests(ReproCase):
    def test_bug_test_check_and_red_green_need_expect_fail_pattern(self):
        self.bug_acs(pattern=None)
        self.ok("gate", self.rid, "intake")
        rc, out = self.gate("plan")
        self.assertEqual(rc, 1)
        self.assertIn("CHK-1 needs expect_fail_pattern", out)
        self.assertIn("CHK-2: red-green needs expect_fail_pattern", out)
        self.bug_acs(pattern="([")
        rc, out = self.gate("plan")
        self.assertIn("not a valid regex", out)
        self.bug_acs()
        self.ok("gate", self.rid, "plan")


class SetupFailureIsNotReproductionTests(ReproCase):
    def test_exit_without_pattern_is_error_not_reproduced(self):
        self.bug_acs()  # no quality.setup: check exits 3 "NO SETUP"
        ev = self.repro()
        self.assertEqual(ev["status"], "error")
        self.assertIn("could not reproduce: setup failure", ev["summary"])

    def test_setup_runs_in_temp_worktree_then_reproduces(self):
        self.with_setup()
        self.bug_acs()
        ev = self.repro()
        self.assertEqual(ev["status"], "reproduced", ev["summary"])

    def test_missing_pattern_refused_at_repro(self):
        self.with_setup()
        self.bug_acs(pattern=None)
        ev = self.repro()
        self.assertEqual(ev["status"], "error")
        self.assertIn("expect_fail_pattern", ev["summary"])


class RedGreenSetupTests(ReproCase):
    def fix(self):
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")

    def test_both_sides_get_setup_and_pass(self):
        self.with_setup()
        self.bug_acs()
        self.fix()
        rc, out = cli("prove", self.rid, "--check", "CHK-2")
        self.assertEqual(rc, 0, out)

    def test_red_side_setup_failure_is_error(self):
        self.bug_acs()  # no setup: red exits 3 without the pattern
        self.fix()
        cli("prove", self.rid, "--check", "CHK-2")
        ev = core.load_evidence(self.rid, "CHK-2")
        self.assertEqual(ev["status"], "error")

    def test_cmd_red_green_red_side_needs_pattern(self):
        self.acs([{"id": "CHK-2", "type": "red-green", "repo": "React", "cmd": f"{PY} check.py",
                   "test_files": ["check.py"], "expect_fail_pattern": "ASSERT v"}],
                 [{"id": "AC-1", "ticket": "BUG-1", "text": "v", "kind": "bug", "checks": ["CHK-2"]}])
        self.fix()
        cli("prove", self.rid, "--check", "CHK-2")
        ev = core.load_evidence(self.rid, "CHK-2")
        self.assertEqual(ev["status"], "error")
        self.assertIn("red side: could not reproduce: setup failure", ev["summary"])
        self.with_setup()
        rc, out = cli("prove", self.rid, "--check", "CHK-2")
        self.assertEqual(rc, 0, out)

    def test_failed_setup_command_is_error(self):
        self.with_setup(f'{PY} -c "import sys; sys.exit(7)"')
        self.bug_acs()
        ev = self.repro()
        self.assertEqual(ev["status"], "error")
        self.assertIn("setup", ev["summary"])

    def test_quality_setup_is_not_a_quality_command(self):
        from factory.commands.prove import run_quality
        out = run_quality(self.wt, {"setup": SETUP, "lint": f'{PY} -c "pass"'}, None)
        self.assertEqual(list(out), ["lint"])


class RuntimeReproTests(unittest.TestCase):
    def test_browser_failure_must_be_an_assertion(self):
        net = {"status": "fail", "details": {"tests": [
            {"title": "t", "ok": False, "error": "page.goto: net::ERR_CONNECTION_REFUSED at http://x"}]}}
        self.assertIn("without an assertion", prove.runtime_failure_error("browser", {}, net))
        real = {"status": "fail", "details": {"tests": [
            {"title": "t", "ok": False, "error": "Error: expect(locator).toHaveText(expected) failed"}]}}
        self.assertIsNone(prove.runtime_failure_error("browser", {}, real))
        self.assertIn("without a failed test", prove.runtime_failure_error("browser", {}, {"status": "fail", "details": {}}))
        self.assertIn("expect_fail_pattern",
                      prove.runtime_failure_error("browser", {"expect_fail_pattern": "Total: 10"}, real))

    def test_api_failure_must_be_a_response_mismatch(self):
        self.assertIsNotNone(prove.runtime_failure_error("api", {}, {"status": "fail", "details": {}}))
        mismatch = {"status": "fail", "details": {"http_status": 200, "problems": ["$.total: 9 != 10"]}}
        self.assertIsNone(prove.runtime_failure_error("api", {}, mismatch))
        self.assertIsNone(prove.runtime_failure_error("api", {}, {"status": "pass"}))


if __name__ == "__main__":
    unittest.main()
