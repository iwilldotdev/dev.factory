"""Major 9: the feedback gate needs a green pipeline.v1 on each change head and no unresolved human threads (change.v1)."""

from __future__ import annotations

import unittest

from tests.change_support import put_change, put_pipeline
from tests.integrity_support import IntegrityCase, cli, core


def thread(resolved: bool, system: bool = False) -> dict:
    return {"id": "d1", "resolved": resolved, "resolvable": not system, "system": system,
            "notes": [{"author": "rev", "body_md": "please fix", "system": system}]}


class FeedbackGateTests(IntegrityCase):
    def setUp(self) -> None:
        super().setUp()
        plan = self.shipped()
        self.change = plan["changes"][0]
        self.head = self.change["head_sha"]

    def files(self, status="success", sha=None, threads=(), pipelines=None, pid="5", threads_known=True):
        pipes = pipelines if pipelines is not None else [{"id": pid, "status": status, "sha": sha or self.head}]
        put_change(self.rid, self.change["repo"], self.change["target"], source_branch=self.change["source_branch"],
                   target_branch=self.change["target_branch"], title=self.change["title"], draft=self.change["draft"],
                   head_sha=self.head, threads=list(threads) if threads_known else None, pipelines=pipes)
        put_pipeline(self.rid, self.change["repo"], self.change["target"], id=pid, status=status, sha=sha or self.head)

    def test_missing_inputs_fail_with_fetch(self):
        # the shipped readback exists; its pipeline contract does not
        put_change(self.rid, self.change["repo"], self.change["target"], source_branch=self.change["source_branch"],
                   target_branch=self.change["target_branch"], title=self.change["title"], draft=self.change["draft"],
                   head_sha=self.head, threads=[])
        rc, out = self.gate("feedback")
        self.assertEqual(rc, 1)
        self.assertIn("no usable pipeline contract", out)
        self.assertIn("FETCH pipeline", out)
        self.assertIn("--raw jobs=", out)

    def test_threads_unknown_is_not_none(self):
        self.files(threads_known=False)
        self.assertIn("no thread data (unknown, not none)", self.gate("feedback")[1])

    def test_explicit_empty_threads_pass(self):
        self.files(threads=[])
        self.assertEqual(self.gate("feedback")[0], 0)

    def test_failed_running_or_other_sha_pipeline_fails(self):
        self.files(status="failed")
        self.assertIn("status 'failed'", self.gate("feedback")[1])
        self.files(status="running")
        self.assertIn("status 'running'", self.gate("feedback")[1])
        self.files(sha="9" * 40)
        self.assertIn("change head is", self.gate("feedback")[1])

    def test_not_latest_pipeline_fails(self):
        self.files(pipelines=[{"id": "7", "status": "success"}, {"id": "5", "status": "success"}], pid="5")
        self.assertIn("latest pipeline is #7", self.gate("feedback")[1])

    def test_latest_pipeline_ranks_by_numeric_id(self):
        self.files(pipelines=[{"id": "9", "status": "success"}, {"id": "10", "status": "success"}], pid="10")
        self.assertEqual(self.gate("feedback")[0], 0)

    def test_unresolved_human_thread_fails(self):
        self.files(threads=[thread(False)])
        rc, out = self.gate("feedback")
        self.assertEqual(rc, 1)
        self.assertIn("1 unresolved review thread", out)

    def test_green_head_and_resolved_threads_pass(self):
        self.files(threads=[thread(True), thread(False, system=True)])
        rc, out = self.gate("feedback")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.rid)["phase"], "done")
        self.assertTrue((self.rd / ("metrics" + ".json")).is_file())  # recomputed on entering done

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
