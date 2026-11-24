"""Evidence history, failure reason/kind on events, `SSL Authentication Error` as env kind db, flaky detection (DESIGN 2.4, 13)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_failfast_prove import ProveCase, cli  # noqa: E402

from factory import core  # noqa: E402
from factory.commands import metrics, prove  # noqa: E402

FAIL = {"status": "fail", "summary": "expect(received).toBe(1)\nmore text"}
PASS = {"status": "pass", "summary": "ok"}
SSL = {"status": "fail", "summary": "api check mismatch", "details": {"body_tail": "500 SSL Authentication Error at db"}}


class Base(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)
        self.acs([{"id": "B1", "type": "browser", "repo": "Web", "spec": "x"}])

    def prove(self, *results, args=("--no-retry",)):
        with mock.patch.object(prove, "dispatch", side_effect=list(results)):
            return cli("prove", "r1", *args)

    def events(self):
        return [e["data"] for e in core.run_load("r1")["events"] if e["kind"] == "evidence"]

    def history(self, cid="B1"):
        return core.evidence_history("r1", cid)


class History(Base):
    def test_previous_attempts_are_kept_and_latest_stays_in_place(self):
        self.prove(FAIL)
        self.prove(PASS)
        self.assertEqual(self.evidence("B1")["status"], "pass")  # readers of evidence/<id>.json unchanged
        hist = self.history()
        self.assertEqual([h["status"] for h in hist], ["fail"])
        self.assertTrue((self.home / "runs/r1/evidence/history/B1.jsonl").is_file())
        self.assertEqual(self.history()[0]["failure"]["kind"], "assert")

    def test_history_is_capped(self):
        results = [FAIL if i % 2 == 0 else PASS for i in range(core.HISTORY_CAP + 6)]
        for r in results:
            self.prove(r, args=("--no-retry", "--all"))
        self.assertEqual(len(self.history()), core.HISTORY_CAP)

    def test_gate_reading_is_unchanged(self):
        self.prove(FAIL)
        self.prove(PASS)
        run = core.run_load("r1")
        from factory import integrity
        self.assertIsNone(integrity.evidence_error(run, "B1"))


class EventReason(Base):
    def test_failure_event_carries_reason_and_kind(self):
        self.prove(FAIL)
        ev = self.events()[-1]
        self.assertEqual((ev["status"], ev["kind"]), ("fail", "assert"))
        self.assertEqual(ev["reason"], "expect(received).toBe(1)")
        self.assertEqual(self.evidence("B1")["failure"]["kind"], "assert")

    def test_pass_event_has_neither(self):
        self.prove(PASS)
        ev = self.events()[-1]
        self.assertNotIn("reason", ev)
        self.assertNotIn("kind", ev)
        self.assertNotIn("failure", self.evidence("B1"))

    def test_timeout_kind(self):
        self.prove({"status": "fail", "summary": "Timeout 30000ms exceeded."})
        self.assertEqual(self.events()[-1]["kind"], "timeout")


class SslClassification(Base):
    def test_ssl_authentication_error_is_env_fail_db(self):
        self.acs([{"id": "A1", "type": "api", "repo": "Web", "path": "/x", "expect": {"status": 200}},
                  {"id": "A2", "type": "api", "repo": "Web", "path": "/y", "expect": {"status": 200}}])
        rc, out, err = None, None, None
        with mock.patch.object(prove, "dispatch", return_value=SSL) as d:
            rc, out, err = cli("prove", "r1")
        self.assertEqual(rc, 3, out)  # env failure, not a check failure
        self.assertIn("ENV-FAIL repo=Web kind=db", out)
        self.assertEqual(d.call_count, 1)  # the remaining runtime checks are skipped like any env failure
        self.assertEqual(self.events()[0]["kind"], "env")
        self.assertEqual(self.evidence("A1")["failure"]["env_kind"], "db")

    def test_table_is_extensible(self):
        import re
        with mock.patch.object(prove, "ENV_BODY_CLASSES", prove.ENV_BODY_CLASSES + [(re.compile("quota exhausted"), "quota")]):
            rec = {"type": "api", "status": "fail", "summary": "x", "details": {"body_tail": "quota exhausted"}}
            self.assertEqual(prove.classify(rec), ("env", "quota"))
        rec = {"type": "api", "status": "fail", "summary": "x", "details": {"body_tail": "quota exhausted"}}
        self.assertEqual(prove.classify(rec), ("assert", None))

    def test_a_test_check_mentioning_it_is_not_env(self):
        rec = {"type": "test", "status": "fail", "summary": "SSL Authentication Error", "details": {}}
        self.assertEqual(prove.classify(rec), ("assert", None))


class Flaky(Base):
    def test_fail_then_pass_same_sha_and_digest_is_flaky(self):
        self.prove(FAIL)
        self.assertNotIn("flaky", self.evidence("B1"))
        rc, out, _ = self.prove(PASS)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.evidence("B1")["flaky"])
        self.assertIn("FLAKY: B1", out)
        self.assertEqual(core.flaky_checks("r1"), ["B1"])

    def test_pass_only_is_not_flaky(self):
        self.prove(PASS)
        self.assertNotIn("flaky", self.evidence("B1"))

    def test_two_passes_are_not_flaky(self):
        self.prove(PASS)
        self.prove(PASS, args=("--no-retry", "--all"))
        self.assertNotIn("flaky", self.evidence("B1"))

    def test_another_sha_is_not_flaky(self):
        self.prove(FAIL)
        (self.web / "n.txt").write_text("1")
        from ops_helpers import git
        git(self.web, "add", ".")
        git(self.web, "commit", "-qm", "more")
        self.prove(PASS)
        self.assertNotIn("flaky", self.evidence("B1"))

    def test_another_check_digest_is_not_flaky(self):
        self.prove(FAIL)
        self.acs([{"id": "B1", "type": "browser", "repo": "Web", "spec": "changed"}])
        self.prove(PASS)
        self.assertNotIn("flaky", self.evidence("B1"))

    def test_metrics_counts_it_and_status_shows_it(self):
        self.prove(FAIL)
        self.prove(PASS)
        run = core.run_load("r1")
        self.assertEqual(metrics.check_stats(run, core.run_dir("r1"))["flaky"], 1)
        rc, out, _ = cli("run", "status", "r1")
        self.assertIn("flaky: B1", out)
        rc, out, _ = cli("--json", "run", "status", "r1")
        self.assertEqual(json.loads(out)["flaky"], ["B1"])

    def test_no_gate_behaviour_change(self):
        self.prove(FAIL)
        self.prove(PASS)
        from factory import integrity
        self.assertIsNone(integrity.evidence_error(core.run_load("r1"), "B1"))


if __name__ == "__main__":
    unittest.main()
