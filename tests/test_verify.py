"""`factory verify` and `gate verify`: evidence records, reuse, ordering, flaky, fail-fast and exit codes."""
import json
import unittest

from dev_factory.util import canonical, sha256_bytes
from tests.helpers import PY, lesson
from tests.m2_support import M2Case, check_cmd, cmd

BOOM = cmd("print('boom'); import sys; sys.exit(1)")


class VerifyCase(M2Case):
    def setUp(self):
        super().setUp()
        self.run_dir = self.prepare()

    def verify(self, *flags, code=None):
        got, out, err = self.cli("verify", "--json", *flags)
        if code is not None:
            self.assertEqual(got, code, out + err)
        return json.loads(out) if out.strip().startswith("{") else {"error": err, "exit": got}

    def set_checks(self, *checks):
        plan = self.read_plan(self.run_dir)
        plan["checks"] = [{"type": "cmd", "repo": "web", "ac": ["AC-1"], **c} for c in checks]
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "checks for the scenario")


class EvidenceTests(VerifyCase):
    def test_a_record_has_every_design_field_and_a_hash_of_itself(self):
        sha = self.fix()
        self.verify(code=0)
        (rec,) = self.evidence(self.run_dir)
        for key in ("v", "ts", "cli", "type", "check", "spec_hash", "repo", "mode", "attempt", "sha", "worktree", "clean", "cmd", "exit",
                    "duration_s", "tail", "result", "kind", "reason", "artifacts", "hash"):
            self.assertIn(key, rec)
        lock = [e for e in self.events(self.run_dir) if e["type"] == "plan_locked"][0]
        self.assertEqual((rec["check"], rec["mode"], rec["attempt"], rec["sha"], rec["clean"], rec["result"], rec["exit"]),
                         ("CHK-1", "verify", 1, sha, True, "pass", 0))
        self.assertEqual(rec["spec_hash"], lock["spec_hashes"]["CHK-1"])
        self.assertEqual(rec["worktree"], self.tree().as_posix())
        body = {k: v for k, v in rec.items() if k != "hash"}
        self.assertEqual(rec["hash"], sha256_bytes(canonical(body).encode("utf-8")))
        self.assertIn("checks passed", (self.run_dir / rec["artifacts"][0]).read_text(encoding="utf-8"))

    @lesson("L-01")
    def test_gate_verify_fails_for_an_ac_without_passing_evidence_from_verify(self):
        """L-01: no AC is passed from reading code; the gate needs a pass record that `verify` wrote."""
        self.fix()
        text = self.failing("verify")
        self.assertIn("CHK-1: no verify record", text)
        self.assertIn("AC-1: no passing check backs this AC", text)
        self.verify(code=0)
        self.assertEqual(self.gate("verify")[0], 0)

    @lesson("L-08")
    def test_a_new_commit_invalidates_the_pass_at_the_old_head(self):
        """L-08: the gate needs a pass at the current HEAD of every affected repo."""
        self.fix()
        self.verify(code=0)
        self.assertEqual(self.gate("verify")[0], 0)
        self.commit("notes.txt", "more\n", "docs: notes")
        self.assertIn("CHK-1: no verify record at", self.failing("verify"))
        self.verify(code=0)
        self.assertEqual(self.gate("verify")[0], 0)

    @lesson("L-07")
    def test_evidence_from_a_dirty_worktree_is_recorded_but_never_counts(self):
        """L-07: untracked files count as dirty; the record says clean false and the gate refuses it."""
        self.fix()
        (self.tree() / "scratch.txt").write_text("untracked")
        out = self.verify(code=0)
        self.assertEqual(out["dirty"], ["web"])
        self.assertFalse(self.evidence(self.run_dir)[0]["clean"])
        text = self.failing("verify")
        self.assertIn("uncommitted or untracked", text)
        self.assertIn("CHK-1: no verify record", text)
        self.verify()
        self.assertEqual(len(self.evidence(self.run_dir)), 2, "a dirty record is never reused")
        (self.tree() / "scratch.txt").unlink()
        self.verify(code=0)
        self.assertEqual(self.gate("verify")[0], 0)

    def test_every_affected_repo_needs_a_commit_past_its_branch_point(self):
        """L-87 (by design, enforced here): a repo with no commit of the run cannot pass verify, even with passing checks."""
        self.set_checks({"id": "CHK-1", "run": cmd("print('passes anyway')"), "expect_pattern": "passes anyway"})
        self.verify(code=0)
        self.assertIn("no commit past the branch point", self.failing("verify"))
        self.commit("notes.txt", "x", "docs: notes")
        self.verify(code=0)
        self.assertEqual(self.gate("verify")[0], 0)

    def test_an_unknown_check_id_is_a_usage_error_that_lists_the_declared_ones(self):
        self.assertIn("no check 'CHK-9' (declared: CHK-1)", self.fails("verify", "--check", "CHK-9"))


