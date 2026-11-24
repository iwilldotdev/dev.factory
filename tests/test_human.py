"""ask, answer, decide, human, block, unblock: typed events, decisions consumed once."""
import json
import os
import subprocess
import sys
import unittest

from tests.helpers import ROOT, FactoryCase, lesson


class QuestionTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.start("A-1")

    def test_ask_and_answer_round_trip(self):
        self.assertIn("opened Q-1", self.ok("ask", "Which locale?"))
        self.assertIn("opened Q-2", self.ok("ask", "And the timezone?", "--ref", "attachment:7"))
        self.assertEqual(json.loads(self.ok("status", "--json"))["open_questions"], ["Q-1", "Q-2"])
        self.ok("answer", "Q-1", "--user-said", "pt-BR please")
        evs = self.events(self.run_dir)
        ans = [e for e in evs if e["type"] == "answer"][0]
        self.assertEqual((ans["question"], ans["user_said"], ans["decision"]), ("Q-1", "pt-BR please", "Q-1"))
        self.assertEqual(json.loads(self.ok("status", "--json"))["open_questions"], ["Q-2"])

    @lesson("L-126")
    def test_an_answer_or_decision_needs_the_users_words(self):
        """L-126: empty or blank --user-said is refused."""
        self.ok("ask", "q")
        for words in ("", "   "):
            self.assertIn("user's own words", self.fails("answer", "Q-1", "--user-said", words))
            self.assertIn("user's own words", self.fails("decide", "--user-said", words))
        self.assertEqual([e["type"] for e in self.events(self.run_dir)], ["run_started", "question"])

    def test_unknown_or_already_answered_questions_are_refused(self):
        self.assertIn("no question Q-9", self.fails("answer", "Q-9", "--user-said", "x"))
        self.ok("ask", "q")
        self.ok("answer", "Q-1", "--user-said", "x")
        self.assertIn("already answered", self.fails("answer", "Q-1", "--user-said", "y"))

    @lesson("L-166", "L-165")
    def test_concurrent_askers_get_distinct_ids(self):
        """L-166, L-165: two sessions appending at once keep every line and never reuse an id."""
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        cmd = [sys.executable, "-P", "-m", "dev_factory", "ask", "q", "--project", str(self.proj), "--run", "A-1"]
        procs = [subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=self.base)
                 for _ in range(4)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0, p.stderr.read())
        ids = sorted(e["id"] for e in self.events(self.run_dir) if e["type"] == "question")
        self.assertEqual(ids, ["Q-1", "Q-2", "Q-3", "Q-4"])


class DecisionTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.start("A-1")

    @lesson("L-126")
    def test_a_decision_is_consumed_once(self):
        """L-126: a recorded decision unblocks exactly once and is listed by `next` until consumed."""
        self.ok("ask", "ship now?")
        self.ok("answer", "Q-1", "--user-said", "yes, ship")
        self.assertIn("not consumed yet: Q-1", self.ok("next"))
        self.ok("block", "--kind", "decision", "--reason", "waiting")
        self.ok("block", "--kind", "decision", "--reason", "waiting again")
        self.ok("unblock", "B-1", "--decision", "Q-1")
        self.assertIn("already consumed", self.fails("unblock", "B-2", "--decision", "Q-1"))
        self.assertNotIn("not consumed", self.ok("next"))
        self.assertEqual(json.loads(self.ok("status", "--json"))["unconsumed_decisions"], [])

    def test_decide_records_d_ids_and_unknown_decisions_are_refused(self):
        self.assertIn("recorded D-1", self.ok("decide", "--user-said", "use the staging data"))
        self.assertIn("recorded D-2", self.ok("decide", "--user-said", "and keep the toast"))
        self.ok("block", "--kind", "decision", "--reason", "r")
        self.assertIn("no recorded decision D-9", self.fails("unblock", "B-1", "--decision", "D-9"))
        self.ok("unblock", "B-1", "--decision", "D-2")
        self.assertEqual(json.loads(self.ok("status", "--json"))["unconsumed_decisions"], ["D-1"])


class BlockerTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.start("A-1")

    @lesson("L-140")
    def test_an_invalid_blocker_kind_lists_the_valid_kinds_with_meanings(self):
        """L-140: a bad kind is refused with every valid kind and a one-line meaning."""
        err = self.fails("block", "--kind", "stuck", "--reason", "r")
        for kind in ("question", "decision", "infra", "tooling"):
            self.assertRegex(err, rf"\n  {kind}: \w")
        self.assertEqual(len(self.events(self.run_dir)), 1)

    @lesson("L-152")
    def test_every_blocker_write_is_exactly_one_event(self):
        """L-152: block and unblock each append exactly one event."""
        for kind in ("question", "decision", "infra", "tooling"):
            before = len(self.events(self.run_dir))
            self.ok("block", "--kind", kind, "--reason", f"{kind} reason")
            self.assertEqual(len(self.events(self.run_dir)), before + 1)
            self.assertEqual(self.events(self.run_dir)[-1]["type"], "blocker")
        before = len(self.events(self.run_dir))
        self.ok("unblock", "B-3")
        self.assertEqual(len(self.events(self.run_dir)), before + 1)

    def test_question_and_decision_blockers_need_a_recorded_decision(self):
        self.ok("block", "--kind", "question", "--reason", "need an answer")
        self.assertIn("pass --decision", self.fails("unblock", "B-1"))
        self.ok("ask", "q")
        self.ok("answer", "Q-1", "--user-said", "go")
        self.ok("unblock", "B-1", "--decision", "Q-1")
        self.assertIn("not an open blocker", self.fails("unblock", "B-1"))

    def test_next_reports_the_blocker_and_status_lists_it(self):
        self.ok("block", "--kind", "infra", "--reason", "database down")
        out = self.ok("next")
        self.assertIn("blocked by B-1 (infra): database down", out)
        self.assertEqual(json.loads(self.ok("status", "--json"))["open_blockers"], ["B-1"])


class HumanTests(FactoryCase):
    @lesson("L-177")
    def test_human_events_are_typed_and_a_bad_type_lists_the_valid_ones(self):
        """L-177: human events carry a type so metrics can count each separately."""
        run = self.start("A-1")
        for kind in ("answer", "decision", "env-hint", "correction"):
            self.ok("human", "--kind", kind, f"note {kind}")
        kinds = [e["kind"] for e in self.events(run) if e["type"] == "human"]
        self.assertEqual(kinds, ["answer", "decision", "env-hint", "correction"])
        err = self.fails("human", "--kind", "note", "x")
        self.assertIn("env-hint:", err)
        self.assertIn("correction:", err)

    def test_actor_is_an_audit_hint_only(self):
        run = self.start("A-1")
        self.ok("human", "--kind", "correction", "x", "--actor", "builder-3")
        self.assertEqual(self.events(run)[-1]["actor"], "builder-3")


if __name__ == "__main__":
    unittest.main()
