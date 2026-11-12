from __future__ import annotations

import json
import unittest

from core_support import (PY, FactoryCase, cli, commit_file, core, sh)


class WorkspaceTests(FactoryCase):
    def test_import_converts_profile_and_check_reports_missing(self):
        profile = self.tmp / "project.json"
        from core_support import SAMPLE_PROFILE
        profile.write_text(json.dumps(SAMPLE_PROFILE), encoding="utf-8")
        rc, _ = cli("workspace", "import", "ws", "--from", str(profile))
        self.assertEqual(rc, 0)
        ws = core.workspace_load("ws")
        repo = ws["repos"][0]
        self.assertEqual((repo["alias"], repo["name"], repo["order"], repo["gitlab_project"]),
                         ("React", "webapp", 2, "g/react"))
        self.assertEqual(repo["path"], "")
        self.assertEqual(repo["quality"], {})
        self.assertEqual(ws["ship"]["targets"], ["pack", "homolog"])
        self.assertEqual(ws["commit"]["forbidden_trailers"], ["Co-authored-by:"])
        self.assertEqual(ws["epics"][0]["key"], "DEV-1")
        rc, out = cli("workspace", "check", "ws")
        self.assertEqual(rc, 1)
        self.assertIn("React: path is missing", out)
        self.assertIn("React: quality commands are missing", out)
        self.assertIn("React: env block is missing", out)

    def test_init_and_duplicate(self):
        self.assertEqual(cli("workspace", "init", "x")[0], 0)
        self.assertEqual(cli("workspace", "init", "x")[0], 2)


class RunTests(FactoryCase):
    def test_start_branch_status(self):
        rid = self.make_run()
        run = core.run_load(rid)
        self.assertEqual(rid, "ws-dev-1")
        self.assertEqual(run["phase"], "intake")
        rec = run["repos"]["React"]
        self.assertTrue(self.wt.is_dir())
        self.assertEqual(self.wt.name, "dev-1-react")
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=self.wt), rec["branch"])
        self.assertEqual(rec["head"], sh("git", "rev-parse", "main", cwd=self.repo))
        # main clone untouched
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo), "main")
        rc, out = cli("run", "status", rid)
        self.assertEqual(rc, 0)
        self.assertIn("phase=intake", out)
        self.assertIn("remote/issue-DEV-1.json missing", out)
        rc, out = cli("run", "list")
        self.assertIn("ws-dev-1", out)

    def test_branch_twice_rejected(self):
        rid = self.make_run()
        rc, _ = cli("run", "branch", rid, "React", "--base", "main")
        self.assertEqual(rc, 2)

    def test_decide_block_unblock_human_and_intake_gate(self):
        rid = self.make_run()
        self.assertEqual(cli("run", "block", rid, "question", "which env?")[0], 0)
        core.write_json(core.run_dir(rid) / "remote" / "issue-DEV-1.json", {"key": "DEV-1"})
        rc, out = cli("gate", rid, "intake")
        self.assertEqual(rc, 1)
        self.assertIn("open question blocker", out)
        self.assertNotEqual(cli("run", "decide", rid, "use pack", "--by", "user")[0], 0)  # needs --user-said
        self.assertEqual(cli("run", "decide", rid, "use pack", "--by", "user", "--user-said", "use the pack")[0], 0)
        cli("run", "human", rid, "user said go")
        self.assertEqual(cli("run", "unblock", rid, "0")[0], 0)
        rc, out = cli("gate", rid, "intake")
        self.assertEqual(rc, 0, out)
        run = core.run_load(rid)
        self.assertEqual(run["phase"], "plan")
        self.assertEqual(run["decisions"][0]["by"], "user")
        kinds = [e["kind"] for e in run["events"]]
        self.assertIn("human", kinds)
        self.assertIn("gate_fail", kinds)
        self.assertIn("gate_pass", kinds)


