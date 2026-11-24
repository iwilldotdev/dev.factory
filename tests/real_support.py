"""Fixtures that run the REAL CLI (every module) against a temp FACTORY_HOME and temp git repos."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402


def sh(*args: str, cwd: Path) -> str:
    proc = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"{args} failed: {proc.stderr}")
    return proc.stdout.strip()


def init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh("git", "init", "-q", "-b", branch, cwd=path)
    for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("core.autocrlf", "false")):
        sh("git", "config", k, v, cwd=path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    sh("git", "add", "-A", cwd=path)
    sh("git", "commit", "-q", "-m", "init", cwd=path)
    sh("git", "update-ref", f"refs/remotes/origin/{branch}", "HEAD", cwd=path)  # fake origin: behind-count 0
    return path


def commit_file(repo: Path, rel: str, content: str, msg: str = "[Area] - change") -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", msg, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


def cli(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class RealCase(unittest.TestCase):
    """Workspace 'ws' with repos API (order 1) and React (order 2), ship target main."""

    repos = ("API", "React")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-real-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.tmp / "home"), "FACTORY_ROLE": "auditor",
                                                  "FACTORY_TRANSCRIPT_ROOTS": str(self.tmp / "no-transcripts")})
        patcher.start()
        self.addCleanup(patcher.stop)
        rc, out = cli("workspace", "init", "ws")
        assert rc == 0, out
        ws = core.workspace_load("ws")
        ws["worktree_root"] = str(self.tmp / "wt")
        ws["ship"] = {"targets": ["main"], "draft_default": True, "ticket_comment": "change-links"}
        ws["commit"] = {"header_regex": r"^\[[^\]]+\] - .+", "forbidden_trailers": []}
        self.repo_dirs = {}
        for i, alias in enumerate(self.repos, 1):
            self.repo_dirs[alias] = init_repo(self.tmp / alias)
            ws["repos"].append({"alias": alias, "name": alias.lower(), "order": i, "path": str(self.repo_dirs[alias]),
                                "quality": {}, "env": {}})
        self.ws = ws
        core.write_json(core.workspace_path("ws"), ws)

    def save_ws(self) -> None:
        core.write_json(core.workspace_path("ws"), self.ws)

    def ok(self, *argv: str) -> str:
        rc, out = cli(*argv)
        self.assertEqual(rc, 0, out)
        return out

    def lock_and_prove(self, run_id: str) -> str:
        """Lock the plan files as the plan gate would, then run the real `factory prove`."""
        from factory import integrity
        run = core.run_load(run_id)
        integrity.lock_plan(run, "test fixture")
        core.run_save(run)
        return self.ok("prove", run_id)
