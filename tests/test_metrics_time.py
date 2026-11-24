"""Metrics time: blocked and idle time are reported apart from the working hours of a phase, and the report is
recomputed when a run reaches a final state."""

import json
import sys as _sys
import unittest
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from ops_helpers import FactoryCase

from factory.commands import metrics


def ph(at, to, frm=None):
    return {"at": at, "kind": "phase", "data": {"from": frm, "to": to}}


def ev(at, kind="ship", **data):
    return {"at": at, "kind": kind, "data": data}


class BlockedAndIdleTests(FactoryCase):
    def test_blocked_time_leaves_the_phase(self):
        events = [ph("2026-01-01T00:00:00+00:00", "plan"), ph("2026-01-01T03:00:00+00:00", "build", "plan")]
        blockers = [{"at": "2026-01-01T01:00:00+00:00", "kind": "question", "text": "q", "resolved": True,
                     "resolved_at": "2026-01-01T02:00:00+00:00"}]
        self.make_run("ws-dev-1", events=events, blockers=blockers, updated="2026-01-01T03:30:00+00:00", phase="build")
        t = metrics.time_breakdown(json.loads((self.home / "runs/ws-dev-1/run.json").read_text(encoding="utf-8")))
        self.assertEqual(t["phase_hours"], {"plan": 2.0, "build": 0.5})
        self.assertEqual(t["blocked_hours"], 1.0)
        self.assertEqual(t["idle_hours"], 0.0)

    def test_open_blocker_runs_to_updated_and_blocked_phase_is_not_a_phase(self):
        events = [ph("2026-01-01T00:00:00+00:00", "build"), ph("2026-01-01T01:00:00+00:00", "blocked", "build")]
        blockers = [{"at": "2026-01-01T01:00:00+00:00", "kind": "question", "text": "q", "resolved": False}]
        run = {"events": events, "blockers": blockers, "updated": "2026-01-01T04:00:00+00:00"}
        t = metrics.time_breakdown(run)
        self.assertEqual(t["phase_hours"], {"build": 1.0})
        self.assertEqual(t["blocked_hours"], 3.0)

    def test_ship_waiting_is_idle_not_work(self):
        events = [ph("2026-01-01T00:00:00+00:00", "ship"), ev("2026-01-01T00:05:00+00:00"),
                  ev("2026-01-01T01:00:00+00:00"), ev("2026-01-01T01:04:00+00:00")]
        t = metrics.time_breakdown({"events": events, "blockers": [], "updated": "2026-01-01T01:05:00+00:00"})
        self.assertEqual(t["idle_hours"], 0.92)  # the 55 minutes between 00:05 and 01:00
        self.assertEqual(t["phase_hours"], {"ship": 0.17})  # 5 + 4 + 1 minutes
        other = metrics.time_breakdown({"events": [ph("2026-01-01T00:00:00+00:00", "build"), ev("2026-01-01T01:00:00+00:00")],
                                        "blockers": [], "updated": "2026-01-01T01:05:00+00:00"})
        self.assertEqual(other["idle_hours"], 0.0)  # only ship waits for humans and pipelines

    def test_old_run_without_blockers_or_events_loads(self):
        self.assertEqual(metrics.time_breakdown({}), {"phase_hours": {}, "blocked_hours": 0.0, "idle_hours": 0.0})

    def test_compute_reports_all_three(self):
        self.make_run("ws-dev-1", events=[ph("2026-01-01T00:00:00+00:00", "build")], updated="2026-01-01T01:00:00+00:00")
        m = metrics.compute("ws-dev-1")
        self.assertEqual((m["phase_hours"], m["blocked_hours"], m["idle_hours"]), ({"build": 1.0}, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
