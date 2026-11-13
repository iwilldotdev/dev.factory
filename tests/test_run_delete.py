"""`factory run delete`: archive first, refuse unfinished/orphaning/other-session deletes."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops_helpers import FactoryCase  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402


def cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = main(list(argv))
    return rc, out.getvalue()


class RunDeleteTests(FactoryCase):
    def setUp(self):
        super().setUp()
        for k in core.SESSION_ID_VARS:
            self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)

    def test_needs_yes(self):
        self.make_run(phase="done")
        rc, out = cli("run", "delete", "ws-dev-1")
        self.assertEqual(rc, 2)
        self.assertIn("--yes", out)
        self.assertTrue(core.run_dir("ws-dev-1").is_dir())

    def test_archives_then_removes(self):
        self.make_run(phase="done", repos={"API": {"worktree": "C:/wt/x"}})
        self.write("runs/ws-dev-1/evidence/CHK-1.json", {"status": "pass"})
        rc, out = cli("run", "delete", "ws-dev-1", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(core.run_dir("ws-dev-1").exists())
        zips = list((self.home / "archive" / "runs").glob("ws-dev-1-*.zip"))
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(zips[0]) as zf:
            names = set(n.replace("\\", "/") for n in zf.namelist())
        self.assertIn("ws-dev-1/run.json", names)
        self.assertIn("ws-dev-1/evidence/CHK-1.json", names)
        self.assertIn("worktree kept: C:/wt/x", out)

    def test_unfinished_needs_force(self):
        self.make_run(phase="review")
        rc, out = cli("run", "delete", "ws-dev-1", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("--force", out)
        self.assertTrue(core.run_dir("ws-dev-1").is_dir())
        self.assertEqual(cli("run", "delete", "ws-dev-1", "--yes", "--force")[0], 0)
        self.assertFalse(core.run_dir("ws-dev-1").exists())

    def test_parent_without_children_refused_atomically(self):
        self.make_run("ws-batch", phase="report", children=["ws-c1", "ws-c2"])
        self.make_run("ws-c1", phase="done", parent="ws-batch")
        self.make_run("ws-c2", phase="done", parent="ws-batch")
        rc, out = cli("run", "delete", "ws-batch", "ws-c1", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("ws-c2", out)
        for rid in ("ws-batch", "ws-c1", "ws-c2"):
            self.assertTrue(core.run_dir(rid).is_dir(), rid)
        rc, out = cli("run", "delete", "ws-batch", "ws-c1", "ws-c2", "--yes")
        self.assertEqual(rc, 0, out)
        for rid in ("ws-batch", "ws-c1", "ws-c2"):
            self.assertFalse(core.run_dir(rid).exists(), rid)

    def test_other_session_refused(self):
        self.make_run(phase="done")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "aaaaaaaa-1111"
        self.assertEqual(cli("run", "claim", "ws-dev-1")[0], 0)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "bbbbbbbb-2222"
        rc, out = cli("run", "delete", "ws-dev-1", "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("claimed by session aaaaaaaa", out)
        self.assertTrue(core.run_dir("ws-dev-1").is_dir())


if __name__ == "__main__":
    unittest.main()
