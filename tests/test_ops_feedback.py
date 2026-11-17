import json
import unittest

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo

from factory.commands import feedback


def note(body, system=False, resolvable=True, resolved=False, pos=None):
    return {"id": 1, "body": body, "system": system, "resolvable": resolvable, "resolved": resolved,
            "author": {"username": "rev"}, "position": pos}


class FeedbackTests(FactoryCase):
    def setUp(self):
        super().setUp()
        repo = make_repo(self.tmp / "react")
        self.write("workspaces/ws/workspace.json", {"schemaVersion": 1, "name": "ws", "repos": [{"alias": "React", "path": str(repo)}]})
        self.make_run("ws-dev-1", repos={"React": {"branch": "b", "base": "pack"}}, phase="ship")

    def test_ingest(self):
        discs = [
            {"id": "d1", "notes": [note("fix this", pos={"new_path": "src/x.tsx", "new_line": 4}), note("reply", resolvable=False)]},
            {"id": "d2", "notes": [note("done", resolved=True)]},
            {"id": "d3", "notes": [note("assigned", system=True, resolvable=False)]},
        ]
        self.write("runs/ws-dev-1/remote/mr-React-pack-discussions.json", discs)
        self.write("runs/ws-dev-1/remote/pipeline-React-77.json", {"id": 77, "status": "failed", "jobs": [
            {"id": 1, "name": "lint", "stage": "test", "status": "failed"}, {"id": 2, "name": "build", "status": "success"}]})
        n, path = feedback.ingest("ws-dev-1")
        self.assertEqual(n, 2)
        rec = json.loads(path.read_text())
        self.assertEqual(rec["kind"], "feedback")
        self.assertEqual(path.name, "feedback-1.json")
        texts = " ".join(f["text"] for f in rec["findings"])
        self.assertIn("fix this", texts)
        self.assertIn("lint", texts)
        self.assertNotIn("done", texts)
        self.assertNotIn("assigned", texts)
        self.assertEqual(rec["findings"][0]["file"], "src/x.tsx:4")
        run = json.loads((self.home / "runs/ws-dev-1/run.json").read_text())
        self.assertEqual(run["phase"], "build")
        self.assertEqual(run["loops"]["review"], 1)
        n2, p2 = feedback.ingest("ws-dev-1")
        self.assertEqual((n2, p2), (0, None))


if __name__ == "__main__":
    unittest.main()
