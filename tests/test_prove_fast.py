"""Fast verification (DESIGN section 14): risk order, bail, evidence reuse, modes, hot reload, amend-check."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_support  # noqa: E402
from ops_helpers import git  # noqa: E402
from test_failfast_prove import ProveCase, cli  # noqa: E402

from factory import core, integrity  # noqa: E402
from factory.collectors import api as api_collector  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import gate, prove, workspace  # noqa: E402


class FastCase(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)

    def spy(self):
        return mock.patch.object(api_collector, "collect", wraps=api_collector.collect)

    def run_events(self, kind):
        return [e["data"] for e in core.run_load("r1")["events"] if e["kind"] == kind]

    def commit_more(self):
        (self.web / "b.txt").write_text("2")
        git(self.web, "add", ".")
        git(self.web, "commit", "-qm", "c2")


class OrderTests(FastCase):
    def order(self):
        run = core.run_load("r1")
        checks = json.loads((self.home / "runs/r1/acs.json").read_text())["checks"]
        return [c["id"] for c in prove.order_checks(run, core.workspace_load("ws"), checks)]

    def test_cheapest_type_first_without_evidence(self):
        self.acs([{"id": "B", "type": "browser", "repo": "Web", "spec": "x"}, self.api_check("A"),
                  {"id": "T", "type": "test", "repo": "Web", "cmd": "true"},
                  {"id": "V", "type": "visual", "repo": "Web", "path": "/"},
                  {"id": "S", "type": "style", "repo": "Web", "path": "/"}])
        self.assertEqual(self.order(), ["T", "A", "S", "V", "B"])

    def test_failed_first_then_changed_then_moved_then_rest(self):
        self.acs([self.api_check("A1"), self.api_check("A2", status=201), self.api_check("A3"), self.api_check("A4")])
        cli("prove", "r1")  # A1 A3 A4 pass, A2 fails
        self.acs([self.api_check("A1"), self.api_check("A2", status=201), self.api_check("A3", path="/y"),
                  self.api_check("A4"), self.api_check("A5")])
        def grouped(order):
            return order[0], set(order[1:3]), set(order[3:])

        self.assertEqual(grouped(self.order()), ("A2", {"A3", "A5"}, {"A1", "A4"}))
        self.commit_more()  # A1/A4 passed on an older sha now: group (c) instead of (d), still after (a) and (b)
        self.assertEqual(grouped(self.order()), ("A2", {"A3", "A5"}, {"A1", "A4"}))

    def test_shorter_last_duration_first_inside_a_group(self):
        self.acs([self.api_check("A1"), self.api_check("A2")])
        cli("prove", "r1")
        for cid, duration in (("A1", 9.0), ("A2", 1.0)):
            path = self.home / "runs/r1/evidence" / f"{cid}.json"
            ev = self.evidence(cid)
            ev["duration_s"] = duration
            path.write_text(json.dumps(ev), encoding="utf-8")
        self.assertEqual(self.order(), ["A2", "A1"])


class BailTests(FastCase):
    def test_bail_skips_remaining_runtime_checks(self):
        self.acs([self.api_check("A1", status=201), self.api_check("A2"), self.api_check("A3", status=202)])
        with self.spy() as spy:
            rc, out, _ = cli("prove", "r1", "--bail")
        self.assertEqual(rc, 1, out)
        self.assertEqual(spy.call_count, 1)
        for cid in ("A2", "A3"):
            rec = self.evidence(cid)
            self.assertTrue(rec["summary"].startswith("skipped: bail (not run: A1 failed"), rec["summary"])
            self.assertEqual(rec["details"]["skipped"], "bail")

    def test_without_bail_different_failures_continue(self):
        self.acs([self.api_check("A1", status=201), self.api_check("A2")])
        with self.spy() as spy:
            self.assertEqual(cli("prove", "r1")[0], 1)
        self.assertEqual(spy.call_count, 2)

    def test_final_never_bails(self):
        self.acs([self.api_check("A1", status=201), self.api_check("A2")])
        with self.spy() as spy:
            self.assertEqual(cli("prove", "r1", "--final", "--bail")[0], 1)
        self.assertEqual(spy.call_count, 2)

    def test_fast_and_final_are_exclusive(self):
        self.acs([self.api_check("A1")])
        self.assertEqual(cli("prove", "r1", "--fast", "--final")[0], 2)


class ReuseTests(FastCase):
    def setUp(self):
        super().setUp()
        self.acs([self.api_check("A1"), self.api_check("A2")])
        self.assertEqual(cli("prove", "r1")[0], 0)
        self.file = self.home / "runs/r1/evidence/A1.json"

    def test_same_sha_and_digest_is_reused_and_not_rewritten(self):
        before = self.file.read_bytes()
        with self.spy() as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(spy.call_count, 0)
        self.assertIn("REUSED A1", out)
        self.assertIn("REUSED A2", out)
        self.assertEqual(self.file.read_bytes(), before)
        self.assertEqual(len([e for e in core.run_load("r1")["events"] if e["kind"] == "evidence"]), 2)
        last = self.run_events("prove")[-1]
        self.assertEqual((last["reused"], last["ran"]), (2, 0))

    def test_new_sha_reruns(self):
        self.commit_more()
        with self.spy() as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(spy.call_count, 2)
        self.assertNotIn("REUSED", out)

    def test_changed_check_reruns_only_that_check(self):
        self.acs([self.api_check("A1", path="/other"), self.api_check("A2")])
        with self.spy() as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(spy.call_count, 1)
        self.assertIn("REUSED A2", out)
        self.assertNotIn("REUSED A1", out)

    def test_dirty_worktree_reruns(self):
        (self.web / "dirty.txt").write_text("x")
        with self.spy() as spy:
            cli("prove", "r1")
        self.assertEqual(spy.call_count, 2)

    def test_all_and_final_force_rerun(self):
        for flag in ("--all", "--final"):
            with self.spy() as spy:
                rc, out, _ = cli("prove", "r1", flag)
            self.assertEqual(spy.call_count, 2, flag)
            self.assertNotIn("REUSED", out)

    def test_failed_evidence_is_not_reused(self):
        self.acs([self.api_check("A1", status=201), self.api_check("A2")])
        cli("prove", "r1")
        with self.spy() as spy:
            cli("prove", "r1")
        self.assertEqual(spy.call_count, 1)  # A1 again, A2 reused


class ModeGateTests(FastCase):
    def errors(self, check):
        return gate._evidence_errors("r1", core.run_load("r1"), core.workspace_load("ws"), check)

    def test_gate_refuses_fast_evidence_until_final(self):
        chk = self.api_check("A1")
        self.acs([chk])
        cli("prove", "r1", "--fast")
        self.assertEqual(self.evidence("A1")["details"]["mode"], "fast")
        self.assertTrue(any("--final" in e for e in self.errors(chk)), self.errors(chk))
        rc, out, _ = cli("prove", "r1", "--final")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.evidence("A1")["details"]["mode"], "final")
        self.assertEqual(self.errors(chk), [])
        self.assertEqual(self.run_events("prove")[-1]["mode"], "final")

    def test_default_mode_is_accepted(self):
        chk = self.api_check("A1")
        self.acs([chk])
        cli("prove", "r1")
        self.assertEqual(self.evidence("A1")["details"]["mode"], "default")
        self.assertEqual(self.errors(chk), [])


class HotReloadTests(unittest.TestCase):
    def stale(self, hot, cwd_ok=True):
        ws = {"name": "ws", "repos": [{"alias": "Web", "name": "web", "path": "/p", "env": {"hot_reload": hot}}]}
        rec = {"pid": 1, "cwd": str(Path("/wt")) if cwd_ok else "/other", "head": "old"}
        with mock.patch.object(envmod, "read_record", return_value=rec), \
                mock.patch.object(envmod, "record_state", return_value="same"), \
                mock.patch.object(envmod, "is_protected_pid", return_value=False), \
                mock.patch.object(envmod, "_cwd_for", return_value=Path("/wt")), \
                mock.patch.object(envmod, "_same_path", side_effect=lambda a, b: str(a) == str(b)), \
                mock.patch.object(Path, "is_dir", return_value=True), \
                mock.patch.object(envmod, "_head_of", return_value="aaa"):
            return envmod.stale_reason(ws, {"id": "r"}, "Web")

    def test_head_mismatch_is_stale_without_hot_reload(self):
        self.assertIn("started at", self.stale(False))

    def test_head_mismatch_ignored_with_hot_reload(self):
        self.assertIsNone(self.stale(True))

    def test_cwd_mismatch_still_stale_with_hot_reload(self):
        self.assertIn("serves", self.stale(True, cwd_ok=False))

    def test_workspace_check_validates_keys(self):
        ws = {"worktree_root": "x", "repos": [{"alias": "A", "path": "x", "quality": {"a": "b"},
                                               "env": {"start": "s", "health": "h", "base_url": "b", "hot_reload": "yes"}}],
              "autonomy": {"allow_timing_amends": 1}}
        errors = workspace.check_workspace(ws)
        self.assertTrue(any("env.hot_reload" in e for e in errors), errors)
        self.assertTrue(any("allow_timing_amends" in e for e in errors), errors)


class AmendTests(core_support.FactoryCase):
    def start(self, checks, **autonomy):
        self.make_run()
        if autonomy:
            ws = core.workspace_load("ws")
            ws["autonomy"].update(autonomy)
            core.write_json(core.workspace_path("ws"), ws)
        (core.run_dir(self.run_id) / "checks").mkdir(parents=True, exist_ok=True)
        (core.run_dir(self.run_id) / "checks" / "a.spec.ts").write_text("one", encoding="utf-8")
        self.write_acs([{"id": "AC-1", "text": "t", "kind": "functional", "checks": [c["id"] for c in checks]}], checks)
        self.set_phase("prove")

    def browser(self, **extra):
        return {"id": "CHK-1", "type": "browser", "repo": "React", "spec": "checks/a.spec.ts", "timeout_s": 60, **extra}

    def edit(self, **changes):
        path = core.run_dir(self.run_id) / "acs.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["checks"][0].update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def amend(self, kind="timing", *extra):
        return core_support.cli("run", "amend-check", self.run_id, "CHK-1", "--kind", kind, "--reason", "slow page", *extra)

    def test_timing_amend_relocks_and_counts_like_a_replan(self):
        self.start([self.browser()])
        self.edit(timeout_s=120, wait_for="#ready")
        rc, out = self.amend()
        self.assertEqual(rc, 0, out)
        run = core.run_load(self.run_id)
        self.assertEqual(run["phase"], "prove")
        self.assertEqual(run["loops"]["amend"], 1)  # counts against max_replans, but is not a replan
        self.assertNotIn("replan", run["loops"])
        self.assertEqual(integrity.lock_errors(run), [])
        self.assertEqual(run["plan_lock"]["checks"]["CHK-1"]["timeout_s"], 120)
        ev = [e for e in run["events"] if e["kind"] == "amend_check"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["data"]["fields"], ["timeout_s", "wait_for"])

    def test_timing_amend_is_free_when_allowed(self):
        self.start([self.browser()], allow_timing_amends=True, max_replans=0)
        self.edit(timeout_s=120)
        rc, out = self.amend()
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.run_id)["loops"].get("amend", 0), 0)

    def test_selector_amend_always_counts_and_allows_spec_changes(self):
        self.start([self.browser()], allow_timing_amends=True)
        (core.run_dir(self.run_id) / "checks" / "a.spec.ts").write_text("two", encoding="utf-8")
        rc, out = self.amend("selector")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.run_id)["loops"]["amend"], 1)

    def test_timing_amend_refuses_spec_change_and_other_fields(self):
        self.start([self.browser()])
        spec = core.run_dir(self.run_id) / "checks" / "a.spec.ts"
        spec.write_text("two", encoding="utf-8")
        rc, out = self.amend()
        self.assertNotEqual(rc, 0)
        self.assertIn("checks/a.spec.ts", out)
        spec.write_text("one", encoding="utf-8")
        self.edit(path="/other", timeout_s=99)
        rc, out = self.amend()
        self.assertNotEqual(rc, 0)
        self.assertIn("path", out)
        self.assertEqual(core.run_load(self.run_id)["loops"].get("replan", 0), 0)

    def test_refuses_changes_to_other_checks_or_acs(self):
        self.start([self.browser(), {"id": "CHK-2", "type": "test", "repo": "React", "cmd": "true"}])
        path = core.run_dir(self.run_id) / "acs.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["checks"][1]["cmd"] = "false"
        data["acs"][0]["text"] = "other"
        data["checks"][0]["timeout_s"] = 5
        path.write_text(json.dumps(data), encoding="utf-8")
        rc, out = self.amend()
        self.assertNotEqual(rc, 0)
        self.assertIn("CHK-2", out)
        self.assertIn("outside `checks`", out)

    def test_cap_blocks_agent_but_not_user(self):
        self.start([self.browser()], max_replans=0)
        self.edit(timeout_s=120)
        rc, out = self.amend()
        self.assertNotEqual(rc, 0)
        self.assertIn("max_replans", out)
        rc, out = self.amend("timing", "--by", "user")
        self.assertNotEqual(rc, 0)  # --user-said missing
        rc, out = self.amend("timing", "--by", "user", "--user-said", "ok go")
        self.assertEqual(rc, 0, out)

    def test_refused_before_the_plan_lock(self):
        self.make_run()
        self.write_acs([], [self.browser()])
        rc, out = self.amend()
        self.assertNotEqual(rc, 0)
        self.assertIn("no plan lock", out)

    def test_evidence_before_an_amend_is_stale(self):
        run = {"id": "x"}
        self.make_run()
        run = core.run_load(self.run_id)
        core.write_json(core.evidence_file(self.run_id, "CHK-1"), {"status": "pass"})
        core.event(run, "evidence", check="CHK-1", digest=core.file_sha256(core.evidence_file(self.run_id, "CHK-1")))
        self.assertIsNone(integrity.evidence_error(run, "CHK-1"))
        core.event(run, "amend_check", check="CHK-1")
        self.assertIn("amended", integrity.evidence_error(run, "CHK-1"))


if __name__ == "__main__":
    unittest.main()
