import json
import unittest

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from change_support import put_change, put_pipeline
from ops_helpers import FactoryCase, make_repo

from factory.commands import feedback
from factory.core import FactoryError


def note(body, author="rev", system=False, path=None, line=None):
    out = {"author": author, "body_md": body, "system": system}
    if path:
        out.update(path=path, line=line)
    return out


def thread(tid, notes, resolved=False, resolvable=True, system=False):
    return {"id": tid, "resolved": resolved, "resolvable": resolvable, "system": system, "notes": notes}


class FeedbackTests(FactoryCase):
    def setUp(self):
        super().setUp()
        repo = make_repo(self.tmp / "react")
        self.write("workspaces/ws/workspace.json", {"schemaVersion": 1, "name": "ws", "repos": [{"alias": "React", "path": str(repo)}]})
        self.make_run("ws-dev-1", repos={"React": {"branch": "b", "base": "pack"}}, phase="ship")
        self.write("runs/ws-dev-1/ship-plan.json", {"schemaVersion": 2, "changes": [{"repo": "React", "target": "pack"}]})

    def put_threads(self, threads):
        put_change("ws-dev-1", "React", "pack", threads=threads)

    def test_ingest(self):
        self.put_threads([
            thread("d1", [note("fix this", path="src/x.tsx", line=4), note("reply")]),
            thread("d2", [note("done")], resolved=True),
            thread("d3", [note("assigned", system=True)], resolvable=False, system=True),
            thread("d4", [note("just a remark")], resolvable=False),
        ])
        put_pipeline("ws-dev-1", "React", "pack", id="77", status="failed", jobs=[
            {"id": "1", "name": "lint", "stage": "test", "status": "failed", "allow_failure": False},
            {"id": "2", "name": "build", "status": "success", "allow_failure": False},
            {"id": "3", "name": "slow", "status": "failed", "allow_failure": True, "url": "https://ci.example/3"}])
        n, path = feedback.ingest("ws-dev-1")
        self.assertEqual(n, 3)
        rec = json.loads(path.read_text())
        self.assertEqual(rec["kind"], "feedback")
        self.assertEqual(path.name, "feedback-1.json")
        by_source = {f["source"]: f for f in rec["findings"]}
        self.assertEqual(sorted(by_source), ["job:1", "job:3", "thread:d1"])
        self.assertEqual(by_source["thread:d1"]["file"], "src/x.tsx:4")
        self.assertEqual(by_source["thread:d1"]["text"], "rev: fix this (+1 reply)")
        self.assertEqual((by_source["thread:d1"]["severity"], by_source["thread:d1"]["repo"]), ("major", "React"))
        self.assertEqual((by_source["job:1"]["severity"], by_source["job:3"]["severity"]), ("major", "minor"))
        self.assertEqual(by_source["job:1"]["repo"], "React")
        self.assertIn("lint (stage test)", by_source["job:1"]["text"])
        self.assertIn("https://ci.example/3", by_source["job:3"]["text"])
        texts = " ".join(f["text"] for f in rec["findings"])
        self.assertNotIn("done", texts)
        self.assertNotIn("assigned", texts)
        self.assertNotIn("remark", texts)
        run = json.loads((self.home / "runs/ws-dev-1/run.json").read_text())
        self.assertEqual(run["phase"], "build")
        self.assertEqual(run["loops"]["review"], 1)
        n2, p2 = feedback.ingest("ws-dev-1")
        self.assertEqual((n2, p2), (0, None))

    def test_pipeline_failure_without_job_detail(self):
        self.put_threads([])
        put_pipeline("ws-dev-1", "React", "pack", id="55", status="failed")
        n, path = feedback.ingest("ws-dev-1")
        self.assertEqual(n, 1)
        finding = json.loads(path.read_text())["findings"][0]
        self.assertEqual((finding["source"], finding["repo"]), ("pipeline:55", "React"))

    def test_legacy_discussion_ids_dedupe_as_threads(self):
        self.put_threads([thread("abc", [note("old remark")]), thread("new", [note("new remark")])])
        legacy = {"schemaVersion": 1, "kind": "feedback", "round": 1, "verdict": "CHANGES", "reviewer": "feedback-ingest",
                  "findings": [{"id": "F1", "severity": "major", "file": "", "text": "x", "repo": "React", "source": "discussion:abc"}]}
        self.write("runs/ws-dev-1/reviews/feedback-1.json", legacy)
        n, path = feedback.ingest("ws-dev-1")
        self.assertEqual(n, 1)
        rec = json.loads(path.read_text())
        self.assertEqual(path.name, "feedback-2.json")
        self.assertEqual([f["source"] for f in rec["findings"]], ["thread:new"])
        self.assertEqual(feedback.ingest("ws-dev-1"), (0, None))

    def test_stale_contracts_are_reported_not_dropped(self):
        self.put_threads([thread("d1", [note("fix this")])])
        (self.home / "runs/ws-dev-1/raw/change/React-pack.json").write_text("{}", encoding="utf-8")  # contract is stale
        with self.assertRaises(FactoryError) as cm:
            feedback.ingest("ws-dev-1")
        self.assertIn("stale or invalid", str(cm.exception))
        self.assertIn("changed since ingest", str(cm.exception))
        self.assertFalse(list((self.home / "runs/ws-dev-1/reviews").glob("feedback-*.json")))

    def test_only_changes_of_the_ship_plan_are_read(self):
        self.put_threads([])
        put_change("ws-dev-1", "React", "other", threads=[thread("x1", [note("not shipped")])])  # not in the plan
        self.assertEqual(feedback.ingest("ws-dev-1"), (0, None))
        (self.home / "runs/ws-dev-1/ship-plan.json").unlink()
        with self.assertRaises(FactoryError) as cm:
            feedback.ingest("ws-dev-1")
        self.assertIn("ship-plan.json missing", str(cm.exception))

    def test_changes_without_thread_data_yield_nothing(self):
        put_change("ws-dev-1", "React", "pack")  # threads absent: unknown, not none
        self.assertEqual(feedback.ingest("ws-dev-1"), (0, None))

    def test_the_run_dir_remote_files_are_not_read(self):
        self.write("runs/ws-dev-1/remote/anything.json", [{"id": "z", "notes": [{"body": "x"}]}])
        self.assertEqual(feedback.ingest("ws-dev-1"), (0, None))


if __name__ == "__main__":
    unittest.main()
