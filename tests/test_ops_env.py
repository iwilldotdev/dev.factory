import os
import socket
import sys
import time
import unittest

from ops_helpers import FactoryCase

from factory.commands import env as envmod


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ws_with(repo_path, port, **env_over):
    env = {
        "start": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        "cwd": ".", "health": f"http://127.0.0.1:{port}/", "ports": [port],
        "base_url": f"http://127.0.0.1:{port}", "startup_timeout_s": 30,
    }
    env.update(env_over)
    return {"schemaVersion": 1, "name": "ws", "repos": [{"alias": "Web", "name": "web", "path": str(repo_path), "env": env}]}


class CapabilityTests(FactoryCase):
    def test_errors(self):
        ws = {"repos": [{"alias": "A", "path": "x"}, {"alias": "B", "path": "x", "env": {"depends_on": ["Nope"]}}]}
        self.assertEqual(envmod.capability(ws, "A"), ["A: no env block in workspace"])
        errs = envmod.capability(ws, "B")
        self.assertTrue(any("start" in e for e in errs))
        self.assertTrue(any("health" in e for e in errs))
        self.assertTrue(any("Nope" in e for e in errs))

    def test_ok(self):
        self.assertEqual(envmod.capability(ws_with(self.tmp, 1234), "Web"), [])

    def test_base_url_fallback(self):
        ws = ws_with(self.tmp, 1234)
        del ws["repos"][0]["env"]["base_url"]
        self.assertEqual(envmod.base_url(ws, "Web"), "http://127.0.0.1:1234")


class UpDownTests(FactoryCase):
    def test_up_down(self):
        port = free_port()
        ws = ws_with(self.tmp, port)
        ok, lines = envmod.up(ws, "Web")
        self.addCleanup(envmod.down, ws, "Web")
        self.assertTrue(ok, lines)
        pid = int(envmod.pid_path("ws", "Web").read_text())
        self.assertTrue(envmod.pid_alive(pid))
        self.assertTrue(envmod.health_ok(f"http://127.0.0.1:{port}/"))
        ok2, lines2 = envmod.up(ws, "Web")
        self.assertTrue(ok2)
        self.assertIn("already up", lines2[0])
        envmod.down(ws, "Web")
        self.assertFalse(envmod.pid_path("ws", "Web").exists())
        for _ in range(20):
            if not envmod.port_in_use(port):
                break
            time.sleep(0.25)
        self.assertFalse(envmod.port_in_use(port))

    def test_port_busy_reports_owner(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            owner = envmod.port_owner(port)
            if owner is None:
                self.skipTest("port owner lookup unavailable on this host")
            self.assertEqual(owner[0], os.getpid())
            ok, lines = envmod.up(ws_with(self.tmp, port), "Web")
            self.assertFalse(ok)
            self.assertIn(f"PID {os.getpid()}", lines[0])

    def test_dependency_started_first(self):
        p1, p2 = free_port(), free_port()
        ws = ws_with(self.tmp, p1)
        dep = ws_with(self.tmp, p2)["repos"][0]
        dep.update(alias="Api", name="api")
        ws["repos"][0]["env"]["depends_on"] = ["Api"]
        ws["repos"].append(dep)
        ok, lines = envmod.up(ws, "Web")
        self.addCleanup(envmod.down, ws, "Web")
        self.addCleanup(envmod.down, ws, "Api")
        self.assertTrue(ok, lines)
        self.assertTrue(lines[0].startswith("Api"))
        self.assertTrue(envmod.pid_path("ws", "Api").exists())

    def test_exit_before_healthy_prints_log(self):
        port = free_port()
        ws = ws_with(self.tmp, port, start=[sys.executable, "-c", "print('boom'); raise SystemExit(3)"])
        ok, lines = envmod.up(ws, "Web")
        self.assertFalse(ok)
        self.assertIn("boom", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
