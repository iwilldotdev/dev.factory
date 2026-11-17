"""Visual evidence on request (DESIGN section 12): the question is enforced on UI runs, a yes needs screenshot checks,
bug runs need before images on the merge-base, and ship lists the images."""

from __future__ import annotations

import json
import unittest

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))  # a single-module run finds real_support

from real_support import RealCase, cli, commit_file, core, sh

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


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

    def png(self, rid: str, rel: str, data: bytes = PNG) -> str:
        path = core.run_dir(rid) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return rel

    def worktree(self, rid: str, alias: str = "React"):
        return core.Path(core.run_load(rid)["repos"][alias]["worktree"])

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
        self.png(rid, "evidence/CHK-2/app.png")
        self.png(rid, "evidence/BEFORE-CHK-2/app.png")
        for cid in ("CHK-1", "CHK-2"):
            self.write_evidence(rid, cid, "pass", head, ["evidence/CHK-2/app.png"] if cid == "CHK-2" else [])
        self.write_evidence(rid, "REPRO-CHK-1", "reproduced", mb)
        from factory.commands import gate
        errs = gate._prove(rid, core.run_load(rid), core.workspace_load("ws"))
        self.assertTrue(any("BEFORE-CHK-2: no before screenshot" in e for e in errs), errs)
        self.write_evidence(rid, "BEFORE-CHK-2", "pass", head, ["evidence/BEFORE-CHK-2/app.png"])
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

    def test_before_must_list_an_existing_png(self):
        rid, mb = self.bug_yes()
        from factory import shots
        ws = core.workspace_load("ws")
        self.png(rid, "evidence/BEFORE-CHK-2/app.png", b"<html>not an image</html>")
        self.write_evidence(rid, "BEFORE-CHK-2", "pass", mb, ["evidence/BEFORE-CHK-2/app.png"])
        errs = shots.before_errors(rid, core.run_load(rid), ws)
        self.assertTrue(any("BEFORE-CHK-2" in e and "PNG" in e for e in errs), errs)
        self.write_evidence(rid, "BEFORE-CHK-2", "pass", mb, ["evidence/BEFORE-CHK-2/gone.png"])
        self.assertTrue(any("PNG" in e for e in shots.before_errors(rid, core.run_load(rid), ws)))
        self.png(rid, "evidence/BEFORE-CHK-2/app.png")
        self.write_evidence(rid, "BEFORE-CHK-2", "pass", mb, ["evidence/BEFORE-CHK-2/app.png"])
        self.assertEqual(shots.before_errors(rid, core.run_load(rid), ws), [])

    def test_reproduce_gate_reports_missing_before(self):
        rid, mb = self.bug_yes()
        from factory.commands import gate
        errs = gate._reproduce(rid, core.run_load(rid), core.workspace_load("ws"))
        self.assertTrue(any("BEFORE-CHK-2: no before screenshot" in e for e in errs), errs)

    def test_decision_ignores_non_user_visual(self):
        self.start("ws-dev-1", "DEV-1")
        run = core.run_load("ws-dev-1")
        run["decisions"] = [{"text": "x", "by": "agent", "visual": "no"}, {"text": "y", "visual": "yes"}]
        from factory import shots
        self.assertIsNone(shots.decision(run))
        run["decisions"].append({"text": "z", "by": "user", "visual": "no"})
        self.assertEqual(shots.decision(run), "no")

    def test_before_key_is_rejected_in_screenshot_check(self):
        from factory import shots
        errs = shots.check_errors({"checks": [{"id": "CHK-2", "type": "screenshot", "repo": "React",
                                               "spec": "checks/a.spec.ts", "before": True}]})
        self.assertTrue(any("`before`" in e for e in errs), errs)

    def test_ui_files_in_diff_demand_the_question_even_without_flags(self):
        for r in self.ws["repos"]:
            if r["alias"] == "React":
                r["env"] = {"base_url": "http://localhost:1"}  # no ui flag, no login block
        self.save_ws()
        rid = "ws-dev-1"
        self.start(rid, "DEV-1")
        self.acs(rid, [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        from factory import shots
        ws = core.workspace_load("ws")
        self.assertEqual(self.shots_errors(rid), [])  # nothing committed yet: existing reasons only
        commit_file(self.worktree(rid), "src/Page.TSX", "export {}\n", "[Area] - page")
        errs = self.shots_errors(rid)
        self.assertTrue(errs and "UI repo React" in errs[0], errs)
        self.assertTrue(shots.prove_errors(rid, core.run_load(rid), ws))  # gate prove re-evaluates the diff
        self.decide(rid, "no")
        self.assertEqual(shots.prove_errors(rid, core.run_load(rid), ws), [])

    def test_non_ui_files_do_not_count(self):
        for r in self.ws["repos"]:
            if r["alias"] == "React":
                r["env"] = {"base_url": "http://localhost:1"}
        self.save_ws()
        self.start("ws-dev-1", "DEV-1")
        self.acs("ws-dev-1", [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        commit_file(self.worktree("ws-dev-1"), "src/util.ts", "export {}\n", "[Area] - util")
        self.assertEqual(self.shots_errors("ws-dev-1"), [])

    def test_unchanged_ui_repo_does_not_demand_a_screenshot_check(self):
        rid = "ws-dev-1"
        self.start(rid, "DEV-1", repos=("API", "React"))
        self.acs(rid, [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "api", "checks": ["CHK-1"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "API", "cmd": "x"},
                  {"id": "CHK-2", "type": "screenshot", "repo": "API", "path": "/"}])
        commit_file(self.worktree(rid, "API"), "api.py", "x = 1\n", "[Area] - api")
        self.decide(rid, "yes")
        errs = self.shots_errors(rid)
        self.assertFalse([e for e in errs if "React" in e], errs)
        from factory import shots
        self.assertEqual(shots.ui_repos(core.run_load(rid), core.workspace_load("ws")), [])

    def test_workspace_check_warns_without_ui_and_rejects_non_bool(self):
        from factory.commands import workspace
        ws = core.workspace_load("ws")
        for r in ws["repos"]:
            r["env"] = r.get("env") or {"start": "x"}
            r.pop("ui", None)
        warns = workspace.workspace_warnings(ws)
        self.assertEqual(len(warns), len(ws["repos"]), warns)
        ws["repos"][0]["ui"] = False
        self.assertEqual(len(workspace.workspace_warnings(ws)), len(ws["repos"]) - 1)
        ws["repos"][0]["ui"] = "yes"
        self.assertTrue(any("ui must be true or false" in e for e in workspace.check_workspace(ws)))

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

    def test_repro_runs_before_only_after_a_yes(self):
        rid = "ws-bug-1"
        self.start(rid, "BUG-1", bug=True)
        self.acs(rid, [{"id": "AC-1", "ticket": "BUG-1", "text": "t", "kind": "bug", "checks": ["CHK-1", "CHK-2"]}],
                 [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x", "expect_fail_pattern": "button works"},
                  {"id": "CHK-2", "type": "screenshot", "repo": "React", "path": "/obras/1"}])
        for answer in (None, "no"):
            if answer:
                self.decide(rid, answer)
            rc, out = cli("--json", "prove", rid, "--repro")
            self.assertNotIn("BEFORE-CHK-2", out)
            self.assertFalse((core.run_dir(rid) / "evidence" / "BEFORE-CHK-2.json").exists())
            self.assertIn("REPRO-CHK-1", out)


if __name__ == "__main__":
    unittest.main()
