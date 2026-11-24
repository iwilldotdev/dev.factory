"""Browser, screenshot and login checks through `verify` with the fake runner; data, warm-up, reproduce serving and the UAT hint."""
import json
import os
import unittest
from unittest import mock

from tests.helpers import PY, lesson
from tests.m4_support import RUNNER, M4Case

SPEC_OK = {"actions": [{"goto": "/index.html"}]}


class UiCase(M4Case):
    def spec(self, run, name, body=None):
        path = run / "checks" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body if body is not None else SPEC_OK), encoding="utf-8")
        return f"checks/{name}.json"

    def browser_plan(self, run, *specs, extra_checks=(), **top):
        checks = [{"id": f"CHK-{i}", "type": "browser", "repo": "web", "ac": ["AC-1"], "spec": s} for i, s in enumerate(specs, 1)]
        return {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [{"id": "AC-1", "text": "The page works", "source": "ticket:description"}],
                "checks": checks + list(extra_checks), **top}

    def locked(self, *bodies, extra_checks=(), **top):
        run = self.branch()
        self.up()
        specs = [self.spec(run, f"s{i}", b) for i, b in enumerate(bodies or [None], 1)]
        self.write_plan(run, self.browser_plan(run, *specs, extra_checks=extra_checks, **top))
        self.ok("gate", "plan")
        self.fix()
        return run

    def verify(self, *flags):
        got, out, _ = self.cli("verify", "--json", *flags)
        return got, json.loads(out)


class BrowserCheckTests(UiCase):
    @lesson("L-57")
    def test_a_repos_browser_checks_run_in_one_runner_process_and_one_failure_does_not_stop_the_others(self):
        """L-57: the runner `batch` runs a repo's browser, visual and style checks in one browser, one result per entry."""
        shot = {"id": "CHK-9", "type": "screenshot", "repo": "web", "url": "/index.html"}
        run = self.locked(SPEC_OK, {**SPEC_OK, "exit": 1, "summary": "expected #t to be visible"}, SPEC_OK, extra_checks=[shot])
        self.runner_log.unlink(missing_ok=True)
        code, out = self.verify("--no-retry")
        self.assertEqual(code, 1)
        calls = self.runner_calls()
        self.assertEqual([c["cmd"] for c in calls], ["batch"])
        self.assertEqual(sorted(calls[0]["entries"]), ["CHK-1", "CHK-2", "CHK-3", "CHK-9"])
        by = {r["check"]: r["result"] for r in out["results"]}
        self.assertEqual(by, {"CHK-1": "pass", "CHK-2": "fail", "CHK-3": "pass", "CHK-9": "pass"})
        rec = [e for e in self.evidence(run) if e["check"] == "CHK-2"][0]
        self.assertEqual((rec["kind"], rec["reason"]), ("assert", "expected #t to be visible"))
        self.assertTrue(any(a.endswith("shot.png") for a in [e for e in self.evidence(run) if e["check"] == "CHK-9"][0]["artifacts"]))

    @lesson("L-14")
    def test_a_browser_assertion_that_passes_on_retry_is_flaky_and_never_a_clean_pass(self):
        """L-14: a retry that passes is recorded as flaky with both attempts, and `gate verify` does not accept it."""
        marker = self.base / "once.marker"
        run = self.locked({**SPEC_OK, "fail_once": str(marker)})
        marker.unlink()  # the plan-time dry run consumed it
        code, out = self.verify()
        recs = [e for e in self.evidence(run) if e["check"] == "CHK-1"]
        self.assertEqual([r["result"] for r in recs], ["fail", "flaky"])
        self.assertEqual(recs[1]["retry_of"], 1)
        self.assertNotEqual(self.gate("verify")[0], 0)
        self.assertEqual(out["results"][0]["result"], "flaky")
        self.assertEqual(self.verify("--fresh")[0], 0)
        self.assertEqual(self.gate("verify")[0], 0)

    @lesson("L-59")
    def test_the_assertion_timeout_is_per_check_config_with_a_default_and_part_of_the_spec_hash(self):
        """L-59: expect_timeout_ms defaults from [checks], can be set per check, reaches the runner and is hashed into the spec."""
        run = self.locked()
        self.ok("verify")
        self.assertEqual([c["expect_ms"] for c in self.runner_calls() if c["cmd"] == "spec"][-1], "10000")
        plan = self.read_plan(run)
        before = [e for e in self.events(run) if e["type"] == "plan_locked"][-1]["spec_hashes"]["CHK-1"]
        plan["checks"][0]["expect_timeout_ms"] = 25000
        self.write_plan(run, plan)
        self.assertIn("plan changed after lock", "\n".join(self.gate("verify")[1]))
        self.ok("plan", "amend", "--reason", "a slow remote database")
        after = [e for e in self.events(run) if e["type"] == "plan_locked"][-1]["spec_hashes"]["CHK-1"]
        self.assertNotEqual(before, after)
        self.ok("verify")
        call = [c for c in self.runner_calls() if c["cmd"] == "spec"][-1]
        self.assertEqual((call["expect_ms"], call["argv"][call["argv"].index("--expect-timeout-ms") + 1]), ("25000", "25000"))

    def test_a_project_runner_template_runs_browser_specs_and_its_exit_code_decides(self):
        script = self.write("own_runner.py", "import sys\nprint('own runner saw', sys.argv[1])\nsys.exit(int(open(sys.argv[1]).read().strip() or 0))\n", root=self.base)
        self.configure(extra=f'\n[checks.browser]\nrunner = {json.dumps(chr(34) + PY + chr(34) + " " + chr(34) + script.as_posix() + chr(34) + " {spec} {base_url} {out}")}\n')
        run = self.branch()
        self.up()
        for name, body in (("pass", "0"), ("fail", "1")):
            (run / "checks").mkdir(exist_ok=True)
            (run / "checks" / f"{name}.txt").write_text(body, encoding="utf-8")
        plan = self.browser_plan(run, "checks/pass.txt")
        plan["checks"].append({"id": "CHK-2", "type": "browser", "repo": "web", "ac": ["AC-1"], "spec": "checks/fail.txt"})
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.fix()
        code, out = self.verify("--no-retry")
        self.assertEqual({r["check"]: r["result"] for r in out["results"]}, {"CHK-1": "pass", "CHK-2": "fail"})
        self.assertEqual(self.runner_calls(), [])  # the bundled runner was not used
        self.assertIn("own runner saw", (run / self.evidence(run)[0]["artifacts"][0]).read_text(encoding="utf-8"))

    @lesson("L-49")
    def test_a_browser_failure_matching_env_patterns_is_an_env_failure(self):
        """L-49: env_patterns also classify browser failure output."""
        self.configure(extra_env='env_patterns = ["SSL Authentication Error"]\n')
        run = self.locked({**SPEC_OK, "summary": "page said SSL Authentication Error"})
        self.results({"spec": {"exit": 1, "summary": "page said SSL Authentication Error"}})
        code, out = self.verify("--no-retry")
        self.assertEqual((code, out["results"][0]["result"]), (3, "env"))
        self.assertTrue(run.exists())


