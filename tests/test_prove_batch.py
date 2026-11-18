"""Batched browser checks (DESIGN section 14) and `prove --estimate`."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_failfast_prove import ProveCase, cli  # noqa: E402

from factory import core  # noqa: E402
from factory.collectors import api as api_collector  # noqa: E402
from factory.collectors import browser  # noqa: E402
from factory.commands import prove  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_batch_cli.py"))


class BatchCase(ProveCase):
    def setUp(self):
        super().setUp()
        self.log_file = self.tmp / "fake.log"
        self.cfg_file = self.tmp / "fake.json"
        p = mock.patch.dict(os.environ, {"FACTORY_NODE_CLI": FAKE, "FAKE_CFG": str(self.cfg_file), "FAKE_LOG": str(self.log_file)})
        p.start()
        self.addCleanup(p.stop)
        self.fake()

    def fake(self, **cfg):
        self.cfg_file.write_text(json.dumps(cfg), encoding="utf-8")

    def calls(self):
        if not self.log_file.is_file():
            return []
        return [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]

    def bcheck(self, cid, **extra):
        spec = self.home / "runs/r1/checks" / f"{cid}.spec.ts"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("// spec", encoding="utf-8")
        return {"id": cid, "type": "browser", "repo": "Web", "spec": f"checks/{cid}.spec.ts", "timeout_s": 20, **extra}

    def three(self):
        self.acs([self.bcheck("B1"), self.bcheck("B2"), self.bcheck("B3")])


class BatchTests(BatchCase):
    def test_checks_of_one_repo_run_in_one_process_and_split_into_records(self):
        self.three()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        calls = self.calls()
        self.assertEqual([c["cmd"] for c in calls], ["run-specs"])
        self.assertEqual(calls[0]["ids"], ["B1", "B2", "B3"])  # prove order kept
        self.assertEqual(calls[0]["caps"], [17000] * 3)  # per-check caps (20 s minus the CLI margin)
        self.assertEqual(calls[0]["timeout_ms"], str((60 - browser.CLI_MARGIN_S) * 1000))  # overall = sum of the caps
        self.assertIsNone(calls[0]["max_failures"])
        for i, cid in enumerate(("B1", "B2", "B3")):
            rec = self.evidence(cid)
            self.assertEqual((rec["status"], rec["type"], rec["repo"]), ("pass", "browser", "Web"))
            self.assertTrue(rec["details"]["batched"])
            self.assertEqual(rec["duration_s"], 1.5 + i * 0.5)  # from the reporter, per check
            self.assertEqual(rec["artifacts"], [f"evidence/{cid}/report.json", f"evidence/{cid}/run.log"])
            self.assertRegex(rec["collector"], r"^factory/\S+ browser$")
            self.assertEqual(rec["details"]["mode"], "default")

    def test_opt_out_runs_one_process_per_check(self):
        self.ws["limits"] = {"browser_batch": False}
        self.write("workspaces/ws/workspace.json", self.ws)
        self.three()
        self.assertEqual(cli("prove", "r1")[0], 0)
        self.assertEqual([c["cmd"] for c in self.calls()], ["run-spec"] * 3)
        self.assertNotIn("batched", self.evidence("B1")["details"])

    def test_a_lone_browser_check_is_not_batched(self):
        self.acs([self.bcheck("B1"), self.api_check("A1")])
        self.set_ws(self.live)
        self.assertEqual(cli("prove", "r1")[0], 0)
        self.assertEqual([c["cmd"] for c in self.calls()], ["run-spec"])

    def test_reused_checks_stay_out_of_the_batch(self):
        self.three()
        self.assertEqual(cli("prove", "r1")[0], 0)
        self.log_file.unlink()
        self.acs([self.bcheck("B1"), self.bcheck("B2"), self.bcheck("B3", timeout_s=30), self.bcheck("B4")])
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertIn("REUSED B1", out)
        self.assertIn("REUSED B2", out)
        self.assertEqual([c["ids"] for c in self.calls()], [["B4", "B3"]])

    def test_failure_without_bail_does_not_stop_the_batch_and_flaky_is_retried_alone(self):
        self.fake(statuses={"B2": "fail"}, single={"B2": "pass"})
        self.three()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 1, out)
        self.assertEqual([(c["cmd"], c["ids"]) for c in self.calls()],
                         [("run-specs", ["B1", "B2", "B3"]), ("run-spec", ["B2"])])  # only the failed check is retried
        rec = self.evidence("B2")
        self.assertEqual(rec["status"], "fail")
        self.assertTrue(rec["details"]["flaky"])
        self.assertTrue(rec["summary"].startswith("FLAKY:"))
        self.assertIn("PROVE-FAIL check=B2 kind=flaky", out)
        self.assertEqual(self.evidence("B3")["status"], "pass")

    def test_no_retry_flag_is_honoured(self):
        self.fake(statuses={"B2": "fail"}, single={"B2": "pass"})
        self.three()
        self.assertEqual(cli("prove", "r1", "--no-retry")[0], 1)
        self.assertEqual([c["cmd"] for c in self.calls()], ["run-specs"])

    def test_fast_bails_with_max_failures_and_records_the_rest_as_skipped(self):
        self.fake(statuses={"B2": "fail"})
        self.three()
        rc, out, _ = cli("prove", "r1", "--fast")
        self.assertEqual(rc, 1, out)
        calls = self.calls()
        self.assertEqual(calls[0]["max_failures"], "1")
        self.assertEqual([c["cmd"] for c in calls], ["run-specs", "run-spec"])  # the retry of B2 only
        self.assertEqual(self.evidence("B1")["status"], "pass")
        self.assertEqual(self.evidence("B2")["status"], "fail")
        rec = self.evidence("B3")
        self.assertTrue(rec["summary"].startswith("skipped: bail (not run: B2 failed"), rec["summary"])
        self.assertEqual(rec["details"]["skipped"], "bail")

    def test_env_failure_in_the_batch_stops_the_repo(self):
        self.fake(statuses={"B1": "error"}, error="net::ERR_CONNECTION_REFUSED at http://x")
        self.three()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 3, out)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("ENV-FAIL")), 1)
        for cid in ("B2", "B3"):
            self.assertEqual(self.evidence(cid)["details"]["skipped"], "env")

    def test_batch_crash_falls_back_to_one_by_one(self):
        self.fake(mode="crash")
        self.three()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertIn("could not run as one process; running them one by one", out)
        self.assertEqual([c["cmd"] for c in self.calls()], ["run-specs", "run-spec", "run-spec", "run-spec"])
        self.assertNotIn("batched", self.evidence("B1")["details"])

    def test_unreadable_batch_report_falls_back(self):
        self.fake(mode="garbage")
        self.three()
        self.assertEqual(cli("prove", "r1")[0], 0)
        self.assertEqual([c["cmd"] for c in self.calls()].count("run-spec"), 3)

    def test_collector_exception_falls_back(self):
        self.three()
        with mock.patch.object(browser, "collect_batch", side_effect=RuntimeError("boom")):
            rc, out, err = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual([c["cmd"] for c in self.calls()], ["run-spec"] * 3)

    def test_checks_playwright_did_not_reach_run_alone(self):
        self.fake(mode="interrupt")  # B2 and B3 not run (global timeout), no failure to explain it
        self.three()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual([(c["cmd"], c["ids"]) for c in self.calls()],
                         [("run-specs", ["B1", "B2", "B3"]), ("run-spec", ["B2"]), ("run-spec", ["B3"])])
        self.assertTrue(self.evidence("B1")["details"]["batched"])
        self.assertNotIn("batched", self.evidence("B2")["details"])

    def test_unusable_spec_gets_its_own_error_and_the_rest_still_batch(self):
        self.three()
        (self.home / "runs/r1/checks/B2.spec.ts").unlink()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 1, out)
        self.assertEqual([c["ids"] for c in self.calls()], [["B1", "B3"]])
        self.assertIn("spec not found", self.evidence("B2")["summary"])

    def test_stale_env_binding_is_recorded_per_check_without_a_batch(self):
        prove.env_binding.return_value = "env Web: started at abcd1234, worktree HEAD is ffff0000; `factory env down`"
        self.three()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.calls(), [])


class CollectBatchUnitTests(BatchCase):
    def test_fewer_than_two_usable_specs_is_no_batch(self):
        run = core.run_load("r1")
        ws = core.workspace_load("ws")
        items = []
        for cid in ("B1", "B2"):
            chk, ctx, _, _ = prove.check_ctx(run, ws, self.bcheck(cid), None)
            items.append((chk, ctx))
        (self.home / "runs/r1/checks/B2.spec.ts").unlink()
        self.assertIsNone(browser.collect_batch(items))
        self.assertEqual(self.calls(), [])


class EstimateTests(BatchCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)

    def spy(self):
        return mock.patch.object(api_collector, "collect", wraps=api_collector.collect)

    def test_estimate_runs_nothing_and_writes_nothing(self):
        self.acs([self.api_check("A1"), self.bcheck("B1"), self.bcheck("B2"), {"id": "T1", "type": "test", "repo": "Web", "cmd": "true"}])
        before = {p.name for p in (self.home / "runs/r1").rglob("*")}
        with self.spy() as spy:
            rc, out, _ = cli("prove", "r1", "--estimate")
        self.assertEqual(rc, 0, out)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual({p.name for p in (self.home / "runs/r1").rglob("*")}, before)
        lines = out.splitlines()
        self.assertEqual([l.split()[1] for l in lines[:4]], ["T1", "A1", "B1", "B2"])  # prove order: cheapest first
        self.assertTrue(all(l.startswith("RUN") for l in lines[:4]))
        self.assertIn("~10s", lines[0])
        self.assertIn("~15s", lines[1])
        self.assertIn("~60s", lines[2])
        self.assertIn("total ~145s", lines[-1])
        self.assertIn("--fast ~145s", lines[-1])
        self.assertIn("--final ~145s", lines[-1])

    def test_estimate_uses_last_duration_and_marks_reused(self):
        self.acs([self.api_check("A1"), self.api_check("A2", path="/y")])
        self.assertEqual(cli("prove", "r1")[0], 0)
        durations = {"A1": 4.0, "A2": 6.0}  # evidence files cannot be edited: reused only when untouched
        self.acs([self.api_check("A1"), self.api_check("A2", path="/z")])  # A2 changed: runs again
        with mock.patch.object(prove, "_last_duration", side_effect=lambda run, eid: durations.get(eid)):
            rc, out, _ = cli("--json", "prove", "r1", "--estimate")
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        rows = {r["check_id"]: r for r in data["checks"]}
        self.assertEqual((rows["A1"]["action"], rows["A1"]["expected_s"]), ("REUSED", 0.0))
        self.assertEqual((rows["A2"]["action"], rows["A2"]["expected_s"], rows["A2"]["last_s"]), ("RUN", 6.0, 6.0))
        self.assertEqual((data["total_s"], data["fast_s"], data["final_s"]), (6.0, 6.0, 10.0))
        with mock.patch.object(prove, "_last_duration", side_effect=lambda run, eid: durations.get(eid)):
            rc, out, _ = cli("prove", "r1", "--estimate", "--final")
        self.assertIn("RUN    A1", out)
        self.assertIn("total ~10s (final)", out)

    def test_quality_and_unknown_check(self):
        self.acs([self.api_check("A1")])
        rc, out, _ = cli("prove", "r1", "--estimate", "--check", "NOPE")
        self.assertEqual(rc, 2, out)


if __name__ == "__main__":
    unittest.main()
