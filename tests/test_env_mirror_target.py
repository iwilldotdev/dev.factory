"""Hotfix mirror runtime: `env up|check --target` serves the mirror worktree; `prove --target` runs one target."""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo  # noqa: E402
from test_hotfix_flow import HotfixCase  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from real_support import cli  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def quiet_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class MirrorEnvCase(FactoryCase):
    def setUp(self):
        super().setUp()
        self.base = make_repo(self.tmp / "react-base").resolve()
        self.mirror = make_repo(self.tmp / "react-pack").resolve()
        self.api_pack = make_repo(self.tmp / "api-pack").resolve()
        self.api_main = make_repo(self.tmp / "api-main").resolve()
        self.react_port, self.api_port = free_port(), free_port()
        serve = lambda port: [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]  # noqa: E731
        self.ws = {"schemaVersion": 1, "name": "ws", "repos": [
            {"alias": "React", "name": "react", "path": str(self.base), "env": {
                "start": serve(self.react_port), "cwd": ".", "ports": [self.react_port], "depends_on": ["API"],
                "health": f"http://127.0.0.1:{self.react_port}/", "startup_timeout_s": 20}},
            {"alias": "API", "name": "api", "path": str(self.api_main), "target_paths": {"pack": str(self.api_pack)}, "env": {
                "start": serve(self.api_port), "cwd": ".", "ports": [self.api_port],
                "health": f"http://127.0.0.1:{self.api_port}/", "startup_timeout_s": 20}}]}
        self.write("workspaces/ws/workspace.json", self.ws)
        self.make_run("r1", phase="prove", targets=["homolog", "pack"], hotfix={"base": "homolog", "mirror": ["pack"]},
                      repos={"React": {"worktree": str(self.base), "branch": "b", "worktrees": {"pack": str(self.mirror)}}})
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for alias in ("React", "API"):
            try:
                envmod.down(self.ws, alias, force=True)
            except core.FactoryError:
                pass
        time.sleep(1)  # Windows keeps the log open a moment after the process is killed

    def head(self, path: Path) -> str:
        return envmod._head_of(path)

    def binding(self, wt: Path):
        return envmod.binding_error(self.ws, "React", wt, self.head(wt), run=core.run_load("r1"))