class DriftTests(VerifyCase):
    @lesson("L-132")
    def test_any_plan_change_after_the_lock_fails_every_later_gate_and_verify(self):
        """L-132: plan.json is locked; an edit fails gates and verify until `plan amend --reason`."""
        self.fix()
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["expect_pattern"] = "checks passed: VALUE=1"
        self.write_plan(self.run_dir, plan)
        text = self.failing("verify")
        self.assertIn("plan changed after lock (CHK-1)", text)
        self.assertIn("plan amend --reason", text)
        self.assertIn("plan changed after lock", self.failing("plan"))
        self.assertIn("plan changed after lock", self.fails("verify", code=2))

    @lesson("L-09")
    def test_evidence_is_stale_when_the_spec_hash_differs_from_the_locked_one(self):
        """L-09: after an amend that changes the check, the old pass no longer counts at the same SHA."""
        self.fix()
        self.verify(code=0)
        self.assertEqual(self.gate("verify")[0], 0)
        self.set_checks({"id": "CHK-1", "run": check_cmd(2), "expect_pattern": "checks passed: VALUE=2$"})
        text = self.failing("verify")
        self.assertIn("CHK-1: no verify record at", text)
        self.verify(code=0)
        self.assertEqual(self.gate("verify")[0], 0)


class NoLockTests(M2Case):
    def test_verify_and_reproduce_refuse_a_plan_that_is_not_locked(self):
        self.prepare(lock=False)
        self.assertIn("the plan is not locked", self.fails("verify"))
        self.assertIn("the plan is not locked", self.fails("reproduce"))


class ReuseOrderTests(VerifyCase):
    @lesson("L-10")
    def test_the_same_sha_and_spec_with_a_pass_is_reused_not_rerun(self):
        """L-10: nothing is re-run at the same SHA and spec hash; `--fresh` is the explicit override."""
        self.fix()
        self.verify(code=0)
        again = self.verify(code=0)
        self.assertEqual([(r["check"], r["reused"]) for r in again["results"]], [("CHK-1", True)])
        self.assertEqual(len(self.evidence(self.run_dir)), 1)
        self.verify("--fresh", code=0)
        self.assertEqual(len(self.evidence(self.run_dir)), 2)

    @lesson("L-11")
    def test_last_failed_runs_first_then_new_then_the_cheapest_type(self):
        """L-11: ordering is last-failed, new or changed, then cheapest type (the commit check before commands)."""
        self.write("factory.toml", self.TOML.replace('name = "web"\n', 'name = "web"\ncommit_pattern = "^(feat|fix|docs): .+"\n'))
        self.ok("plan", "amend", "--reason", "commit pattern")
        self.fix()
        self.verify("--check", "CHK-1", code=0)
        self.set_checks({"id": "CHK-1", "run": check_cmd(2), "expect_pattern": "checks passed: VALUE=2"},
                        {"id": "CHK-2", "run": check_cmd(5)},
                        {"id": "CHK-3", "run": check_cmd(2), "expect_pattern": "VALUE=2"})
        self.verify("--check", "CHK-2", "--no-retry", code=1)
        self.set_checks({"id": "CHK-1", "run": check_cmd(2), "expect_pattern": "checks passed: VALUE=2"},
                        {"id": "CHK-2", "run": check_cmd(5)},
                        {"id": "CHK-3", "run": check_cmd(2), "expect_pattern": "VALUE=2"},
                        {"id": "CHK-4", "run": check_cmd(2), "expect_pattern": "passed: VALUE"})
        out = self.verify("--no-retry")
        self.assertEqual([r["check"] for r in out["results"]], ["CHK-2", "web:commits", "CHK-3", "CHK-4", "CHK-1"])

    def test_a_check_flag_runs_any_declared_check_and_a_repo_flag_filters(self):
        self.fix()
        out = self.verify("--check", "CHK-1", "--repo", "web", code=0)
        self.assertEqual([r["check"] for r in out["results"]], ["CHK-1"])
        self.assertEqual(self.verify("--repo", "nope", code=1)["results"], [])


