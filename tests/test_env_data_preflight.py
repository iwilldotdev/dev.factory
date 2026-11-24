"""`env.preflight[].request`: HTTP test-data preflight (ENV-FAIL kind=data), with the stored login session (DESIGN section 14)."""

from __future__ import annotations

import http.server
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_failfast_prove import ProveCase, cli  # noqa: E402

from factory import core  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import prove, workspace  # noqa: E402


class AppHandler(http.server.BaseHTTPRequestHandler):
    """A tiny app: /ok, /expired (500 like a failing loader), /me (needs the session cookie and a bearer token)."""

    def do_GET(self):
        status, body = 200, {"obra": {"active": True, "items": [{"id": 7}]}}
        if self.path.startswith("/expired"):
            status, body = 500, {"error": "Empreendimento Expirado - 5955"}
        elif self.path.startswith("/me"):
            if "sid=abc" not in (self.headers.get("Cookie") or "") or self.headers.get("Authorization") != "Bearer tok":
                status, body = 401, {"error": "login"}
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def req(path="/ok", **kw):
    return {"name": kw.pop("name", "obra"), "request": dict({"path": path}, **kw)}


class Base(ProveCase):
    def setUp(self):
        super().setUp()
        self.app = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        threading.Thread(target=self.app.serve_forever, daemon=True).start()
        self.addCleanup(self.app.server_close)
        self.addCleanup(self.app.shutdown)
        self.port = self.app.server_address[1]

    def configure(self, *preflight):
        self.set_ws(self.port, preflight=list(preflight))
        return core.workspace_load("ws")

    def failed(self, *preflight):
        return envmod.request_preflights(self.configure(*preflight), None, "Web")


class Evaluate(unittest.TestCase):
    def test_status_body_and_json(self):
        p = envmod.response_problem
        self.assertIsNone(p({}, 200, ""))
        self.assertIn("expected 200", p({}, 500, ""))
        self.assertIn("no answer", p({}, None, ""))
        self.assertIsNone(p({"expect_status": [200, 204]}, 204, ""))
        self.assertIn("does not contain", p({"expect_body_contains": "ativo"}, 200, "obra off"))
        self.assertIsNone(p({"expect_body_contains": "ativo"}, 200, "obra ativo"))
        body = json.dumps({"a": {"b": [{"c": 1}]}})
        self.assertIsNone(p({"expect_json": {"a.b.0.c": 1}}, 200, body))
        self.assertIn("is 1, expected 2", p({"expect_json": {"a.b.0.c": 2}}, 200, body))
        self.assertIn("absent", p({"expect_json": {"a.x": 1}}, 200, body))
        self.assertIn("not JSON", p({"expect_json": {"a": 1}}, 200, "<html>"))


class Requests(Base):
    def test_passing_request(self):
        self.assertEqual(self.failed(req("/ok", expect_json={"obra.active": True})), [])

    def test_failing_status_names_the_preflight(self):
        (name, why), = self.failed(req("/expired", name="obra-ativa"))
        self.assertEqual(name, "obra-ativa")
        self.assertIn("status 500, expected 200", why)
        self.assertIn(f"127.0.0.1:{self.port}/expired", why)

    def test_expect_status_can_accept_the_error(self):
        self.assertEqual(self.failed(req("/expired", expect_status=500, expect_body_contains="Expirado")), [])

    def test_no_answer_is_reported(self):
        self.set_ws(self.closed, preflight=[req("/ok")])
        (name, why), = envmod.request_preflights(core.workspace_load("ws"), None, "Web")
        self.assertIn("no answer", why)

    def test_cmd_entries_are_ignored_by_the_request_runner(self):
        self.assertEqual(self.failed({"name": "c", "cmd": [sys.executable, "-c", "pass"]}), [])

    def test_storage_session_is_sent(self):
        state = {"cookies": [{"name": "sid", "value": "abc", "domain": "127.0.0.1", "path": "/", "expires": -1},
                             {"name": "gone", "value": "x", "domain": "127.0.0.1", "path": "/", "expires": 5},
                             {"name": "other", "value": "y", "domain": "elsewhere.test", "path": "/", "expires": -1}],
                 "origins": [{"origin": f"http://127.0.0.1:{self.port}", "localStorage": [{"name": "token", "value": "tok"}]}]}
        self.configure(req("/me", name="session", auth="storage", bearer_local_storage="token"))
        (name, why), = envmod.request_preflights(core.workspace_load("ws"), None, "Web")
        self.assertIn("no stored login session", why)  # no login yet
        path = envmod.storage_state_path("ws", "Web")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(envmod.request_preflights(core.workspace_load("ws"), None, "Web"), [])
        headers, err = envmod.storage_headers("ws", "Web", 0, f"http://127.0.0.1:{self.port}/me", None, "token")
        self.assertEqual(headers["Cookie"], "sid=abc")  # not the expired cookie, not the other domain's
        headers, err = envmod.storage_headers("ws", "Web", 0, f"http://127.0.0.1:{self.port}/me", None, "missing")
        self.assertIn("not found", err)


