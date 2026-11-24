"""Bug flow: reproduce phase, `prove --repro`, gate reproduce, red-green `of` (DESIGN §Bugs)."""

from __future__ import annotations

import sys
import unittest

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))  # a single-module run finds real_support

from real_support import RealCase, cli, commit_file, core, sh
from tests.ticket_support import put_ticket

PY = f'"{sys.executable}"'
CHECK = "import sys, mod\nif mod.v != 2:\n    print('ASSERT v == 2')\n    sys.exit(1)\n"
DIAGNOSIS = "## Root cause\nmod.v is 1.\n\n## Evidence\ncheck.py exits 1 on base.\n\n## Ruled out\nCaching.\n"


class BugCase(RealCase):
    repos = ("React",)
    base_v = 1

    def setUp(self) -> None:
        super().setUp()
        repo = self.repo_dirs["React"]
        commit_file(repo, "mod.py", f"v = {self.base_v}\n")
        commit_file(repo, "check.py", CHECK)
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
        self.rid = "ws-bug-1"
        self.ok("run", "start", "ws", "BUG-1", "--bug", "BUG-1")
        self.ok("run", "branch", self.rid, "React", "--base", "main")
        self.wt = core.Path(core.run_load(self.rid)["repos"]["React"]["worktree"])
        put_ticket(self.rid, "BUG-1", type="Bug", is_bug=True)
        self.write_acs()

    def write_acs(self, checks=None, acs=None):
        checks = checks or [
            {"id": "CHK-1", "type": "test", "repo": "React", "cmd": f"{PY} check.py", "expect_fail_pattern": "ASSERT v"},
            {"id": "CHK-2", "type": "red-green", "repo": "React", "of": "CHK-1", "test_files": ["check.py"]}]
        acs = acs or [{"id": "AC-1", "ticket": "BUG-1", "text": "v is 2", "kind": "bug", "checks": ["CHK-1", "CHK-2"]}]
        core.write_json(core.run_dir(self.rid) / "acs.json", {"schemaVersion": 1, "acs": acs, "checks": checks})

    def to_reproduce(self):
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")

    def diagnosis(self, text=DIAGNOSIS):
        (core.run_dir(self.rid) / "diagnosis.md").write_text(text, encoding="utf-8")

    def evidence(self):
        return core.read_json(core.run_dir(self.rid) / "evidence" / "REPRO-CHK-1.json")


class ReproTests(BugCase):
    def test_reproduced_on_base(self):
        rc, out = cli("prove", self.rid, "--repro")
        self.assertEqual(rc, 0, out)
        ev = self.evidence()
        self.assertEqual(ev["status"], "reproduced")
        mb = sh("git", "merge-base", "HEAD", "origin/main", cwd=self.wt)
        self.assertEqual(ev["git_sha"], mb)
        self.assertFalse((core.run_dir(self.rid) / "evidence" / "CHK-1.json").exists())

    def test_not_reproduced_when_check_passes_on_base(self):
        self.base_v_fix = commit_file(self.repo_dirs["React"], "mod.py", "v = 2\n")
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo_dirs["React"])
        sh("git", "reset", "-q", "--hard", "origin/main", cwd=self.wt)
        rc, out = cli("prove", self.rid, "--repro")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.evidence()["status"], "not-reproduced")

    def test_api_check_needs_assume_env(self):
        self.write_acs(
            [{"id": "CHK-1", "type": "api", "repo": "React", "path": "/x"},
             {"id": "CHK-2", "type": "red-green", "repo": "React", "cmd": "x"}])
        rc, out = cli("prove", self.rid, "--repro")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.evidence()["status"], "error")
        self.assertIn("--assume-env", self.evidence()["summary"])

    def test_repro_rejected_on_feature_run(self):
        self.ok("run", "start", "ws", "FEAT-1")
        rc, _ = cli("prove", "ws-feat-1", "--repro")
        self.assertEqual(rc, 2)


