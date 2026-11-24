"""`env up` log capture: the output of a grandchild started through a `.cmd`/shell shim (the `npm run dev` chain) lands in
the env log, unbuffered. On Windows a DETACHED_PROCESS `cmd.exe` dropped the redirected handles (empty `React.log`)."""

from __future__ import annotations

import os
import stat
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase  # noqa: E402

from factory.commands import env as envmod  # noqa: E402

CHILD = (
    "import sys, time\n"
    "print('grandchild-stdout')\n"          # not flushed on purpose: needs PYTHONUNBUFFERED
    "print('grandchild-stderr', file=sys.stderr)\n"
    "import os\n"
    "print('colour-%s-unbuf-%s' % (os.environ.get('FORCE_COLOR'), os.environ.get('PYTHONUNBUFFERED')))\n"
    "time.sleep(60)\n"
)


class LogCapture(FactoryCase):
    def shim(self) -> Path:
        child = self.tmp / "child.py"
        child.write_text(CHILD, encoding="utf-8")
        if os.name == "nt":
            shim = self.tmp / "dev.cmd"
            shim.write_text(f'@echo off\r\n"{sys.executable}" "{child}"\r\n', encoding="utf-8")
        else:
            shim = self.tmp / "dev.sh"
            shim.write_text(f'#!/bin/sh\n"{sys.executable}" "{child}"\n', encoding="utf-8")
            shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        return shim

    def start(self, env_vars=None):
        env = {"start": [str(self.shim())], "ports": [], "env_vars": env_vars or {}}
        out: list[str] = []
        status, proc = envmod._start_locked(env, "ws", "Web", "http://127.0.0.1:9/", self.tmp, None, out)
        self.assertEqual(status, "started", out)
        self.addCleanup(self.stop, proc)
        return envmod.log_path("ws", "Web")

    def stop(self, proc):
        envmod.kill_tree(proc.pid)
        envmod._drop_record("ws", "Web")

    def wait_for(self, log: Path, *needles: str, timeout: float = 20.0) -> str:
        deadline, text = time.time() + timeout, ""
        while time.time() < deadline:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if all(n in text for n in needles):
                return text
            time.sleep(0.2)
        return text

    def test_grandchild_stdout_and_stderr_reach_the_log(self):
        log = self.start()
        text = self.wait_for(log, "grandchild-stdout", "grandchild-stderr")
        self.assertIn("grandchild-stdout", text)
        self.assertIn("grandchild-stderr", text)
        self.assertIn("grandchild-stdout", envmod.tail(log))  # what `factory env logs` prints

    def test_output_is_unbuffered_and_colourless(self):
        log = self.start()
        self.assertIn("colour-0-unbuf-1", self.wait_for(log, "colour-0-unbuf-1"))

    def test_workspace_env_vars_win_over_the_defaults(self):
        log = self.start({"FORCE_COLOR": "1"})
        self.assertIn("colour-1-unbuf-1", self.wait_for(log, "colour-1-unbuf-1"))


if __name__ == "__main__":
    unittest.main()
