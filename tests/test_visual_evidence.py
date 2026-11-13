"""Visual evidence on request (DESIGN section 12): the question is enforced on UI runs, a yes needs screenshot checks,
bug runs need before images on the merge-base, and ship lists the images."""

from __future__ import annotations

import json
import unittest

from real_support import RealCase, cli, commit_file, core, sh


class VisualEvidenceCase(RealCase):
    repos = ("API", "React")

    def setUp(self) -> None:
        super().setUp()
        for r in self.ws["repos"]:
            if r["alias"] == "React":
                r["env"] = {"login": {"url": "/login"}}  # a browser login form = UI repo
        self.save_ws()
        for alias in self.repos:
            commit_file(self.repo_dirs[alias], "mod.py", "v = 1\n")
            sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo_dirs[alias])

    def start(self, rid: str, key: str, *, bug: bool = False, repos=("React",)) -> None:
        self.ok("run", "start", "ws", key, *(("--bug", key) if bug else ()))
        for alias in repos:
            self.ok("run", "branch", rid, alias, "--base", "main")
        core.write_json(core.run_dir(rid) / "remote" / f"issue-{key}.json", {"key": key})

    def acs(self, rid: str, acs: list, checks: list) -> None:
        core.write_json(core.run_dir(rid) / "acs.json", {"schemaVersion": 1, "acs": acs, "checks": checks})

    def shots_errors(self, rid: str) -> list[str]:
        from factory import shots
        return shots.plan_errors(rid, core.run_load(rid), core.workspace_load("ws"))

    def decide(self, rid: str, answer: str) -> None:
        self.ok("run", "decide", rid, f"visual evidence: {answer}", "--by", "user", "--user-said", answer,
                "--visual", answer)

    def write_evidence(self, rid, name, status, sha, artifacts=()):
        """As `factory prove` does: the file plus an `evidence` event carrying its digest."""
        rec = {"check_id": name, "status": status, "git_sha": sha, "dirty": False, "summary": "s",
               "artifacts": list(artifacts)}
        core.write_json(core.run_dir(rid) / "evidence" / f"{name}.json", rec)
        run = core.run_load(rid)
        core.evidence_event(run, rec)
        core.run_save(run)

    # ---------- decision ----------

    def test_visual_needs_by_user(self):
        self.start("ws-dev-1", "DEV-1")
        rc, out = cli("run", "decide", "ws-dev-1", "screens", "--visual", "yes")
        self.assertEqual(rc, 2, out)
        self.decide("ws-dev-1", "yes")
        rec = core.run_load("ws-dev-1")["decisions"][-1]
        self.assertEqual((rec["visual"], rec["by"]), ("yes", "user"))

    # ---------- plan gate ----------

    def test_ui_run_without_answer_fails_plan_with_the_command(self):
        self.start("ws-dev-1", "DEV-1")
        self.acs("ws-dev-1", [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        from factory.commands import gate
        errs = gate._plan("ws-dev-1", core.run_load("ws-dev-1"), core.workspace_load("ws"))
        msg = [e for e in errs if e.startswith("visual evidence")]
        self.assertTrue(msg, errs)
        self.assertIn("UI repo React", msg[0])
        self.assertIn("--visual yes|no", msg[0])
        self.assertIn("screenshots of the implemented screens", msg[0])
        self.decide("ws-dev-1", "no")
        self.assertEqual(self.shots_errors("ws-dev-1"), [])

    def test_bug_question_asks_for_before_after(self):
        self.start("ws-bug-1", "BUG-1", bug=True)
        self.acs("ws-bug-1", [{"id": "AC-1", "ticket": "BUG-1", "text": "t", "kind": "bug", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x", "expect_fail_pattern": "button works"}])
        self.assertIn("before/after screenshots of the fix", self.shots_errors("ws-bug-1")[0])

    def test_api_only_run_is_not_asked(self):
        self.start("ws-dev-2", "DEV-2", repos=("API",))
        self.acs("ws-dev-2", [{"id": "AC-1", "ticket": "DEV-2", "text": "t", "kind": "api", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "API", "cmd": "x"}])
        self.assertEqual(self.shots_errors("ws-dev-2"), [])

    def test_ui_false_opts_out_and_ui_ac_or_browser_check_still_asks(self):
        for r in self.ws["repos"]:
            if r["alias"] == "React":
                r["ui"] = False
        self.save_ws()
        self.start("ws-dev-1", "DEV-1")
        self.acs("ws-dev-1", [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        self.assertEqual(self.shots_errors("ws-dev-1"), [])
        self.acs("ws-dev-1", [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "browser", "repo": "React", "spec": "checks/a.spec.ts"}])
        self.assertIn("browser checks", self.shots_errors("ws-dev-1")[0])

    def test_yes_requires_a_screenshot_check_per_ui_repo(self):
        self.start("ws-dev-1", "DEV-1")
        self.acs("ws-dev-1", [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        self.decide("ws-dev-1", "yes")
        errs = self.shots_errors("ws-dev-1")
        self.assertTrue(any("React has no screenshot check" in e for e in errs), errs)
        self.acs("ws-dev-1", [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1", "CHK-2"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"},
                  {"id": "CHK-2", "type": "screenshot", "repo": "React", "path": "/obras/1"}])
        self.assertEqual(self.shots_errors("ws-dev-1"), [])
        from factory.commands import gate
        errs = gate.validate_acs("ws-dev-1", core.run_load("ws-dev-1"), core.workspace_load("ws"))
        self.assertFalse([e for e in errs if "CHK-2" in e], errs)

    def test_screenshot_shape_and_not_a_bug_reproduction(self):
        self.start("ws-bug-1", "BUG-1", bug=True)
        self.acs("ws-bug-1", [{"id": "AC-1", "ticket": "BUG-1", "text": "t", "kind": "bug", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "screenshot", "repo": "React", "path": "/", "spec": "checks/a.spec.ts"}])
        self.assertTrue(any("exactly one of path" in e for e in self.shots_errors("ws-bug-1")))
        from factory.commands import gate
        errs = gate.validate_acs("ws-bug-1", core.run_load("ws-bug-1"), core.workspace_load("ws"))
        self.assertTrue(any("bug AC needs" in e for e in errs), errs)
        self.assertEqual(gate.repro_checks("ws-bug-1"), [])

    # ---------- bug: before on base, after at HEAD ----------

    def bug_yes(self) -> tuple[str, str]:
        rid = "ws-bug-1"
        self.start(rid, "BUG-1", bug=True)
        self.acs(rid, [{"id": "AC-1", "ticket": "BUG-1", "text": "t", "kind": "bug", "checks": ["CHK-1", "CHK-2"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x", "expect_fail_pattern": "button works"},
                  {"id": "CHK-2", "type": "screenshot", "repo": "React", "path": "/obras/1", "title": "Obras list"}])
        self.decide(rid, "yes")
        wt = core.Path(core.run_load(rid)["repos"]["React"]["worktree"])
        mb = sh("git", "merge-base", "HEAD", "origin/main", cwd=wt)
        return rid, mb

    def test_prove_gate_requires_before_bound_to_merge_base(self):
        rid, mb = self.bug_yes()
        wt = core.Path(core.run_load(rid)["repos"]["React"]["worktree"])
        head = commit_file(wt, "mod.py", "v = 2\n", "[Area] - fix")
        from factory import integrity
        run = core.run_load(rid)
        integrity.lock_plan(run, "test fixture")
        core.run_save(run)
        for cid in ("CHK-1", "CHK-2"):
            self.write_evidence(rid, cid, "pass", head, ["evidence/CHK-2/app.png"] if cid == "CHK-2" else [])
        self.write_evidence(rid, "REPRO-CHK-1", "reproduced", mb)
        from factory.commands import gate
        errs = gate._prove(rid, core.run_load(rid), core.workspace_load("ws"))
        self.assertTrue(any("BEFORE-CHK-2: no before screenshot" in e for e in errs), errs)
        self.write_evidence(rid, "BEFORE-CHK-2", "pass", head)
        errs = gate._prove(rid, core.run_load(rid), core.workspace_load("ws"))
        self.assertTrue(any("BEFORE-CHK-2: not bound to merge-base" in e for e in errs), errs)
        self.write_evidence(rid, "BEFORE-CHK-2", "pass", mb, ["evidence/BEFORE-CHK-2/app.png"])
        self.assertEqual(gate._prove(rid, core.run_load(rid), core.workspace_load("ws")), [])
        repro_errs = gate._reproduce(rid, core.run_load(rid), core.workspace_load("ws"))
        self.assertFalse([e for e in repro_errs if "BEFORE-CHK-2" in e], repro_errs)

        from factory import shots
        items = shots.gallery(rid, core.run_load(rid))
        self.assertEqual(items[0]["before"], ["evidence/BEFORE-CHK-2/app.png"])
        self.assertEqual(items[0]["after"], ["evidence/CHK-2/app.png"])
        self.assertEqual((items[0]["before_sha"], items[0]["after_sha"]), (mb, head))
        self.assertEqual(items[0]["acs"], ["AC-1"])
        text = "\n".join(shots.description_lines(core.run_load(rid), "React", None))
        self.assertIn("## Visual evidence", text)
        self.assertIn("| CHK-2 (AC-1) | Obras list | app.png | app.png |", text)

    def test_yes_after_plan_needs_replan(self):
        self.start("ws-dev-1", "DEV-1")
        self.acs("ws-dev-1", [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        self.decide("ws-dev-1", "yes")
        from factory import shots
        errs = shots.prove_errors("ws-dev-1", core.run_load("ws-dev-1"), core.workspace_load("ws"))
        self.assertTrue(any("factory run replan" in e for e in errs), errs)

    def test_repro_captures_before_and_needs_assume_env(self):
        rid, mb = self.bug_yes()
        rc, out = cli("--json", "prove", rid, "--repro")
        self.assertEqual(rc, 1, out)
        recs = {r["check_id"]: r for r in json.loads(out[out.index("["):])}
        self.assertEqual(recs["BEFORE-CHK-2"]["status"], "error")
        self.assertIn("--assume-env", recs["BEFORE-CHK-2"]["summary"])
        self.assertEqual(recs["BEFORE-CHK-2"]["git_sha"], mb)
        ev = core.read_json(core.run_dir(rid) / "evidence" / "BEFORE-CHK-2.json")
        self.assertEqual((ev["kind"], ev["of"]), ("before", "CHK-2"))


if __name__ == "__main__":
    unittest.main()