class PlanGateTests(FactoryCase):
    NODE = "3544:5710"

    def plan(self) -> tuple[int, str]:
        self.set_phase("plan")
        return cli("gate", self.run_id, "plan")

    def ui_checks(self):
        return [
            {"id": "CHK-1", "type": "visual", "repo": "React", "node": self.NODE, "path": "/x"},
            {"id": "CHK-2", "type": "style", "repo": "React", "node": self.NODE, "path": "/x", "map": "checks/style/s.json"},
        ]

    def test_plan_passes_when_complete(self):
        self.make_run()
        self.write_pack(self.NODE)
        self.write_style_map(self.NODE, {"fontSize": 12, "color": "#333333", "fontWeight": 600})
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1", "CHK-2"]}],
                       self.ui_checks())
        rc, out = self.plan()
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.run_id)["phase"], "build")

    def test_ui_ac_without_visual_and_style(self):
        self.make_run()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("ui AC needs a visual check", out)
        self.assertIn("ui AC needs a style check", out)
        self.assertEqual(core.run_load(self.run_id)["phase"], "plan")

    def test_missing_pack(self):
        self.make_run()
        self.write_style_map(self.NODE, {"fontSize": 12})
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1", "CHK-2"]}],
                       self.ui_checks())
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("3544-5710.json missing", out)
        self.assertIn("3544-5710.png missing", out)
        self.assertIn("figma/meta.json missing", out)

    def test_undecodable_png(self):
        self.make_run()
        self.write_pack(self.NODE)
        (core.run_dir(self.run_id) / "figma" / "3544-5710.png").write_bytes(b"nope")
        self.write_style_map(self.NODE, {"fontSize": 12})
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1", "CHK-2"]}],
                       self.ui_checks())
        rc, out = self.plan()
        self.assertIn("not a decodable PNG", out)

    def test_style_value_not_in_pack(self):
        self.make_run()
        self.write_pack(self.NODE)
        self.write_style_map(self.NODE, {"fontSize": 99, "color": "#ABCDEF"})
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1", "CHK-2"]}],
                       self.ui_checks())
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("title.fontSize=99 number not in the node's fontSize", out)
        self.assertIn("title.color='#ABCDEF' color not in the node's fills/strokes", out)

    def test_style_color_matches_hex_case_insensitive_and_rgba_floats(self):
        self.make_run()
        self.write_pack(self.NODE, {"fontSize": 12, "stroke": "#aBcDeF", "fill": {"r": 1, "g": 0, "b": 0, "a": 1}})
        self.write_style_map(self.NODE, {"fontSize": 12, "color": "#ABCDEF", "backgroundColor": "#ff0000",
                                         "borderColor": "rgb(255, 0, 0)"})
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1", "CHK-2"]}],
                       self.ui_checks())
        rc, out = self.plan()
        self.assertEqual(rc, 0, out)

    def test_bug_ac_without_red_green(self):
        self.make_run(bug=True)
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "bug", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"}])
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("bug AC needs a red-green check", out)

    def test_unreferenced_check_and_unknown_ref(self):
        self.make_run()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1", "CHK-9"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "x"},
                        {"id": "CHK-2", "type": "test", "repo": "React", "cmd": "y"}])
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("CHK-2: not referenced by any AC", out)
        self.assertIn("unknown check CHK-9", out)

    def test_human_check_never_satisfies_ui(self):
        self.make_run()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "human", "text": "look", "reason": "picker"}])
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("ui AC needs a visual check", out)

    def test_external_failures_and_unavailable(self):
        self.make_run()
        self.write_pack(self.NODE)
        self.write_style_map(self.NODE, {"fontSize": 12})
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1", "CHK-2"]}],
                       self.ui_checks())
        self.ext_errors["capability"] = ["no health url"]
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("env React: no health url", out)

    def test_env_block_missing_listed(self):
        self.make_run()
        ws = core.workspace_load("ws")
        ws["repos"][0]["env"] = {}
        core.write_json(core.workspace_path("ws"), ws)
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "api", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "api", "repo": "React", "path": "/x"}])
        rc, out = self.plan()
        self.assertEqual(rc, 1)
        self.assertIn("React has no env block", out)


