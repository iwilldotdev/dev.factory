import json
import unittest
from pathlib import Path
from unittest import mock

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from ops_helpers import FactoryCase

from factory.commands import metrics


def line(**kw):
    return json.dumps(kw) + "\n"


class MetricsTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "claude" / "projects" / "proj"
        self.root.mkdir(parents=True)
        p = mock.patch.object(metrics, "claude_roots", lambda: [self.root.parent])
        p.start()
        self.addCleanup(p.stop)

    def test_compute(self):
        events = [
            {"at": "2026-01-01T00:10:00+00:00", "kind": "gate_fail", "data": {"phase": "plan"}},
            {"at": "2026-01-01T00:20:00+00:00", "kind": "human", "data": {"note": "x"}},
            {"at": "2026-01-01T00:30:00+00:00", "kind": "gate_pass", "data": {"phase": "prove"}},
            {"at": "2026-01-01T02:00:00+00:00", "kind": "phase", "data": {}},
        ]
        self.make_run("ws-dev-1", events=events, loops={"build": 1, "review": 0}, sessions=["sess1"])
        self.write("runs/ws-dev-1/reviews/code-1.json", {
            "recorded": "2026-01-01T01:00:00+00:00", "findings": [{"id": "F1", "severity": "major"}, {"id": "F2", "severity": "minor"}]})
        self.write("runs/ws-dev-1/reviews/code-0.json", {
            "recorded": "2026-01-01T00:05:00+00:00", "findings": [{"id": "F1", "severity": "major"}]})

        main = self.root / "sess1.jsonl"
        main.write_text(
            line(type="user", timestamp="2026-01-01T00:00:00Z", message={"role": "user", "content": "factory-run: ws-dev-1 go"})
            + line(type="user", timestamp="2026-01-01T00:00:01Z", isMeta=True, message={"role": "user", "content": "meta"})
            + line(type="assistant", timestamp="2026-01-01T00:01:00Z", message={
                "id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
                "content": [{"type": "tool_use", "name": "Agent", "input": {"subagent_type": "factory-builder", "model": "sonnet"}}]})
            + line(type="user", timestamp="2026-01-01T00:02:00Z", message={"role": "user", "content": [{"type": "tool_result", "content": "ok"}]})
            + line(type="user", timestamp="2026-01-01T00:03:00Z", message={"role": "user", "content": [{"type": "text", "text": "again"}]})
            + line(type="assistant", timestamp="2026-01-01T03:00:00Z", message={
                "id": "m1", "usage": {"input_tokens": 10, "output_tokens": 6, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
                "content": []}),
            encoding="utf-8")
        sub = self.root / "sess1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.jsonl").write_text(
            line(type="assistant", timestamp="2026-01-01T00:05:00Z", message={"id": "s1", "usage": {"input_tokens": 1, "output_tokens": 2}, "content": []}),
            encoding="utf-8")

        m = metrics.compute("ws-dev-1")
        self.assertEqual(m["human_messages"], 2)
        self.assertEqual(m["human_interventions"], 1)
        self.assertEqual(m["subagents"], {"factory-builder/sonnet": 1})
        self.assertEqual(m["tokens"]["main"]["output"], 6)  # streamed duplicate: last wins
        self.assertEqual(m["tokens"]["main"]["cache_read"], 100)
        self.assertEqual(m["tokens"]["subagents"]["output"], 2)
        self.assertEqual(m["tokens"]["total"]["output"], 8)
        self.assertEqual(m["gate_fail"], {"plan": 1})
        self.assertEqual(m["loops"]["build"], 1)
        self.assertEqual(m["wall_time_h"], 2.0)
        self.assertEqual(m["transcript_span_h"], 3.0)
        self.assertEqual(m["escaped_findings"]["count"], 2)
        self.assertEqual(m["escaped_findings"]["by_severity"], {"major": 1, "minor": 1})
        self.assertTrue((self.home / "runs/ws-dev-1/metrics.json").is_file())
        rows = metrics.report(None)
        self.assertEqual(len(rows), 1)
        self.assertIn("ws-dev-1", metrics._table(rows))

    def test_discover_by_marker(self):
        self.make_run("ws-dev-2")
        f = self.root / "other.jsonl"
        f.write_text(line(type="user", timestamp="2026-02-01T00:00:00Z", message={"role": "user", "content": "factory-run: ws-dev-2"}),
                     encoding="utf-8")
        (self.root / "unrelated.jsonl").write_text(line(type="user", message={"content": "hi"}), encoding="utf-8")
        self.assertEqual([Path(p).name for p in metrics.find_transcripts({"id": "ws-dev-2", "created": "2026-01-01T00:00:00+00:00"})],
                         ["other.jsonl"])


if __name__ == "__main__":
    unittest.main()
