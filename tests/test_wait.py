"""`factory wait`: file, cmd and url modes, timeout and usage errors."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory.cli import main  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["wait", *argv])
    return code, out.getvalue(), err.getvalue()


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redir":
            self.send_response(307)
            self.send_header("Location", "/ok")
        elif self.path == "/ok":
            self.send_response(200)
        else:
            self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def pycmd(code: int) -> str:
    return f'"{sys.executable}" -c "import sys; sys.exit({code})"'


class WaitTest(unittest.TestCase):
    def test_file_exists_immediately(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "a.txt")
            f.write_text("hi", encoding="utf-8")
            code, out, _ = run("--file", str(f), "--timeout", "5")
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip().splitlines()), 1)

    def test_file_missing_times_out(self):
        with tempfile.TemporaryDirectory() as d:
            t = time.monotonic()
            code, out, _ = run("--file", str(Path(d, "no")), "--timeout", "1", "--poll", "0.2")
            self.assertEqual(code, 1)
            self.assertIn("WAIT-TIMEOUT", out)
            self.assertLess(time.monotonic() - t, 5)

    def test_file_contains(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "log.txt")
            f.write_text("starting", encoding="utf-8")
            self.assertEqual(run("--file", str(f), "--contains", "ready", "--timeout", "1", "--poll", "0.2")[0], 1)
            threading.Timer(0.4, lambda: f.write_text("starting\nready", encoding="utf-8")).start()
            self.assertEqual(run("--file", str(f), "--contains", "ready", "--timeout", "5", "--poll", "0.1")[0], 0)

    def test_cmd_exit(self):
        self.assertEqual(run("--cmd", pycmd(0), "--timeout", "10")[0], 0)
        self.assertEqual(run("--cmd", pycmd(3), "--until-exit", "3", "--timeout", "10")[0], 0)
        self.assertEqual(run("--cmd", pycmd(1), "--timeout", "1", "--poll", "0.2")[0], 1)

    def test_url_status(self):
        srv = HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            self.assertEqual(run("--url", base + "/ok", "--timeout", "5")[0], 0)
            self.assertEqual(run("--url", base + "/redir", "--status", "3xx", "--timeout", "5")[0], 0)
            self.assertEqual(run("--url", base + "/redir", "--timeout", "1", "--poll", "0.2")[0], 1)
            self.assertEqual(run("--url", base + "/down", "--timeout", "1", "--poll", "0.2")[0], 1)
            self.assertEqual(run("--url", base + "/down", "--status", "503", "--timeout", "2")[0], 0)
        finally:
            srv.shutdown()

    def test_url_unreachable_times_out(self):
        self.assertEqual(run("--url", "http://127.0.0.1:1/", "--timeout", "1", "--poll", "0.2")[0], 1)

    def test_timeout_is_capped_and_poll_has_a_floor(self):
        import argparse

        from factory.commands import wait as w

        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "x")
            f.write_text("ok")
            ns = argparse.Namespace(file=str(f), cmd=None, url=None, contains=None, until_exit=0, status="2xx",
                                    timeout=99999.0, poll=0.0001)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(w._cmd(ns), 0)
            self.assertEqual(ns.timeout, 600.0)
            self.assertEqual(ns.poll, 0.2)

    def test_usage_errors(self):
        self.assertEqual(run()[0], 2)
        self.assertEqual(run("--file", "a", "--cmd", "b")[0], 2)
        self.assertEqual(run("--url", "http://x", "--status", "bogus")[0], 2)
        self.assertEqual(run("--file", "a", "--timeout", "0")[0], 2)


if __name__ == "__main__":
    unittest.main()
