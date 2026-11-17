from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

from core_support import PY, FactoryCase, cli, commit_file, core, sh

from factory.collectors import api, red_green


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self._send(201, {"id": 7, "name": body.get("name"), "items": [{"k": "v"}]})

    def do_GET(self):
        self._send(404, {"error": "nope"})


class ApiCollectorTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.ctx = core.Ctx(run={}, workspace={}, run_dir=self.tmp, env={"base_url": f"http://127.0.0.1:{self.server.server_port}"},
                            out_dir=self.tmp / "out")

    def test_pass(self):
        check = {"id": "CHK-5", "type": "api", "method": "POST", "path": "/v2/campos", "body": {"name": "x"},
                 "expect": {"status": 201, "json": {"$.id": "exists", "$.name": "x", "$.items[0].k": "v", "$.zip": "absent"}}}
        res = api.collect(check, self.ctx)
        self.assertEqual(res["status"], "pass", res)
        self.assertTrue((self.tmp / "out" / "response.txt").is_file())

    def test_fail_on_status_and_json(self):
        check = {"id": "CHK-5", "type": "api", "method": "POST", "path": "/p", "body": {"name": "x"},
                 "expect": {"status": 200, "json": {"$.id": 8, "$.missing": "exists"}}}
        res = api.collect(check, self.ctx)
        self.assertEqual(res["status"], "fail")
        for text in ("status 201 != 200", "$.id: 7 != 8", "$.missing: missing"):
            self.assertIn(text, res["summary"])

    def test_env_down_is_error(self):
        ctx = core.Ctx(run={}, workspace={}, run_dir=self.tmp, env={"base_url": "http://127.0.0.1:1"})
        res = api.collect({"id": "C", "type": "api", "path": "/"}, ctx)
        self.assertEqual(res["status"], "error")

    def test_prove_dispatches_api(self):
        self.make_run()
        ws = core.workspace_load("ws")
        ws["repos"][0]["env"]["base_url"] = self.ctx.env["base_url"]
        core.write_json(core.workspace_path("ws"), ws)
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "api", "checks": ["CHK-5"]}],
                       [{"id": "CHK-5", "type": "api", "repo": "React", "method": "POST", "path": "/x", "body": {},
                         "expect": {"status": 201, "json": {"$.id": "exists"}}}])
        commit_file(self.wt, "a.txt", "a")
        rc, out = cli("prove", self.run_id)
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.load_evidence(self.run_id, "CHK-5")["type"], "api")


class RedGreenTests(FactoryCase):
    def build(self, with_fix=True):
        self.make_run(bug=True)
        commit_file(self.wt, "mod.py", "v = 1\n")
        script = ("import sys\nsys.path.insert(0, '.')\nimport mod\n"
                  "if mod.v != 2:\n    print('ASSERT mod.v == 2 failed')\n    sys.exit(1)\n")
        # fix commit: changes code and adds the test
        (self.wt / "tests").mkdir(exist_ok=True)
        (self.wt / "tests" / "check_mod.py").write_text(script, encoding="utf-8")
        (self.wt / "mod.py").write_text("v = 2\n" if with_fix else "v = 1\n", encoding="utf-8")
        sh("git", "add", "-A", cwd=self.wt)
        sh("git", "commit", "-q", "-m", "[A] - fix", cwd=self.wt)
        self.check = {"id": "CHK-6", "type": "red-green", "repo": "React",
                      "cmd": f"{PY} tests/check_mod.py", "fix_commit": "HEAD",
                      "expect_fail_pattern": "ASSERT mod.v"}

    def ctx(self):
        return core.Ctx(run={}, workspace={}, run_dir=self.tmp, repo_path=self.wt, out_dir=self.tmp / "o")

    def test_red_then_green_passes(self):
        self.build()
        res = red_green.collect(self.check, self.ctx())
        self.assertEqual(res["status"], "pass", res)
        self.assertNotEqual(res["details"]["red_exit"], 0)
        self.assertEqual(res["details"]["green_exit"], 0)
        # temp worktrees cleaned up
        self.assertEqual(sh("git", "worktree", "list", cwd=self.wt).count("factory-wt"), 0)

    def test_test_that_cannot_fail_is_rejected(self):
        self.build(with_fix=False)
        res = red_green.collect(dict(self.check, cmd=f'{PY} -c "pass"'), self.ctx())
        self.assertEqual(res["status"], "fail")
        self.assertIn("cannot fail", res["summary"])

    def test_still_red_with_fix_fails(self):
        self.build(with_fix=False)
        res = red_green.collect(self.check, self.ctx())
        self.assertEqual(res["status"], "fail")
        self.assertIn("still fails", res["summary"])


class RedGreenNoTestsTests(FactoryCase):
    def test_zero_tests_matched_is_a_setup_failure(self):
        self.make_run(bug=True)
        commit_file(self.wt, "mod.py", "v = 1\n")
        (self.wt / "tests").mkdir(exist_ok=True)
        (self.wt / "tests" / "noisy.py").write_text(
            "import sys\nsys.path.insert(0, '.')\nimport mod\nprint('ASSERT mod.v No tests found')\n"
            "sys.exit(0 if mod.v == 2 else 1)\n", encoding="utf-8")
        (self.wt / "mod.py").write_text("v = 2\n", encoding="utf-8")
        sh("git", "add", "-A", cwd=self.wt)
        sh("git", "commit", "-q", "-m", "[A] - fix", cwd=self.wt)
        ctx = core.Ctx(run={}, workspace={}, run_dir=self.tmp, repo_path=self.wt, out_dir=self.tmp / "o")
        res = red_green.collect({"id": "CHK-6", "type": "red-green", "repo": "React", "cmd": f"{PY} tests/noisy.py",
                                 "fix_commit": "HEAD", "expect_fail_pattern": "ASSERT mod.v"}, ctx)
        self.assertEqual(res["status"], "fail", res)
        self.assertEqual(res["details"]["kind"], "setup")
        self.assertIn("no tests matched", res["summary"])


