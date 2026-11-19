"""env_only repos: registered in the run only so the env starts from a run worktree; they carry no code."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_support import FactoryCase, cli, commit_file, init_repo  # noqa: E402
from ops_helpers import FactoryCase as OpsCase, git, make_repo  # noqa: E402

from factory import core, shots  # noqa: E402
from factory.commands import gate  # noqa: E402


class EnvOnlyRunTests(FactoryCase):
    def add_repos(self, *aliases: str) -> None:
        data = core.workspace_load("ws")
        for alias in aliases:
            path = init_repo(self.tmp / alias)
            data["repos"].append({"alias": alias, "path": str(path), "order": 5,
                                  "env": {"start": ["x"], "health": "http://127.0.0.1:1/", "base_url": "http://127.0.0.1:1"}})
        core.write_json(core.workspace_path("ws"), data)

    def wt_of(self, alias: str) -> Path:
        return Path(core.run_load(self.run_id)["repos"][alias]["worktree"])

    def test_gate_build_ignores_env_only_repos(self):
        self.make_run()
        self.add_repos("API", "IA")
        rc, out = cli("run", "branch", self.run_id, "API", "--base", "main", "--env-only")
        self.assertEqual(rc, 0, out)
        rc, out = cli("run", "branch", self.run_id, "IA", "--base", "main")
        self.assertEqual(rc, 0, out)
        (self.wt_of("IA") / "appsettings.Development.json").write_text("{}", encoding="utf-8")
        (self.wt_of("API") / "README.md").write_text("local tweak\n", encoding="utf-8")
        commit_file(self.wt, "src/a.txt", "a\n", msg="[A] - fix")
        self.set_phase("build")
        rc, out = cli("gate", self.run_id, "build")
        self.assertEqual(rc, 1)
        self.assertIn("IA: worktree has uncommitted changes", out)
        self.assertIn("IA: no commits ahead", out)
        self.assertNotIn("API:", out)

        rc, out = cli("run", "env-only", self.run_id, "IA")
        self.assertEqual(rc, 0, out)
        run = core.run_load(self.run_id)
        self.assertTrue(run["repos"]["IA"]["env_only"])
        self.assertEqual(run["phase"], "build")  # no replan
        evs = [e["data"] for e in run["events"] if e["kind"] == "env_only"]
        self.assertEqual([(e["repo"], e["on"]) for e in evs], [("API", True), ("IA", True)])
        rc, out = cli("gate", self.run_id, "build")
        self.assertEqual(rc, 0, out)
        self.assertEqual(gate.touched_repos(core.run_load(self.run_id)), ["React"])
        self.assertEqual(shots.touched_repos(core.run_load(self.run_id), core.workspace_load("ws")), ["React"])

    def test_env_only_toggle_and_errors(self):
        self.make_run()
        self.add_repos("API")
        rc, out = cli("run", "env-only", self.run_id, "API")
        self.assertNotEqual(rc, 0)
        self.assertIn("has no branch", out)
        rc, out = cli("run", "branch", self.run_id, "API", "--base", "main", "--env-only")
        self.assertEqual(rc, 0, out)
        rc, out = cli("run", "env-only", self.run_id, "API")
        self.assertEqual(rc, 0, out)
        self.assertIn("unchanged", out)
        rc, out = cli("run", "env-only", self.run_id, "API", "--off")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("env_only", core.run_load(self.run_id)["repos"]["API"])
        self.assertEqual(sorted(gate.touched_repos(core.run_load(self.run_id))), ["API", "React"])

    def test_only_env_only_repos_fail_build(self):
        self.make_run()
        rc, out = cli("run", "env-only", self.run_id, "React")
        self.assertEqual(rc, 0, out)
        self.set_phase("build")
        rc, out = cli("gate", self.run_id, "build")
        self.assertEqual(rc, 1)
        self.assertIn("every repo of the run is env_only", out)

    def test_env_uses_env_only_worktree(self):
        self.make_run()
        self.add_repos("API")
        rc, out = cli("run", "branch", self.run_id, "API", "--base", "main", "--env-only")
        self.assertEqual(rc, 0, out)
        run, ws = core.run_load(self.run_id), core.workspace_load("ws")
        mod = sys.modules.pop("factory.commands.env")  # FactoryCase stubs it; load the real module
        try:
            import importlib
            env = importlib.import_module("factory.commands.env")
            self.assertEqual(env._cwd_for(ws, run, "API"), self.wt_of("API").resolve())
        finally:
            sys.modules["factory.commands.env"] = mod
        self.assertEqual(core.repo_path_for_env(run, ws, "API"), self.wt_of("API"))


class EnvOnlyShipTests(OpsCase):
    def test_ship_plan_has_no_actions_for_env_only(self):
        from factory.commands import ship
        for name, value in (("integrity_errors", []), ("review_errors", []), ("branch_head", "f" * 40)):
            p = mock.patch.object(ship, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        react, api = make_repo(self.tmp / "react"), make_repo(self.tmp / "api")
        for r in (react, api):
            git(r, "update-ref", "refs/remotes/origin/pack", "HEAD")
        self.write("workspaces/ws/workspace.json", {
            "schemaVersion": 1, "name": "ws",
            "repos": [{"alias": "React", "path": str(react), "order": 2}, {"alias": "API", "path": str(api), "order": 1}],
            "ship": {"targets": ["pack"], "draft_default": True}, "autonomy": {"ship": "ask"}})
        self.make_run("ws-dev-1", repos={"React": {"branch": "feature/x", "base": "pack"},
                                         "API": {"branch": "feature/y", "base": "pack", "env_only": True}})
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [
            {"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "logic", "checks": ["C1"]}],
            "checks": [{"id": "C1", "type": "test", "repo": "React", "cmd": "x"}]})
        self.write("runs/ws-dev-1/evidence/C1.json", {"check_id": "C1", "status": "pass"})
        plan = ship.build_plan("ws-dev-1")
        self.assertEqual([m["repo"] for m in plan["mrs"]], ["React"])
        self.assertFalse([a for a in plan["actions"] if a.get("repo") == "API"])
        self.assertNotIn("API", str([a for a in plan["actions"] if a["type"] in ("push", "mr", "readback")]))


if __name__ == "__main__":
    unittest.main()
