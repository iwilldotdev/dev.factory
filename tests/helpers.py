"""Shared test helpers: isolated temp dirs (with a space and an apostrophe), in-process CLI, fixtures."""
from __future__ import annotations

import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).as_posix()
FAKE_ADAPTER = (Path(__file__).resolve().parent / "fake_adapter.py").as_posix()
_TMP_ROOTS: list[str] = []
_GIT_FIXTURE: list[Path] = []


def lesson(*ids: str):
    """Tag a test with the lessons it covers; tests/test_lessons.py checks the tags against LESSONS.md."""
    def deco(obj):
        obj.lessons = ids
        return obj
    return deco


def tmpdir(prefix: str = "fac tory's ") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    _TMP_ROOTS.append(str(path))
    return path


@atexit.register
def _cleanup() -> None:
    for p in _TMP_ROOTS:
        shutil.rmtree(p, ignore_errors=True)


def git(*args: str, cwd: Path) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                          "-c", "init.defaultBranch=main", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"git {args} failed: {out.stderr}")
    return out.stdout.strip()


def copy_git_fixture(dest: Path) -> Path:
    """A small git repo, built once per process and copied for each test."""
    if not _GIT_FIXTURE:
        base = tmpdir("fixture ")
        repo = base / "repo"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        (repo / "README.txt").write_text("fixture\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "init", cwd=repo)
        _GIT_FIXTURE.append(repo)
    shutil.copytree(_GIT_FIXTURE[0], dest)
    return dest


def valid_ticket(**over) -> dict:
    data = {"id": "ABC-1", "title": "Show overdue invoices", "type": "feature", "status": "Open",
            "description_md": "As a user I see overdue invoices.", "attachments": []}
    data.update(over)
    return data


class FactoryCase(unittest.TestCase):
    """A temp project directory plus an isolated user environment for every test."""

    TOML = 'schema = 1\n\n[project]\nname = "demo"\n\n[[repo]]\npath = "."\nname = "web"\n'

    def setUp(self):
        self.base = tmpdir()
        self.proj = self.base / "project"
        self.proj.mkdir()
        env = {"HOME": str(self.base / "home"), "USERPROFILE": str(self.base / "home"),
               "XDG_CONFIG_HOME": str(self.base / "config"), "XDG_CACHE_HOME": str(self.base / "cache"),
               "LOCALAPPDATA": str(self.base / "cache")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write("factory.toml", self.TOML)

    def write(self, rel: str, text: str, root: Path | None = None) -> Path:
        path = (root or self.proj) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def cli(self, *args: str, cwd: Path | None = None, stdin: str | None = None, project: bool = True):
        """Run the CLI in-process; returns (exit code, stdout, stderr)."""
        from dev_factory.cli import main
        argv = list(args)
        if project and cwd is None:
            cwd = self.proj
            if "--project" not in argv:
                argv += ["--project", str(self.proj)]
        out, err = io.StringIO(), io.StringIO()
        old_cwd = os.getcwd()
        stdin_patch = mock.patch("sys.stdin", io.StringIO(stdin or ""))
        try:
            if cwd:
                os.chdir(cwd)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), stdin_patch:
                code = main(argv)
        finally:
            os.chdir(old_cwd)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args: str, **kw) -> str:
        code, out, err = self.cli(*args, **kw)
        self.assertEqual(code, 0, f"{args} -> {code}\nstdout: {out}\nstderr: {err}")
        return out

    def fails(self, *args: str, code: int = 2, **kw) -> str:
        got, out, err = self.cli(*args, **kw)
        self.assertEqual(got, code, f"{args} -> {got}\nstdout: {out}\nstderr: {err}")
        return err + out

    def start(self, ticket: str = "ABC-1", *extra: str) -> Path:
        self.ok("run", "start", ticket, *extra)
        return self.proj / ".factory" / "runs" / ticket

    def ingest_ticket(self, run_dir: Path, **over) -> dict:
        data = valid_ticket(**over)
        raw = run_dir / "raw" / "ticket" / f"{data['id']}.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps({"payload": data}), encoding="utf-8")
        self.ok("ingest", "ticket", "--raw", str(raw), "--stdin", stdin=json.dumps(data))
        return data

    def events(self, run_dir: Path) -> list[dict]:
        text = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def cli_subprocess(self, *args: str, cwd: Path | None = None, extra_env: dict | None = None, pythonpath: str | None = None):
        """Run `python -P -m dev_factory` with stdin closed and a hard timeout (L-44)."""
        env = dict(os.environ, PYTHONPATH=str(ROOT) if pythonpath is None else pythonpath, **(extra_env or {}))
        return subprocess.run([sys.executable, "-P", "-m", "dev_factory", *args], cwd=cwd or self.proj, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)


def adapter_scenario(case: FactoryCase, **scenario) -> str:
    """Write a scenario file for tests/fake_adapter.py and return a fetch template that runs it."""
    path = case.write("scenario.json", json.dumps(scenario), root=case.base)
    return f'"{PY}" "{FAKE_ADAPTER}" "{path.as_posix()}" {{id}}'


def provider_toml(template: str, kind: str = "ticket", extra: str = "") -> str:
    return FactoryCase.TOML + f"\n[providers.{kind}]\nfetch = {json.dumps(template)}\n{extra}"
