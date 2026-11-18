"""Evidence reuse across repos, scoped quality, flaky classification, seed/reset/preflight (DESIGN section 14)."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import git, make_repo  # noqa: E402
from test_failfast_env import EnvCase, Redirector, cli as env_cli  # noqa: E402
from test_failfast_prove import ProveCase, cli  # noqa: E402

from factory import core  # noqa: E402
from factory.commands import gate, prove, workspace  # noqa: E402

PY = sys.executable


class TwoRepoCase(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)
        self.api = make_repo(self.tmp / "api").resolve()
        self.ws["repos"].append({"alias": "Api", "name": "api", "path": str(self.api)})
        self.write("workspaces/ws/workspace.json", self.ws)
        self.make_run("r1", phase="prove", repos={"Web": {"worktree": str(self.web), "branch": "b"},
                                                  "Api": {"worktree": str(self.api), "branch": "b"}})
        self.chk = {"id": "B1", "type": "browser", "repo": "Web", "spec": "x"}
        self.acs([self.chk])

    def prove_with(self, *args):
        with mock.patch.object(prove, "dispatch", return_value={"status": "pass", "summary": "ok"}) as d:
            rc, out, _ = cli("prove", "r1", *args)
        return rc, out, d.call_count

    def commit(self, repo):
        (repo / "n.txt").write_text(str(len(list(repo.iterdir()))))
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "more")


class RunHeadsTests(TwoRepoCase):
    def test_runtime_evidence_records_the_heads_of_every_run_repo(self):
        self.assertEqual(self.prove_with()[0], 0)
        heads = self.evidence("B1")["details"]["run_heads"]
        self.assertEqual(heads, {"Web": core.git_head(self.web), "Api": core.git_head(self.api)})

    def test_unchanged_heads_are_reused(self):
        self.prove_with()
        rc, out, calls = self.prove_with()
        self.assertEqual((rc, calls), (0, 0))
        self.assertIn("REUSED B1", out)

    def test_commit_in_another_repo_reruns_a_runtime_check(self):
        self.prove_with()
        self.commit(self.api)
        rc, out, calls = self.prove_with()
        self.assertEqual(calls, 1)
        self.assertNotIn("REUSED", out)

    def test_evidence_without_run_heads_is_not_reused(self):
        self.prove_with()
        path = self.home / "runs/r1/evidence/B1.json"
        ev = self.evidence("B1")
        del ev["details"]["run_heads"]
        path.write_text(json.dumps(ev), encoding="utf-8")
        core.run_update("r1", lambda r: core.evidence_event(r, ev))
        self.assertEqual(self.prove_with()[2], 1)

    def test_gate_refuses_runtime_evidence_with_stale_run_heads(self):
        self.prove_with("--final")
        run, ws = core.run_load("r1"), core.workspace_load("ws")
        self.assertEqual(gate._evidence_errors("r1", run, ws, self.chk), [])
        self.commit(self.api)
        errors = gate._evidence_errors("r1", core.run_load("r1"), ws, self.chk)
        self.assertTrue(any("run_heads" in e and "Api" in e for e in errors), errors)

    def test_old_evidence_without_run_heads_keeps_today_behaviour(self):
        self.prove_with("--final")
        ev = self.evidence("B1")
        del ev["details"]["run_heads"]
        (self.home / "runs/r1/evidence/B1.json").write_text(json.dumps(ev), encoding="utf-8")
        core.run_update("r1", lambda r: core.evidence_event(r, ev))
        self.commit(self.api)
        run = core.run_load("r1")
        self.assertIsNone(__import__("factory.integrity", fromlist=["x"]).evidence_error(run, "B1"))

    def test_non_runtime_evidence_does_not_record_or_need_run_heads(self):
        self.acs([{"id": "T1", "type": "test", "repo": "Web", "cmd": "true"}])
        cli("prove", "r1")
        self.assertNotIn("run_heads", self.evidence("T1")["details"])


class QualityFastTests(ProveCase):
    FAST = f'"{PY}" -c "import sys;open(\'fast.txt\',\'w\').write(\' \'.join(sys.argv[1:]))" {{changed}}'
    FULL = f'"{PY}" -c "open(\'full.txt\',\'w\').write(\'1\')"'

    def setUp(self):
        super().setUp()
        self.base = core.git_head(self.web)
        self.write("runs/r1/baseline/Web.json", {"schemaVersion": 1, "repo": "Web", "merge_base": self.base,
                                                  "commands": {"lint": {"failures": []}}})

    def conf(self, **extra):
        self.ws["repos"][0].update(quality={"lint": self.FULL}, **extra)
        self.write("workspaces/ws/workspace.json", self.ws)

    def quality(self, mode):
        return prove.quality_delta(core.run_load("r1"), core.workspace_load("ws"), "Web", mode=mode)

    def test_changed_files_are_the_committed_existing_files(self):
        (self.web / "b b.txt").write_text("2")
        (self.web / "gone.txt").write_text("2")
        git(self.web, "add", ".")
        git(self.web, "commit", "-qm", "c2")
        git(self.web, "rm", "-q", "gone.txt")
        git(self.web, "commit", "-qm", "c3")
        self.assertEqual(prove.changed_files(self.web, self.base), ["b b.txt"])

    def test_fast_mode_uses_quality_fast_with_the_changed_files(self):
        self.conf(quality_fast={"lint": self.FAST})
        (self.web / "b.txt").write_text("2")
        git(self.web, "add", ".")
        git(self.web, "commit", "-qm", "c2")
        rec = self.quality("fast")
        self.assertEqual(rec["status"], "pass", rec)
        self.assertEqual((self.web / "fast.txt").read_text(), "b.txt")
        self.assertFalse((self.web / "full.txt").exists())
        self.assertEqual(rec["details"]["mode"], "fast")

    def test_default_and_final_keep_the_full_quality(self):
        self.conf(quality_fast={"lint": self.FAST})
        for mode in ("default", "final"):
            (self.web / "full.txt").unlink(missing_ok=True)
            self.quality(mode)
            self.assertTrue((self.web / "full.txt").exists(), mode)
            self.assertFalse((self.web / "fast.txt").exists(), mode)

    def test_no_changed_files_skips_as_pass(self):
        self.conf(quality_fast={"lint": self.FAST})
        rec = self.quality("fast")
        self.assertEqual(rec["status"], "pass")
        self.assertIn("lint: no changed files", rec["summary"])
        self.assertFalse((self.web / "fast.txt").exists())

    def test_fast_without_quality_fast_runs_the_full_quality(self):
        self.conf()
        self.quality("fast")
        self.assertTrue((self.web / "full.txt").exists())

    def test_workspace_check_validates_quality_fast(self):
        ws = {"worktree_root": "x", "repos": [{"alias": "A", "path": "x", "quality": {"a": "b"}, "quality_fast": ["lint"],
                                               "env": {"start": "s", "health": "h", "base_url": "b"}}]}
        self.assertTrue(any("quality_fast" in e for e in workspace.check_workspace(ws)))
        ws["repos"][0]["quality_fast"] = {"lint": "eslint {changed}"}
        self.assertFalse(any("quality_fast" in e for e in workspace.check_workspace(ws)))


class FlakyTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)
        self.acs([{"id": "B1", "type": "browser", "repo": "Web", "spec": "x"}])

    def run_prove(self, results, *args):
        with mock.patch.object(prove, "dispatch", side_effect=results) as d:
            rc, out, _ = cli("prove", "r1", *args)
        return rc, out, d.call_count

    def test_fail_then_pass_is_flaky_and_never_a_pass(self):
        rc, out, calls = self.run_prove([{"status": "fail", "summary": "expect(x).toBe(1)"}, {"status": "pass", "summary": "ok"}])
        self.assertEqual((rc, calls), (1, 2), out)
        ev = self.evidence("B1")
        self.assertEqual(ev["status"], "fail")
        self.assertTrue(ev["details"]["flaky"])
        self.assertTrue(ev["summary"].startswith("FLAKY: expect(x)"), ev["summary"])
        self.assertIn("PROVE-FAIL check=B1 kind=flaky", out)

    def test_fail_twice_is_a_normal_fail(self):
        rc, out, calls = self.run_prove([{"status": "fail", "summary": "expect(x).toBe(1)"}] * 2)
        self.assertEqual((rc, calls), (1, 2))
        self.assertNotIn("flaky", self.evidence("B1")["details"])
        self.assertIn("kind=assert", out)

    def test_no_retry_runs_once(self):
        rc, out, calls = self.run_prove([{"status": "fail", "summary": "expect(x).toBe(1)"}], "--no-retry")
        self.assertEqual((rc, calls), (1, 1))

    def test_setup_and_timeout_failures_are_not_retried(self):
        for body in ({"status": "error", "summary": "spec not found"},
                     {"status": "fail", "summary": "browser CLI timed out after 120s", "details": {"timeout": True}}):
            rc, out, calls = self.run_prove([body, {"status": "pass"}])
            self.assertEqual(calls, 1, body)

    def test_a_clean_pass_is_not_retried(self):
        rc, out, calls = self.run_prove([{"status": "pass", "summary": "ok"}])
        self.assertEqual((rc, calls), (0, 1))


class ResetBeforeProveTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.marker = self.tmp / "reset.log"

    def seed(self, code=0):
        script = f"import os,sys;open(r'{self.marker}','a').write(os.environ['FACTORY_DATA_PREFIX']+chr(10));sys.exit({code})"
        self.set_ws(self.live, seed={"cmd": [PY, "-c", "pass"], "reset": [PY, "-c", script]})
        self.acs([self.api_check("A1"), self.api_check("A2")])

    def test_reset_runs_once_before_the_runtime_batch(self):
        self.seed()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.marker.read_text().split(), ["UAT1"])
        events = [e["data"] for e in core.run_load("r1")["events"] if e["kind"] == "env_reset"]
        self.assertEqual(events, [{"repo": "Web", "exit": 0}])

    def test_failing_reset_is_an_env_failure_and_stops_the_batch(self):
        self.seed(code=5)
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 3, out)
        self.assertIn("ENV-FAIL repo=Web kind=seed", out)
        self.assertTrue(self.evidence("A1")["summary"].startswith("skipped: env"))

    def test_single_check_runs_do_not_reset(self):
        self.seed()
        cli("prove", "r1", "--check", "A1")
        self.assertFalse(self.marker.exists())

    def test_reused_runtime_checks_do_not_reset(self):
        self.seed()
        cli("prove", "r1")
        self.marker.unlink()
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.marker.exists())


class SeedCommandTests(EnvCase):
    def setUp(self):
        super().setUp()
        out = self.web / "out.txt"
        script = ("import os;open('out.txt','w').write(os.environ['FACTORY_DATA_PREFIX']+'|'+os.environ['FACTORY_TICKET']"
                  "+'|'+os.environ['FACTORY_RUN'])")
        self.seed = {"cmd": [PY, "-c", script], "reset": [PY, "-c", "import sys;sys.exit(4)"]}
        self.rebuild_ws(seed=self.seed)
        self.make_run("r1", phase="prove", repos={"Web": {"worktree": str(self.web), "branch": "b"}},
                      tickets=[{"key": "DEV-9451", "type": "feature", "summary": "s", "area": "a"}])
        self.out = out

    def events(self, kind):
        return [e["data"] for e in core.run_load("r1")["events"] if e["kind"] == kind]

    def test_seed_runs_cmd_with_the_data_env(self):
        rc, out = env_cli("env", "seed", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.out.read_text(), "UAT9451|DEV-9451|r1")
        self.assertEqual(self.events("env_seed"), [{"repo": "Web", "exit": 0}])

    def test_reset_data_failure_is_env_fail_seed(self):
        rc, out = env_cli("env", "reset-data", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 1, out)
        self.assertIn("ENV-FAIL repo=Web kind=seed", out)
        self.assertEqual(self.events("env_reset"), [{"repo": "Web", "exit": 4}])

    def test_not_configured_is_env_fail_seed(self):
        self.rebuild_ws(seed=None)
        rc, out = env_cli("env", "seed", "ws", "Web", "--run", "r1")
        self.assertEqual(rc, 1, out)
        self.assertIn("ENV-FAIL repo=Web kind=seed", out)

    def test_cwd_is_relative_to_the_worktree(self):
        (self.web / "sub").mkdir()
        self.rebuild_ws(seed={"cmd": [PY, "-c", "open('here.txt','w').write('1')"], "cwd": "sub"})
        self.assertEqual(env_cli("env", "seed", "ws", "Web", "--run", "r1")[0], 0)
        self.assertTrue((self.web / "sub" / "here.txt").exists())

    def test_preflight_failure_fails_env_check(self):
        self.serve(Redirector)
        self.rebuild_ws(preflight=[{"name": "ok", "cmd": [PY, "-c", "pass"]},
                                   {"name": "trigger", "cmd": [PY, "-c", "import sys;sys.exit(3)"]}])
        rc, out = env_cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 1, out)
        self.assertIn("ENV-FAIL repo=Web kind=preflight detail=trigger", out)
        self.assertNotIn("detail=ok", out)

    def test_passing_preflight_keeps_env_check_green(self):
        self.serve(Redirector)
        self.rebuild_ws(preflight=[{"name": "ok", "cmd": [PY, "-c", "pass"]}])
        rc, out = env_cli("env", "check", "ws", "Web")
        self.assertEqual(rc, 0, out)

    def test_workspace_check_validates_seed_and_preflight(self):
        def errors(**env_over):
            env = {"start": "s", "health": "h", "base_url": "b", **env_over}
            ws = {"worktree_root": "x", "repos": [{"alias": "A", "path": "x", "quality": {"a": "b"}, "env": env}]}
            return [e for e in workspace.check_workspace(ws) if "seed" in e or "preflight" in e]

        self.assertEqual(errors(seed=None), [])
        self.assertEqual(errors(seed={"cmd": ["a"], "cwd": ".", "reset": ["b"]}, preflight=[{"name": "n", "cmd": ["c"]}]), [])
        self.assertTrue(errors(seed={"cmd": "a b"}))
        self.assertTrue(errors(seed="x"))
        self.assertTrue(errors(preflight=[{"cmd": ["c"]}]))
        self.assertTrue(errors(preflight={"name": "n"}))


if __name__ == "__main__":
    unittest.main()
