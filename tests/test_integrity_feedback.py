"""Major 9: the feedback gate needs a green pipeline on each MR head and no unresolved human threads."""

from __future__ import annotations

import unittest

from tests.integrity_support import IntegrityCase, cli, core


def thread(resolved: bool, system: bool = False) -> dict:
    return {"id": "d1", "notes": [{"id": 1, "body": "please fix", "system": system, "resolvable": not system,
                                   "resolved": resolved, "author": {"username": "rev"}}]}


class FeedbackGateTests(IntegrityCase):
    def setUp(self) -> None:
        super().setUp()
        plan = self.shipped()
        self.mr = plan["mrs"][0]
        self.tag = f"{self.mr['repo']}-{self.mr['target']}"
        self.head = self.mr["head_sha"]

    def files(self, status="success", sha=None, threads=(), pipelines=None, pid=5):
        disc = {"merge_request": {"iid": 1}, "discussions": list(threads)}
        if pipelines is not None:
            disc["pipelines"] = pipelines
        core.write_json(self.rd / "remote" / f"mr-{self.tag}-discussions.json", disc)
        core.write_json(self.rd / "remote" / f"pipeline-{self.tag}.json",
                        {"id": pid, "status": status, "sha": sha or self.head})

    def test_missing_inputs_fail_with_fetch(self):
        rc, out = self.gate("feedback")
        self.assertEqual(rc, 1)
        self.assertIn("discussions.json missing", out)
        self.assertIn(f"pipeline-{self.tag}.json missing", out)
        self.assertIn("FETCH gitlab_get_pipeline", out)

    def test_failed_running_or_other_sha_pipeline_fails(self):
        self.files(status="failed")
        self.assertIn("status 'failed'", self.gate("feedback")[1])
        self.files(status="running")
        self.assertIn("status 'running'", self.gate("feedback")[1])
        self.files(sha="9" * 40)
        self.assertIn("MR head is", self.gate("feedback")[1])

    def test_not_latest_pipeline_fails(self):
        self.files(pipelines=[{"id": 7}, {"id": 5}], pid=5)
        self.assertIn("latest pipeline is #7", self.gate("feedback")[1])

    def test_unresolved_human_thread_fails(self):
        self.files(threads=[thread(False)])
        rc, out = self.gate("feedback")
        self.assertEqual(rc, 1)
        self.assertIn("1 unresolved review thread", out)

    def test_green_head_and_resolved_threads_pass(self):
        self.files(threads=[thread(True), thread(False, system=True)], pipelines=[{"id": 5}])
        rc, out = self.gate("feedback")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.rid)["phase"], "done")

    def test_user_decision_waives_pipeline_not_threads(self):
        self.files(status="failed", threads=[thread(False)])
        self.ok("run", "decide", self.rid, "pipeline #5: CI is down, user accepts", "--by", "user",
                "--user-said", "CI is down, ship anyway")
        rc, out = self.gate("feedback")
        self.assertNotIn("status 'failed'", out)
        self.assertIn("unresolved review thread", out)
        self.files(status="failed")
        self.ok("gate", self.rid, "feedback")

    def test_agent_decision_does_not_waive(self):
        self.files(status="failed")
        cli("run", "decide", self.rid, "pipeline: flaky", "--by", "agent")
        self.assertEqual(self.gate("feedback")[0], 1)


if __name__ == "__main__":
    unittest.main()