class FlakyFailFastTests(VerifyCase):
    def flaky_check(self, name="flag.txt"):
        flag = self.base / name
        return {"id": "CHK-1", "run": f'"{PY}" flaky.py "{flag.as_posix()}"', "expect_pattern": "flaky ran"}

    @lesson("L-14", "L-139")
    def test_a_retry_that_passes_is_flaky_with_both_attempts_and_a_later_clean_run_satisfies(self):
        """L-14, L-139: an assertion failure followed by a pass is `flaky` and never counts; a later clean pass does."""
        self.fix()
        self.set_checks(self.flaky_check())
        out = self.verify(code=1)
        self.assertEqual(out["results"][0]["result"], "flaky")
        recs = self.evidence(self.run_dir, check="CHK-1")
        self.assertEqual([(r["attempt"], r["result"]) for r in recs], [(1, "fail"), (2, "flaky")])
        self.assertEqual(recs[1]["retry_of"], 1)
        self.assertIn("flaky never satisfies", self.failing("verify"))
        self.verify(code=0)
        self.assertEqual([r["result"] for r in self.evidence(self.run_dir, check="CHK-1")], ["fail", "flaky", "pass"])
        self.assertEqual(self.gate("verify")[0], 0)

    @lesson("L-139")
    def test_timeouts_and_setup_failures_are_never_retried_into_flaky(self):
        """L-139: only an assertion failure is retried; timeout and setup results stay what they are."""
        self.fix()
        self.set_checks({"id": "CHK-1", "run": cmd("import time; time.sleep(30)"), "timeout_s": 1})
        self.verify(code=1)
        self.set_checks({"id": "CHK-1", "run": cmd("import sys; sys.exit(127)")})
        self.verify(code=1)
        self.assertEqual([r["result"] for r in self.evidence(self.run_dir, check="CHK-1")], ["timeout", "setup"])

    @lesson("L-47")
    def test_two_identical_consecutive_failures_stop_the_batch(self):
        """L-47: the same kind and first line twice in a row ends the run of checks instead of burning the rest."""
        self.fix()
        self.set_checks({"id": "CHK-1", "run": BOOM}, {"id": "CHK-2", "run": BOOM}, {"id": "CHK-3", "run": check_cmd(2)})
        out = self.verify("--no-retry", code=1)
        self.assertEqual([(r["check"], r["result"]) for r in out["results"]], [("CHK-1", "fail"), ("CHK-2", "fail"), ("CHK-3", "not_run")])
        self.assertIn("two identical failures", out["stopped"])
        self.assertEqual(len(self.evidence(self.run_dir)), 2)

    def test_different_failures_do_not_stop_the_batch(self):
        self.fix()
        self.set_checks({"id": "CHK-1", "run": BOOM}, {"id": "CHK-2", "run": cmd("print('other'); import sys; sys.exit(1)")},
                        {"id": "CHK-3", "run": check_cmd(2)})
        out = self.verify("--no-retry", code=1)
        self.assertEqual([r["result"] for r in out["results"]], ["fail", "fail", "pass"])
        self.assertIsNone(out["stopped"])


class ExitCodeTests(VerifyCase):
    @lesson("L-48")
    def test_exit_codes_separate_pass_failure_and_usage_and_json_carries_the_kind(self):
        """L-48: 0 pass, 1 check failure, 2 usage or lock error; `--json` names the kind of every result."""
        self.fix()
        self.assertEqual(self.verify(code=0)["exit"], 0)
        self.set_checks({"id": "CHK-1", "run": BOOM})
        out = self.verify("--no-retry", code=1)
        self.assertEqual((out["results"][0]["kind"], out["exit"]), ("assert", 1))
        self.set_checks({"id": "CHK-1", "run": "factory-test-no-such-command"})
        self.assertEqual(self.verify("--no-retry", code=1)["results"][0]["kind"], "setup")
        self.fails("verify", "--check", "CHK-77", code=2)
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["expect_exit"] = 4
        self.write_plan(self.run_dir, plan)
        self.assertIn("plan changed after lock", self.fails("verify", code=2))

    def test_the_text_output_names_every_check_and_the_reason(self):
        self.fix()
        self.set_checks({"id": "CHK-1", "run": BOOM})
        code, out, _ = self.cli("verify", "--no-retry")
        self.assertEqual(code, 1)
        self.assertIn("CHK-1", out)
        self.assertIn("fail: exit 1, expected 0", out)
        self.assertIn("verify: FAILED", out)


class StandingTests(M2Case):
    def test_standing_checks_and_the_commit_check_gate_verify(self):
        self.write("factory.toml", self.TOML.replace('name = "web"\n', 'name = "web"\ncommit_pattern = "^(feat|fix): .+"\n')
                   + f'\n[[repo.check]]\nid = "lint"\nrun = {json.dumps(cmd("print(1)"))}\nexpect_pattern = "never printed here"\n')
        run = self.prepare()
        self.commit("app.py", "VALUE = 2\n", "oops not conventional")
        out = json.loads(self.cli("verify", "--json", "--no-retry")[1])
        by = {r["check"]: r for r in out["results"]}
        self.assertEqual(by["web:lint"]["result"], "fail")
        self.assertEqual(by["web:commits"]["result"], "fail")
        self.assertIn("does not match commit_pattern", by["web:commits"]["reason"])
        text = "\n".join(self.gate("verify")[1])
        self.assertIn("web:lint", text)
        self.assertIn("web:commits", text)
        self.assertEqual(self.evidence(run, check="web:lint")[0]["repo"], "web")


if __name__ == "__main__":
    unittest.main()
