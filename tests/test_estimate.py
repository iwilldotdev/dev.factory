"""`verify --estimate`: reuse or run per check, with the median duration of earlier runs, and nothing is run or recorded."""
import json
import unittest

from tests.helpers import lesson
from tests.m2_support import M2Case


class EstimateTests(M2Case):
    def estimate(self, *flags: str) -> dict:
        return json.loads(self.ok("verify", "--estimate", "--json", *flags))

    @lesson("L-189")
    def test_the_estimate_says_reuse_or_run_per_check_with_the_median_of_past_durations_and_runs_nothing(self):
        """L-189: `verify --estimate` prints `reuse` or `run` per check with the median duration from past events, and records nothing."""
        run = self.prepare()
        before = self.estimate()
        self.assertEqual([(c["check"], c["action"], c["median_s"]) for c in before["checks"]], [("CHK-1", "run", None)])
        self.assertEqual(before["unknown"], ["CHK-1"])
        self.fix()
        self.ok("verify")
        self.commit("extra.txt", "x\n")
        self.ok("verify")
        count = len(self.events(run))
        reused = self.estimate()
        self.assertEqual((reused["checks"][0]["action"], reused["reuse"], reused["run"]), ("reuse", 1, 0))
        self.commit("extra2.txt", "x\n")
        moved = self.estimate()
        past = sorted(e["duration_s"] for e in self.evidence(run, check="CHK-1"))
        row = moved["checks"][0]
        self.assertEqual((row["action"], row["history"]), ("run", len(past)))
        self.assertEqual(row["median_s"], round((past[0] + past[1]) / 2, 2))
        self.assertEqual(moved["estimate_s"], round(row["median_s"], 1))
        self.assertEqual(self.estimate("--fresh")["checks"][0]["action"], "run")
        self.assertEqual(len(self.events(run)), count, "an estimate appends no event")
        text = self.ok("verify", "--estimate")
        self.assertIn("CHK-1", text)
        self.assertIn("estimate: 1 to run", text)

    def test_a_human_check_is_asked_not_run_and_a_quality_standing_check_is_listed(self):
        plan = self.default_plan()
        plan["checks"].append({"id": "CHK-2", "type": "human", "repo": "web", "ac": ["AC-1"], "text": "Looks right"})
        self.prepare(plan=plan)
        self.fix()
        rows = {c["check"]: c["action"] for c in self.estimate()["checks"]}
        self.assertEqual(rows, {"CHK-1": "run", "CHK-2": "ask"})


if __name__ == "__main__":
    unittest.main()
