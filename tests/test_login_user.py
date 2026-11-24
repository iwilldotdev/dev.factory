import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory.collectors import browser, style, visual  # noqa: E402
from factory.commands import env as env_mod  # noqa: E402
from factory.commands import run as run_mod  # noqa: E402
from factory.core import Ctx, FactoryError, run_load  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_cli.py"))
EMAIL = "Maria.Silva@Acme.com"
SLUG = "maria_silva_acme_com"


class LoginUserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / "secrets").mkdir(parents=True)
        (self.home / "workspaces" / "ws").mkdir(parents=True)
        (self.home / "workspaces" / "ws" / "workspace.json").write_text(json.dumps({"schemaVersion": 1, "name": "ws", "repos": []}))
        p = mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.home), "FACTORY_NODE_CLI": FAKE})
        p.start()
        self.addCleanup(p.stop)

    def secrets(self, text):
        (self.home / "secrets" / "ws.env").write_text(text, encoding="utf-8")

    # --- storage path / password ---

    def test_storage_path_per_user(self):
        default = env_mod.storage_state_path("ws", "web")
        other = env_mod.storage_state_path("ws", "web", EMAIL)
        self.assertEqual(default.name, "web.storage.json")
        self.assertEqual(other.name, f"web.{SLUG}.storage.json")
        self.assertEqual(default.parent, other.parent)

    def test_password_falls_back_to_default(self):
        self.secrets("WEB_USER=def@x.com\nWEB_PASS=default-pw\n")
        self.assertEqual(env_mod._credentials("ws", "web"), ("def@x.com", "default-pw"))
        self.assertEqual(env_mod._credentials("ws", "web", EMAIL), (EMAIL, "default-pw"))

    def test_password_override_by_slug(self):
        self.secrets(f"USER=def@x.com\nPASS=default-pw\nPASS_{SLUG}=generic\n")
        self.assertEqual(env_mod._credentials("ws", "web", EMAIL)[1], "generic")
        self.secrets(f"USER=def@x.com\nPASS=default-pw\nPASS_{SLUG}=generic\nWEB_PASS_{SLUG}=specific\n")
        self.assertEqual(env_mod._credentials("ws", "web", EMAIL)[1], "specific")

    def test_login_user_needs_some_password(self):
        self.secrets("USER=def@x.com\n")
        with self.assertRaises(FactoryError):
            env_mod._credentials("ws", "web", EMAIL)

    # --- run start / login-user ---

    def start_args(self, **kw):
        base = dict(workspace="ws", tickets=["T-1"], epic=None, risk="normal", bug=None, hotfix=False,
                    feature=False, json=False, login_user=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_run_start_records_login_user(self):
        with mock.patch("builtins.print"):
            run_mod.cmd_start(self.start_args(login_user=EMAIL))
            run_mod.cmd_start(self.start_args(tickets=["T-2"]))
        self.assertEqual(run_load("ws-t-1")["login_user"], EMAIL)
        self.assertNotIn("login_user", run_load("ws-t-2"))

    def test_run_login_user_records_event_and_locks_after_prove(self):
        with mock.patch("builtins.print"):
            run_mod.cmd_start(self.start_args())
            run_mod.cmd_login_user(argparse.Namespace(run="ws-t-1", email=EMAIL, json=False))
        run = run_load("ws-t-1")
        self.assertEqual(run["login_user"], EMAIL)
        ev = [e for e in run["events"] if e["kind"] == "login_user"]
        self.assertEqual(ev[0]["data"]["user"], EMAIL)
        run["events"].append({"at": "x", "kind": "gate_pass", "data": {"phase": "prove"}})
        from factory.core import run_save
        run_save(run)
        with self.assertRaises(FactoryError):
            run_mod.cmd_login_user(argparse.Namespace(run="ws-t-1", email="b@x.com", json=False))
        self.assertEqual(run_load("ws-t-1")["login_user"], EMAIL)

    # --- collectors ---

    def ctx(self, login_user=None):
        run_dir = Path(self.tmp.name) / "run"
        (run_dir / "checks").mkdir(parents=True, exist_ok=True)
        from tests.design_support import write_frame_files
        write_frame_files(run_dir, "1:2")
        (run_dir / "checks" / "a.spec.ts").write_text("x")
        (run_dir / "checks" / "map.json").write_text("{}")
        out = run_dir / "evidence" / "CHK-1"
        run = {"login_user": login_user} if login_user else {}
        return Ctx(run=run, workspace={"name": "ws"}, run_dir=run_dir,
                   env={"base_url": "http://localhost:5173"}, out_dir=out), out

    def write_states(self):
        for path in (env_mod.storage_state_path("ws", "web"), env_mod.storage_state_path("ws", "web", EMAIL)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")

    def argv(self, out, name):
        return json.loads((out / f"argv-{name}.json").read_text())

    def test_collectors_pick_state_of_login_user(self):
        self.write_states()
        want = str(env_mod.storage_state_path("ws", "web", EMAIL))
        default = str(env_mod.storage_state_path("ws", "web"))
        for login_user, expected in ((EMAIL, want), (None, default)):
            ctx, out = self.ctx(login_user)
            browser.collect({"id": "CHK-1", "repo": "web", "spec": "checks/a.spec.ts"}, ctx)
            a = self.argv(out, "run-spec")
            self.assertEqual(a[a.index("--storage-state") + 1], expected)
            visual.collect({"id": "CHK-1", "repo": "web", "node": "1:2", "path": "/"}, ctx)
            a = self.argv(out, "capture")
            self.assertEqual(a[a.index("--storage-state") + 1], expected)
            style.collect({"id": "CHK-1", "repo": "web", "path": "/", "map": "checks/map.json"}, ctx)
            a = self.argv(out, "style-probe")
            self.assertEqual(a[a.index("--storage-state") + 1], expected)

    def test_missing_storage_is_error_with_command(self):
        ctx, _ = self.ctx(EMAIL)
        cmd = f"factory env login ws web --user {EMAIL}"
        for res in (
            browser.collect({"id": "CHK-1", "repo": "web", "spec": "checks/a.spec.ts"}, ctx),
            visual.collect({"id": "CHK-1", "repo": "web", "node": "1:2", "path": "/"}, ctx),
            style.collect({"id": "CHK-1", "repo": "web", "path": "/", "map": "checks/map.json"}, ctx),
        ):
            self.assertEqual(res["status"], "error")
            self.assertIn(cmd, res["summary"])


if __name__ == "__main__":
    unittest.main()
