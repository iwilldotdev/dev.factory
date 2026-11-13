"""Collector `screenshot` (DESIGN section 12) against the fake browser CLI."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory.collectors import screenshot  # noqa: E402
from factory.core import Ctx  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_cli.py"))


class ScreenshotCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run"
        (self.run_dir / "checks").mkdir(parents=True)
        (self.run_dir / "checks" / "steps.spec.ts").write_text("x")
        self.out = self.run_dir / "evidence" / "CHK-9"
        self.ctx = Ctx(run={}, workspace={"name": "ws"}, run_dir=self.run_dir,
                       env={"base_url": "http://localhost:5173/"}, out_dir=self.out)
        patcher = mock.patch.dict(os.environ, {"FACTORY_NODE_CLI": FAKE})
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in ("FAKE_STATUS", "FAKE_NO_RESULT", "FAKE_NO_PNG"):
            os.environ.pop(k, None)

    def argv(self, name):
        return json.loads((self.out / f"argv-{name}.json").read_text())

    def test_path_mode_captures_without_compare(self):
        res = screenshot.collect({"id": "CHK-9", "type": "screenshot", "repo": "React", "path": "/obras/1",
                                  "wait_for": "#list", "viewport": [1280, 800]}, self.ctx)
        self.assertEqual(res["status"], "pass", res)
        self.assertEqual(res["artifacts"], ["evidence/CHK-9/app.png"])
        cap = self.argv("capture")
        self.assertEqual(cap[cap.index("--url") + 1], "http://localhost:5173/obras/1")
        self.assertEqual(cap[cap.index("--viewport") + 1], "1280x800")
        self.assertIn("#list", cap)
        self.assertFalse((self.out / "argv-compare.json").exists())

    def test_spec_mode_runs_with_screenshot_on_and_lists_pngs(self):
        res = screenshot.collect({"id": "CHK-9", "type": "screenshot", "repo": "React",
                                  "spec": "checks/steps.spec.ts"}, self.ctx)
        self.assertEqual(res["status"], "pass", res)
        a = self.argv("run-spec")
        self.assertEqual(a[a.index("--screenshot") + 1], "on")
        self.assertEqual(res["artifacts"], ["evidence/CHK-9/artifacts/t/test-finished-1.png"])

    def test_spec_failure_fails_at_head_but_is_tolerated_before(self):
        os.environ["FAKE_STATUS"] = "fail"
        chk = {"id": "CHK-9", "type": "screenshot", "repo": "React", "spec": "checks/steps.spec.ts"}
        self.assertEqual(screenshot.collect(chk, self.ctx)["status"], "fail")
        res = screenshot.collect(dict(chk, before=True), self.ctx)
        self.assertEqual(res["status"], "pass", res)
        self.assertIn("steps on base", res["summary"])

    def test_spec_without_png_is_error(self):
        os.environ["FAKE_NO_PNG"] = "1"
        res = screenshot.collect({"id": "CHK-9", "type": "screenshot", "repo": "React",
                                  "spec": "checks/steps.spec.ts", "before": True}, self.ctx)
        self.assertEqual(res["status"], "error", res)

    def test_no_base_url_is_error(self):
        ctx = Ctx(run={}, workspace={"name": "ws"}, run_dir=self.run_dir, env={}, out_dir=self.out)
        res = screenshot.collect({"id": "CHK-9", "type": "screenshot", "repo": "React", "path": "/"}, ctx)
        self.assertEqual(res["status"], "error")


if __name__ == "__main__":
    unittest.main()
