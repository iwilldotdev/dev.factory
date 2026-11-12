"""CLI prints ready-to-use dev.mcp calls (save_to absolute, forward slashes) for missing remote files."""

import contextlib
import io
import json
import unittest

from ops_helpers import FactoryCase, make_repo
from test_ops_figma import PAYLOAD, png

from factory.cli import main
from factory.commands import figma


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class FetchHintTests(FactoryCase):
    def setUp(self):
        super().setUp()
        repo = make_repo(self.tmp / "react")
        self.write("workspaces/ws/workspace.json", {
            "schemaVersion": 1, "name": "ws",
            "repos": [{"alias": "React", "path": str(repo), "gitlab_project": "grp/react"}]})
        self.make_run("ws-dev-1", phase="plan")
        self.rd = (self.home / "runs/ws-dev-1").as_posix()

    def test_figma_check_prints_calls(self):
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [],
                                              "checks": [{"id": "C1", "type": "visual", "node": "1:2", "fileKey": "FK"}]})
        rc, out, _ = cli("figma", "check", "ws-dev-1")
        self.assertEqual(rc, 1)
        self.assertIn(f"FETCH figma_get_design_context fileKey=FK nodeIds=1:2 includeScreenshot=false save_to={self.rd}/figma/1-2.json overwrite=true", out)
        self.assertIn(f"figma_fetch_screenshot fileKey=FK nodeIds=1:2 scale=1 format=png save_to={self.rd}/figma/1-2.png overwrite=true", out)
        self.assertIn(f"figma_get_file_meta fileKey=FK save_to={self.rd}/figma/meta.json overwrite=true", out)
        self.assertNotIn("\\", self.rd)

    def test_figma_check_json_and_placeholder(self):
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [], "checks": [{"id": "C1", "type": "visual", "node": "1:2"}]})
        rc, out, _ = cli("--json", "figma", "check", "ws-dev-1")
        data = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertFalse(data["passed"])
        first = data["fetch"][0]
        self.assertEqual(first["tool"], "figma_get_design_context")
        self.assertEqual(first["args"]["fileKey"], "<fileKey>")
        self.assertIs(first["args"]["overwrite"], True)
        self.assertTrue(first["args"]["save_to"].endswith("/figma/1-2.json"))

    def test_complete_pack_has_no_fetch_and_meta_per_file(self):
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [], "checks": [{"id": "C1", "type": "visual", "node": "1:2"}]})
        self.write("runs/ws-dev-1/figma/1-2.json", PAYLOAD)
        self.write("runs/ws-dev-1/figma/1-2.png", png())
        self.write("runs/ws-dev-1/figma/meta-FK.json", {"fileKey": "FK", "name": "n", "lastModified": "2026-01-01", "version": "9"})
        self.assertEqual(figma.check_pack("ws-dev-1"), [])
        rc, out, _ = cli("figma", "check", "ws-dev-1")
        self.assertEqual(rc, 0)
        self.assertNotIn("FETCH", out)

    def test_gate_intake_issue(self):
        self.make_run("ws-dev-1", phase="intake")
        rc, out, _ = cli("gate", "ws-dev-1", "intake")
        self.assertEqual(rc, 1)
        self.assertIn(f"FETCH jira_get_issue issueIdOrKey=DEV-1 save_to={self.rd}/remote/issue-DEV-1.json overwrite=true", out)

    def test_ship_verify_readback(self):
        self.write("runs/ws-dev-1/ship-plan.json", {"mrs": [{"repo": "React", "target": "pack", "source_branch": "b", "target_branch": "pack",
                                                            "title": "t", "draft": False, "readback": "remote/mr-React-pack.json"}]})
        rc, out, _ = cli("ship", "verify", "ws-dev-1")
        self.assertEqual(rc, 1)
        self.assertIn(f"FETCH gitlab_get_merge_request projectId=grp/react mergeRequestIid=<iid> save_to={self.rd}/remote/mr-React-pack.json overwrite=true", out)

    def test_ship_verify_accepts_wrapped_readback(self):
        self.write("runs/ws-dev-1/ship-plan.json", {"mrs": [{"repo": "React", "target": "pack", "source_branch": "b", "target_branch": "pack",
                                                            "title": "t", "draft": False, "readback": "remote/mr-React-pack.json"}]})
        self.write("runs/ws-dev-1/remote/mr-React-pack.json", {"merge_request": {
            "iid": 4, "source_branch": "b", "target_branch": "pack", "title": "t", "web_url": "u"}})
        rc, out, _ = cli("ship", "verify", "ws-dev-1")
        self.assertEqual((rc, out.strip()), (0, "OK ship verified"))

    def test_feedback_ingest_lists_missing(self):
        self.write("runs/ws-dev-1/ship-plan.json", {"mrs": [{"repo": "React", "target": "pack"}]})
        self.write("runs/ws-dev-1/remote/mr-React-pack.json", {"merge_request": {"iid": 7}})
        rc, out, _ = cli("feedback", "ingest", "ws-dev-1")
        self.assertEqual(rc, 0)
        self.assertIn(f"FETCH gitlab_get_merge_request projectId=grp/react mergeRequestIid=7 includeNotes=true includePipelines=true save_to={self.rd}/remote/mr-React-pack-discussions.json overwrite=true", out)
        self.assertIn(f"gitlab_get_pipeline projectId=grp/react pipelineId=<pipelineId> save_to={self.rd}/remote/pipeline-React-pack.json", out)
        self.assertIn(f"gitlab_get_pipeline_jobs projectId=grp/react pipelineId=<pipelineId> save_to={self.rd}/remote/pipeline-React-pack-jobs.json", out)

    def test_feedback_ingest_reads_raw_saves(self):
        self.write("runs/ws-dev-1/ship-plan.json", {"mrs": [{"repo": "React", "target": "pack"}]})
        self.write("runs/ws-dev-1/remote/mr-React-pack.json", {"merge_request": {"iid": 7}})
        self.write("runs/ws-dev-1/remote/mr-React-pack-discussions.json", {
            "merge_request": {"iid": 7}, "pipelines": [{"id": 55}],
            "notes": [{"id": 1, "body": "fix", "system": False, "resolvable": True, "resolved": False, "author": {"username": "rev"}},
                      {"id": 2, "body": "merged", "system": True}]})
        self.write("runs/ws-dev-1/remote/pipeline-React-pack.json", {"id": 55, "status": "failed"})
        self.write("runs/ws-dev-1/remote/pipeline-React-pack-jobs.json", [{"id": 9, "name": "lint", "stage": "test", "status": "failed"}])
        rc, out, _ = cli("--json", "feedback", "ingest", "ws-dev-1")
        data = json.loads(out)
        self.assertEqual((rc, data["findings"], data["fetch"]), (0, 2, []))

    def test_review_start_missing_payload(self):
        rc, out, err = cli("review", "start", "ws", "--mr", "React!5=DEV-1")
        self.assertEqual(rc, 2)
        inbox = (self.home / "runs/_inbox").as_posix()
        self.assertIn(f"FETCH gitlab_get_merge_request projectId=grp/react mergeRequestIid=5 save_to={inbox}/mr-React-5.json overwrite=true", err)
        rc, out, _ = cli("--json", "review", "start", "ws", "--mr", "React!5=DEV-1")
        self.assertEqual(json.loads(out)["fetch"][0]["args"]["mergeRequestIid"], 5)

    def test_review_queue_missing_file(self):
        path = self.tmp / "q.json"
        rc, _, err = cli("review", "queue", "ws", "--mrs-json", str(path))
        self.assertEqual(rc, 2)
        self.assertIn(f"FETCH gitlab_list_merge_requests projectId=grp/react state=opened save_to={path.as_posix()} overwrite=true", err)


if __name__ == "__main__":
    unittest.main()
