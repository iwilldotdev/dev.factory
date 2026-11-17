import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factory import core

WS = {"name": "ws", "repos": [{"alias": "API", "path": "/main/api"}, {"alias": "React", "path": "/main/react"},
                              {"alias": "Legado", "path": "/main/legado"}]}


class EnvDepWorktreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.dict(os.environ, {"FACTORY_HOME": tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def make(self, rid, key, repos=None, depends=None, parent="ws-ep-1"):
        d = core.run_dir(rid)
        d.mkdir(parents=True)
        run = {"schemaVersion": core.SCHEMA_VERSION, "id": rid, "parent": parent,
               "tickets": [{"key": key, "depends_on": depends or []}],
               "repos": {a: {"worktree": w} for a, w in (repos or {}).items()}}
        (d / "run.json").write_text(json.dumps(run), encoding="utf-8")
        return run

    def test_uses_dependency_worktree(self):
        self.make("ws-dev-1", "DEV-1", {"API": "/wt/api1"})
        child = self.make("ws-dev-2", "DEV-2", {"React": "/wt/react2"}, ["DEV-1"])
        self.assertEqual(core.repo_path_for_env(child, WS, "API"), Path("/wt/api1"))
        self.assertEqual(core.repo_path_for_env(child, WS, "React"), Path("/wt/react2"))
        self.assertEqual(core.repo_path_for_env(child, WS, "Legado"), Path("/main/legado"))
        self.assertEqual(core.repo_path_for_run(child, WS, "API"), Path("/main/api"))

    def test_transitive_nearest_first_and_epic_json(self):
        self.make("ws-dev-1", "DEV-1", {"API": "/wt/api1"})
        self.make("ws-dev-2", "DEV-2", {"API": "/wt/api2"}, ["DEV-1"])
        self.make("ws-dev-3", "DEV-3", {}, ["DEV-2"])
        child = self.make("ws-dev-4", "DEV-4", {}, [])
        core.run_dir("ws-ep-1").mkdir(parents=True)
        (core.run_dir("ws-ep-1") / "epic.json").write_text(json.dumps(
            {"tickets": [{"key": "DEV-4", "depends_on": ["DEV-3"]}]}), encoding="utf-8")
        self.assertEqual(core.repo_path_for_env(child, WS, "API"), Path("/wt/api2"))

    def test_no_parent_or_cycle_falls_back(self):
        solo = self.make("ws-dev-9", "DEV-9", {}, ["DEV-8"], parent=None)
        self.assertEqual(core.repo_path_for_env(solo, WS, "API"), Path("/main/api"))
        self.make("ws-dev-8", "DEV-8", {}, ["DEV-7"])
        c = self.make("ws-dev-7", "DEV-7", {}, ["DEV-8"])
        self.assertEqual(core.repo_path_for_env(c, WS, "API"), Path("/main/api"))


if __name__ == "__main__":
    unittest.main()
