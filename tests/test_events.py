"""events.jsonl: the only run state, append-only, safe for concurrent writers, state derived from it."""
import json
import os
import subprocess
import sys
import unittest

from tests.helpers import ROOT, lesson, tmpdir
from dev_factory import __version__
from dev_factory.run import Run, derive
from dev_factory.util import FactoryError

WORKER = """
import sys
from pathlib import Path
from dev_factory.run import Run
run = Run(Path(sys.argv[1]), "r1")
for i in range(int(sys.argv[3])):
    run.append({"type": "human", "kind": "correction", "text": sys.argv[2] + "-" + str(i) + "-" + "x" * 300})
"""


class EventTests(unittest.TestCase):
    def setUp(self):
        self.runs = tmpdir()
        self.run = Run(self.runs, "r1")

    def test_every_append_is_one_canonical_line_and_nothing_is_overwritten(self):
        """L-15: records are append-only lines; an earlier record survives every later one."""
        self.run.append({"type": "gate", "name": "intake", "result": "fail", "reasons": ["a"]})
        self.run.append({"type": "gate", "name": "intake", "result": "pass", "reasons": []})
        lines = self.run.events_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["result"], "fail")
        self.assertEqual(lines[0], json.dumps(first, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        self.assertEqual(self.run.state()["gates"]["intake"]["result"], "pass")

    @lesson("L-150")
    def test_events_record_the_cli_version_and_the_actor_hint(self):
        """L-150: every event carries the CLI version; the actor is an optional audit hint."""
        ev = self.run.append({"type": "human", "kind": "correction", "text": "x"}, actor="builder-1")
        self.assertEqual((ev["v"], ev["cli"], ev["actor"]), (1, __version__, "builder-1"))
        self.assertRegex(ev["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    @lesson("L-165")
    def test_concurrent_appenders_lose_and_tear_no_line(self):
        """L-165, L-164: four processes appending at once keep every line intact."""
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        procs = [subprocess.Popen([sys.executable, "-P", "-c", WORKER, str(self.runs), f"w{n}", "25"], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for n in range(4)]
        for p in procs:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode())
        events = self.run.read()
        self.assertEqual(len(events), 100)
        self.assertEqual(len({e["text"] for e in events}), 100)

    @lesson("L-165")
    def test_callable_append_sees_current_events_under_the_lock(self):
        """L-165: ids derived inside the lock never repeat, even when many writers ask at once."""
        script = ("import sys\nfrom pathlib import Path\nfrom dev_factory.run import Run, next_id\n"
                  "r = Run(Path(sys.argv[1]), 'r1')\nfor _ in range(10):\n"
                  "    r.append(lambda evs: {'type': 'question', 'id': next_id('Q', evs, 'question'), 'text': 't', 'ref': None})\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        procs = [subprocess.Popen([sys.executable, "-P", "-c", script, str(self.runs)], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(3)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        ids = [e["id"] for e in self.run.read()]
        self.assertEqual(sorted(ids, key=lambda i: int(i[2:])), [f"Q-{n}" for n in range(1, 31)])

    def test_callable_returning_none_appends_nothing(self):
        self.assertIsNone(self.run.append(lambda evs: None))
        self.assertEqual(self.run.read(), [])

    def test_corrupt_line_is_reported_with_its_number(self):
        self.run.append({"type": "human", "kind": "correction", "text": "x"})
        with open(self.run.events_path, "a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        with self.assertRaises(FactoryError) as cm:
            self.run.read()
        self.assertIn("line 2", str(cm.exception))

    @lesson("L-134")
    def test_state_is_derived_from_events_only(self):
        """L-134: counters and states come from the events, in order."""
        evs = [{"type": "run_started", "ticket": "T-1", "kind": None, "repos": ["web"], "ts": "t1"},
               {"type": "question", "id": "Q-1", "text": "q", "ref": None, "ts": "t2"},
               {"type": "answer", "question": "Q-1", "user_said": "yes", "decision": "Q-1", "ts": "t3"},
               {"type": "blocker", "id": "B-1", "kind": "question", "reason": "r", "ts": "t4"},
               {"type": "unblock", "blocker": "B-1", "decision": "Q-1", "ts": "t5"},
               {"type": "retype", "kind": "bug", "ts": "t6"}]
        st = derive(evs)
        self.assertEqual((st["ticket"], st["kind"], st["repos"]), ("T-1", "bug", ["web"]))
        self.assertEqual(st["questions"]["Q-1"]["answered"], "yes")
        self.assertFalse(st["blockers"]["B-1"]["open"])
        self.assertTrue(st["decisions"]["Q-1"]["consumed"])
        self.assertEqual((st["first_ts"], st["last_ts"], st["count"]), ("t1", "t6", 6))


if __name__ == "__main__":
    unittest.main()
