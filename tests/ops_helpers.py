"""Shared fixtures for test_ops_*.py (temp FACTORY_HOME, temp git repos)."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(repo, *args):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True).stdout.strip()


def make_repo(path, files=None):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "a.txt").write_text("1")
    git(path, "add", ".")
    git(path, "commit", "-qm", "c1")
    return path


class FactoryCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._old = {k: os.environ.get(k) for k in ("FACTORY_HOME", "FACTORY_TRANSCRIPT_ROOTS", *GIT_ENV)}
        os.environ["FACTORY_HOME"] = str(self.home)
        os.environ["FACTORY_TRANSCRIPT_ROOTS"] = str(self.tmp / "no-transcripts")
        os.environ.update(GIT_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write(self, rel, data):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (dict, list)):
            p.write_text(json.dumps(data), encoding="utf-8")
        elif isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data, encoding="utf-8")
        return p

    def make_run(self, run_id="ws-dev-1", **over):
        run = {
            "schemaVersion": 1, "id": run_id, "workspace": "ws",
            "tickets": [{"key": "DEV-1", "type": "feature", "summary": "Do thing", "area": "Obras"}],
            "repos": {}, "targets": ["pack"], "phase": "ship", "risk": "normal",
            "loops": {"build": 0, "review": 0}, "decisions": [], "blockers": [], "events": [],
            "sessions": [], "created": "2026-01-01T00:00:00+00:00", "updated": "2026-01-01T01:00:00+00:00",
        }
        run.update(over)
        self.write(f"runs/{run_id}/run.json", run)
        return run
