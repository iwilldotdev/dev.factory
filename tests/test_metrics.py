"""`factory metrics`: derived from events, never stored."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from dev_factory import metrics
from tests.helpers import FAKE_ADAPTER, PY, lesson
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
        self.assertEqual(m["loops"], {"amend": 1, "review": 1, "feedback": 0, "exempt": 1})
        self.assertEqual(m["human_events"], {"correction": 2, "answer": 1, "decision": 1})

    def test_an_empty_run_has_zero_everything(self):
        self.assertEqual(metrics.compute([])["wall_s"], 0)

    def test_idle_time_is_apart_from_phase_time_and_from_blocked_time(self):
        events = FLOW[:4] + [gate(120 + 7200, "reproduce"), ev(120 + 7200 + 60, "blocker", id="B-1", kind="question", reason="x"),
                             ev(120 + 7200 + 60 + 3600, "unblock", blocker="B-1", decision="Q-1")]
        m = metrics.compute(events, bug=True)
        self.assertEqual((m["idle_hours"], m["blocked_hours"]), (2.0, 1.0))
        self.assertEqual(m["phases_s"].get("build", 0), 60, "only the minute before the blocker is work; the two idle hours are not")
        self.assertNotIn("reproduce", m["phases_s"])
        self.assertEqual(metrics.compute(events, bug=True, idle_s=10 ** 6)["idle_hours"], 0)

    def test_the_feedback_phase_has_its_own_time_and_a_feedback_round_returns_to_build(self):
        events = FLOW[:4] + [gate(130, "verify"), gate(140, "review"), gate(150, "ship"), ev(200, "review", kind="feedback", findings=[]),
                             gate(260, "ship"), gate(300, "feedback")]
        m = metrics.compute(events)
        self.assertEqual(m["phases_s"], {"intake": 60, "plan": 60, "build": 10 + 60, "review": 10, "ship": 10 + 0, "feedback": 50 + 40})

    @lesson("L-183")
    def test_tokens_are_kept_per_session_and_an_empty_report_never_replaces_real_numbers(self):
        """L-183: `tokens` events are summed per session (the latest real report wins) and a zero report is ignored."""
        events = [ev(0, "run_started", ticket="ABC-1"), ev(10, "tokens", session="s1", input=100, output=40), ev(20, "tokens", session="s2", input=7, output=3),
                  ev(30, "tokens", session="s1", input=250, output=90), ev(40, "tokens", session="s1", input=0, output=0)]
        t = metrics.compute(events)["tokens"]
        self.assertEqual(t["sessions"], {"s1": {"input": 250, "output": 90}, "s2": {"input": 7, "output": 3}})
        self.assertEqual((t["input"], t["output"]), (257, 93))


class TokenTests(M2Case):
    def adapter(self, stdout: str, exit_: int = 0) -> None:
        path = self.write("tokens.json", json.dumps({"stdout": stdout, "exit": exit_}), root=self.base)
        template = f'"{PY}" "{FAKE_ADAPTER}" "{path.as_posix()}" {{run}} {{session}}'
        self.write("factory.toml", self.TOML + f"\n[metrics]\ntokens = {json.dumps(template)}\n")

    @lesson("L-183")
    def test_the_tokens_adapter_is_asked_per_session_and_run_and_nothing_empty_is_recorded(self):
        """L-183 (and L-182, which is by design): the adapter gets {run} and {session}; its numbers become a `tokens` event, an empty report records nothing, and a session id
        that is a path or a pattern is refused."""
        run = self.start("ABC-1")
        self.adapter(json.dumps({"session": "s1", "input": 120, "output": 30}))
        self.assertIn("recorded tokens for session s1: input 120, output 30", self.ok("metrics", "--tokens", "--session", "s1"))
        self.adapter(json.dumps({"session": "s1", "input": 0, "output": 0}))
        self.assertIn("nothing recorded, earlier numbers stay", self.ok("metrics", "--tokens", "--session", "s1"))
        self.assertEqual([(e["session"], e["input"], e["output"]) for e in self.events(run) if e["type"] == "tokens"], [("s1", 120, 30)])
        self.assertEqual(json.loads(self.ok("metrics", "--json"))["tokens"]["output"], 30)
        self.assertIn("input 120, output 30 over 1 session(s)", self.ok("metrics"))
        for bad in ("*", "../x", "a b", ""):
            self.assertIn("--session needs", self.fails("metrics", "--tokens", "--session", bad))
        self.adapter(json.dumps({"session": "other", "input": 1, "output": 1}))
        self.assertIn("must print", self.fails("metrics", "--tokens", "--session", "s1"))
        self.write("factory.toml", self.TOML)
        self.assertIn("no [metrics] tokens adapter", self.fails("metrics", "--tokens", "--session", "s1"))


class ReportTests(M2Case):
    def test_all_aggregates_every_run_of_the_project_and_since_filters_by_start_date(self):
        self.start("ABC-1")
        self.start("ABC-2")
        data = json.loads(self.ok("metrics", "--all", "--json"))
        self.assertEqual([r["run"] for r in data["runs"]], ["ABC-1", "ABC-2"])
        self.assertEqual(data["totals"]["runs"], 2)
        text = self.ok("metrics", "--all")
        self.assertIn("2 run(s)", text)
        self.assertIn("ABC-2", text)
        self.assertEqual(json.loads(self.ok("metrics", "--all", "--since", "2999-01-01", "--json"))["totals"]["runs"], 0)
        self.assertIn("use a date", self.fails("metrics", "--all", "--since", "yesterday"))


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