class ZeroTestsMarkerTests(unittest.TestCase):
    def marker(self, text):
        from factory.collectors.test import no_tests_marker
        return no_tests_marker(text)

    def test_go_no_tests_to_run(self):
        self.assertTrue(self.marker("testing: warning: no tests to run\nPASS\nok  \tpkg\t0.003s [no tests to run]\n"))
        self.assertTrue(self.marker("ok  \tpkg\t0.003s [no tests to run]\n"))
        self.assertIsNone(self.marker("ok  \tpkg\t0.4s\nok  \tother\t0.003s [no tests to run]\n"))

    def test_cargo_running_zero_tests(self):
        self.assertTrue(self.marker("running 0 tests\n\ntest result: ok. 0 passed; 0 failed\n"))
        self.assertIsNone(self.marker("running 0 tests\n\nrunning 3 tests\ntest a ... ok\n"))

    def test_mocha_zero_passing(self):
        self.assertTrue(self.marker("\n  0 passing (2ms)\n"))
        self.assertIsNone(self.marker("  0 passing (2ms)\n  2 failing\n"))
        self.assertIsNone(self.marker("  5 passing (2ms)\n"))

    def test_dotnet_parsed_totals(self):
        self.assertTrue(self.marker("Total tests: 0\n"))
        self.assertTrue(self.marker("Passed!  - Failed:     0, Passed:     0, Skipped:     0, Total:     0\n"))
        self.assertIsNone(self.marker("Passed!  - Failed:     0, Passed:     4, Skipped:     0, Total:     4\n"))

    def test_multi_project_dotnet_one_empty_one_passed_is_a_real_run(self):
        out = ("No test matches the given testcase filter `Name~X` in A.dll\n"
               "Passed!  - Failed:     0, Passed:     2, Skipped:     0, Total:     2, Duration: 1 s - B.dll\n")
        self.assertIsNone(self.marker(out))
        self.assertTrue(self.marker("No test matches the given testcase filter `Name~X` in A.dll\n"))
        self.assertTrue(self.marker("No test matches the given testcase filter in A.dll\n"
                                    "Passed!  - Failed:     0, Passed:     0, Skipped:     0, Total:     0\n"))

    def test_collect_fails_zero_run_and_passes_multi_project(self):
        from factory.collectors import test as tc
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        ctx = core.Ctx(run={}, workspace={}, run_dir=tmp, repo_path=tmp, out_dir=tmp / "o")
        res = tc.collect({"id": "C1", "type": "test", "repo": "R", "cmd": f"{PY} -c \"print('running 0 tests')\""}, ctx)
        self.assertEqual(res["status"], "fail")
        self.assertIn("no tests matched", res["summary"])
        text = "No test matches the given testcase filter in A.dll; Passed!  - Failed: 0, Passed: 2, Skipped: 0, Total: 2"
        res = tc.collect({"id": "C2", "type": "test", "repo": "R", "cmd": f"{PY} -c \"print('{text}')\""}, ctx)
        self.assertEqual(res["status"], "pass", res)


class TestCollectorTests(FactoryCase):
    def test_expect_variants(self):
        from factory.collectors import test as tc
        ctx = core.Ctx(run={}, workspace={}, run_dir=self.tmp, repo_path=self.tmp, out_dir=self.tmp / "o")
        ok = tc.collect({"id": "T", "type": "test", "cmd": f'{PY} -c "pass"'}, ctx)
        self.assertEqual(ok["status"], "pass")
        bad = tc.collect({"id": "T", "type": "test", "cmd": f'{PY} -c "import sys; sys.exit(2)"'}, ctx)
        self.assertEqual(bad["status"], "fail")
        self.assertEqual(bad["details"]["exit_code"], 2)
        exp = tc.collect({"id": "T", "type": "test", "cmd": f'{PY} -c "import sys; sys.exit(2)"', "expect": "exit2"}, ctx)
        self.assertEqual(exp["status"], "pass")
        slow = tc.collect({"id": "T", "type": "test", "cmd": f'{PY} -c "import time; time.sleep(5)"', "timeout_s": 1}, ctx)
        self.assertEqual(slow["status"], "fail")
        self.assertEqual(slow["details"]["exit_code"], 124)

    def test_missing_collector_is_error_evidence(self):
        self.make_run()
        self.write_acs([{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["CHK-1"]}],
                       [{"id": "CHK-1", "type": "browser", "repo": "React", "spec": "x"}])
        import sys
        from unittest import mock
        with mock.patch.dict(sys.modules, {"factory.collectors.browser": None}):
            rc, out = cli("prove", self.run_id)
        self.assertEqual(rc, 1)
        self.assertEqual(core.load_evidence(self.run_id, "CHK-1")["status"], "error")


if __name__ == "__main__":
    unittest.main()
