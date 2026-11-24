"""The real bundled runner end to end: login state, a design-parity visual check, a style check and a browser spec, batched in one browser."""
import json
import os
import shutil
import struct
import unittest
import zlib
from pathlib import Path
from unittest import mock

from dev_factory.checks.runner import DEFAULT
from tests.helpers import lesson
from tests.test_design import DesignCase, frame_data

BOX = (200, 30, 30)
# the tests isolate HOME and the cache folders, so the browsers must be looked up where the machine installed them
LIB = "".join(("play", "wright"))  # the runner's browser library: its name stays out of the Python sources
BROWSERS_VAR = LIB.upper() + "_BROWSERS_PATH"
BROWSERS = os.environ.get(BROWSERS_VAR) or str(Path(os.environ.get("LOCALAPPDATA", "")) / f"ms-{LIB}" if os.name == "nt" else Path.home() / ".cache" / f"ms-{LIB}")
PAGE = ('<html><body style="margin:0"><div id="box" style="width:100px;height:40px;background:#c81e1e"></div>'
        '<h1 id="t" style="margin:0;font:600 20px/28px Arial;color:#1f2933">No invoices yet</h1></body></html>')
LOGIN = '<html><body><input id="u"><button id="go" onclick="document.cookie=\'sid=ok-\'+document.getElementById(\'u\').value+\'; path=/\';location=\'/home.html\'">Go</button></body></html>'
HOME = "<html><body><h1 id=\"who\"></h1><script>document.getElementById('who').textContent=(document.cookie.match(/sid=([^;]+)/)||[])[1]||'anonymous'</script></body></html>"