class EnvTargetTests(MirrorEnvCase):
    def test_up_target_serves_mirror_and_binds_only_to_it(self):
        rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        self.assertEqual(rc, 0, out)
        rec = envmod.read_record("ws", "React")
        self.assertTrue(envmod._same_path(rec["cwd"], self.mirror))
        self.assertEqual(rec["target"], "pack")
        self.assertIsNone(self.binding(self.mirror))
        self.assertIn("serves", self.binding(self.base))
        api = envmod.read_record("ws", "API")
        self.assertTrue(envmod._same_path(api["cwd"], self.api_pack))  # target_paths of a dep without a worktree

    def test_check_reports_the_served_target_and_follows_the_record(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        rc, out = quiet_cli("env", "check", "ws", "React", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.assertIn("(target pack)", out)
        rc, out = quiet_cli("env", "check", "ws", "React", "--run", "r1", "--target", "homolog")
        self.assertEqual(rc, 3, out)  # stale: serves the mirror, not the base target

    def test_restart_keeps_the_target(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        ok, _ = envmod.restart_env(self.ws, core.run_load("r1"), "React")
        self.assertTrue(ok)
        rec = envmod.read_record("ws", "React")
        self.assertTrue(envmod._same_path(rec["cwd"], self.mirror))
        self.assertEqual(rec["target"], "pack")
        self.assertIsNone(self.binding(self.mirror))

    def test_up_without_target_or_with_base_is_unchanged(self):
        for extra in ([], ["--target", "homolog"]):
            rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r1", *extra)
            self.assertEqual(rc, 0, out)
            rec = envmod.read_record("ws", "React")
            self.assertTrue(envmod._same_path(rec["cwd"], self.base))
            self.assertNotIn("target", rec)
            self.assertIsNone(self.binding(self.base))
            self.assertIn("serves", self.binding(self.mirror))
            self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_main))
            self.assertEqual(quiet_cli("env", "down", "ws", "React", "--run", "r1")[0], 0)
            envmod.down(self.ws, "API", force=True)

    def switch_to_base(self):
        """Prove preflight for the base target while a mirror env runs; returns the output lines."""
        from factory.commands import prove
        rep, stopped = prove.Reporter(False), {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            prove._preflight_envs(core.run_load("r1"), self.ws, ["React", "API"], rep, stopped, target="homolog")
        return rep, stopped, out.getvalue()

    def test_prove_preflight_switches_mirror_env_and_its_target_paths_dep_to_base(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_pack))
        rep, stopped, out = self.switch_to_base()
        self.assertEqual(stopped, {}, out)
        self.assertFalse(rep.env_failed, out)
        for alias, want in (("React", self.base), ("API", self.api_main)):
            rec = envmod.read_record("ws", alias)
            self.assertTrue(envmod._same_path(rec["cwd"], want), (alias, rec))
            self.assertNotIn("target", rec)

    def test_prove_preflight_restarts_to_the_mirror_target(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1")
        from factory.commands import prove
        rep, stopped = prove.Reporter(False), {}
        with contextlib.redirect_stdout(io.StringIO()):
            prove._preflight_envs(core.run_load("r1"), self.ws, ["React", "API"], rep, stopped, target="pack")
        self.assertEqual(stopped, {})
        rec = envmod.read_record("ws", "React")
        self.assertTrue(envmod._same_path(rec["cwd"], self.mirror))
        self.assertEqual(rec["target"], "pack")
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_pack))

    def test_restart_blocker_allows_a_mirror_dependency_but_not_a_foreign_cwd(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        run = core.run_load("r1")
        self.assertIsNone(envmod.restart_blocker(self.ws, run, "API"))
        rec = envmod.read_record("ws", "API")
        rec["cwd"] = str(self.tmp)  # a checkout that is none of the run's targets: someone else's
        envmod.pid_path("ws", "API", 0).write_text(json.dumps(rec), encoding="utf-8")
        self.assertIn("env serves", envmod.restart_blocker(self.ws, run, "API") or "")

    def test_down_target_stops_the_mirror_deps_so_up_can_switch(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        rc, out = quiet_cli("env", "down", "ws", "React", "--run", "r1", "--target", "pack")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(envmod.read_record("ws", "React"))
        self.assertIsNone(envmod.read_record("ws", "API"))
        rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_main))

    def test_down_target_leaves_a_dep_started_for_another_target_or_without_one(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1")  # base: no target on any record
        rc, out = quiet_cli("env", "down", "ws", "React", "--run", "r1", "--target", "pack")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(envmod.read_record("ws", "React"))
        self.assertIsNotNone(envmod.read_record("ws", "API"))

    def test_up_another_target_while_running_is_refused_until_down(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r1")
        self.assertEqual(rc, 1, out)
        self.assertIn("kind=stale", out)

    def test_unknown_target_and_non_hotfix_run_are_usage_errors(self):
        rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "nope")
        self.assertEqual(rc, 2, out)
        self.make_run("r2", repos={"React": {"worktree": str(self.base), "branch": "b"}})
        rc, out = quiet_cli("env", "check", "ws", "React", "--run", "r2", "--target", "pack")
        self.assertEqual(rc, 2, out)
        rc, out = quiet_cli("env", "up", "ws", "React", "--target", "pack")
        self.assertEqual(rc, 2, out)


    # --- review fixes: restart order, ownership of a record, check --auto-restart, down --target ---

    def preflight(self, run, aliases, target=None):
        from factory.commands import prove
        rep, stopped = prove.Reporter(False), {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            prove._preflight_envs(run, self.ws, aliases, rep, stopped, target=target)
        return rep, stopped, out.getvalue()

    def set_record(self, alias, **changes):
        rec = envmod.read_record("ws", alias)
        for k, v in changes.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v
        envmod.pid_path("ws", alias, 0).write_text(json.dumps(rec), encoding="utf-8")

    def test_prove_preflight_orders_restarts_by_depends_on_not_alphabet(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1")  # base target: React, and API from the main checkout
        aliases = sorted(["React", "API"])  # what prove passes: alphabetical, so the dependent is not last by design
        rep, stopped, out = self.preflight(core.run_load("r1"), aliases, target="pack")
        self.assertEqual(stopped, {}, out)
        self.assertFalse(rep.env_failed, out)
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "React")["cwd"], self.mirror))
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_pack))
        self.assertLess(out.index("env API: stale"), out.index("env React: stale"))

    def test_non_hotfix_preflight_restarts_the_dependency_before_its_dependent(self):
        from ops_helpers import git
        self.make_run("r2", repos={"React": {"worktree": str(self.base), "branch": "b"},
                                   "API": {"worktree": str(self.api_main), "branch": "b"}})
        rc, out = quiet_cli("env", "up", "ws", "React", "--run", "r2")
        self.assertEqual(rc, 0, out)
        for repo in (self.base, self.api_main):
            git(repo, "commit", "--allow-empty", "-qm", "moved")
        rep, stopped, out = self.preflight(core.run_load("r2"), sorted(["React", "API"]))
        self.assertEqual(stopped, {}, out)
        self.assertFalse(rep.env_failed, out)
        for alias, repo in (("React", self.base), ("API", self.api_main)):
            self.assertEqual(envmod.read_record("ws", alias)["head"], self.head(repo), alias)

    def test_deps_first_orders_a_dependent_after_its_dependency(self):
        self.assertEqual(envmod.deps_first(self.ws, ["React", "API"]), ["API", "React"])
        self.assertEqual(envmod.deps_first(self.ws, ["API", "React"]), ["API", "React"])

    def test_record_carries_the_run_that_started_it(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1")
        self.assertEqual(envmod.read_record("ws", "React")["run"], "r1")
        self.assertEqual(envmod.read_record("ws", "API")["run"], "r1")
        envmod.down(self.ws, "React", "r1")
        envmod.down(self.ws, "API", "r1")
        ok, _ = envmod.up(self.ws, "API")
        self.assertTrue(ok)
        self.assertNotIn("run", envmod.read_record("ws", "API"))

    def test_hotfix_run_never_takes_over_an_env_started_by_another_run_or_by_hand(self):
        self.make_run("rA", repos={"React": {"worktree": str(self.base), "branch": "b"}})
        for starter in (None, core.run_load("rA")):
            ok, _ = envmod.up(self.ws, "API", starter)  # served from the main checkout, which r1 resolves to as well
            self.assertTrue(ok)
            run = core.run_load("r1")
            self.assertTrue(envmod.stale_reason(self.ws, run, "API", target="pack"))
            blocked = envmod.restart_blocker(self.ws, run, "API")
            self.assertIn("env serves", blocked or "")
            _, stopped, _ = self.preflight(run, ["React", "API"], target="pack")
            self.assertIn("env not restarted", stopped.get("API", ""))
            self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_main))
            envmod.down(self.ws, "API", force=True)

    def test_a_record_without_run_id_in_a_target_path_is_not_the_runs_own(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        run = core.run_load("r1")
        self.assertIsNone(envmod.restart_blocker(self.ws, run, "API"))
        self.set_record("API", run=None)  # an old record, or a manual start
        self.assertIn("env serves", envmod.restart_blocker(self.ws, run, "API") or "")
        self.set_record("API", run="someone-else")
        self.assertIn("env serves", envmod.restart_blocker(self.ws, run, "API") or "")

    def test_dependency_run_worktree_is_not_the_runs_own_unless_it_started_it(self):
        dep_api = make_repo(self.tmp / "api-dep-run").resolve()
        self.make_run("dep", tickets=[{"key": "DEV-2", "type": "bug", "summary": "s", "area": "a"}],
                      repos={"API": {"worktree": str(dep_api), "branch": "b"}})
        self.make_run("child", parent="epic", targets=["homolog", "pack"], hotfix={"base": "homolog", "mirror": ["pack"]},
                      tickets=[{"key": "DEV-1", "type": "bug", "summary": "s", "area": "a", "depends_on": ["DEV-2"]}],
                      repos={"React": {"worktree": str(self.base), "branch": "b", "worktrees": {"pack": str(self.mirror)}}})
        run = core.run_load("child")
        self.assertTrue(envmod._same_path(envmod._cwd_for(self.ws, run, "API"), dep_api))  # the dependency run's worktree
        rec = {"pid": 1, "cwd": str(dep_api), "head": "x", "started": "x", "create_time": None, "run": "dep"}
        pid = envmod.pid_path("ws", "API", 0)
        pid.parent.mkdir(parents=True, exist_ok=True)
        pid.write_text(json.dumps(rec), encoding="utf-8")
        self.addCleanup(lambda: pid.unlink(missing_ok=True))  # a fake pid must never reach stop_all
        self.assertFalse(envmod._started_for_target(self.ws, run, "API", rec))
        self.assertTrue(envmod._serves_a_target_of(self.ws, run, "API", rec))
        self.assertIn("env serves", envmod.restart_blocker(self.ws, run, "API") or "")
        self.assertTrue(envmod._started_for_target(self.ws, run, "API", {**rec, "run": "child"}))

    def test_check_auto_restart_with_target_restarts_the_stale_dependencies_first(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        rc, out = quiet_cli("env", "check", "ws", "React", "--run", "r1", "--target", "homolog", "--auto-restart")
        self.assertEqual(rc, 0, out)
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "React")["cwd"], self.base))
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_main))
        self.assertLess(out.index("API: stale"), out.index("React: stale"))

    def test_check_auto_restart_leaves_a_dependency_it_did_not_start(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        self.set_record("API", run=None)
        rc, out = quiet_cli("env", "check", "ws", "React", "--run", "r1", "--target", "homolog", "--auto-restart")
        self.assertEqual(rc, 3, out)
        self.assertIn("API: NOT restarted", out)
        self.assertIn("React: NOT restarted", out)
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "API")["cwd"], self.api_pack))
        self.assertTrue(envmod._same_path(envmod.read_record("ws", "React")["cwd"], self.mirror))

    def test_down_target_leaves_a_dep_another_run_started_in_the_same_target_path(self):
        quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")
        for owner in ("run-b", None):
            self.set_record("API", run=owner)
            rc, out = quiet_cli("env", "down", "ws", "React", "--run", "r1", "--target", "pack")
            self.assertEqual(rc, 0, out)
            self.assertIn(f"API: left running (run {owner or 'none'})", out)
            self.assertIsNotNone(envmod.read_record("ws", "API"))
            quiet_cli("env", "up", "ws", "React", "--run", "r1", "--target", "pack")


