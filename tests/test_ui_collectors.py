import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory.collectors import browser, style, visual  # noqa: E402
from factory.core import Ctx  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_cli.py"))


class UiCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name) / "run"
        (self.run_dir / "figma").mkdir(parents=True)
        (self.run_dir / "checks").mkdir()
        (self.run_dir / "figma" / "3544-5710.png").write_bytes(b"png")
        (self.run_dir / "checks" / "a.spec.ts").write_text("x")
        (self.run_dir / "checks" / "map.json").write_text("{}")
        self.out = self.run_dir / "evidence" / "CHK-1"
        self.ctx = Ctx(run={}, workspace={"name": "ws"}, run_dir=self.run_dir,
                       env={"base_url": "http://localhost:5173/"}, out_dir=self.out)
        patcher = mock.patch.dict(os.environ, {"FACTORY_NODE_CLI": FAKE})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("FAKE_STATUS", None)
        os.environ.pop("FAKE_NO_RESULT", None)

    def argv(self, name):
        return json.loads((self.out / f"argv-{name}.json").read_text())

    def test_style_pass_builds_url_and_relative_artifacts(self):
        res = style.collect({"id": "CHK-1", "type": "style", "path": "/obras/1", "map": "checks/map.json",
                             "viewport": [1440, 900]}, self.ctx)
        self.assertEqual(res["status"], "pass")
        self.assertEqual(res["artifacts"], ["evidence/CHK-1/app.png"])
        a = self.argv("style-probe")
        self.assertEqual(a[a.index("--url") + 1], "http://localhost:5173/obras/1")
        self.assertEqual(a[a.index("--viewport") + 1], "1440x900")

    def test_style_fail_is_passed_through(self):
        os.environ["FAKE_STATUS"] = "fail"
        res = style.collect({"id": "CHK-1", "type": "style", "path": "/", "map": "checks/map.json"}, self.ctx)
        self.assertEqual(res["status"], "fail")
        del os.environ["FAKE_STATUS"]

    def test_visual_runs_capture_then_compare_with_masks_and_copies(self):
        res = visual.collect({"id": "CHK-1", "type": "visual", "node": "3544:5710", "path": "/x",
                              "selector": "#d", "viewport": [800, 600], "max_diff_ratio": 0.02,
                              "masks": [{"x": 0, "y": 0, "width": 2, "height": 2, "reason": "clock"}],
                              "copies": ["Pay", "Now"]}, self.ctx)
        self.assertEqual(res["status"], "pass")
        cap, cmp = self.argv("capture"), self.argv("compare")
        self.assertIn("#d", cap)
        self.assertTrue(cmp[cmp.index("--figma") + 1].endswith("3544-5710.png"))
        self.assertEqual(cmp[cmp.index("--copies") + 1], "Pay|Now")
        self.assertEqual(cmp[cmp.index("--max-diff-ratio") + 1], "0.02")
        masks = json.loads(Path(cmp[cmp.index("--masks") + 1]).read_text())
        self.assertEqual(masks[0]["reason"], "clock")

    def test_visual_missing_figma_png_errors(self):
        (self.run_dir / "figma" / "3544-5710.png").unlink()
        res = visual.collect({"id": "CHK-1", "type": "visual", "node": "3544:5710", "path": "/"}, self.ctx)
        self.assertEqual(res["status"], "error")

    def test_browser_runs_spec_relative_to_run_dir(self):
        res = browser.collect({"id": "CHK-1", "type": "browser", "spec": "checks/a.spec.ts"}, self.ctx)
        self.assertEqual(res["status"], "pass")
        a = self.argv("run-spec")
        self.assertEqual(Path(a[a.index("--spec") + 1]), self.run_dir / "checks" / "a.spec.ts")
        self.assertEqual(a[a.index("--base-url") + 1], "http://localhost:5173/")

    def test_storage_state_used_when_file_exists(self):
        state = Path(self.tmp.name) / "state.json"
        state.write_text("{}")
        fake_env = mock.Mock(storage_state_path=lambda ws, repo: state)
        with mock.patch.dict(sys.modules, {"factory.commands.env": fake_env}):
            browser.collect({"id": "CHK-1", "type": "browser", "repo": "R", "spec": "checks/a.spec.ts"}, self.ctx)
        a = self.argv("run-spec")
        self.assertEqual(a[a.index("--storage-state") + 1], str(state))

    def test_missing_result_json_is_error(self):
        os.environ["FAKE_NO_RESULT"] = "1"
        res = style.collect({"id": "CHK-1", "type": "style", "path": "/", "map": "checks/map.json"}, self.ctx)
        self.assertEqual(res["status"], "error")
        del os.environ["FAKE_NO_RESULT"]

    def test_missing_dist_is_clear_error(self):
        del os.environ["FACTORY_NODE_CLI"]
        with mock.patch.object(browser, "FACTORY_ROOT", Path(self.tmp.name)), \
                mock.patch("shutil.which", return_value="node"):
            res = style.collect({"id": "CHK-1", "type": "style", "path": "/", "map": "checks/map.json"}, self.ctx)
        self.assertEqual(res["status"], "error")
        self.assertIn("not built", res["summary"])

    def test_node_missing_is_error(self):
        del os.environ["FACTORY_NODE_CLI"]
        with mock.patch("shutil.which", return_value=None):
            res = browser.collect({"id": "CHK-1", "type": "browser", "spec": "checks/a.spec.ts"}, self.ctx)
        self.assertEqual(res["status"], "error")
        self.assertIn("node", res["summary"])


if __name__ == "__main__":
    unittest.main()
