"""Blocker 5: runtime evidence is bound to the env started from the check's worktree at HEAD; pid reuse."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from factory.commands import env as envmod
from tests.integrity_support import IntegrityCase, cli, commit_file, core, fake_env_record


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"total": 10}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class EnvCase(IntegrityCase):
    def setUp(self) -> None:
        super().setUp()
        self.srv, self.port = serve()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.ws["repos"][0]["env"] = {"start": [sys.executable, "-c", "pass"],
                                      "health": f"http://127.0.0.1:{self.port}/", "ports": [self.port]}
        self.save_ws()
        self.start()
        self.acs([{"id": "CHK-1", "type": "api", "repo": "React", "path": "/x", "expect": {"status": 200}}],
                 [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "api", "checks": ["CHK-1"]}])
        commit_file(self.wt, "src/a.txt", "a\n")


class BindingTests(EnvCase):
    def test_no_record_is_error_evidence(self):
        cli("prove", self.rid, "--check", "CHK-1")
        ev = core.load_evidence(self.rid, "CHK-1")
        self.assertEqual(ev["status"], "error")
        self.assertIn("not started by `factory env up", ev["summary"])

    def test_bound_env_runs_the_check(self):
        fake_env_record("React", self.wt)
        rc, out = cli("prove", self.rid, "--check", "CHK-1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.load_evidence(self.rid, "CHK-1")["status"], "pass")

    def test_env_from_another_worktree_is_error(self):
        fake_env_record("React", self.repo_dirs["React"])
        cli("prove", self.rid, "--check", "CHK-1")
        ev = core.load_evidence(self.rid, "CHK-1")
        self.assertEqual(ev["status"], "error")
        self.assertIn("running env serves", ev["summary"])

    def test_env_started_at_older_head_is_error(self):
        fake_env_record("React", self.wt)
        commit_file(self.wt, "src/b.txt", "b\n")
        cli("prove", self.rid, "--check", "CHK-1")
        ev = core.load_evidence(self.rid, "CHK-1")
        self.assertEqual(ev["status"], "error")
        self.assertIn("worktree HEAD is", ev["summary"])

    def test_reused_pid_is_not_our_env(self):
        fake_env_record("React", self.wt, create_time="1")
        err = envmod.binding_error(self.ws, "React", self.wt, core.git_head(self.wt))
        self.assertIn("not running (or its pid was reused)", err)


class UpDownTests(EnvCase):
    def test_up_refuses_when_already_up_for_another_worktree(self):
        fake_env_record("React", self.repo_dirs["React"])
        ok, lines = envmod.up(self.ws, "React", core.run_load(self.rid))
        self.assertFalse(ok)
        self.assertIn("env down", lines[-1])

    def test_up_refuses_when_already_up_at_another_head(self):
        fake_env_record("React", self.wt, head="0" * 40)
        ok, lines = envmod.up(self.ws, "React", core.run_load(self.rid))
        self.assertFalse(ok)
        self.assertIn("first, then up again", lines[-1])

    def test_unknown_server_answering_without_ports_is_refused(self):
        self.ws["repos"][0]["env"].pop("ports")
        self.save_ws()
        ok, lines = envmod.up(self.ws, "React", core.run_load(self.rid))
        self.assertFalse(ok)
        self.assertIn(f"unknown server already answering at http://127.0.0.1:{self.port}/", lines[-1])

    def test_down_refuses_to_kill_reused_pid(self):
        fake_env_record("React", self.wt, create_time="1")  # our pid, someone else's start time
        with mock.patch.object(envmod, "kill_tree") as kill:
            msg = envmod.down(self.ws, "React")
        kill.assert_not_called()
        self.assertIn("not killed", msg)
        self.assertFalse(envmod.pid_path("ws", "React").exists())

    def test_record_has_create_time_and_matches_live_process(self):
        stamp = envmod.process_create_time(os.getpid())
        self.assertTrue(stamp)
        self.assertEqual(stamp, envmod.process_create_time(os.getpid()))
        self.assertTrue(envmod.record_alive({"pid": os.getpid(), "create_time": stamp}))
        self.assertFalse(envmod.record_alive({"pid": os.getpid(), "create_time": stamp + "0"}))

    def test_up_writes_bound_record(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.ws["repos"][0]["env"] = {"start": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                                      "health": f"http://127.0.0.1:{port}/", "ports": [port], "startup_timeout_s": 30}
        self.save_ws()
        ok, lines = envmod.up(self.ws, "React", core.run_load(self.rid))
        self.addCleanup(envmod.down, self.ws, "React")
        self.assertTrue(ok, lines)
        rec = envmod.read_record("ws", "React")
        self.assertEqual(Path(rec["cwd"]), self.wt.resolve())
        self.assertEqual(rec["head"], core.git_head(self.wt))
        self.assertTrue(rec["create_time"])
        self.assertIsNone(envmod.binding_error(self.ws, "React", self.wt, core.git_head(self.wt)))


class LoginSecretsTests(IntegrityCase):
    def test_password_goes_through_env_not_argv(self):
        self.ws["repos"][0]["env"] = {"start": ["x"], "health": "http://127.0.0.1:1/",
                                      "login": {"url": "/login", "user_field": "#u", "pass_field": "#p",
                                                "submit": "button", "success_url_contains": "/app"}}
        self.save_ws()
        secrets = core.secrets_path("ws")
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text("REACT_USER=bob\nREACT_PASS=hunter22\n", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return mock.Mock(returncode=0, stdout="ok", stderr="")

        cli_js = envmod.factory_root() / "browser" / "dist" / "cli.js"
        with mock.patch.object(envmod.subprocess, "run", fake_run), \
                mock.patch.object(envmod.shutil, "which", return_value="node"), \
                mock.patch.object(Path, "is_file", lambda p: True if p == cli_js else Path.exists(p)):
            ok, _ = envmod.login(self.ws, "React")
        self.assertTrue(ok)
        cmd, kw = calls[0]
        self.assertNotIn("hunter22", " ".join(cmd))
        self.assertNotIn("--pass", cmd)
        self.assertEqual(kw["env"]["FACTORY_LOGIN_PASS"], "hunter22")
        self.assertEqual(kw["env"]["FACTORY_LOGIN_USER"], "bob")
        cfg = Path(cmd[cmd.index("--login-json") + 1])
        self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["url"], "/login")


if __name__ == "__main__":
    unittest.main()