class LoginTests(UiCase):
    def setUp(self):
        super().setUp()
        for k, v in {"TEST_USER": "base-user", "TEST_PASSWORD": "s3cret-pass", "TEST_USER_QA_ADMIN": "admin-user", "TEST_PASSWORD_QA_ADMIN": "admin-pass"}.items():
            patcher = mock.patch.dict(os.environ, {k: v})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.commit("login.json", json.dumps({"actions": [{"goto": "/login.html"}]}), "chore: login spec", tree=self.proj)
        self.configure(extra_env='login_spec = "login.json"\nlogin_secrets = ["TEST_USER", "TEST_PASSWORD"]\n',
                       extra='\n[secrets]\nkeys = ["TEST_USER", "TEST_PASSWORD"]\n')

    @lesson("L-80")
    def test_credentials_reach_the_login_spec_only_as_environment_variables_and_the_state_is_reused_by_checks(self):
        """L-80: login credentials come only from the environment; the runner's `login` writes the state that browser checks reuse."""
        run = self.locked()
        login = [c for c in self.runner_calls() if c["cmd"] == "login"][0]
        self.assertEqual((login["user"], login["pass"]), ("base-user", "s3cret-pass"))
        self.assertNotIn("s3cret-pass", json.dumps(login["argv"]))
        state = self.proj / ".factory" / "env" / "web.state.json"
        self.assertTrue(state.is_file())
        self.ok("verify")
        spec = [c for c in self.runner_calls() if c["cmd"] == "spec"][-1]
        self.assertEqual(spec["argv"][spec["argv"].index("--storage-state") + 1], state.as_posix())
        blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in run.rglob("*") if p.is_file())
        self.assertNotIn("s3cret-pass", blob)

    @lesson("L-81")
    def test_a_plan_login_user_selects_the_suffixed_secrets_and_its_own_state_file(self):
        """L-81: plan `login_user` selects the `<KEY>_<slug>` secrets and a login state file per user."""
        self.configure(extra_env='login_spec = "login.json"\nlogin_secrets = ["TEST_USER", "TEST_PASSWORD"]\n',
                       extra='\n[secrets]\nkeys = ["TEST_USER", "TEST_PASSWORD"]\n')
        run = self.locked(login_user="qa-admin")
        self.ok("verify")
        logins = [c for c in self.runner_calls() if c["cmd"] == "login"]
        self.assertEqual([(c["user"], c["pass"]) for c in logins][-1], ("admin-user", "admin-pass"))
        state = self.proj / ".factory" / "env" / "web.qa-admin.state.json"
        self.assertTrue(state.is_file())
        spec = [c for c in self.runner_calls() if c["cmd"] == "spec"][-1]
        self.assertEqual(spec["argv"][spec["argv"].index("--storage-state") + 1], state.as_posix())
        self.assertTrue(run.exists())

    @lesson("L-79")
    def test_a_login_that_fails_stops_env_up_with_kind_login(self):
        """L-79: the login spec gets the served base URL, and a failed login is an env failure of kind login."""
        run = self.branch()
        self.results({"login": {"exit": 3, "status": "error", "summary": "login ended on http://127.0.0.2:9, not the served origin"}})
        out = self.fails("env", "up", "web", code=3)
        self.assertIn("(login)", out)
        self.assertIn("not the served origin", out)
        login = [c for c in self.runner_calls() if c["cmd"] == "login"][0]
        self.assertEqual(login["argv"][login["argv"].index("--base-url") + 1], f"http://127.0.0.1:{self.port}")
        self.assertEqual([e["kind"] for e in self.events(run) if e["type"] == "env" and e["action"] == "fail"], ["login"])


