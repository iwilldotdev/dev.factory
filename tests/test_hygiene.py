"""Check hygiene found on a real run: checks that write the worktree, a `parse` that reads noise, envs that share a worktree with
checks, worktrees without dependencies, reviewer sandboxes, mutant proofs, `gate <name> <run>` and intake decisions."""
import json
import unittest
from pathlib import Path
from unittest import mock

from dev_factory import env as envmod, envproc, quality
from tests.helpers import PY, git, head_of, tmpdir
from tests.m2_support import M2Case, check_cmd, cmd
from tests.m3_support import ShipCase


def plan_with(*extra: dict) -> dict:
    ac = {"id": "AC-1", "text": "VALUE is 2", "source": "ticket:description"}
    first = {"id": "CHK-1", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": check_cmd(2), "expect_pattern": "checks passed: VALUE=2"}
    return {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [ac], "checks": [first, *extra]}


class MutatingCheckTests(M2Case):
    def test_a_check_that_writes_the_worktree_is_setup_and_later_evidence_is_not_clean(self):
        writer = {"id": "CHK-2", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": cmd("open('f.txt', 'w').write('x')")}
        after = {"id": "CHK-3", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": check_cmd(2)}
        self.run_dir = self.prepare(plan=plan_with(writer, after))
        self.fix()
        code, out, _ = self.cli("verify", "--json")
        res = {r["check"]: r for r in json.loads(out)["results"]}
        self.assertEqual(code, 1)
        self.assertEqual((res["CHK-2"]["result"], res["CHK-1"]["result"]), ("setup", "pass"))
        self.assertIn("changed 1 path(s) in the worktree: f.txt; a check must only read", res["CHK-2"]["reason"])
        self.assertTrue(self.evidence(self.run_dir, check="CHK-1")[-1]["clean"])
        self.assertFalse(self.evidence(self.run_dir, check="CHK-3")[-1]["clean"], "a check after the writer does not count as clean")
        self.assertEqual(json.loads(out)["dirty"], ["web"])


class ParseTests(unittest.TestCase):
    def test_ids_under_exit_zero_and_lines_that_take_most_of_the_output_warn(self):
        self.assertIn("exit 0 but 1 failure id(s)", quality.warning({"parse": "regex:x"}, 0, ["a"], "a"))
        self.assertIsNone(quality.warning({"parse": "regex:x"}, 1, ["a"], "a"))
        self.assertIsNone(quality.warning({"parse": "lines"}, 1, ["exit:1"], ""))
        noise = [f"line {i}" for i in range(30)]
        self.assertIn("parse = lines made 30 of 31", quality.warning({"parse": "lines"}, 1, noise, "\n".join(noise + ["x"])))
        self.assertIsNone(quality.warning({"parse": "lines"}, 1, noise[:5], "\n".join(noise)))

    def test_the_latest_baseline_of_each_check_reports_its_quarantine(self):
        st = {"baselines": [{"check": "A", "quarantined": ["x", "y"]}, {"check": "A", "quarantined": ["x"]}, {"check": "B", "quarantined": []}]}
        self.assertEqual(quality.quarantines(st), {"A": 1})


class BaselineNoiseTests(M2Case):
    def test_a_baseline_whose_ids_change_every_run_fails_instead_of_quarantining_them(self):
        noise = {"id": "CHK-2", "type": "quality", "repo": "web", "ac": ["AC-1"],
                 "run": cmd("import uuid; [print(uuid.uuid4()) for _ in range(25)]; raise SystemExit(1)")}
        self.prepare(plan=plan_with(noise))
        code, out, _ = self.cli("baseline")
        self.assertEqual(code, 1, out)
        self.assertIn("75 failure id(s) differ between identical runs", out)
        self.assertIn("fix `parse`", out)

    def test_factory_check_warns_on_a_default_parse(self):
        self.write("factory.toml", self.TOML + '\n[[repo.check]]\nid = "LINT"\ntype = "quality"\nrun = "lint"\n')
        out = self.ok("check")
        self.assertIn("a quality check without `parse` uses `lines`", out)


class EnvSharingTests(unittest.TestCase):
    def test_a_pid_of_a_live_env_named_in_a_failure_is_pointed_out(self):
        live = [{"repo": "api", "pid": 4321, "cwd": "C:/wt/R-1/api"}]
        with mock.patch.object(envmod, "live_of_run", return_value=live):
            self.assertEqual(envmod.pid_in(None, None, 'The file is locked by: "Api (4321)"'), (4321, "api"))
            self.assertIsNone(envmod.pid_in(None, None, "Ran 43210 tests in 4321 ms"))
            warn = envmod.sharing(None, None, {"api": Path("C:/wt/R-1/api"), "web": Path("C:/wt/R-1/web")})
        self.assertEqual(len(warn), 1)
        self.assertIn("env api (pid 4321) serves from the worktree of api", warn[0])


class IgnoredFoldersTests(M2Case):
    def test_ignored_folders_of_the_main_checkout_missing_in_a_new_worktree(self):
        repo = tmpdir("ignored ") / "repo"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        (repo / ".gitignore").write_text("deps/\n.factory/\nout.log\n")
        (repo / "a.txt").write_text("a")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "init", cwd=repo)
        for d in ("deps", ".factory"):
            (repo / d).mkdir()
            (repo / d / "x").write_text("x")
        (repo / "out.log").write_text("x")
        tree = repo.parent / "wt"
        git("worktree", "add", "-q", "--detach", str(tree), cwd=repo)
        self.assertEqual(envproc.ignored_missing(repo, tree), ["deps"])
        (tree / "deps").mkdir()
        self.assertEqual(envproc.ignored_missing(repo, tree), [])

    def test_run_branch_warns_when_a_repo_without_setup_lacks_them(self):
        self.write(".gitignore", ".factory/\ndeps/\n")
        self.write("deps/tool.txt", "x")
        run = self.start()
        self.ingest_ticket(run)
        self.ok("gate", "intake", "ABC-1")
        out = self.ok("run", "branch")
        self.assertIn("ignored folders the run worktree lacks (deps)", out)
        self.assertIn("repo web has no `setup`", out)


class CliAndDecisionTests(M2Case):
    def test_gate_takes_the_run_as_its_last_word_and_unknown_words_name_the_run_flag(self):
        run = self.start()
        self.ingest_ticket(run)
        self.ok("decide", "--user-said", "keep the old layout")
        self.ok("gate", "intake", "ABC-1")
        self.ok("decide", "--user-said", "use the new endpoint")
        nxt = json.loads(self.ok("next", "--json"))
        self.assertEqual(nxt["decisions"], ["D-2"], "a decision recorded before the intake gate passed is intake history")
        err = self.fails("verify", "ABC-1", code=2)
        self.assertIn("to name a run use --run <RUN>", err)


class SandboxTests(M2Case):
    def test_a_reviewer_sandbox_is_a_detached_worktree_at_head_and_drop_removes_it(self):
        self.prepare()
        sha = self.fix()
        made = json.loads(self.ok("review", "sandbox", "--json", "--actor", "qa reviewer"))
        path = Path(made[0]["path"])
        self.assertEqual((made[0]["repo"], made[0]["sha"], head_of(path)), ("web", sha, sha))
        self.assertIn("qa-reviewer-", path.parent.name)
        (path / "app.py").write_text("VALUE = 9\n")  # a mutation test never touches the run worktree
        self.assertEqual(git("status", "--porcelain", cwd=self.tree()), "")
        self.assertIn("removed sandbox", self.ok("review", "sandbox", "--drop"))
        self.assertFalse(path.exists())


class MutantTests(M2Case):
    def test_a_check_must_fail_with_its_mutant_applied_before_the_gate_passes(self):
        killer = cmd("open('app.py', 'w').write('VALUE = 3')")
        run_dir = self.prepare(plan={**plan_with(), "checks": [{**plan_with()["checks"][0], "mutant": killer}]})
        self.fix()
        self.ok("verify")
        rec = self.evidence(run_dir, check="CHK-1", mode="mutant")[-1]
        self.assertEqual(rec["result"], "pass")
        self.assertIn("killed", rec["reason"])
        self.assertEqual(self.gate("verify")[0], 0)
        self.assertIn("(reused)", self.ok("verify"), "a kill at the same SHA and spec is reused")

    def test_a_mutant_the_check_survives_fails_verify_and_the_gate(self):
        harmless = cmd("open('other.txt', 'w').write('x')")
        self.prepare(plan={**plan_with(), "checks": [{**plan_with()["checks"][0], "mutant": harmless}]})
        self.fix()
        code, out, _ = self.cli("verify")
        self.assertEqual(code, 1, out)
        self.assertIn("survived: the check still passes with the mutant applied", out)
        self.assertIn("CHK-1: its mutant is not killed", self.failing("verify"))

    def test_only_cmd_and_quality_checks_take_a_mutant(self):
        run = self.prepare(lock=False)
        plan = plan_with({"id": "CHK-2", "type": "human", "repo": "web", "ac": ["AC-1"], "text": "looks right", "mutant": "x"})
        self.write_plan(run, plan)
        self.assertIn("only a cmd or quality check takes a mutant", self.failing("plan"))


class ShipFrictionTests(ShipCase):
    def stored_change(self) -> dict:
        found = [p for p in self.run_dir.rglob("42.json") if "raw" not in p.parts and "readback" not in p.parts]
        return json.loads(sorted(found, key=lambda p: p.stat().st_mtime)[-1].read_text(encoding="utf-8"))["data"]

    def test_a_draft_marker_in_the_title_is_named_then_stripped_by_the_configured_regex(self):
        self.to_ship()
        title = self.ship_plan()["title"]
        self.ingest_change(title=f"Draft: {title}", draft=False)
        why = "\n".join(self.ship_verify(code=1)["mismatches"])
        self.assertIn("set [providers.change] draft_title_marker", why)
        self.write("factory.toml", (self.proj / "factory.toml").read_text(encoding="utf-8") + r'''
[providers.change]
draft_title_marker = "(?i)draft:\\s*"
''')
        self.ingest_change(title=f"Draft: {title}", draft=False)
        self.assertEqual((self.stored_change()["title"], self.stored_change()["draft"]), (title, True))

    def test_contract_examples_and_the_summary_written_through_the_cli(self):
        self.to_ship()
        example = json.loads(self.ok("ingest", "change", "--example"))
        self.assertEqual((example["state"], example["draft"]), ("open", True))
        raw = self.write("raw.json", "{}")
        err = self.fails("ingest", "change", "--raw", str(raw), "--stdin", stdin=json.dumps({"id": "42", "state": "opened"}))
        self.assertIn("example (factory ingest change --example)", err)
        bad = self.fails("ship", "summary", "--stdin", stdin="ABC-1: see CHK-1", code=2)
        self.assertIn("bookkeeping", bad)
        self.assertFalse((self.run_dir / "ship" / "summary.md").is_file())
        self.ok("ship", "summary", "--stdin", stdin=self.SUMMARY)
        self.assertEqual((self.run_dir / "ship" / "summary.md").read_text(encoding="utf-8"), self.SUMMARY)


if __name__ == "__main__":
    unittest.main()
