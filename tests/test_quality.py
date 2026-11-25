"""Quality checks: failure ids, the baseline at the branch point (flaky ids quarantined), path normalization and the scoped mode."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from dev_factory import quality
from tests.helpers import PY, git, lesson
from tests.m3_support import M3Case

LINT = f'"{PY}" lint.py'


class IdTests(unittest.TestCase):
    def test_ids_ignore_line_and_column_but_count_repeats(self):
        base = quality.positionless(["a.tsx:100:3: hook deps", "b.ts(12,5): error TS1: x"])
        moved = quality.positionless(["a.tsx:118:24: hook deps", "b.ts(40,1): error TS1: x"])
        self.assertEqual(set(moved) - set(base), set(), "code that only moved keeps its findings")
        again = quality.positionless(["a.tsx:118:24: hook deps", "a.tsx:150:2: hook deps", "b.ts(40,1): error TS1: x"])
        self.assertEqual(set(again) - set(base), {"a.tsx: hook deps#2"}, "one more copy of a finding is new")

    @lesson("L-62")
    def test_worktree_and_checkout_paths_are_stripped_so_a_baseline_id_equals_a_run_id(self):
        """L-62: failure ids are normalized (worktree paths stripped) before the baseline comparison."""
        base, tree, main = Path("C:/wt/ABC-1/_throwaway/web-baseline-1a2b3c"), Path("C:/wt/ABC-1/web"), Path("C:/Users/dev/code/web")
        line = "{}/src/a.ts(3,4): error TS1005 \x1b[31mred\x1b[0m"
        ids = [quality.failure_ids("lines", "", line.format(p.as_posix()), 1, [p, main]) for p in (base, tree)]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[0], ["src/a.ts(3,4): error TS1005 red"])
        windows = quality.failure_ids("lines", "", str(tree).replace("/", "\\") + "\\src\\a.ts: E", 1, [tree])
        self.assertEqual(windows, ["src\\a.ts: E"])

    def test_the_parse_modes_and_the_exit_code_sentinel(self):
        self.assertEqual(quality.failure_ids("regex:^FAIL (.+)$", "", "ok\nFAIL a\nFAIL b\n", 1, []), ["a", "b"])
        self.assertEqual(quality.failure_ids("json:issues", '{"issues": [{"id": "x"}, "y", {"k": 1}]}', "", 1, []), ["x", "y", '{"k":1}'])
        self.assertEqual(quality.failure_ids("json:$.a.0", '{"a": [["p", "q"]]}', "", 1, []), ["p", "q"])
        self.assertEqual(quality.failure_ids("lines", "", "boom without ids\n".replace("boom without ids\n", ""), 3, []), ["exit:3"])
        self.assertEqual(quality.failure_ids("lines", "", "", 0, []), [])
        with self.assertRaises(ValueError):
            quality.failure_ids("json:missing.path", "{}", "", 1, [])
        for bad in ("json", "regex:(", "xml:a", "lines:x"):
            self.assertIn("parse", quality.parse_problem(bad, "c") or "")
        self.assertIsNone(quality.parse_problem("regex:^(.+): BAD", "c"))


class QualityCase(M3Case):
    def plan(self, scoped: str | None = None, parse: str = "lines") -> dict:
        plan = self.default_plan()
        chk = {"id": "CHK-2", "type": "quality", "repo": "web", "ac": ["AC-1"], "run": LINT, "parse": parse}
        plan["checks"].append({**chk, **({"scoped": scoped} if scoped else {})})
        return plan

    def prepared(self, **kw) -> None:
        """The base branch already has a failing file (`old.txt`), so the baseline holds one stable id."""
        (self.proj / "old.txt").write_text("BAD\n")
        git("add", "-A", cwd=self.proj)
        git("commit", "-q", "-m", "chore: a known failure", cwd=self.proj)
        self.run_dir = self.prepare(plan=self.plan(**kw))
        self.fix()

    def baselines(self) -> list[dict]:
        return [e for e in self.events(self.run_dir) if e["type"] == "baseline"]

    def verify_json(self, *flags: str) -> tuple[int, dict]:
        code, out, _ = self.cli("verify", "--json", *flags)
        return code, {r["check"]: r for r in json.loads(out)["results"]}


class BaselineTests(QualityCase):
    @lesson("L-61", "L-62")
    def test_verify_takes_the_baseline_once_and_fails_only_on_new_failure_ids(self):
        """L-61, L-62: the baseline at the branch point is recorded once; a known id (even from another path) passes, a new one fails."""
        self.prepared()
        code, res = self.verify_json()
        self.assertEqual((code, res["CHK-2"]["result"]), (0, "pass"))
        b = self.baselines()
        self.assertEqual(len(b), 1)
        self.assertEqual((b[0]["branch_point"], b[0]["runs"], b[0]["quarantined"], len(b[0]["stable"])), (self.tree_base(), 2, [], 1))
        self.assertEqual(b[0]["stable"], ["old.txt: BAD word"])
        rec = self.evidence(self.run_dir, check="CHK-2")[-1]
        self.assertEqual((rec["details"]["scope"], rec["details"]["new"], rec["details"]["total"]), ("full", [], 1))
        self.commit("new.txt", "BAD\n")
        code, res = self.verify_json()
        self.assertEqual((code, res["CHK-2"]["result"], res["CHK-2"]["kind"]), (1, "fail", "assert"))
        self.assertIn("1 new failure id(s) beyond the baseline", res["CHK-2"]["reason"])
        self.assertIn("new.txt: BAD word", res["CHK-2"]["reason"])
        self.assertEqual(len(self.baselines()), 1, "the baseline is not taken again at a later commit")
        self.assertIn("latest verify result is fail", "\n".join(self.gate("verify")[1]))

    def tree_base(self) -> str:
        return git("rev-parse", "main", cwd=self.proj)

    @lesson("L-61")
    def test_a_flaky_failure_at_the_branch_point_is_quarantined_not_recorded_as_a_failure_or_as_zero(self):
        """L-61: `factory baseline` runs the command up to 3 times at the branch point; ids that do not repeat are quarantined and never fail verify."""
        self.prepared()
        marker = self.base / "flaky.marker"
        with mock.patch.dict(os.environ, {"LINT_FLAKY": str(marker)}):
            out = self.ok("baseline")
            self.assertIn("1 stable failure id(s), 1 quarantined as flaky (3 run(s))", out)
            b = self.baselines()[-1]
            self.assertEqual((b["stable"], b["quarantined"], b["runs"]), (["old.txt: BAD word"], ["timing.txt: BAD flaky"], 3))
            marker.unlink()  # the flake shows up again at HEAD
            code, res = self.verify_json()
            self.assertEqual((code, res["CHK-2"]["result"]), (0, "pass"))
            self.assertEqual(self.evidence(self.run_dir, check="CHK-2")[-1]["details"]["quarantined"], 1)
            marker.unlink()
            self.assertIn("1 stable failure id(s), 1 quarantined", self.ok("baseline", "--fresh"))
            self.assertEqual(len(self.baselines()), 2)
            self.assertIn("no quality check", self.fails("baseline", "--check", "CHK-1", code=2))

    def test_a_standing_quality_check_from_the_config_runs_on_every_plan_of_the_repo(self):
        extra = f'\n[[repo.check]]\nid = "LINT"\ntype = "quality"\nrun = {json.dumps(LINT)}\nparse = "regex:^(.+): BAD word"\n'
        self.ready(extra_toml=extra)
        self.assertEqual(self.baselines()[0]["check"], "web:LINT")
        self.assertEqual(self.evidence(self.run_dir, check="web:LINT")[-1]["result"], "pass")
        self.write("factory.toml", self.TOML + extra.replace("regex:^(.+): BAD word", "regex:("))
        self.assertIn("parse", self.fails("check", code=2))
        self.write("factory.toml", self.TOML + '\n[[repo.check]]\nid = "x"\nrun = "echo"\nparse = "lines"\n')
        self.assertIn('belong to type = "quality"', self.fails("check", code=2))


class ScopedTests(QualityCase):
    @lesson("L-60")
    def test_the_scoped_template_gets_the_changed_files_and_never_replaces_the_full_run(self):
        """L-60: `verify --scoped` runs the scoped template with {changed_files} from branch_point..HEAD; only the full run satisfies the gates."""
        self.prepared(scoped=f"{LINT} {{changed_files}}", parse="regex:^(.+): BAD word")
        self.commit("new.txt", "fine\n")
        with mock.patch.dict(os.environ, {"LINT_ARGS": "1"}):
            code, res = self.verify_json("--scoped")
        self.assertEqual((code, res["CHK-2"]["result"]), (0, "pass"))
        rec = self.evidence(self.run_dir, check="CHK-2")[-1]
        self.assertEqual(rec["details"]["scope"], "scoped")
        self.assertIn("args: app.py new.txt", rec["tail"])
        self.assertNotIn("old.txt", rec["tail"], "files the branch did not touch are not scanned")
        why = "\n".join(self.gate("verify")[1])
        self.assertIn("CHK-2: no verify record", why)
        self.assertIn("without --scoped", why)
        self.ok("verify")
        self.assertEqual(self.gate("verify")[0], 0)

    def test_a_scoped_run_fails_on_a_new_id_in_a_changed_file(self):
        self.prepared(scoped=f"{LINT} {{changed_files}}", parse="regex:^(.+): BAD word")
        self.commit("bad.txt", "BAD\n")
        code, res = self.verify_json("--scoped")
        self.assertEqual((code, res["CHK-2"]["result"]), (1, "fail"))
        self.assertIn("bad.txt", res["CHK-2"]["reason"])

    def test_a_scoped_template_must_contain_the_placeholder(self):
        plan = self.plan(scoped=f"{LINT} nothing")
        self.run_dir = self.prepare(plan=plan, lock=False)
        self.assertIn("scoped: must be a command template that contains {changed_files}", self.failing("plan"))


if __name__ == "__main__":
    unittest.main()
