"""Fail-fast prove: wall-clock caps, env-failure skipping, machine-readable lines, exit codes, run.json merge."""

import contextlib
import http.server
import io
import json
import os
import socket
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.collectors import api as api_collector  # noqa: E402
from factory.collectors import browser  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import prove  # noqa: E402


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"nope"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ProveCase(FactoryCase):
    def setUp(self):
        super().setUp()
        self.web = make_repo(self.tmp / "web").resolve()
        self.closed = free_port()  # nothing listens here
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.live = self.server.server_address[1]
        self.set_ws(self.closed)
        self.make_run("r1", phase="prove", repos={"Web": {"worktree": str(self.web), "branch": "b"}})
        p = mock.patch.object(prove, "env_binding", return_value=None)  # no real env process in these tests
        p.start()
        self.addCleanup(p.stop)

    def set_ws(self, port, **env_over):
        env = {"start": ["x"], "cwd": ".", "health": f"http://127.0.0.1:{port}/", "base_url": f"http://127.0.0.1:{port}"}
        env.update(env_over)
        self.ws = {"schemaVersion": 1, "name": "ws", "repos": [{"alias": "Web", "name": "web", "path": str(self.web), "env": env}]}
        self.write("workspaces/ws/workspace.json", self.ws)

    def acs(self, checks):
        self.write("runs/r1/acs.json", {"schemaVersion": 1, "acs": [], "checks": checks})

    def api_check(self, cid, path="/x", status=200):
        return {"id": cid, "type": "api", "repo": "Web", "path": path, "expect": {"status": status}}

    def evidence(self, cid):
        return json.loads((self.home / "runs" / "r1" / "evidence" / f"{cid}.json").read_text(encoding="utf-8"))


class EnvFailureTests(ProveCase):
    def test_env_failure_skips_remaining_runtime_checks_and_exits_3(self):
        self.acs([self.api_check("A1"), self.api_check("A2"), self.api_check("A3")])
        with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 3, out)
        self.assertEqual(spy.call_count, 1)  # only the first check reached the network
        self.assertIn("PROVE-FAIL check=A1 kind=env detail=env not reachable", out)
        self.assertIn("ENV-FAIL repo=Web kind=unhealthy detail=env not reachable", out)
        for cid in ("A2", "A3"):
            rec = self.evidence(cid)
            self.assertTrue(rec["summary"].startswith("skipped: env"), rec["summary"])
            self.assertEqual(rec["details"]["skipped"], "env")
            self.assertIn(f"PROVE-FAIL check={cid} kind=env", out)
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("ENV-FAIL")), 1)

    def test_stale_binding_is_an_env_failure(self):
        prove.env_binding.return_value = "env Web: started at abcd1234, worktree HEAD is ffff0000; `factory env down`"
        self.acs([self.api_check("A1"), self.api_check("A2")])
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 3, out)
        self.assertIn("ENV-FAIL repo=Web kind=stale", out)
        self.assertTrue(self.evidence("A2")["summary"].startswith("skipped: env"))

    def test_reporter_json_mode_writes_stderr(self):
        rep = prove.Reporter(json_mode=True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rep.prove_fail("A1", "env", "boom\nsecond line")
        self.assertEqual(buf.getvalue().strip(), "PROVE-FAIL check=A1 kind=env detail=boom")
        self.assertTrue(rep.env_failed)


class RepeatTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)

    def test_two_identical_failures_stop_the_batch(self):
        self.acs([self.api_check("A1", status=201), self.api_check("A2", status=201), self.api_check("A3", status=201)])
        with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 1, out)
        self.assertEqual(spy.call_count, 2)
        self.assertIn("PROVE-FAIL check=A1 kind=assert detail=status 200 != 201", out)
        self.assertTrue(self.evidence("A3")["summary"].startswith("skipped: batch-stopped"))

    def test_different_failures_do_not_stop(self):
        self.acs([self.api_check("A1", status=201), self.api_check("A2", status=202), self.api_check("A3", status=200)])
        with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 1, out)
        self.assertEqual(spy.call_count, 3)
        self.assertEqual(self.evidence("A3")["status"], "pass")

    def test_all_pass_exits_0_and_unknown_check_is_usage(self):
        self.acs([self.api_check("A1")])
        self.assertEqual(cli("prove", "r1")[0], 0)
        self.assertEqual(cli("prove", "r1", "--check", "NOPE")[0], 2)


FAKE_HANG = textwrap.dedent("""
    import json, subprocess, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    out = Path(args[args.index("--out") + 1]); out.mkdir(parents=True, exist_ok=True)
    (out / f"argv-{args[0]}.json").write_text(json.dumps(args))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (out / "child.pid").write_text(str(child.pid))
    time.sleep(120)
""")


class CapTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)
        fake = self.tmp / "hang_cli.py"
        fake.write_text(FAKE_HANG, encoding="utf-8")
        p = mock.patch.dict(os.environ, {"FACTORY_NODE_CLI": str(fake)})
        p.start()
        self.addCleanup(p.stop)
        (self.home / "runs" / "r1" / "checks").mkdir(parents=True, exist_ok=True)
        (self.home / "runs" / "r1" / "checks" / "a.spec.ts").write_text("x", encoding="utf-8")

    def browser_check(self, cid, **extra):
        return {"id": cid, "type": "browser", "repo": "Web", "spec": "checks/a.spec.ts", **extra}

    def test_cap_kills_a_hanging_check_and_its_children(self):
        self.acs([self.browser_check("B1", timeout_s=2), self.browser_check("B2", timeout_s=2)])
        t0 = time.monotonic()
        rc, out, _ = cli("prove", "r1")
        self.assertLess(time.monotonic() - t0, 2 + 5)
        self.assertEqual(rc, 3, out)  # a timeout on the first runtime check is an env failure
        self.assertIn("PROVE-FAIL check=B1 kind=timeout", out)
        self.assertIn("ENV-FAIL repo=Web kind=timeout", out)
        self.assertTrue(self.evidence("B2")["summary"].startswith("skipped: env"))
        out_dir = self.home / "runs" / "r1" / "evidence" / "B1"
        argv = json.loads((out_dir / "argv-run-spec.json").read_text())
        self.assertLessEqual(int(argv[argv.index("--timeout-ms") + 1]), 2000)
        self.assertLessEqual(int(argv[argv.index("--goto-timeout-ms") + 1]), 30000)
        pid = int((out_dir / "child.pid").read_text())
        for _ in range(40):
            if not envmod.pid_alive(pid):
                break
            time.sleep(0.25)
        self.assertFalse(envmod.pid_alive(pid), "grandchild of the hung CLI survived the cap")

    def test_cap_resolution(self):
        ctx = core.Ctx(run={}, workspace={"limits": {"check_timeout_s": 45}}, run_dir=self.home)
        plain = core.Ctx(run={}, workspace={}, run_dir=self.home)
        self.assertEqual(browser.check_cap({"type": "browser"}, plain), 120)
        for t in ("visual", "style", "screenshot"):
            self.assertEqual(browser.check_cap({"type": t}, plain), 120)
        self.assertEqual(browser.check_cap({"type": "api"}, plain), 60)
        self.assertEqual(browser.check_cap({"type": "browser"}, ctx), 45)
        self.assertEqual(browser.check_cap({"type": "browser", "timeout_s": 9}, ctx), 9)
        self.assertEqual(browser.check_cap({"type": "browser", "timeout_s": "x"}, plain), 120)

    def test_api_cap_is_wall_clock(self):
        gate = threading.Event()

        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                gate.wait(30)

            def log_message(self, *a):
                pass

        slow = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=slow.serve_forever, daemon=True).start()
        self.addCleanup(slow.server_close)
        self.addCleanup(slow.shutdown)
        self.addCleanup(gate.set)
        ctx = core.Ctx(run={}, workspace={}, run_dir=self.home, env={"base_url": f"http://127.0.0.1:{slow.server_address[1]}"})
        t0 = time.monotonic()
        res = api_collector.collect({"id": "A", "type": "api", "path": "/", "timeout_s": 1}, ctx)
        self.assertLess(time.monotonic() - t0, 6)
        self.assertEqual(res["status"], "error")
        self.assertTrue(res["details"]["timeout"])


class StaleRestartTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)

    def test_stale_env_is_restarted_once_before_runtime_checks(self):
        self.acs([self.api_check("A1"), self.api_check("A2")])
        with mock.patch.object(envmod, "stale_reason", return_value="started at aaaa, worktree HEAD is bbbb"), \
                mock.patch.object(envmod, "can_restart", return_value=True), \
                mock.patch.object(envmod, "restart_env", return_value=(True, ["Web: stopped", "Web: up"])) as restart:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(restart.call_count, 1)
        self.assertEqual(sum(1 for line in out.splitlines() if "auto-restarting" in line), 1)

    def test_no_restart_without_the_lease(self):
        self.acs([self.api_check("A1")])
        with mock.patch.object(envmod, "stale_reason", return_value="started at aaaa, worktree HEAD is bbbb"), \
                mock.patch.object(envmod, "can_restart", return_value=False), \
                mock.patch.object(envmod, "restart_env") as restart:
            cli("prove", "r1")
        restart.assert_not_called()

    def test_failed_restart_is_env_failure(self):
        self.acs([self.api_check("A1")])
        with mock.patch.object(envmod, "stale_reason", return_value="serves other"), \
                mock.patch.object(envmod, "can_restart", return_value=True), \
                mock.patch.object(envmod, "restart_env", return_value=(False, ["Web: port 1 is already in use"])):
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 3, out)
        self.assertIn("ENV-FAIL repo=Web kind=start detail=restart failed", out)
        self.assertTrue(self.evidence("A1")["summary"].startswith("skipped: env"))


class LeaseAndMergeTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)
        self.ws["env"] = {"lease": True, "lease_ttl_s": 900}
        self.write("workspaces/ws/workspace.json", self.ws)

    def test_heartbeat_before_every_check(self):
        self.assertTrue(envmod.lease_try("ws", "r1", 900)["granted"])
        self.acs([self.api_check("A1"), self.api_check("A2"), self.api_check("A3")])
        ages = []
        real = api_collector.collect

        def spy(check, ctx):
            lease = envmod.lease_read("ws", "Web")
            ages.append(envmod._age(lease["heartbeat"]))
            lease["heartbeat"] = "2000-01-01T00:00:00+00:00"  # the next check's pre-heartbeat must refresh it
            self.write("env/ws/Web/lease.json", lease)
            return real(check, ctx)

        with mock.patch.object(api_collector, "collect", side_effect=spy):
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(ages), 3)
        self.assertTrue(all(a < 60 for a in ages), ages)

    def test_concurrent_run_human_survives_the_proof(self):
        self.acs([self.api_check("A1"), self.api_check("A2")])
        real = api_collector.collect
        calls = []

        def concurrent(check, ctx):
            if not calls:  # as if another process ran `factory run human/decide` mid-proof
                core.run_update("r1", lambda r: r["decisions"].append({"text": "human says hi", "by": "user"}))
            calls.append(check["id"])
            return real(check, ctx)

        with mock.patch.object(api_collector, "collect", side_effect=concurrent):
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        run = core.run_load("r1")
        self.assertEqual([d["text"] for d in run["decisions"]], ["human says hi"])
        self.assertEqual({e["data"]["check"] for e in run["events"] if e["kind"] == "evidence"}, {"A1", "A2"})


class QualityRuleIdTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)
        self.ws["repos"][0]["quality"] = {"lint": "echo ok"}
        self.write("workspaces/ws/workspace.json", self.ws)
        self.acs([self.api_check("A1")])

    def fake_record(self, cid):
        return {"check_id": cid, "type": "quality", "status": "pass", "summary": "ok", "git_sha": "x", "dirty": False}

    def test_check_accepts_quality_id(self):
        with mock.patch.object(prove, "quality_delta", return_value=self.fake_record("QUALITY-Web")) as q:
            rc, out, _ = cli("prove", "r1", "--check", "QUALITY-Web")
        self.assertEqual(rc, 0, out)
        q.assert_called_once()
        self.assertEqual(q.call_args[0][2], "Web")
        self.assertIn("QUALITY-Web", out)

    def test_check_accepts_rule_id(self):
        rule = {"id": "RULE-lint-Web", "type": "test", "repo": "Web", "cmd": "echo"}
        rec = dict(self.fake_record("RULE-lint-Web"), type="test")
        with mock.patch.object(prove.gate, "rule_checks", return_value=[rule]), \
                mock.patch.object(prove, "collect_one", return_value=rec) as c:
            rc, out, _ = cli("prove", "r1", "--check", "RULE-lint-Web")
        self.assertEqual(rc, 0, out)
        self.assertEqual(c.call_args[0][2]["id"], "RULE-lint-Web")

    def test_unknown_quality_or_rule_is_usage(self):
        self.assertEqual(cli("prove", "r1", "--check", "QUALITY-Nope")[0], 2)
        with mock.patch.object(prove.gate, "rule_checks", return_value=[]):
            self.assertEqual(cli("prove", "r1", "--check", "RULE-x-Web")[0], 2)


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        c = prove.classify
        self.assertIsNone(c({"status": "pass"}))
        self.assertEqual(c({"status": "fail", "type": "browser", "summary": "expect(x).toBe(1)"}), ("assert", None))
        self.assertEqual(c({"status": "error", "type": "browser", "summary": "spec not found"}), ("setup", None))
        self.assertEqual(c({"status": "error", "type": "browser", "summary": "net::ERR_CONNECTION_REFUSED at x"})[0], "env")
        self.assertEqual(c({"status": "error", "type": "browser", "summary": "browser CLI timed out after 120s"}),
                         ("timeout", None))
        self.assertEqual(c({"status": "error", "type": "browser", "summary": "browser CLI timed out after 120s"},
                           first_runtime=True), ("timeout", "timeout"))
        self.assertEqual(c({"status": "fail", "type": "test", "summary": "storage state missing"}), ("assert", None))


if __name__ == "__main__":
    unittest.main()
