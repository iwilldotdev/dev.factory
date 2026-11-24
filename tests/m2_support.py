"""Fixtures for the plan and proof tests: a git project built once, copied per test, plus plan helpers."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest import mock

from tests.helpers import PY, FactoryCase, git, head_of, tmpdir

CHECK_PY = '''import sys
import app
want = int(sys.argv[1])
if app.VALUE != want:
    print(f"AssertionError: VALUE={app.VALUE} expected {want}")
    sys.exit(1)
print(f"checks passed: VALUE={app.VALUE}")
'''
FLAKY_PY = '''import os
import sys
flag = sys.argv[1]
first = not os.path.exists(flag)
open(flag, "w").close()
print("flaky ran")
sys.exit(1 if first else 0)
'''
SPAWN_PY = '''import subprocess
import sys
import time
child = "import sys, time; time.sleep(2.5); open(sys.argv[1], 'w').write('alive')"
subprocess.Popen([sys.executable, "-c", child, sys.argv[1]])
time.sleep(60)
'''
FILES = {"app.py": "VALUE = 1\n", "check.py": CHECK_PY, "flaky.py": FLAKY_PY, "spawn.py": SPAWN_PY, "README.txt": "fixture\n"}
_BASE: list[Path] = []


def fixture_repo() -> Path:
    if not _BASE:
        repo = tmpdir("m2 fixture ") / "repo"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        for name, text in FILES.items():
            (repo / name).write_text(text, newline="\n")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "chore: init", cwd=repo)
        _BASE.append(repo)
    return _BASE[0]


def cmd(code: str) -> str:
    """A check command line that runs a one-line Python program (single quotes inside, so any shell takes it)."""
    return f'"{PY}" -c "{code}"'


def check_cmd(want: int = 2) -> str:
    return f'"{PY}" check.py {want}'


class M2Case(FactoryCase):
    """The project directory is a git repo (base branch main) with app.py and check.py."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})  # same line endings everywhere
        patcher.start()
        self.addCleanup(patcher.stop)
        shutil.rmtree(self.proj)
        shutil.copytree(fixture_repo(), self.proj)
        self.write("factory.toml", self.TOML)
        self.write(".gitignore", ".factory/\n")

    def prepare(self, kind: str = "feature", plan: dict | None = None, ticket: str = "ABC-1", lock: bool = True, **ticket_over) -> Path:
        """Start a run, pass intake, create the worktree, write the plan and (by default) lock it. The finished tree is built once
        per module for the same inputs and copied back afterwards."""
        plan = plan if plan is not None else self.default_plan(kind)
        self.cached("prepare", lambda: self._prepare(kind, plan, ticket, lock, **ticket_over), kind, json.dumps(plan, sort_keys=True), ticket, lock,
                    json.dumps(ticket_over, sort_keys=True))
        return self.proj / ".factory" / "runs" / ticket

    def _prepare(self, kind: str, plan: dict, ticket: str, lock: bool, **ticket_over) -> Path:
        run = self.start(ticket)
        self.ingest_ticket(run, id=ticket, type=kind, **ticket_over)
        self.ok("gate", "intake")
        self.ok("run", "branch")
        self.write_plan(run, plan)
        if lock:
            self.ok("gate", "plan")
        return run

    def default_plan(self, kind: str = "feature") -> dict:
        check = {"id": "CHK-1", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": check_cmd(2), "expect_pattern": "checks passed: VALUE=2"}
        ac = {"id": "AC-1", "text": "VALUE is 2", "source": "ticket:description"}
        if kind == "bug":
            check["reproduce"] = {"expect_fail_pattern": "AssertionError: VALUE=1 expected 2"}
            check["red_green"] = {"test_files": ["check.py"]}
        return {"schema": 1, "kind": kind, "repos": ["web"], "acs": [ac], "checks": [check]}

    def gate(self, name: str) -> tuple[int, list[str]]:
        """Run a gate through the CLI; returns (exit code, reasons)."""
        code, out, err = self.cli("gate", name, "--json")
        return code, json.loads(out)["reasons"] if out.strip() else [err]

    def failing(self, name: str) -> str:
        code, reasons = self.gate(name)
        self.assertEqual(code, 1, reasons)
        return "\n".join(reasons)

    def write_plan(self, run: Path, plan: dict) -> None:
        (run / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def read_plan(self, run: Path) -> dict:
        return json.loads((run / "plan.json").read_text(encoding="utf-8"))

    def tree(self, repo: str = "web") -> Path:
        for d in (self.proj / ".factory" / "runs").iterdir():
            wts = [e for e in self.events(d) if e["type"] == "worktree" and e["repo"] == repo and not e["throwaway"]]
            if wts:
                return Path(wts[-1]["path"])
        raise AssertionError("no worktree")

    def commit(self, rel: str, text: str, msg: str = "feat: change", tree: Path | None = None) -> str:
        tree = tree or self.tree()
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text, newline="\n")
        git("add", "-A", cwd=tree)
        git("commit", "-q", "-m", msg, cwd=tree)
        return head_of(tree)

    def commit_many(self, files: dict[str, str], msg: str, tree: Path | None = None) -> str:
        tree = tree or self.tree()
        for rel, text in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text, newline="\n")
        git("add", "-A", cwd=tree)
        git("commit", "-q", "-m", msg, cwd=tree)
        return head_of(tree)

    def fix(self, tree: Path | None = None) -> str:
        return self.commit("app.py", "VALUE = 2\n", "fix: value", tree)

    def evidence(self, run: Path, **match) -> list[dict]:
        return [e for e in self.events(run) if e["type"] == "evidence" and all(e.get(k) == v for k, v in match.items())]