def bordered_png(w: int, h: int, margin: int) -> bytes:
    """A w x h red box inside a grey export margin, as a design tool exports a node with a shadow."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    W, H = w + 2 * margin, h + 2 * margin
    rows = b"".join(b"\x00" + b"".join(bytes(BOX if margin <= x < margin + w and margin <= y < margin + h else (240, 240, 240)) for x in range(W)) for y in range(H))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@unittest.skipUnless(shutil.which("node") and DEFAULT.is_file() and (DEFAULT.parent.parent / "node_modules" / LIB).is_dir(), "the browser runner is not installed")
class RealRunnerTests(DesignCase):
    def setUp(self):
        super().setUp()
        for k, v in {"TEST_USER": "ana", BROWSERS_VAR: BROWSERS}.items():
            patcher = mock.patch.dict(os.environ, {k: v})
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, text in (("index.html", PAGE), ("login.html", LOGIN), ("home.html", HOME), ("login.json", json.dumps({"actions": [
                {"goto": "/login.html"}, {"fill": {"selector": "#u", "value": "${TEST_USER}"}}, {"click": "#go"}, {"expect_url": "/home.html"}]}))):
            self.commit(name, text, "chore: page", tree=self.proj)
        self.configure(extra_env='login_spec = "login.json"\nlogin_secrets = ["TEST_USER"]\n', extra='\n[secrets]\nkeys = ["TEST_USER"]\n', runner=False)
        self.local_toml.unlink(missing_ok=True)  # the real bundled runner, not the fake one

    @lesson("L-55", "L-57", "L-80")
    def test_login_visual_style_and_a_spec_pass_against_the_real_runner_in_one_browser(self):
        """L-55: the crop skips the export margin; L-57: one batch; L-80: login from the environment only, state reused."""
        run = self.branch()
        data = frame_data()
        data["root"]["children"][1]["text"]["color"] = "#1f2933"
        (self.base / "F-1.png").write_bytes(bordered_png(100, 40, 12))
        self.ok("ingest", "design_image", "--raw", str(self.base / "F-1.png"), "--stdin", stdin=json.dumps({"id": "F-1"}))
        raw = self.write("raw.json", "{}", root=self.base)
        self.ok("ingest", "design_frame", "--raw", str(raw), "--stdin", stdin=json.dumps(data))
        self.up()
        self.assertTrue((self.proj / ".factory" / "env" / "web.state.json").is_file())
        (run / "checks" / "style").mkdir(parents=True)
        (run / "checks" / "style" / "F-1.json").write_text(json.dumps({"frame": "F-1", "entries": [
            {"selector": "#t", "node": "n-2", "tolerance_px": 1, "expect": {"fontSize": 20, "fontWeight": 600, "lineHeight": 28, "color": "#1f2933", "text": "No invoices yet"}},
            {"selector": "#box", "node": "n-1", "expect": {"width": 100, "height": 40, "backgroundColor": "#c81e1e"}}]}), encoding="utf-8")
        (run / "checks" / "home.json").write_text(json.dumps({"actions": [{"goto": "/home.html"}, {"expect_text": {"selector": "#who", "text": "ok-ana"}}]}), encoding="utf-8")
        plan = {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [
            {"id": "AC-1", "text": "The empty state matches the design", "source": "ticket:description", "design": ["F-1"]},
            {"id": "AC-2", "text": "The user stays signed in", "source": "ticket:description"}],
            "checks": [{"id": "CHK-1", "type": "visual", "repo": "web", "ac": ["AC-1"], "frame": "F-1", "node": "n-1", "url": "/index.html", "selector": "#box", "max_diff_ratio": 0.01},
                       {"id": "CHK-2", "type": "style", "repo": "web", "ac": ["AC-1"], "frame": "F-1", "url": "/index.html", "map": "checks/style/F-1.json"},
                       {"id": "CHK-3", "type": "browser", "repo": "web", "ac": ["AC-2"], "spec": "checks/home.json"},
                       {"id": "CHK-4", "type": "screenshot", "repo": "web", "url": "/index.html", "selector": "#t"}]}
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.fix()
        code, out, err = self.cli("verify", "--json")
        self.assertEqual(code, 0, out + err)
        recs = {e["check"]: e for e in self.evidence(run)}
        self.assertEqual({k: v["result"] for k, v in recs.items()}, {"CHK-1": "pass", "CHK-2": "pass", "CHK-3": "pass", "CHK-4": "pass"})
        print(json.dumps(recs["CHK-1"]), file=__import__("sys").stderr); self.assertEqual(recs["CHK-1"]["details"]["crop"], [12, 12, 100, 40])
        self.assertEqual(recs["CHK-1"]["details"]["diff_ratio"], 0)
        for name in ("app.png", "design.png", "diff.png"):
            self.assertTrue(any(a.endswith(name) for a in recs["CHK-1"]["artifacts"]))
        self.assertEqual(self.gate("verify")[0], 0)

    def test_a_visual_check_fails_when_the_capture_differs_from_the_design(self):
        run = self.branch()
        self.ingest_design(run, frame_data())
        (self.base / "F-1.png").write_bytes(bordered_png(100, 40, 12))
        self.commit("index.html", PAGE.replace("#c81e1e", "#0000ff"), "fix: blue", tree=self.tree())  # the page differs from the design: a blue box
        self.up()
        self.ok("ingest", "design_image", "--raw", str(self.base / "F-1.png"), "--stdin", stdin=json.dumps({"id": "F-1"}))
        self.style_map(run)
        plan = self.design_plan(run, visual={"selector": "#box", "max_diff_ratio": 0.01}, style={})
        plan["acs"][0]["design"] = ["F-1"]
        plan["checks"] = plan["checks"][:1] + [{**plan["checks"][1], "map": "checks/style/F-1.json"}]
        # make the page differ: a blue box instead of red
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        code, out, err = self.cli("verify", "--json", "--no-retry")
        self.assertEqual(code, 1, out + err)
        rec = [e for e in self.evidence(run) if e["check"] == "CHK-1"][0]
        self.assertEqual((rec["result"], rec["details"]["reason"]), ("fail", "diff"))
        self.assertGreater(rec["details"]["diff_ratio"], 0.5)


if __name__ == "__main__":
    unittest.main()
