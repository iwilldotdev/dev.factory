"""UI bug ACs: a runtime check (reproduced on base, passing at HEAD) replaces red-green (DESIGN section 8)."""

from __future__ import annotations

import json
import unittest

from real_support import RealCase, cli, commit_file, core, sh


class UiBugCase(RealCase):
    repos = ("React",)

    def setUp(self) -> None:
        super().setUp()
        repo = self.repo_dirs["React"]
        commit_file(repo, "mod.py", "v = 1\n")
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
        self.rid = "ws-bug-1"
        self.ok("run", "start", "ws", "BUG-1", "--bug", "BUG-1")
        self.ok("run", "branch", self.rid, "React", "--base", "main")
        self.wt = core.Path(core.run_load(self.rid)["repos"]["React"]["worktree"])
        core.write_json(core.run_dir(self.rid) / "remote" / "issue-BUG-1.json", {"key": "BUG-1"})
        (core.run_dir(self.rid) / "checks" / "browser").mkdir(parents=True)
        core.write_json(core.run_dir(self.rid) / "acs.json", {"schemaVersion": 1, "acs": [
            {"id": "AC-1", "ticket": "BUG-1", "text": "button works", "kind": "bug", "checks": ["CHK-1"]}],
            "checks": [{"id": "CHK-1", "type": "browser", "repo": "React", "spec": "checks/browser/ac-1.spec.ts"}]})

    def write_evidence(self, name, status, sha):
        core.write_json(core.run_dir(self.rid) / "evidence" / f"{name}.json", {
            "check_id": name, "status": status, "git_sha": sha, "dirty": False, "summary": "s"})

    def test_plan_accepts_runtime_check_for_bug_ac(self):
        from factory.commands import gate
        run = core.run_load(self.rid)
        errs = gate.validate_acs(self.rid, run, core.workspace_load("ws"))
        self.assertFalse([e for e in errs if "red-green" in e], errs)

    def test_plan_rejects_bug_ac_with_no_runtime_or_red_green(self):
        acs = core.read_json(core.run_dir(self.rid) / "acs.json")
        acs["checks"] = [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}]
        core.write_json(core.run_dir(self.rid) / "acs.json", acs)
        from factory.commands import gate
        errs = gate.validate_acs(self.rid, core.run_load(self.rid), core.workspace_load("ws"))
        self.assertTrue(any("bug AC needs" in e for e in errs), errs)

    def test_prove_gate_requires_repro_then_accepts(self):
        mb = sh("git", "merge-base", "HEAD", "origin/main", cwd=self.wt)
        commit_file(self.wt, "mod.py", "v = 2\n", "[Area] - fix")
        head = sh("git", "rev-parse", "HEAD", cwd=self.wt)
        self.write_evidence("CHK-1", "pass", head)
        run = core.run_load(self.rid)
        from factory.commands import gate
        errs = gate._prove(self.rid, run, core.workspace_load("ws"))
        self.assertTrue(any("REPRO" in e for e in errs), errs)
        self.write_evidence("REPRO-CHK-1", "not-reproduced", mb)
        errs = gate._prove(self.rid, run, core.workspace_load("ws"))
        self.assertTrue(any("not-reproduced" in e for e in errs), errs)
        self.write_evidence("REPRO-CHK-1", "reproduced", mb)
        self.assertEqual(gate._prove(self.rid, run, core.workspace_load("ws")), [])

    def test_repro_browser_runs_with_assume_env_path(self):
        rc, out = cli("--json", "prove", self.rid, "--repro")
        self.assertEqual(rc, 1, out)  # no --assume-env: error status, never reproduced
        self.assertIn("--assume-env", json.loads(out[out.index("["):])[0]["summary"])


if __name__ == "__main__":
    unittest.main()
