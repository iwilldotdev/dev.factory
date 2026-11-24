"""Residual integrity gaps (verification audit probes A-M): caps on replan/rewind, reviews bound to the plan lock,
setup failures are not reproductions, auditor-only review record with a session hint, workspace.json in the plan
lock, env identity never assumed, pipeline waivers naming the pipeline (DESIGN "Threat model")."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from factory.commands import env as envmod
from tests.integrity_support import PASS, PY, IntegrityCase, cli, core, fake_env_record
from tests.test_integrity_repro import ReproCase


def user_decision(case, text: str = "user asked to change the plan") -> int:
    out = case.ok("--json", "run", "decide", case.rid, text, "--by", "user", "--user-said", "ok, go ahead")
    return json.loads(out)["decisions"] - 1


class ReplanRewindCapTests(IntegrityCase):
    """Probes B and C: replan/rewind need a user decision or count against autonomy caps."""

    def replan_cycle(self, *extra: str) -> tuple[int, str]:
        rc, out = cli("run", "replan", self.rid, "change", *extra)
        if rc == 0:
            self.ok("gate", self.rid, "plan")
            self.ok("gate", self.rid, "build")
        return rc, out

    def test_replan_without_user_decision_blocks_past_max_replans(self):
        self.to_prove()
        self.assertEqual(self.replan_cycle()[0], 0)
        self.assertEqual(self.replan_cycle()[0], 0)
        rc, out = self.replan_cycle()  # third without a decision: default max_replans 2
        self.assertNotEqual(rc, 0)
        self.assertIn("max_replans (2)", out)
        run = core.run_load(self.rid)
        self.assertEqual(run["phase"], "blocked")
        self.assertEqual(run["loops"]["replan"], 2)

    def test_replan_with_user_decision_does_not_count_and_is_single_use(self):
        self.set_autonomy(max_replans=0)
        self.to_prove()
        rc, out = cli("run", "replan", self.rid, "x")
        self.assertNotEqual(rc, 0)
        self.assertIn("max_replans (0)", out)
        self.ok("run", "unblock", self.rid, "0")
        n = user_decision(self)
        self.assertEqual(self.replan_cycle("--decision", str(n))[0], 0)
        run = core.run_load(self.rid)
        self.assertEqual(run["loops"]["replan_approved"], 1)
        self.assertEqual(run["decisions"][n]["used"]["action"], "replan")
        rc, out = cli("run", "replan", self.rid, "again", "--decision", str(n))
        self.assertNotEqual(rc, 0)
        self.assertIn("already authorised", out)

    def test_caps_raised_after_plan_do_not_count(self):
        self.set_autonomy(max_replans=0)
        self.to_prove()
        self.set_autonomy(max_replans=9)
        rc, out = cli("run", "replan", self.rid, "x")
        self.assertNotEqual(rc, 0)
        self.assertIn("max_replans (0)", out)

    def test_agent_or_other_phase_decision_does_not_authorise(self):
        self.to_prove()
        self.ok("run", "decide", self.rid, "agent thinks so")  # --by agent
        rc, out = cli("run", "replan", self.rid, "x", "--decision", "0")
        self.assertIn("not a user decision", out)
        old = user_decision(self)
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")
        rc, out = cli("run", "rewind", self.rid, "prove", "x", "--decision", str(old))
        self.assertNotEqual(rc, 0)
        self.assertIn("not the current phase", out)

    def test_rewinds_count_into_build_loops_and_block(self):
        self.reviewed()
        rcs = []
        for i in range(5):
            rc, out = cli("run", "rewind", self.rid, "prove", f"r{i}")
            rcs.append(rc)
            if rc:
                break
            self.ok("prove", self.rid)
            self.ok("gate", self.rid, "prove")
            self.record("code")
            self.record("qa")
            self.ok("gate", self.rid, "review")
        self.assertEqual(rcs, [0, 0, 0, 1])  # max_build_loops 3
        self.assertIn("max_build_loops (3)", out)
        self.assertEqual(core.run_load(self.rid)["phase"], "blocked")

    def test_rewind_with_user_decision_does_not_count(self):
        self.reviewed()
        before = core.run_load(self.rid)["loops"]["build"]
        n = user_decision(self, "rebase on main")
        self.ok("run", "rewind", self.rid, "prove", "rebased", "--decision", str(n))
        self.assertEqual(core.run_load(self.rid)["loops"]["build"], before)


class ReviewBoundToLockTests(IntegrityCase):
    """Probes M and H: a replan invalidates reviews; ship verify / gate ship re-check the reviews."""

    def test_review_from_before_replan_is_ignored(self):
        self.reviewed()
        self.ok("run", "replan", self.rid, "add AC", "--decision", str(user_decision(self)))
        checks = [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": PASS},
                  {"id": "CHK-2", "type": "test", "repo": "React", "cmd": PASS}]
        self.acs(checks, [{"id": "AC-1", "ticket": "DEV-1", "text": "works", "kind": "logic", "checks": ["CHK-1"]},
                          {"id": "AC-2", "ticket": "DEV-1", "text": "new", "kind": "logic", "checks": ["CHK-2"]}])
        for step in (("gate", self.rid, "plan"), ("gate", self.rid, "build"), ("prove", self.rid),
                     ("gate", self.rid, "prove")):
            self.ok(*step)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("recorded against another plan lock", out)
        self.assertEqual(self.record("code")[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")

    def test_review_edited_after_review_gate_fails_ship_verify_and_gate(self):
        self.reviewed()
        plan = json.loads(self.ok("--json", "ship", "plan", self.rid))
        self.readbacks(plan)
        path = sorted((self.rd / "reviews").glob("code-*.json"))[-1]
        rec = core.read_json(path)
        rec["verdict"] = "CHANGES"
        core.write_json(path, rec)
        rc, out = cli("ship", "verify", self.rid)
        self.assertEqual(rc, 1)
        self.assertIn("does not match the digest", out)
        rc, out = self.gate("ship")
        self.assertEqual(rc, 1)
        self.assertIn("does not match the digest", out)


class SetupFailureTests(ReproCase):
    """Probes J, K, L: missing file / command not found / trivial patterns are not reproductions."""

    def bug(self, cmd: str, pattern: str) -> None:
        chk = {"id": "CHK-1", "type": "test", "repo": "React", "cmd": cmd, "expect_fail_pattern": pattern}
        self.acs([chk], [{"id": "AC-1", "ticket": "BUG-1", "text": "v", "kind": "bug", "checks": ["CHK-1"]}])

    def test_missing_test_file_is_error(self):
        self.bug(f"{PY} tests/test_login_bug.py", "test_login_bug")
        ev = self.repro()
        self.assertEqual(ev["status"], "error", ev["summary"])
        self.assertIn("setup failure", ev["summary"])

    def test_command_not_found_is_error(self):
        self.bug("nosuchtool_xyz --grep login_bug", "login_bug|nosuchtool")
        ev = self.repro()
        self.assertEqual(ev["status"], "error", ev["summary"])
        self.assertIn("setup failure", ev["summary"])

    def test_setup_exit_codes_are_error(self):
        for code in (126, 127, 9009):
            self.bug(f'{PY} -c "import sys; print(\'ASSERT v failed\'); sys.exit({code})"', "ASSERT v")
            ev = self.repro()
            self.assertEqual(ev["status"], "error", f"{code}: {ev['summary']}")
            self.assertIn(f"exit {code}", ev["summary"])

    def test_trivial_patterns_refused_by_plan_gate_and_repro(self):
        self.ok("gate", self.rid, "intake")
        for pattern in (".", ".*", r"\w+", r"\w+\w+\w+", "[a-z]{6}", "ab.cd"):
            self.bug(f"{PY} check.py", pattern)
            rc, out = self.gate("plan")
            self.assertEqual(rc, 1, pattern)
            self.assertIn("expect_fail_pattern", out)
            self.assertEqual(self.repro()["status"], "error", pattern)
        self.with_setup()
        self.bug(f"{PY} check.py", "ASSERT v")
        self.assertEqual(self.repro()["status"], "reproduced")


class AuditorRecordTests(IntegrityCase):
    """Probe G: record stays auditor-only (FACTORY_ROLE, enforced by worker-guard); a session hint is kept."""

    def test_record_keeps_session_hint_and_plan_lock(self):
        self.proved()
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
        with mock.patch.dict(os.environ, dict(env, CLAUDE_AGENT_ID="agent-123"), clear=True):
            self.assertEqual(self.record("code")[0], 0)
        rec = core.read_json(sorted((self.rd / "reviews").glob("code-*.json"))[-1])
        self.assertEqual(rec["recorded_by"], {"CLAUDE_AGENT_ID": "agent-123"})
        self.assertEqual(rec["plan_lock"], core.run_load(self.rid)["plan_lock"]["digest"])
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.record("qa")[0], 0)
        self.assertNotIn("recorded_by", core.read_json(sorted((self.rd / "reviews").glob("qa-*.json"))[-1]))

    def test_record_without_auditor_role_refused(self):
        self.proved()
        env = {k: v for k, v in os.environ.items() if k != "FACTORY_ROLE"}
        with mock.patch.dict(os.environ, env, clear=True):
            rc, out = self.record("code")
        self.assertNotEqual(rc, 0)
        self.assertIn("FACTORY_ROLE=auditor", out)


class WorkspaceLockTests(IntegrityCase):
    """Probe F: caps raised in workspace.json after the plan gate are not honoured."""

    def test_workspace_change_after_plan_fails_gates(self):
        self.set_autonomy(max_review_rounds=1, max_build_loops=1)
        self.proved()
        self.record("code", "CHANGES", findings=[{"id": "F1", "severity": "major", "file": "a", "text": "x"}],
                    completeness={"done": 1, "gap": 1, "not_checked": 0})
        self.record("qa")
        self.set_autonomy(max_review_rounds=99, max_build_loops=99)
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("workspace changed after plan; replan", out)
        run = core.run_load(self.rid)
        self.assertEqual(run["phase"], "review")
        self.assertEqual(run["loops"]["build"], 0)
        self.assertIn("workspace changed after plan", cli("ship", "verify", self.rid)[1] + cli("run", "status", self.rid)[1])

    def test_key_order_is_not_a_change(self):
        self.to_prove()
        path = core.workspace_path("ws")
        data = core.read_json(path)
        path.write_text(json.dumps(dict(reversed(list(data.items())))), encoding="utf-8")
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")


class EnvIdentityTests(IntegrityCase):
    """Never kill (or bind to) a process whose create time cannot be read."""

    def setUp(self) -> None:
        super().setUp()
        self.start()
        self.ws["repos"][0]["env"] = {"start": "x", "health": "http://127.0.0.1:9/", "base_url": "http://127.0.0.1:9/"}
        self.save_ws()

    def test_record_without_create_time_is_not_killed(self):
        fake_env_record("React", self.wt, create_time=None)
        with mock.patch.object(envmod, "kill_tree") as kill:
            with self.assertRaises(core.FactoryError) as ctx:
                envmod.down(self.ws, "React")
            ok, lines = envmod.up(self.ws, "React", core.run_load(self.rid))
        kill.assert_not_called()
        self.assertIn("NOT killed", str(ctx.exception))
        self.assertFalse(ok)
        self.assertIn("Ask the user", lines[-1])
        self.assertTrue(envmod.pid_path("ws", "React").exists())
        self.assertIn("cannot be verified", envmod.binding_error(self.ws, "React", self.wt, core.git_head(self.wt)))

    def test_unreadable_current_create_time_is_not_killed(self):
        fake_env_record("React", self.wt)
        with mock.patch.object(envmod, "kill_tree") as kill, \
                mock.patch.object(envmod, "process_create_time", return_value=None):
            with self.assertRaises(core.FactoryError):
                envmod.down(self.ws, "React")
            self.assertEqual(envmod.record_state(envmod.read_record("ws", "React")), "unknown")
        kill.assert_not_called()


class PipelineWaiverTests(IntegrityCase):
    """Probe E: a pipeline waiver is a user decision with the user's words and the pipeline id."""

    def setUp(self) -> None:
        super().setUp()
        plan = self.shipped()
        change = plan["changes"][0]
        self.tag = f"{change['repo']}-{change['target']}"
        from tests.change_support import put_change, put_pipeline
        put_change(self.rid, change["repo"], change["target"], source_branch=change["source_branch"],
                   target_branch=change["target_branch"], title=change["title"], draft=change["draft"], head_sha=change["head_sha"],
                   threads=[], pipelines=[{"id": "42", "status": "failed", "sha": change["head_sha"]}])
        put_pipeline(self.rid, change["repo"], change["target"], id="42", status="failed", sha=change["head_sha"])

    def test_by_user_needs_user_said(self):
        rc, out = cli("run", "decide", self.rid, "pipeline #42: skip it", "--by", "user")
        self.assertNotEqual(rc, 0)
        self.assertIn("--user-said", out)

    def test_waiver_must_name_the_pipeline_id(self):
        self.ok("run", "decide", self.rid, "pipeline: skip it", "--by", "user", "--user-said", "skip CI")
        self.ok("run", "decide", self.rid, "pipeline #420 is flaky", "--by", "user", "--user-said", "skip 420")
        rc, out = self.gate("feedback")
        self.assertEqual(rc, 1)
        self.assertIn("pipeline #42 status 'failed'", out)
        self.ok("run", "decide", self.rid, "pipeline #42: flaky e2e, accepted", "--by", "user",
                "--user-said", "the e2e job is flaky, ship it")
        self.ok("gate", self.rid, "feedback")
        d = core.run_load(self.rid)["decisions"][-1]
        self.assertEqual(d["user_said"], "the e2e job is flaky, ship it")


if __name__ == "__main__":
    unittest.main()
