"""`factory metrics`: derived from events, never stored."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from dev_factory import metrics
from tests.helpers import lesson
from tests.m2_support import M2Case

T0 = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)


def ev(sec: int, type_: str, **kw) -> dict:
    return {"v": 1, "ts": (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%SZ"), "type": type_, **kw}


def gate(sec: int, name: str, result: str = "pass") -> dict:
    return ev(sec, "gate", name=name, result=result, reasons=[])


FLOW = [ev(0, "run_started", ticket="ABC-1"), gate(30, "intake", "fail"), gate(60, "intake"), gate(120, "plan"),
        ev(150, "evidence", mode="reproduce", duration_s=4.0, check="CHK-1", result="reproduced"), gate(300, "reproduce"),
        ev(330, "evidence", mode="verify", duration_s=12.5, check="CHK-1", result="pass"), gate(400, "verify")]


class MetricsTests(unittest.TestCase):
    @lesson("L-178")
    def test_time_per_phase_comes_from_events_and_reproduce_is_reported_apart(self):
        """L-178: the gap before each gate belongs to the phase it closes; reproduce is its own phase."""
        m = metrics.compute(FLOW, bug=True)
        self.assertEqual(m["phases_s"], {"intake": 60, "plan": 60, "reproduce": 180, "build": 100})
        self.assertEqual((m["wall_s"], m["verify_s"], m["reproduce_check_s"]), (400, 12.5, 4.0))
        self.assertEqual(m["gate_failures"], {"intake": 1})
        feature = metrics.compute([e for e in FLOW if e.get("name") != "reproduce" and e.get("mode") != "reproduce"], bug=False)
        self.assertNotIn("reproduce", feature["phases_s"])

    @lesson("L-180")
    def test_time_under_an_open_blocker_is_blocked_hours_not_phase_time(self):
        """L-180: while a blocker is open the clock goes to `blocked_hours`, so waiting for the user is not build time."""
        events = FLOW[:4] + [ev(130, "blocker", id="B-1", kind="question", reason="needs the user"),
                             ev(130 + 5400, "unblock", blocker="B-1", decision="Q-1"), gate(130 + 5400 + 100, "plan", "fail")]
        m = metrics.compute(events, bug=False)
        self.assertEqual(m["blocked_hours"], 1.5)
        self.assertEqual(m["phases_s"]["build"], 10 + 100)
        self.assertEqual(m["gate_failures"], {"intake": 1, "plan": 1})

    @lesson("L-181")
    def test_only_blocker_and_major_findings_after_the_first_verify_pass_count_once_each(self):
        """L-181: escaped findings are blocker or major findings recorded after verify first passed, de-duplicated across rounds."""
        early = {"id": "F1", "severity": "major", "file": "a.py:1", "text": "Total is wrong"}
        late = {"id": "F2", "severity": "blocker", "file": "b.py:9", "text": "Crashes on empty input"}
        again = {"id": "F7", "severity": "major", "file": "b.py:9", "text": "  crashes on EMPTY input "}
        events = FLOW + [ev(410, "review", kind="code", findings=[early, {"id": "F3", "severity": "minor", "file": "c.py", "text": "naming"}]),
                         ev(420, "review", kind="code", findings=[late]), ev(430, "review", kind="qa", findings=[again, late])]
        self.assertEqual(metrics.compute(events)["escaped_findings"], 2)  # F1, and F2 once although F7 repeats it
        self.assertEqual(metrics.compute(FLOW[:5] + [ev(310, "review", findings=[early])])["escaped_findings"], 0)

    def test_loops_and_human_events_are_counted_by_kind(self):
        events = FLOW + [ev(500, "loop", kind="amend", decision=None, counts=True), ev(510, "loop", kind="amend", decision="Q-1", counts=False),
                         ev(520, "loop", kind="review", decision=None, counts=True), ev(530, "human", kind="correction", text="x"),
                         ev(540, "human", kind="correction", text="y"), ev(550, "answer", question="Q-1", user_said="ok"),
                         ev(560, "decision", id="D-1", user_said="go")]
        m = metrics.compute(events)
        self.assertEqual(m["loops"], {"amend": 1, "review": 1, "exempt": 1})
        self.assertEqual(m["human_events"], {"correction": 2, "answer": 1, "decision": 1})

    def test_an_empty_run_has_zero_everything(self):
        self.assertEqual(metrics.compute([])["wall_s"], 0)


class MetricsCliTests(M2Case):
    def test_the_command_derives_everything_from_the_events_of_a_real_run(self):
        run = self.prepare()
        self.fix()
        self.ok("verify")
        self.ok("gate", "verify")
        data = json.loads(self.ok("metrics", "--json"))
        self.assertEqual(data["run"], "ABC-1")
        self.assertGreater(data["verify_s"], 0)
        self.assertEqual(set(data["phases_s"]), {"intake", "plan", "build"})
        self.assertEqual(data["gate_failures"], {})
        self.assertIn("time per phase:", self.ok("metrics"))
        self.assertFalse((run / "metrics.json").exists(), "metrics are derived on demand, never stored [L-179]")


if __name__ == "__main__":
    unittest.main()
