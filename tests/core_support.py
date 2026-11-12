"""Shared fixtures for test_core_*.py (temp FACTORY_HOME and temp git repos)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402

PY = f'"{sys.executable}"'
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SAMPLE_PROFILE = {
    "schemaVersion": 1,
    "repos": [{"alias": "React", "name": "construcodereact", "gitlab_project": "g/react", "order": 2}],
    "ship": {"targets": ["pack", "homolog"], "draft_default": True},
    "commit": {"header_regex": "^\\[[^\\]]+\\] - .+", "forbidden_trailers": ["Co-authored-by:"]},
    "epics": [{"key": "DEV-1", "roadmap": "r.md", "ship_comment": "s.md", "jira_transition": None}],
}


def sh(*args: str, cwd: Path) -> str:
    proc = subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"{args} failed: {proc.stderr}")
    return proc.stdout.strip()


def init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh("git", "init", "-q", "-b", branch, cwd=path)
    sh("git", "config", "user.email", "t@example.com", cwd=path)
    sh("git", "config", "user.name", "T", cwd=path)
    sh("git", "config", "core.autocrlf", "false", cwd=path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    sh("git", "add", "-A", cwd=path)
    sh("git", "commit", "-q", "-m", "init", cwd=path)
    return path


def commit_file(repo: Path, rel: str, content: str, msg: str = "[Area] - change") -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", msg, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


def fake_module(name: str, **funcs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, fn in funcs.items():
        setattr(mod, key, fn)
    return mod


def cli(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    real = pkgutil.iter_modules
    mine = {"workspace", "run", "gate", "review", "prove"}
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),             mock.patch("factory.cli.pkgutil.iter_modules", lambda path: [m for m in real(path) if m.name in mine]):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class FactoryCase(unittest.TestCase):
    """Temp FACTORY_HOME, a React repo with origin-less main, workspace 'ws', run 'ws-dev-1'."""

    quality: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-test-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.tmp / "home"), "FACTORY_ROLE": "auditor"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ext_errors = {"check_pack": [], "capability": [], "verify": []}
        mods = {
            "factory.commands.figma": fake_module("factory.commands.figma", check_pack=lambda rid: list(self.ext_errors["check_pack"])),
            "factory.commands.env": fake_module("factory.commands.env", capability=lambda ws, alias: list(self.ext_errors["capability"]),
                                                binding_error=lambda ws, alias, wt, head: None),
            "factory.commands.ship": fake_module("factory.commands.ship", verify=lambda rid: list(self.ext_errors["verify"])),
        }
        mp = mock.patch.dict(sys.modules, mods)
        mp.start()
        self.addCleanup(mp.stop)

    # ----- builders -----

    def make_workspace(self, name: str = "ws") -> dict:
        profile = self.tmp / "project.json"
        profile.write_text(json.dumps(SAMPLE_PROFILE), encoding="utf-8")
        rc, out = cli("workspace", "import", name, "--from", str(profile))
        assert rc == 0, out
        self.repo = init_repo(self.tmp / "React")
        data = core.workspace_load(name)
        repo = data["repos"][0]
        repo["path"] = str(self.repo)
        repo["quality"] = dict(self.quality)
        repo["env"] = {"start": ["x"], "health": "http://127.0.0.1:1/", "base_url": "http://127.0.0.1:1"}
        data["worktree_root"] = str(self.tmp / "wt")
        data["autonomy"]["max_build_loops"] = 2
        core.write_json(core.workspace_path(name), data)
        return data

    def make_run(self, ticket: str = "DEV-1", branch: bool = True, bug: bool = False) -> str:
        self.make_workspace()
        argv = ["run", "start", "ws", ticket] + (["--bug", ticket] if bug else [])
        rc, out = cli(*argv)
        assert rc == 0, out
        self.run_id = f"ws-{ticket.lower()}"
        if branch:
            rc, out = cli("run", "branch", self.run_id, "React", "--base", "main")
            assert rc == 0, out
            self.wt = Path(core.run_load(self.run_id)["repos"]["React"]["worktree"])
            sh("git", "config", "user.email", "t@example.com", cwd=self.wt)
            sh("git", "config", "user.name", "T", cwd=self.wt)
        return self.run_id

    def set_phase(self, phase: str) -> None:
        """Jump to a phase; past plan, lock the plan files as the plan gate would (when not locked yet)."""
        from factory import integrity
        run = core.run_load(self.run_id)
        run["phase"] = phase
        if phase not in ("intake", "plan") and "plan_lock" not in run:
            integrity.lock_plan(run, "test fixture")
        core.run_save(run)

    def write_acs(self, acs: list, checks: list) -> None:
        core.write_json(core.run_dir(self.run_id) / "acs.json", {"schemaVersion": 1, "acs": acs, "checks": checks})

    def write_pack(self, node: str, payload: dict | None = None, png: bool = True, meta: bool = True) -> None:
        fig = core.run_dir(self.run_id) / "figma"
        stem = core.node_file(node)
        core.write_json(fig / f"{stem}.json", payload if payload is not None else
                        {"style": {"fontSize": 12, "fontWeight": 600}, "fills": [{"color": {"r": 0.2, "g": 0.2, "b": 0.2}}]})
        if png:
            (fig / f"{stem}.png").write_bytes(PNG)
        if meta:
            core.write_json(fig / "meta.json", {"fileKey": "abc", "lastModified": "2026-01-01", "version": "1"})

    def write_style_map(self, node: str, expect: dict, name: str = "checks/style/s.json") -> None:
        core.write_json(core.run_dir(self.run_id) / name, {
            "node": node, "entries": [{"id": "title", "selector": "h1", "expect": expect, "tolerance_px": 1}]})