class BuildProveGateTests(FactoryCase):
    def prepare(self, cmd: str | None = None, quality: dict | None = None):
        self.make_run()
        script = self.tmp / "ok.py"
        script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        self.check_cmd = cmd or f"{PY} {script}"
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": self.check_cmd}])
        self.head = commit_file(self.wt, "src/a.txt", "a\n")

    def test_build_gate(self):
        self.make_run()
        self.set_phase("build")
        rc, out = cli("gate", self.run_id, "build")
        self.assertEqual(rc, 1)
        self.assertIn("no commits ahead", out)
        commit_file(self.wt, "a.txt", "a", msg="bad header")
        rc, out = cli("gate", self.run_id, "build")
        self.assertIn("header does not match", out)
        commit_file(self.wt, "b.txt", "b", msg="[A] - ok\n\nCo-authored-by: x")
        (self.wt / "a.txt").write_text("dirty", encoding="utf-8")
        rc, out = cli("gate", self.run_id, "build")
        self.assertIn("forbidden trailer", out)
        self.assertIn("uncommitted changes", out)
        sh("git", "checkout", "--", "a.txt", cwd=self.wt)
        sh("git", "reset", "-q", "--soft", "main", cwd=self.wt)
        sh("git", "commit", "-q", "-m", "[A] - squashed", cwd=self.wt)
        rc, out = cli("gate", self.run_id, "build")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.run_id)["phase"], "prove")

    def test_prove_pass_then_gate(self):
        self.prepare()
        self.set_phase("prove")
        rc, out = cli("gate", self.run_id, "prove")
        self.assertEqual(rc, 1)
        self.assertIn("CHK-1: no evidence", out)
        rc, out = cli("prove", self.run_id)
        self.assertEqual(rc, 0, out)
        ev = core.load_evidence(self.run_id, "CHK-1")
        self.assertEqual(ev["status"], "pass")
        self.assertEqual(ev["git_sha"], self.head)
        self.assertFalse(ev["dirty"])
        rc, out = cli("gate", self.run_id, "prove")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.run_id)["phase"], "review")

    def test_prove_failing_check(self):
        self.prepare(cmd=f'{PY} -c "import sys; sys.exit(3)"')
        self.set_phase("prove")
        rc, out = cli("prove", self.run_id)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  CHK-1", out)
        self.assertEqual(core.load_evidence(self.run_id, "CHK-1")["status"], "fail")
        rc, out = cli("gate", self.run_id, "prove")
        self.assertEqual(rc, 1)
        self.assertIn("evidence status fail", out)
        self.assertEqual(core.run_load(self.run_id)["phase"], "prove")

    def test_dirty_worktree_rejected(self):
        self.prepare()
        self.set_phase("prove")
        (self.wt / "src" / "a.txt").write_text("changed\n", encoding="utf-8")
        rc, out = cli("prove", self.run_id)
        self.assertIn("dirty", out)
        self.assertTrue(core.load_evidence(self.run_id, "CHK-1")["dirty"])
        rc, out = cli("gate", self.run_id, "prove")
        self.assertEqual(rc, 1)
        self.assertIn("dirty", out)

    def test_stale_sha_rejected(self):
        self.prepare()
        self.set_phase("prove")
        self.assertEqual(cli("prove", self.run_id)[0], 0)
        commit_file(self.wt, "src/b.txt", "b\n")
        rc, out = cli("gate", self.run_id, "prove")
        self.assertEqual(rc, 1)
        self.assertIn("stale", out)

    def test_human_check_needs_no_evidence(self):
        self.prepare()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1", "CHK-2"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": self.check_cmd},
                        {"id": "CHK-2", "type": "human", "text": "x", "reason": "y"}])
        self.set_phase("prove")
        self.assertEqual(cli("prove", self.run_id)[0], 0)
        self.assertEqual(cli("gate", self.run_id, "prove")[0], 0)

    def test_rules_run_as_evidence(self):
        self.prepare()
        ws = core.workspace_load("ws")
        ws["rules"] = [{"id": "lint", "text": "t", "check": {"type": "test", "repo": "React",
                                                              "cmd": f'{PY} -c "import sys; sys.exit(1)"'}}]
        core.write_json(core.workspace_path("ws"), ws)
        self.set_phase("prove")
        rc, out = cli("prove", self.run_id)
        self.assertEqual(rc, 1)
        self.assertEqual(core.load_evidence(self.run_id, "RULE-lint-React")["status"], "fail")
        rc, out = cli("gate", self.run_id, "prove")
        self.assertIn("RULE-lint-React", out)


