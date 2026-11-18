import json
import sys as _sys
import unittest
from pathlib import Path as _Path
from unittest import mock

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from ops_helpers import FactoryCase

from factory.commands import batch, metrics


def line(**kw):
    return json.dumps(kw) + "\n"


def ph(at, to, frm=None):
    return {"at": at, "kind": "phase", "data": {"from": frm, "to": to}}


class TimingMetricsTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "claude" / "projects" / "proj"
        self.root.mkdir(parents=True)
        p = mock.patch.object(metrics, "claude_roots", lambda: [self.root.parent])
        p.start()
        self.addCleanup(p.stop)

    def test_phase_prove_checks(self):
        events = [
            ph("2026-01-01T00:00:00+00:00", "build"),
            ph("2026-01-01T01:00:00+00:00", "prove", "build"),
            {"at": "2026-01-01T01:10:00+00:00", "kind": "prove", "data": {"mode": "fast", "ran": 3, "reused": 0, "skipped": 0, "failed": 1, "wall_s": 60, "bail": False}},
            ph("2026-01-01T02:00:00+00:00", "build", "prove"),
            ph("2026-01-01T02:30:00+00:00", "prove", "build"),
            {"at": "2026-01-01T02:40:00+00:00", "kind": "prove", "data": {"mode": "final", "ran": 4, "reused": 1, "skipped": 0, "failed": 0, "wall_s": 30, "bail": False}},
            {"at": "2026-01-01T01:05:00+00:00", "kind": "evidence", "data": {"check": "CHK-1", "status": "fail"}},
            {"at": "2026-01-01T02:35:00+00:00", "kind": "evidence", "data": {"check": "CHK-1", "status": "pass"}},
            {"at": "2026-01-01T02:36:00+00:00", "kind": "evidence", "data": {"check": "CHK-2", "status": "pass"}},
        ]
        self.make_run("ws-dev-1", events=events, updated="2026-01-01T03:30:00+00:00", phase="prove")
        self.write("runs/ws-dev-1/evidence/CHK-1.json", {"id": "CHK-1", "type": "browser", "duration_s": 90.5, "status": "pass"})
        self.write("runs/ws-dev-1/evidence/CHK-2.json", {"id": "CHK-2", "type": "unit", "duration_s": 2, "details": {"flaky": True}})
        m = metrics.compute("ws-dev-1")
        self.assertEqual(m["phase_hours"], {"build": 1.5, "prove": 2.0})
        self.assertEqual(m["prove"]["invocations"], 2)
        self.assertEqual(m["prove"]["phase_entries"], 2)
        self.assertEqual(m["prove"]["by_mode"], {"fast": 1, "final": 1})
        self.assertEqual(m["prove"]["wall_s"], 90.0)
        c = m["checks"]
        self.assertEqual(c["executions"], 3)
        self.assertEqual(c["by_status"], {"fail": 1, "pass": 2})
        self.assertEqual(c["top_slowest"][0], {"id": "CHK-1", "type": "browser", "duration_s": 90.5})
        self.assertEqual(c["most_rerun"][0], {"id": "CHK-1", "count": 2})
        self.assertEqual(c["flaky"], 1)
        self.assertIn("prove 2 runs", metrics._summary(m))

    def test_child_tokens_from_parent(self):
        self.make_run("ws-epic-1", kind="epic", children=["ws-dev-1", "ws-dev-2"], sessions=["sessp"])
        self.make_run("ws-dev-1", parent="ws-epic-1", created="2026-01-01T00:10:00+00:00", updated="2026-01-01T00:50:00+00:00")
        self.make_run("ws-dev-2", parent="ws-epic-1", created="2026-01-01T00:10:00+00:00", updated="2026-01-01T00:50:00+00:00")
        (self.root / "sessp.jsonl").write_text(
            line(type="assistant", timestamp="2026-01-01T00:20:00Z", message={
                "id": "a1", "usage": {"input_tokens": 1, "output_tokens": 7}, "content": [{"type": "text", "text": "factory-run: ws-dev-1"}]})
            + line(type="assistant", timestamp="2026-01-01T00:30:00Z", message={
                "id": "a2", "usage": {"input_tokens": 1, "output_tokens": 100}, "content": [{"type": "text", "text": "nothing"}]})
            + line(type="assistant", timestamp="2026-01-01T05:00:00Z", message={
                "id": "a3", "usage": {"input_tokens": 1, "output_tokens": 50}, "content": [{"type": "text", "text": "ws-dev-1 late"}]}),
            encoding="utf-8")
        m1 = metrics.compute("ws-dev-1")
        self.assertEqual(m1["tokens"]["total"]["output"], 7)
        m2 = metrics.compute("ws-dev-2")
        self.assertIsNone(m2["tokens"])
        self.assertEqual(m2["tokens_note"], "shared with parent ws-epic-1")
        self.assertIn("null", metrics._summary(m2))

    def test_batch_last_prove(self):
        self.assertEqual(batch.last_prove({"events": []}), "-")
        run = {"events": [
            {"kind": "prove", "data": {"mode": "fast", "ran": 1, "reused": 0, "failed": 0, "wall_s": 5}},
            {"kind": "prove", "data": {"mode": "final", "ran": 54, "reused": 0, "failed": 0, "wall_s": 1860}}]}
        self.assertEqual(batch.last_prove(run), "final 54/0/0 31m")


if __name__ == "__main__":
    unittest.main()