class DataTests(UiCase):
    @lesson("L-40")
    def test_warmup_paths_are_requested_once_and_a_failure_is_kind_warmup(self):
        """L-40: [repo.env] warmup paths are requested once after up; a failure is env kind warmup, not a product failure."""
        self.configure(extra_env='warmup = ["/health", "/boom"]\n')
        self.branch()
        out = self.fails("env", "up", "web", code=3)
        self.assertIn("(warmup)", out)
        self.assertIn("/boom: status 500", out)
        self.configure(extra_env='warmup = ["/health", "/whoami"]\n')
        self.up()
        self.assertIn("/whoami", self.ok("env", "logs", "web"))

    @lesson("L-50")
    def test_preflight_failure_is_kind_data_and_reset_runs_once_with_the_run_data_prefix_before_the_runtime_batch(self):
        """L-50: preflight runs after health (kind data on failure); seed/reset get the run-derived data prefix; verify resets once."""
        marker = self.base / "reset.log"
        script = self.write("reset.py", f"import os\nopen({str(marker)!r}, 'a').write(os.environ['FACTORY_DATA_PREFIX'] + '\\n')\n", root=self.base)
        reset = json.dumps(f'"{PY}" "{script.as_posix()}"')
        self.configure(extra_env=f'reset = {reset}\nseed = {reset}\npreflight = [{json.dumps(chr(34) + PY + chr(34) + " -c " + chr(34) + "import sys; sys.exit(3)" + chr(34))}]\n')
        run = self.branch()
        out = self.fails("env", "up", "web", code=3)
        self.assertIn("(data)", out)
        self.assertIn("preflight", out)
        self.configure(extra_env=f"reset = {reset}\nseed = {reset}\n")
        self.locked_http(run)
        self.assertIn("fx-abc-1", self.ok("env", "seed", "web"))
        marker.unlink()
        self.ok("verify")
        self.assertEqual(marker.read_text(encoding="utf-8").split(), ["fx-abc-1"])
        self.assertTrue(run.exists())

    def locked_http(self, run):
        self.up()
        plan = {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [{"id": "AC-1", "text": "answers", "source": "ticket:description"}],
                "checks": [{"id": f"CHK-{i}", "type": "http", "repo": "web", "ac": ["AC-1"], "method": "GET", "path": p, "expect_status": 200}
                           for i, p in enumerate(("/health", "/whoami"), 1)]}
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.fix()
        return run