class QualityDeltaTests(FactoryCase):
    def test_baseline_and_new_failures(self):
        err = self.tmp / "tsc.py"
        err.write_text(
            "import os,sys\n"
            "print('src/a.ts(1,2): error TS2322: old problem')\n"
            "if os.path.exists('newbug.txt'): print('src/b.ts(5,1): error TS1005: new problem')\n"
            "sys.exit(1)\n", encoding="utf-8")
        self.quality = {"typecheck": f"{PY} {err}"}
        self.make_run()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": f'{PY} -c "pass"'}])
        commit_file(self.wt, "x.txt", "x")
        self.set_phase("prove")
        rc, out = cli("prove", self.run_id)
        self.assertEqual(rc, 0, out)
        base = core.read_json(core.run_dir(self.run_id) / "baseline" / "React.json")
        self.assertEqual(base["commands"]["typecheck"]["failures"], ["src/a.ts TS2322 old problem"])
        q = core.load_evidence(self.run_id, "QUALITY-React")
        self.assertEqual(q["status"], "pass")
        self.assertEqual(cli("gate", self.run_id, "prove")[0], 0)
        # introduce a new failure
        self.set_phase("prove")
        commit_file(self.wt, "newbug.txt", "x")
        rc, out = cli("prove", self.run_id)
        self.assertEqual(rc, 1)
        q = core.load_evidence(self.run_id, "QUALITY-React")
        self.assertEqual(q["details"]["new_failures"]["typecheck"], ["src/b.ts TS1005 new problem"])

    def test_parse_failures(self):
        from pathlib import Path
        from factory.commands.prove import parse_failures
        root = Path("C:/tmp/wt")
        text = "C:\\tmp\\wt\\src\\a.ts(3,4): error TS1: boom\nsrc/c.cs(1,1): error CS1002: ; expected [x.csproj]\n"
        self.assertEqual(parse_failures(text, 1, root), ["src/a.ts TS1 boom", "src/c.cs CS1002 ; expected"])
        vitest = json.dumps({"testResults": [{"name": "/r/a.test.ts", "status": "failed", "assertionResults": [
            {"status": "failed", "fullName": "A works"}, {"status": "passed", "fullName": "B"}]}]})
        self.assertEqual(parse_failures(vitest, 1, Path("/r")), ["test:a.test.ts::A works"])
        eslint = json.dumps([{"filePath": "/r/x.ts", "messages": [{"severity": 2, "ruleId": "no-x", "message": "m", "line": 4}]}])
        self.assertEqual(parse_failures(eslint, 1, Path("/r")), ["x.ts no-x m"])
        self.assertEqual(parse_failures("opaque", 2, root, "lint"), ["exit:lint"])
        self.assertEqual(parse_failures("fine", 0, root), [])


