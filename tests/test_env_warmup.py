"""`env.warmup`: after a cold `env up` the listed paths are requested once, logged, and health is checked again."""

from __future__ import annotations

import socket
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo  # noqa: E402

from factory.commands import env as envmod  # noqa: E402
from factory.commands import workspace  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fake:
    """A stand-in for the Popen object `warm_up` writes its log lines through."""

    def __init__(self):
        self.lines: list[bytes] = []
        self.factory_log = types.SimpleNamespace(write=self.lines.append, close=lambda: None)

    @property
    def text(self) -> str:
        return b"".join(self.lines).decode()


class Warmup(FactoryCase):
    def setUp(self):
        super().setUp()
        self.web = make_repo(self.tmp / "web").resolve()
        self.port = free_port()

    def workspace(self, **env_over):
        env = {"start": [sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1"], "cwd": ".",
               "ports": [self.port], "health": f"http://127.0.0.1:{self.port}/", "startup_timeout_s": 20}
        env.update(env_over)
        ws = {"schemaVersion": 1, "name": "ws", "repos": [{"alias": "Web", "name": "web", "path": str(self.web), "env": env}]}
        self.write("workspaces/ws/workspace.json", ws)
        self.addCleanup(lambda: envmod.down(ws, "Web"))
        return ws

    def log(self) -> str:
        return envmod.log_path("ws", "Web").read_text(encoding="utf-8", errors="replace")

    def test_cold_start_requests_each_path_once_and_logs_it(self):
        ws = self.workspace(warmup=["/", "/a.js", f"http://127.0.0.1:{self.port}/b.js"])
        ok, out = envmod.up(ws, "Web")
        self.assertTrue(ok, out)
        text = self.log()
        base = f"http://127.0.0.1:{self.port}"
        self.assertIn(f"[factory] warmup GET {base}/ -> 200", text)
        self.assertIn(f"[factory] warmup GET {base}/a.js -> 404", text)  # the status is ignored
        self.assertIn(f"[factory] warmup GET {base}/b.js -> 404", text)
        self.assertIn('"GET /a.js HTTP/1.1" 404', text)  # the server's own output was not overwritten by ours
        self.assertTrue(any("warm-up requested 3 path(s)" in line for line in out), out)

    def test_an_env_that_is_already_up_is_not_warmed_again(self):
        ws = self.workspace(warmup=["/a.js"])
        self.assertTrue(envmod.up(ws, "Web")[0])
        ok, out = envmod.up(ws, "Web")
        self.assertTrue(ok, out)
        self.assertEqual(self.log().count("[factory] warmup GET"), 1)

    def test_without_the_key_nothing_is_requested_or_kept(self):
        ws = self.workspace()
        ok, out = envmod.up(ws, "Web")
        self.assertTrue(ok, out)
        self.assertNotIn("[factory] warmup", self.log())
        self.assertFalse(any("warm-up" in line for line in out))

    def test_requests_are_bounded(self):
        stall = socket.socket()
        stall.bind(("127.0.0.1", 0))
        stall.listen(5)  # accepts connections in the kernel backlog and never answers
        self.addCleanup(stall.close)
        proc = Fake()
        origin = f"http://127.0.0.1:{stall.getsockname()[1]}"
        env = {"warmup": [f"{origin}/slow"] * 2}
        ws = {"name": "ws", "repos": [{"alias": "Web", "env": {"base_url": origin}}]}
        with mock.patch.object(envmod, "WARMUP_TIMEOUT_S", 0.3):
            done = envmod.warm_up(ws, env, "Web", 0, proc, [])
        self.assertEqual(done, 2)
        self.assertEqual(proc.text.count("no answer"), 2)

    def test_a_url_of_another_origin_is_not_requested(self):
        proc = Fake()
        ws = {"name": "ws", "repos": [{"alias": "Web", "env": {"base_url": "http://127.0.0.1:3000"}}]}
        env = {"warmup": ["/a", "http://203.0.113.9/x", "http://127.0.0.1:3000/b"]}
        with mock.patch.object(envmod, "http_fetch", return_value=(200, "", None)) as fetch:
            done = envmod.warm_up(ws, env, "Web", 0, proc, [])
        self.assertEqual(done, 2)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ["http://127.0.0.1:3000/a", "http://127.0.0.1:3000/b"])
        self.assertIn("warmup skipped", proc.text)

    def test_total_budget_stops_the_warmup(self):
        proc = Fake()
        ws = {"name": "ws", "repos": [{"alias": "Web", "env": {"base_url": "http://127.0.0.1:9"}}]}
        with mock.patch.object(envmod, "WARMUP_BUDGET_S", 0.0):
            done = envmod.warm_up(ws, {"warmup": ["/a"]}, "Web", 0, proc, [])
        self.assertEqual(done, 0)
        self.assertIn("budget used", proc.text)

    def test_health_is_checked_again_after_the_warmup(self):
        proc = Fake()
        proc.pid, proc.poll = 1, lambda: None
        ws = {"name": "ws", "repos": [{"alias": "Web", "env": {"base_url": "http://127.0.0.1:9", "warmup": ["/a"]}}]}
        out: list[str] = []
        with mock.patch.object(envmod, "health_ok", side_effect=[True, False]) as health, \
                mock.patch.object(envmod, "http_fetch", return_value=(504, "", None)):
            ok = envmod._wait_healthy(ws, ws["repos"][0]["env"], "Web", 0, "http://x/", proc, Path("nolog"), out)
        self.assertFalse(ok)
        self.assertEqual(health.call_count, 2)
        self.assertTrue(any("ENV-FAIL repo=Web kind=unhealthy" in line for line in out), out)


class Validation(unittest.TestCase):
    def errors(self, warmup):
        ws = {"worktree_root": "x", "repos": [{"alias": "A", "path": "x", "quality": {"a": "b"},
                                               "env": {"start": "s", "health": "h", "base_url": "b", "warmup": warmup}}]}
        return [e for e in workspace.check_workspace(ws) if "warmup" in e]

    def test_workspace_check(self):
        self.assertEqual(self.errors(["/", "/app", "http://localhost:3300/x"]), [])
        self.assertTrue(self.errors("/"))
        self.assertTrue(self.errors(["app"]))
        self.assertTrue(self.errors([3]))
        self.assertTrue(self.errors([""]))


if __name__ == "__main__":
    unittest.main()
