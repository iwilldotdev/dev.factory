"""`run human --type`: each steering message carries a type and metrics count them apart."""

from __future__ import annotations

import unittest

from core_support import FactoryCase, cli, core  # noqa: F401
from factory.commands import metrics
from factory.commands.run import event_summary


class HumanTypeTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.rid = self.make_run(branch=False)

    def humans(self):
        return [e["data"] for e in core.run_load(self.rid)["events"] if e["kind"] == "human"]

    def test_default_type_is_other_and_a_type_is_recorded(self):
        self.assertEqual(cli("run", "human", self.rid, "plain note")[0], 0)
        self.assertEqual(cli("run", "human", self.rid, "go ahead and push", "--type", "approval")[0], 0)
        self.assertEqual([h["type"] for h in self.humans()], ["other", "approval"])
        self.assertIn("[approval]", event_summary({"kind": "human", "data": self.humans()[1]}))

    def test_unknown_type_is_refused_with_the_valid_ones(self):
        with self.assertRaises(SystemExit):  # argparse: invalid choice, exit 2
            cli("run", "human", self.rid, "x", "--type", "mood")
        self.assertEqual(self.humans(), [])

    def test_metrics_count_per_type_and_old_events_are_other(self):
        for t in ("decision", "decision", "env-note", "correction"):
            cli("run", "human", self.rid, "n", "--type", t)
        run = core.run_load(self.rid)
        run["events"].append({"at": core.now(), "kind": "human", "data": {"note": "old event without a type"}})
        run["events"].append({"at": core.now(), "kind": "human", "data": {"note": "x", "type": "invented"}})
        core.run_save(run)
        m = metrics.compute(self.rid)
        self.assertEqual(m["human_by_type"], {"correction": 1, "decision": 2, "env-note": 1, "other": 2})
        self.assertEqual(m["human_interventions"], 6)
        self.assertIn("decision", metrics._summary(m))


if __name__ == "__main__":
    unittest.main()
