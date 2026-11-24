"""Fixtures for the composite-run tests: a hotfix run with mirror targets, helpers to build small git repos, and a project with several runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from tests.helpers import git, tmpdir
from tests.m3_support import ShipCase
from tests.m4_support import M4Case

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def two_branches(files: dict[str, str]) -> Path:
    """A repo whose main branch holds `files`; the caller commits on `base` and `mirror` branches made from it."""
    repo = tmpdir("diff ") / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    for rel, text in files.items():
        (repo / rel).write_bytes(text.encode())
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


class HotfixCase(M4Case, ShipCase):
    """A hotfix run on `web` with mirror targets `old` (branch rel-old) and, when asked, `older`; `mirror: true` marks CHK-1 (and CHK-2, an http check)."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, IDENTITY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hotfix(self, targets: tuple[str, ...] = ("old",), runtime: bool = False) -> Path:
        self.run_dir = self.proj / ".factory" / "runs" / "ABC-1"
        self.cached("hotfix", lambda: self._hotfix(targets, runtime), targets, runtime)
        return self.run_dir

    def _hotfix(self, targets, runtime: bool) -> None:
        self.configure(repo_extra="".join(f'\n[[repo.mirror]]\nname = "{t}"\nbranch = "rel-{t}"\n' for t in targets))
        for t in targets:
            git("branch", f"rel-{t}", cwd=self.proj)
        plan = self.default_plan("bug")
        plan["checks"][0]["mirror"] = True
        if runtime:
            plan["checks"].append({"id": "CHK-2", "type": "http", "repo": "web", "ac": ["AC-1"], "method": "GET", "path": "/whoami", "expect_status": 200, "mirror": True})
        run = self.start("ABC-1", "--kind", "hotfix")
        self.ingest_ticket(run, type="bug")
        self.ok("gate", "intake")
        self.ok("run", "branch")
        if runtime:
            self.up()
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.write("diagnosis.md", "## Root cause\nVALUE is 1 in app.py.\n\n## Evidence\ncheck.py prints VALUE=1.\n\n## Ruled out\nThe check itself.\n\n## Scenario\nTicket scenario used.\n", root=run)
        self.ok("reproduce", "--actor", "builder-1")
        self.ok("gate", "reproduce")
        self.fix()
        self.ok("verify", "--actor", "builder-1")
        self.ok("gate", "verify")
        self.approve_both()
        self.ok("gate", "review")

    def mirror_event(self, target: str = "old") -> dict:
        return [e for e in self.events(self.run_dir) if e["type"] == "mirror" and e["target"] == target][-1]

    def reasons(self, gate: str) -> str:
        code, out, err = self.cli("gate", gate, "--json")
        return "\n".join(json.loads(out)["reasons"]) if out.strip() else err
