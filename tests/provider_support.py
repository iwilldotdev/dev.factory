"""Shared fixtures for the profile / ingest / migrate tests: temp FACTORY_HOME with the synthetic profiles copied in."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory import core, profiles  # noqa: E402
from factory.cli import main  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "profiles"


def cli(*argv: str, stdin: bytes | None = None) -> tuple[int, str]:
    """Run the real CLI entry in-process; returns (exit code, stdout + stderr)."""
    out, err = io.StringIO(), io.StringIO()
    fake_in = None
    if stdin is not None:
        fake_in = mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8"))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), (fake_in or contextlib.nullcontext()):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class ProviderCase(unittest.TestCase):
    """Temp FACTORY_HOME holding profiles `acme` and `zeta`; helpers for a workspace and a bare run."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-prov-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        shutil.copytree(FIXTURES, self.home / "profiles")
        for pdir in (self.home / "profiles").iterdir():
            profiles.seal(pdir, "test fixture")

    def fixture(self, profile: str, kind: str, name: str) -> Path:
        return self.home / "profiles" / profile / "samples" / kind / name

    def make_workspace(self, providers: dict | None = None, name: str = "ws") -> None:
        ws = {"schemaVersion": 1, "name": name, "repos": [{"alias": "api", "scm_project": "team/api"}],
              "providers": providers if providers is not None else {"tickets": "acme", "scm": "acme", "design": "acme"}}
        core.write_json(core.workspace_path(name), ws)

    def make_run(self, run_id: str = "ws-acme-12", workspace: str = "ws", **extra) -> Path:
        rd = core.run_dir(run_id)
        run = {"schemaVersion": 1, "id": run_id, "workspace": workspace, "phase": "intake", "kind": "ticket",
               "tickets": [], "repos": {}, "events": [], "rev": 1}
        run.update(extra)
        core.write_json(rd / "run.json", run)
        return rd

    def read(self, rd: Path, rel: str):
        return json.loads((rd / rel).read_text(encoding="utf-8"))