class EnvCheck(Base):
    def test_env_check_prints_env_fail_kind_data(self):
        self.configure(req("/ok"), req("/expired", name="obra-ativa"))
        rc, out, err = cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 1, out)
        self.assertIn("ENV-FAIL repo=Web kind=data detail=obra-ativa: ", out)
        self.assertNotIn("detail=obra:", out)

    def test_env_check_passes_when_data_is_fine(self):
        self.configure(req("/ok"))
        rc, out, err = cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("ENV-FAIL", out)


class Prove(Base):
    def run_prove(self, *preflight):
        self.configure(*preflight)
        self.acs([{"id": "A1", "type": "api", "repo": "Web", "path": "/ok", "expect": {"status": 200}},
                  {"id": "A2", "type": "api", "repo": "Web", "path": "/ok", "expect": {"status": 200}}])
        with mock.patch.object(prove, "dispatch", return_value={"status": "pass", "summary": "ok"}) as d:
            rc, out, err = cli("prove", "r1")
        return rc, out, d.call_count

    def test_failing_data_preflight_is_an_env_failure_and_skips_the_checks(self):
        rc, out, calls = self.run_prove(req("/expired", name="obra-ativa"))
        self.assertEqual(rc, 3, out)
        self.assertIn("ENV-FAIL repo=Web kind=data detail=obra-ativa", out)
        self.assertEqual(calls, 0)
        self.assertEqual(self.evidence("A1")["details"]["skipped"], "env")

    def test_passing_preflight_runs_the_checks(self):
        rc, out, calls = self.run_prove(req("/ok"))
        self.assertEqual((rc, calls), (0, 2), out)

    def test_an_env_that_is_down_is_not_reported_as_data(self):
        self.set_ws(self.closed, preflight=[req("/ok")])
        self.acs([{"id": "A1", "type": "api", "repo": "Web", "path": "/ok", "expect": {"status": 200}}])
        with mock.patch.object(prove, "dispatch", return_value={"status": "pass", "summary": "ok"}):
            rc, out, err = cli("prove", "r1")
        self.assertNotIn("kind=data", out)


class Validation(unittest.TestCase):
    def errors(self, pre):
        ws = {"worktree_root": "x", "repos": [{"alias": "A", "path": "x", "quality": {"a": "b"},
                                               "env": {"start": "s", "health": "h", "base_url": "b", "preflight": pre}}]}
        return [e for e in workspace.check_workspace(ws) if "preflight" in e]

    def test_valid_forms(self):
        self.assertEqual(self.errors([{"name": "c", "cmd": ["node", "x.js"]}]), [])
        self.assertEqual(self.errors([req("/api/obras/1", method="GET", expect_status=[200, 204], expect_body_contains="ok",
                                          expect_json={"a.b": 1}, auth="storage", user="a@b.c", bearer_local_storage="tok",
                                          headers={"X": "1"}, timeout_s=5)]), [])
        self.assertEqual(self.errors([{"name": "u", "request": {"url": "https://x.test/health"}}]), [])

    def test_invalid_forms(self):
        for bad in ([{"name": "x"}],                                    # neither cmd nor request
                    [{"name": "x", "cmd": ["a"], "request": {"path": "/"}}],  # both
                    [{"name": "x", "request": "GET /"}],
                    [{"name": "x", "request": {}}],                    # no path/url
                    [{"name": "x", "request": {"path": "api"}}],
                    [{"name": "x", "request": {"path": "/", "url": "http://a"}}],
                    [{"name": "x", "request": {"path": "/", "expect_status": "200"}}],
                    [{"name": "x", "request": {"path": "/", "expect_json": {}}}],
                    [{"name": "x", "request": {"path": "/", "auth": "basic"}}],
                    [{"name": "x", "request": {"path": "/", "user": "a@b.c"}}],  # user needs auth
                    [{"name": "x", "request": {"path": "/", "bogus": 1}}],
                    [{"name": "x", "request": {"path": "/", "timeout_s": 0}}],
                    [{"name": "", "request": {"path": "/"}}],
                    {"name": "x"}):
            self.assertTrue(self.errors(bad), bad)


if __name__ == "__main__":
    unittest.main()
