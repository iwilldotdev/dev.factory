"""Shared test helpers: isolated temp dirs (with a space and an apostrophe), in-process CLI, fixtures."""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
PY = Path(sys.executable).as_posix()
FAKE_ADAPTER = (Path(__file__).resolve().parent / "fake_adapter.py").as_posix()
_TMP_ROOTS: list[str] = []
_GIT_FIXTURE: list[Path] = []
_CASE_ROOT: list[Path] = []
_SPARE = [0]


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


def rmtree(path: Path, tries: int = 20) -> bool:
    """Remove a tree, clearing read-only bits (git objects) and waiting for handles that a killed process still holds."""
    def force(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    for _ in range(tries):
        if not Path(path).exists():
            return True
        try:
            shutil.rmtree(path, **{"onexc" if sys.version_info >= (3, 12) else "onerror": force})
        except OSError:
            time.sleep(0.1)
    return not Path(path).exists()


os.environ["FACTORY_HOME"] = str(tmpdir("fhome "))  # no test ever reads or writes the user's real workspaces


def case_root() -> Path:
    """One temp root per test process: every case runs at the same path, so a finished tree copied back needs no path rewriting."""
    if not _CASE_ROOT:
        _CASE_ROOT.append(tmpdir())
    return _CASE_ROOT[0]


def tree_hash(root: Path) -> str:
    """Names and contents of every file under `root`: the starting point a snapshot was built from."""
    h = hashlib.sha1()
    for p in sorted(Path(root).rglob("*")):
        try:
            if p.is_file():
                h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
        except OSError:
            h.update(b"unreadable")
    return h.hexdigest()


@atexit.register
def _cleanup() -> None:
    for p in _TMP_ROOTS:
        shutil.rmtree(p, ignore_errors=True)


def git(*args: str, cwd: Path) -> str:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")  # same inputs, same commit: snapshots can be reused
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                          "-c", "init.defaultBranch=main",
                          "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(f"git {args} failed: {out.stderr}")
    return out.stdout.strip()


def head_of(tree: Path) -> str:
    """HEAD of a work tree without starting a process when the files say it."""
    from dev_factory import git as product_git
    return product_git._read_head(tree) or git("rev-parse", "HEAD", cwd=tree)


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
        self.base = case_root() / "case"
        if not rmtree(self.base):  # a process of the previous test still holds it: use a spare path (snapshots notice the new path)
            _SPARE[0] += 1
            self.base = case_root() / f"case-{_SPARE[0]}"
        self.base.mkdir()
        self.proj = self.base / "project"
        self.proj.mkdir()
        env = {"HOME": str(self.base / "home"), "USERPROFILE": str(self.base / "home"),
               "XDG_CONFIG_HOME": str(self.base / "config"), "XDG_CACHE_HOME": str(self.base / "cache"),
               "LOCALAPPDATA": str(self.base / "cache"), "FACTORY_HOME": str(self.base / "fhome")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write("factory.toml", self.TOML)

    def cached(self, name: str, build, *args) -> bool:
        """Run `build()` once per module for this exact starting tree and arguments; later calls copy the finished tree back
        over the case directory (paths are identical, so git worktrees and recorded paths stay valid). True when restored."""
        store = case_root() / ("snap-" + hashlib.sha1(repr((name, args, tree_hash(self.base))).encode()).hexdigest()[:16])
        if (store / "path").is_file() and (store / "path").read_text(encoding="utf-8") == str(self.base) and rmtree(self.base):
            shutil.copytree(store / "data", self.base, symlinks=True)
            return True
        build()
        if not store.exists():
            shutil.copytree(self.base, store / "data", symlinks=True)
            (store / "path").write_text(str(self.base), encoding="utf-8")
        return False

    def write(self, rel: str, text: str, root: Path | None = None) -> Path:
        path = (root or self.proj) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def cli(self, *args: str, cwd: Path | None = None, stdin: str | None = None, project: bool = True):
        """Run the CLI in-process; returns (exit code, stdout, stderr)."""
        from dev_factory.cli import main
        argv = list(args)
        if ("ship", "plan") in zip(argv, argv[1:]) and getattr(self, "summaries", True):
            self.default_summaries()
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

    def default_summaries(self) -> None:
        """Every run gets the reviewer's summary that the ship plan needs, unless the test wrote its own."""
        for run in (self.proj / ".factory" / "runs").glob("*"):
            if run.is_dir() and not (run / "ship" / "summary.md").is_file():
                (run / "ship").mkdir(exist_ok=True)
                (run / "ship" / "summary.md").write_text(f"{run.name}: what changes, and a unit test that shows it.\n", encoding="utf-8")

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