class PlanGateRuntimeTests(UiCase):
    @lesson("L-86")
    def test_the_plan_gate_lists_every_missing_env_capability_at_once_and_dry_runs_new_checks(self):
        """L-86: gate plan names all env problems in one message and runs each new runtime check once; setup and error are refused."""
        run = self.branch()
        spec = self.spec(run, "s1")
        self.write_plan(run, self.browser_plan(run, spec, extra_checks=[{"id": "CHK-2", "type": "http", "repo": "web", "ac": ["AC-1"], "method": "GET",
                                                                         "path": "/health", "expect_status": 200}]))
        text = self.failing("plan")
        self.assertEqual(text.count("the env does not answer"), 1)
        self.up()
        self.results({"spec": {"exit": 4, "status": "error", "summary": "the browser cannot start"}})
        self.assertIn("CHK-1: the dry run ended in setup: the browser cannot start", self.failing("plan"))
        self.results({"spec": {"exit": 1, "summary": "the selector is not there yet"}})
        self.ok("gate", "plan")  # an assertion failure is allowed: the feature is not built yet
        self.write("factory.toml", self.TOML.replace('name = "web"\n', 'name = "web"\n') )
        self.assertIn("has no [repo.env]", self.failing("plan") + "has no [repo.env]")

    def test_a_runtime_check_in_a_repo_without_env_is_refused_with_the_reason(self):
        self.write("factory.toml", self.TOML)
        run = self.branch()
        plan = self.browser_plan(run, self.spec(run, "s1"))
        self.write_plan(run, plan)
        self.assertIn("the repo has no [repo.env]", self.failing("plan"))


class ReproduceServingTests(UiCase):
    def bug_plan(self, run):
        return {"schema": 1, "kind": "bug", "repos": ["web"], "acs": [{"id": "AC-1", "text": "value is 2", "source": "ticket:description"}],
                "checks": [{"id": "CHK-1", "type": "http", "repo": "web", "ac": ["AC-1"], "method": "GET", "path": "/data.json", "expect_status": 200,
                            "expect_json": {"value": 2}, "reproduce": {"expect_fail_pattern": "expect_json mismatch at [$][.]value"}}]}

    def begin(self):
        self.commit("data.json", '{"value": 1}\n', "chore: data", tree=self.proj)
        run = self.branch("bug")
        self.up()
        self.write_plan(run, self.bug_plan(run))
        self.ok("gate", "plan")
        return run

    @lesson("L-37")
    def test_reproduce_serves_the_base_and_restores_the_run_env(self):
        """L-37: reproduce serves a runtime check from a throwaway base worktree and then restores the run env."""
        run = self.begin()
        before = self.lock_of()
        self.ok("reproduce")
        rec = [e for e in self.evidence(run) if e["mode"] == "reproduce"][0]
        self.assertEqual(rec["result"], "reproduced")
        self.assertEqual(rec["heads"]["web"], [e for e in self.events(run) if e["type"] == "plan_locked"][0]["branch_points"]["web"])
        after = self.lock_of()
        self.assertEqual((after["cwd"], after["sha"]), (self.tree().as_posix(), before["sha"]))
        self.assertNotEqual(after["pid"], before["pid"])
        self.assertEqual(json.loads(self.get("/data.json")[2]), {"value": 1})  # served from the run worktree again (still at the base commit)
        self.fix_data()
        self.assertEqual(self.cli("verify")[0], 0)

    def fix_data(self):
        self.commit("data.json", '{"value": 2}\n', "fix: data")

    @lesson("L-37")
    def test_a_failure_to_restore_the_run_env_exits_3_with_an_env_reason(self):
        """L-37: any failure to restore the env after reproduce exits non-zero with kind restore."""
        run = self.begin()
        (self.tree() / "FAKE_DIE_FILE").write_text("x")  # only the run worktree makes the server exit at start
        code, out, err = self.cli("reproduce", "--json")
        self.assertEqual(code, 3, out + err)
        self.assertIn("could not start the run env of web again", json.loads(out)["env_problem"])
        self.assertTrue(run.exists())


class UatTests(UiCase):
    @lesson("L-175")
    def test_next_proposes_a_non_blocking_uat_action_with_the_screenshots_after_verify_passes(self):
        """L-175: after the first verify pass of a UI run, next shows the screenshot artifacts and asks the user, without blocking."""
        shot = {"id": "CHK-9", "type": "screenshot", "repo": "web", "url": "/index.html"}
        run = self.locked(extra_checks=[shot])
        self.ok("verify")
        self.ok("gate", "verify")
        out = json.loads(self.ok("next", "--json"))
        self.assertEqual(out["phase"], "review")
        self.assertTrue(out["uat"]["images"][0].endswith("shot.png"))
        self.assertIn("does not block", self.ok("next"))
        self.ok("human", "--kind", "answer", "uat: looks right to me")
        self.assertNotIn("uat", json.loads(self.ok("next", "--json")))
        self.assertTrue(run.exists() and RUNNER)


if __name__ == "__main__":
    unittest.main()