class WorkspaceCheckTests(unittest.TestCase):
    WS = {"ship": {"targets": ["pack", "homolog"], "hotfix": {"base": "homolog", "mirror": ["pack"]}}}

    def errors(self, tp, **extra):
        from factory.commands.workspace import target_paths_errors
        return target_paths_errors({**self.WS, **extra, "repos": [{"alias": "A", "target_paths": tp}, {"alias": "B"}]})

    def test_valid_mirror_path_passes(self):
        self.assertEqual(self.errors({"pack": str(Path(sys.executable).resolve().parent)}), [])

    def test_target_paths_must_be_an_object_of_strings(self):
        self.assertEqual(len(self.errors(["pack"])), 1)
        self.assertEqual(len(self.errors({"pack": ""})), 1)

    def test_unknown_key_is_rejected(self):
        errs = self.errors({"paxk": str(Path(sys.executable).resolve())})
        self.assertEqual(len(errs), 1)
        self.assertIn("not a known target", errs[0])

    def test_hotfix_base_key_is_rejected(self):
        errs = self.errors({"homolog": str(Path(sys.executable).resolve())})
        self.assertEqual(len(errs), 1)
        self.assertIn("base", errs[0])

    def test_relative_path_is_rejected(self):
        errs = self.errors({"pack": "../api-pack"})
        self.assertEqual(len(errs), 1)
        self.assertIn("absolute", errs[0])