class ReviewGateTests(FactoryCase):
    def review(self, verdict="APPROVE", findings=None, kind="code", sha_override=None):
        rec = {"verdict": verdict, "reviewer": "factory-auditor", "findings": findings or [],
               "completeness": {"done": 1, "gap": 0, "not_checked": 0}}
        if kind == "qa":
            rec["ac_coverage"] = [{"ac": "AC-1", "exercises": True}]
        if sha_override:
            rec["repos"] = {"React": sha_override}
        path = self.tmp / f"{kind}.json"
        core.write_json(path, rec)
        return cli("review", "record", self.run_id, "--kind", kind, "--file", str(path))

    def setUp(self):
        super().setUp()
        self.make_run()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": f'{PY} -c "pass"'}])
        self.head = commit_file(self.wt, "a.txt", "a")
        self.set_phase("prove")
        rc, out = cli("prove", self.run_id)
        assert rc == 0, out
        self.set_phase("review")

    def test_record_binds_sha_and_rounds(self):
        rc, out = self.review()
        self.assertEqual(rc, 0, out)
        rec = core.read_json(core.run_dir(self.run_id) / "reviews" / "code-1.json")
        self.assertEqual(rec["repos"], {"React": self.head})
        self.assertEqual(self.review()[0], 0)
        self.assertTrue((core.run_dir(self.run_id) / "reviews" / "code-2.json").is_file())

    def test_record_rejects_stale_claim_and_bad_input(self):
        rc, out = self.review(sha_override="0" * 40)
        self.assertEqual(rc, 1)
        self.assertIn("HEAD is", out)
        rc, out = self.review(verdict="MAYBE")
        self.assertEqual(rc, 1)
        rc, out = self.review(verdict="APPROVE", findings=[{"id": "F1", "severity": "major", "text": "x"}])
        self.assertIn("open blocker/major", out)

    def test_qa_requires_ac_coverage(self):
        path = self.tmp / "qa.json"
        core.write_json(path, {"verdict": "APPROVE", "reviewer": "r", "findings": [],
                               "completeness": {"done": 1, "gap": 0, "not_checked": 0}})
        rc, out = cli("review", "record", self.run_id, "--kind", "qa", "--file", str(path))
        self.assertEqual(rc, 1)
        self.assertIn("AC-1", out)

    def test_approve_both_passes(self):
        self.assertEqual(self.review(kind="code")[0], 0)
        rc, out = cli("gate", self.run_id, "review")
        self.assertEqual(rc, 1)
        self.assertIn("no qa review", out)
        self.assertEqual(core.run_load(self.run_id)["loops"]["build"], 0)
        self.assertEqual(self.review(kind="qa")[0], 0)
        rc, out = cli("gate", self.run_id, "review")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.run_id)["phase"], "ship")

    def test_stale_review_after_new_commit(self):
        self.review(kind="code")
        self.review(kind="qa")
        commit_file(self.wt, "b.txt", "b")
        rc, out = cli("gate", self.run_id, "review")
        self.assertEqual(rc, 1)
        self.assertIn("stale", out)

    def test_changes_loops_back_then_blocks_at_max(self):
        ws = core.workspace_load("ws")
        ws["autonomy"]["max_review_rounds"] = 9  # this test is about max_build_loops
        core.write_json(core.workspace_path("ws"), ws)
        from factory import integrity  # the caps belong to the plan: re-lock as if set before the plan gate
        run = core.run_load(self.run_id)
        integrity.lock_plan(run, "test fixture", keep_seq=True)
        core.run_save(run)
        for expected in (1, 2):
            self.review(verdict="CHANGES", findings=[{"id": "F1", "severity": "blocker", "text": "bad", "file": "a.txt:1"}])
            self.review(kind="qa")
            rc, out = cli("gate", self.run_id, "review")
            self.assertEqual(rc, 1)
            run = core.run_load(self.run_id)
            self.assertEqual(run["phase"], "build")
            self.assertEqual(run["loops"]["build"], expected)
            self.set_phase("review")
        self.review(verdict="CHANGES", findings=[{"id": "F1", "severity": "major", "text": "bad"}])
        rc, out = cli("gate", self.run_id, "review")
        self.assertEqual(rc, 1)
        self.assertIn("max_build_loops", out)
        run = core.run_load(self.run_id)
        self.assertEqual(run["phase"], "blocked")
        self.assertEqual(run["loops"]["build"], 3)
        self.assertEqual(cli("run", "unblock", self.run_id, str(len(run["blockers"]) - 1))[0], 0)
        self.assertEqual(core.run_load(self.run_id)["phase"], "build")  # the loop resumes at build, not review


class ShipGateTests(FactoryCase):
    def test_ship_uses_ship_verify(self):
        self.make_run()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": f'{PY} -c "pass"'}])
        commit_file(self.wt, "a.txt", "a")
        self.set_phase("prove")
        self.assertEqual(cli("prove", self.run_id)[0], 0)
        self.set_phase("ship")
        self.ext_errors["verify"] = ["draft mismatch"]
        rc, out = cli("gate", self.run_id, "ship")
        self.assertEqual(rc, 1)
        self.assertIn("draft mismatch", out)
        self.ext_errors["verify"] = []
        self.assertEqual(cli("gate", self.run_id, "ship")[0], 0)
        self.assertEqual(core.run_load(self.run_id)["phase"], "feedback")


if __name__ == "__main__":
    unittest.main()
