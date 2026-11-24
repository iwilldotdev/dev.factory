"""Plan lock scope (adapters, generated files, code review scope), human checks, review-fix origin, env fallback."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.integrity_support import PASS, IntegrityCase, cli, core
from tests.review_support import SRC, ReviewCase

from factory import integrity
from factory.commands import gate

HUMAN = {"id": "CHK-H", "type": "human", "text": "picker looks right", "reason": "native widget"}
TEST = {"id": "CHK-1", "type": "test", "repo": "React", "cmd": PASS}
HUMAN_FINDING = [{"id": "F1", "severity": "major", "text": "human check not confirmed", "file": "-", "check": "CHK-H"}]
OTHER_FINDING = [{"id": "F2", "severity": "major", "text": "breaks X", "file": "a.ts:1"}]


class PlanScope(IntegrityCase):
    def test_adapter_outside_checks_is_locked_and_generated_files_are_not(self):
        self.start()
        self.acs()
        (self.rd / "adapters").mkdir()
        (self.rd / "adapters" / "open.json").write_text('{"actions": []}', encoding="utf-8")
        (self.rd / "checks" / "out").mkdir(parents=True)
        (self.rd / "checks" / "out" / "shot.png").write_bytes(b"\x89PNG")
        (self.rd / "checks" / "screenshots").mkdir()
        (self.rd / "checks" / "screenshots" / "a.png").write_bytes(b"\x89PNG")
        (self.rd / "checks" / "spec.json").write_text("{}", encoding="utf-8")
        data = core.read_json(self.rd / "acs.json")
        data["checks"][0].update(type="visual", node="1:2", adapter="adapters/open.json")
        core.write_json(self.rd / "acs.json", data)
        files = integrity.plan_files(self.rid)
        self.assertIn("adapters/open.json", files)
        self.assertIn("checks/spec.json", files)
        self.assertNotIn("checks/out/shot.png", files)
        self.assertNotIn("checks/screenshots/a.png", files)
        run = core.run_load(self.rid)
        integrity.lock_plan(run)
        core.run_save(run)
        (self.rd / "adapters" / "open.json").write_text('{"actions": [{"click": "#x"}]}', encoding="utf-8")
        (self.rd / "checks" / "out" / "shot.png").write_bytes(b"\x89PNG2")
        self.assertEqual(integrity.changed_files(core.run_load(self.rid)), ["adapters/open.json"])

    def test_plan_rejects_adapter_resolving_outside_and_output_into_checks(self):
        self.start()
        self.acs()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "a.json").write_text("{}", encoding="utf-8")
        try:
            os.symlink(outside, self.rd / "linked", target_is_directory=True)
            linked = True
        except (OSError, NotImplementedError):
            linked = False
        data = core.read_json(self.rd / "acs.json")
        data["checks"][0].update(type="visual", node="1:2", adapter="linked/a.json" if linked else "x/a.json",
                                 out="checks/out/shot.png", screenshot="./checks/s.png")
        core.write_json(self.rd / "acs.json", data)
        errors = gate.validate_acs(self.rid, core.run_load(self.rid), self.ws)
        if linked:
            self.assertTrue(any("resolves outside the run dir" in e for e in errors), errors)
        self.assertTrue(any("out 'checks/out/shot.png' points into checks/" in e for e in errors), errors)
        self.assertTrue(any("screenshot" in e and "points into checks/" in e for e in errors), errors)

    def test_replan_of_checks_only_keeps_the_code_review_valid(self):
        self.proved()
        self.assertEqual(self.record("code")[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        run = core.run_load(self.rid)
        code, _ = gate.latest_review_file(self.rid, "code")
        qa, _ = gate.latest_review_file(self.rid, "qa")
        (self.rd / "checks" / "note.txt").write_text("v2 other proof\n", encoding="utf-8")
        data = core.read_json(self.rd / "acs.json")
        data["checks"][0]["cmd"] = PASS + " "
        core.write_json(self.rd / "acs.json", data)
        integrity.lock_plan(run, "replan of checks")
        self.assertIsNone(integrity.review_lock_error(run, code, "code"))
        self.assertIn("another plan lock", integrity.review_lock_error(run, qa, "qa"))
        data["acs"][0]["text"] = "works differently"
        core.write_json(self.rd / "acs.json", data)
        integrity.lock_plan(run, "replan of the AC text")
        self.assertIn("another plan lock", integrity.review_lock_error(run, code, "code"))


class HumanChecks(IntegrityCase):
    def setUp(self) -> None:
        super().setUp()
        self.proved([TEST, HUMAN])
        self.assertEqual(self.record("code")[0], 0)

    def verify(self, *extra, check="CHK-H"):
        return cli("run", "verify-human", self.rid, check, "--note", "picker ok on Chrome", *extra)

    def test_verify_human_needs_the_user_and_a_human_check(self):
        rc, out = self.verify()
        self.assertEqual(rc, 2)
        self.assertIn("--by user", out)
        rc, out = self.verify("--by", "user")
        self.assertEqual(rc, 2)
        self.assertIn("--user-said", out)
        rc, out = self.verify("--by", "user", "--user-said", "yes it is fine", check="CHK-1")
        self.assertEqual(rc, 2)
        self.assertIn("only `human` checks", out)
        self.assertEqual(self.verify("--by", "user", "--user-said", "x", check="CHK-9")[0], 2)
        self.assertFalse((self.rd / "evidence" / "CHK-H.json").exists())

    def test_verify_human_writes_bound_pass_evidence(self):
        self.ok("run", "verify-human", self.rid, "CHK-H", "--note", "picker ok on Chrome", "--by", "user",
                "--user-said", "looks right to me")
        ev = core.read_json(self.rd / "evidence" / "CHK-H.json")
        self.assertEqual((ev["status"], ev["type"], ev["git_sha"]), ("pass", "human", core.git_head(self.wt)))
        self.assertEqual(ev["details"]["by"], "user")
        run = core.run_load(self.rid)
        self.assertIsNone(integrity.evidence_error(run, "CHK-H"))
        last = [e for e in run["events"] if e["kind"] == "evidence" and e["data"]["check"] == "CHK-H"][-1]
        self.assertEqual((last["data"]["kind"], last["data"]["by"]), ("human", "user"))
        self.assertEqual(run["decisions"][-1]["by"], "user")
        evpath = self.rd / "evidence" / "CHK-H.json"
        evpath.write_text(evpath.read_text(encoding="utf-8").replace("pass", "fail", 1), encoding="utf-8")
        self.assertIsNotNone(integrity.evidence_error(core.run_load(self.rid), "CHK-H"))

    def test_pending_human_check_is_not_a_review_loop(self):
        self.assertEqual(self.record("qa", "CHANGES", HUMAN_FINDING)[0], 0)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("CHK-H: human check pending", out)
        run = core.run_load(self.rid)
        self.assertEqual((run["phase"], run["loops"]["build"], run["loops"]["review"]), ("review", 0, 0))
        self.ok("run", "verify-human", self.rid, "CHK-H", "--note", "ok", "--by", "user", "--user-said", "fine")
        self.assertEqual(self.gate("review")[0], 0)
        self.assertEqual(core.run_load(self.rid)["phase"], "ship")

    def test_qa_changes_with_a_real_finding_still_loops(self):
        self.assertEqual(self.record("qa", "CHANGES", HUMAN_FINDING + OTHER_FINDING)[0], 0)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("F2 [major]", out)
        run = core.run_load(self.rid)
        self.assertEqual((run["phase"], run["loops"]["review"]), ("build", 1))


class ReviewFixOrigin(ReviewCase):
    def test_gate_build_checks_only_commits_after_the_mr_head(self):
        self.ws["commit"]["header_regex"] = r"^\[[^\]]+\] - fix"
        self.save_ws()
        rid = self.start_single()
        self.ok("run", "branch", rid, "React", "--change", "669")
        self.write_acs(rid, [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": PASS}])
        self.ok("prove", rid)
        self.ok("review", "fix", rid)
        run = core.run_load(rid)
        self.assertEqual(run["review_origin"]["head_sha"], {"React": self.sha})
        wt = Path(run["repos"]["React"]["worktree"])
        self.assertEqual(core.git_head(wt), self.sha)
        from tests.real_support import commit_file
        commit_file(wt, "src/fix.txt", "fix\n", "[DEV-9431] - fix it")
        out = self.ok("gate", rid, "build")  # the change's own "[DEV-9431] - drawer" commit does not match the regex
        self.assertIn("PASS build", out)
        self.assertEqual(SRC, core.run_load(rid)["repos"]["React"]["branch"])


class EnvFallback(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"FACTORY_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ws = {"name": "ws", "repos": [{"alias": "React", "path": "C:/main/react"}]}

    def save(self, run):
        core.write_json(core.run_dir(run["id"]) / "run.json", dict(run, schemaVersion=1))

    def test_fallback_to_the_main_checkout_warns(self):
        msgs: list[str] = []
        run = {"id": "ws-a-1", "tickets": [{"key": "A-1"}], "repos": {}}
        path = core.repo_path_for_env(run, self.ws, "React", warn=msgs.append)
        self.assertEqual(path, Path("C:/main/react"))
        self.assertEqual(len(msgs), 1)
        self.assertIn("main checkout", msgs[0])
        msgs.clear()
        run["repos"]["React"] = {"worktree": "C:/wt/react"}
        self.assertEqual(core.repo_path_for_env(run, self.ws, "React", warn=msgs.append), Path("C:/wt/react"))
        self.assertEqual(msgs, [])

    def test_dependency_run_is_found_by_content_not_by_name(self):
        msgs: list[str] = []
        dep = {"id": "ws-renamed-run", "workspace": "ws", "tickets": [{"key": "A-1"}],
               "repos": {"React": {"worktree": "C:/wt/dep"}}}
        self.save(dep)
        run = {"id": "ws-b-2", "workspace": "ws", "parent": "ws-epic", "repos": {},
               "tickets": [{"key": "B-2", "depends_on": ["A-1"]}]}
        self.assertEqual(core.repo_path_for_env(run, self.ws, "React", warn=msgs.append), Path("C:/wt/dep"))
        self.assertEqual(msgs, [])


if __name__ == "__main__":
    unittest.main()