class GateReproduceTests(BugCase):
    def test_bug_run_cannot_skip_reproduce(self):
        self.to_reproduce()
        self.assertEqual(core.run_load(self.rid)["phase"], "reproduce")
        rc, out = cli("gate", self.rid, "build")
        self.assertEqual(rc, 2, out)
        self.assertIn("not build", out)

    def test_feature_run_has_no_reproduce(self):
        from factory.commands import gate
        self.assertNotIn("reproduce", gate.machine({"kind": "ticket", "tickets": [{"type": "feature"}]})[0])

    def test_requires_repro_evidence(self):
        self.to_reproduce()
        self.diagnosis()
        rc, out = cli("gate", self.rid, "reproduce")
        self.assertEqual(rc, 1)
        self.assertIn("no REPRO evidence", out)

    def test_requires_diagnosis_sections(self):
        self.to_reproduce()
        self.ok("prove", self.rid, "--repro")
        rc, out = cli("gate", self.rid, "reproduce")
        self.assertEqual(rc, 1)
        self.assertIn("diagnosis.md missing", out)
        self.diagnosis("## Root cause\nx\n\n## Evidence\n\n")
        rc, out = cli("gate", self.rid, "reproduce")
        self.assertEqual(rc, 1)
        self.assertIn("'## Evidence' is empty", out)
        self.assertIn("'## Ruled out' missing", out)
        self.diagnosis()
        out = self.ok("gate", self.rid, "reproduce")
        self.assertIn("reproduce -> build", out)
        self.assertEqual(core.run_load(self.rid)["phase"], "build")

    def test_rejects_not_reproduced_and_wrong_sha(self):
        self.to_reproduce()
        self.diagnosis()
        self.ok("prove", self.rid, "--repro")
        path = core.run_dir(self.rid) / "evidence" / "REPRO-CHK-1.json"
        ev = core.read_json(path)
        core.write_json(path, dict(ev, git_sha="0" * 40))
        rc, out = cli("gate", self.rid, "reproduce")
        self.assertEqual(rc, 1)
        self.assertIn("does not match the digest", out)  # hand-edited evidence (tests/test_integrity_digest.py)
        core.write_json(path, dict(ev, status="not-reproduced"))
        rc, out = cli("gate", self.rid, "reproduce")
        self.assertEqual(rc, 1)
        self.assertIn("does not match the digest", out)


class RedGreenOfTests(BugCase):
    def fix(self):
        commit_file(self.wt, "mod.py", "v = 2\n", "[A] - fix")

    def test_of_test_check_red_then_green(self):
        self.fix()
        rc, out = cli("prove", self.rid, "--check", "CHK-2")
        self.assertEqual(rc, 0, out)
        ev = core.load_evidence(self.rid, "CHK-2")
        self.assertEqual(ev["status"], "pass")
        self.assertEqual(ev["details"]["of"], "CHK-1")
        self.assertNotEqual(ev["details"]["red_exit"], 0)

    def test_fails_when_fix_missing(self):
        commit_file(self.wt, "other.txt", "x", "[A] - nofix")
        rc, out = cli("prove", self.rid, "--check", "CHK-2")
        self.assertEqual(rc, 1, out)
        self.assertIn("still fails", core.load_evidence(self.rid, "CHK-2")["summary"])

    def test_plan_rejects_non_test_inner(self):
        self.write_acs([{"id": "CHK-1", "type": "api", "repo": "React", "path": "/x"},
                        {"id": "CHK-2", "type": "red-green", "repo": "React", "of": "CHK-1"}])
        rc, out = cli("gate", self.rid, "intake")
        rc, out = cli("gate", self.rid, "plan")
        self.assertEqual(rc, 1)
        self.assertIn("of must reference a test check", out)


class RedGreenInnerPassesOnBase(BugCase):
    base_v = 2

    def test_fail_when_inner_passes_on_base(self):
        commit_file(self.wt, "check.py", CHECK + "# touched\n", "[A] - fix")
        rc, out = cli("prove", self.rid, "--check", "CHK-2")
        self.assertEqual(rc, 1, out)
        ev = core.load_evidence(self.rid, "CHK-2")
        self.assertEqual(ev["status"], "fail")
        self.assertIn("cannot fail", ev["summary"])


if __name__ == "__main__":
    unittest.main()