class ProveTargetTests(HotfixCase):
    def ev(self, name):
        return (core.run_dir(self.rid) / "evidence" / f"{name}.json").exists()

    def test_prove_runs_only_the_selected_target(self):
        self.through_build()
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        rc, out = cli("prove", self.rid, "--target", "pack")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.ev("CHK-1@pack") and self.ev("CHK-2@pack"))
        self.assertFalse(self.ev("CHK-1@homolog"))
        self.assertFalse(self.ev("QUALITY-React"))
        rc, out = cli("gate", self.rid, "prove")
        self.assertEqual(rc, 1, out)  # the base target is still unproven
        self.ok("prove", self.rid, "--target", "homolog")
        self.assertTrue(self.ev("CHK-1@homolog"))
        self.ok("gate", self.rid, "prove")

    def test_quality_or_rule_check_with_a_mirror_target_is_a_usage_error(self):
        self.through_build()
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        self.ws["repos"][0]["quality"] = {"lint": "x"}
        self.ws["rules"] = [{"id": "lint", "text": "t", "check": {"type": "test", "repo": "React", "cmd": "x"}}]
        self.save_ws()
        for check in ("QUALITY-React", "RULE-lint-React"):
            rc, out = cli("prove", self.rid, "--check", check, "--target", "pack")
            self.assertEqual(rc, 2, out)
            self.assertIn("base target", out)

    def test_estimate_honours_target_and_unknown_target_is_usage(self):
        self.through_build()
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        rc, out = cli("--json", "prove", self.rid, "--estimate", "--target", "pack")
        self.assertEqual(rc, 0, out)
        ids = [c["check_id"] for c in json.loads(out)["checks"]]
        self.assertEqual(sorted(ids), ["CHK-1@pack", "CHK-2@pack"])
        rc, out = cli("--json", "prove", self.rid, "--estimate")
        self.assertIn("CHK-1@homolog", out)
        self.assertIn("CHK-1@pack", out)
        rc, out = cli("prove", self.rid, "--target", "nope")
        self.assertEqual(rc, 2, out)


if __name__ == "__main__":
    unittest.main()
