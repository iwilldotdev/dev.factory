"""Hotfix base target: a repo WITHOUT a run worktree is served from `target_paths[<base>]`; a worktree always wins."""

from __future__ import annotations

import contextlib
import io
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import prove  # noqa: E402
from factory.commands.workspace import target_paths_errors  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def quiet_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class BasePathCase(FactoryCase):
    def setUp(self):
        super().setUp()
        self.react = make_repo(self.tmp / "react").resolve()
        self.api_main = make_repo(self.tmp / "api-main").resolve()
        self.api_base = make_repo(self.tmp / "api-base").resolve()
        self.api_wt = make_repo(self.tmp / "api-wt").resolve()
        self.react_port, self.api_port = free_port(), free_port()
        serve = lambda port: [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]  # noqa: E731
        self.ws = {"schemaVersion": 1, "name": "ws", "repos": [
            {"alias": "React", "name": "react", "path": str(self.react), "env": {
                "start": serve(self.react_port), "cwd": ".", "ports": [self.react_port], "depends_on": ["API"],
                "health": f"http://127.0.0.1:{self.react_port}/", "startup_timeout_s": 20}},
            {"alias": "API", "name": "api", "path": str(self.api_main), "target_paths": {"homolog": str(self.api_base)}, "env": {
                "start": serve(self.api_port), "cwd": ".", "ports": [self.api_port],
                "health": f"http://127.0.0.1:{self.api_port}/", "startup_timeout_s": 20}}]}
        self.write("workspaces/ws/workspace.json", self.ws)
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for alias in ("React", "API"):
            try:
                envmod.down(self.ws, alias, force=True)
            except core.FactoryError:
                pass
        time.sleep(1)  # Windows keeps the log open a moment after the process is killed

    def hotfix_run(self, with_api_worktree=False, hotfix=True):
        repos = {"React": {"worktree": str(self.react), "branch": "b"}}
        if with_api_worktree:
            repos["API"] = {"worktree": str(self.api_wt), "branch": "b"}
        over = {"hotfix": {"base": "homolog", "mirror": ["pack"]}, "targets": ["homolog", "pack"]} if hotfix else {}
        self.make_run("r1", phase="prove", repos=repos, **over)
        return core.run_load("r1")


class ResolutionTests(BasePathCase):
    def test_dependency_without_worktree_resolves_to_the_base_target_path(self):
        run = self.hotfix_run()
        for target in (None, "homolog"):
            self.assertEqual(core.target_serve_path(run, self.ws, "API", target), Path(self.api_base))
            self.assertEqual(core.repo_serve_path(run, self.ws, "API", target, warn=lambda m: None), Path(self.api_base))

    def test_run_worktree_always_wins(self):
        run = self.hotfix_run(with_api_worktree=True)
        self.assertIsNone(core.target_serve_path(run, self.ws, "API", None))
        self.assertEqual(core.repo_serve_path(run, self.ws, "API", None), Path(self.api_wt))
        self.assertEqual(core.repo_serve_path(run, self.ws, "React", None), Path(self.react))

    def test_without_the_key_the_main_path_is_kept(self):
        run = self.hotfix_run()
        del self.ws["repos"][1]["target_paths"]
        self.assertIsNone(core.target_serve_path(run, self.ws, "API", None))
        self.assertEqual(core.repo_serve_path(run, self.ws, "API", None, warn=lambda m: None), Path(self.api_main))

    def test_non_hotfix_run_does_not_consult_target_paths(self):
        run = self.hotfix_run(hotfix=False)
        self.assertIsNone(core.target_serve_path(run, self.ws, "API", None))
        self.assertEqual(core.repo_serve_path(run, self.ws, "API", None, warn=lambda m: None), Path(self.api_main))

    def test_mirror_target_is_unchanged(self):
        run = self.hotfix_run()
        self.assertIsNone(core.target_serve_path(run, self.ws, "API", "pack"))  # no key for pack, no mirror worktree
        self.ws["repos"][1]["target_paths"]["pack"] = str(self.api_wt)
        self.assertEqual(core.target_serve_path(run, self.ws, "API", "pack"), Path(self.api_wt))


class ServedTests(BasePathCase):
    def test_env_up_serves_the_base_path_and_every_check_agrees(self):
        run = self.hotfix_run()
        rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_base))
        self.assertIsNone(prove.dep_binding(run, self.ws, "React", None))
        self.assertIsNone(prove.dep_binding(run, self.ws, "React", "homolog"))
        rc, out = quiet_cli("env", "check", "ws", "React", "--run", "r1")
        self.assertEqual(rc, 0, out)

    def test_dep_binding_expects_the_served_path_not_the_main_checkout(self):
        run = self.hotfix_run()
        no_key = {**self.ws, "repos": [self.ws["repos"][0], {k: v for k, v in self.ws["repos"][1].items() if k != "target_paths"}]}
        self.write("workspaces/ws/workspace.json", no_key)
        rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r1")  # the API is served from its main path
        self.assertEqual(rc, 0, out)
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_main))
        self.assertIsNone(prove.dep_binding(run, no_key, "React", None))
        self.assertIn("serves", prove.dep_binding(run, self.ws, "React", None) or "")  # the key now says: api_base


class WorkspaceCheckTests(unittest.TestCase):
    WS = {"ship": {"targets": ["pack", "homolog"], "hotfix": {"base": "homolog", "mirror": ["pack"]}}}

    def errors(self, tp):
        return target_paths_errors({**self.WS, "repos": [{"alias": "A", "target_paths": tp}]})

    def test_base_key_is_accepted(self):
        self.assertEqual(self.errors({"homolog": str(Path(sys.executable).resolve().parent)}), [])

    def test_unknown_key_non_string_and_relative_are_still_rejected(self):
        self.assertIn("not a known target", self.errors({"nope": str(Path(sys.executable).resolve())})[0])
        self.assertEqual(len(self.errors({"homolog": 3})), 1)
        self.assertIn("absolute", self.errors({"homolog": "../x"})[0])


if __name__ == "__main__":
    unittest.main()
