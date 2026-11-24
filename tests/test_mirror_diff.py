"""Mirror diff gate (DESIGN section 9): a hotfix mirror carries the SAME CHANGES as the base fix, with a user exception.

Real temporary git repositories: the base target `homolog` and the mirror target `pack`, where `pack` may have moved
files (the mirror then has to adapt paths, nothing else).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_support import cli, commit_file, core, sh  # noqa: E402
from test_hotfix_flow import HotfixCase  # noqa: E402

from factory import mirrordiff
from factory.commands import gate  # noqa: E402

UTIL = ("const h = require('./helper');\nfunction a() {\n  return 1;\n}\nfunction b() {\n  return 2;\n}\n"
        "function c() {\n  return 3;\n}\nmodule.exports = { a, b, c };\n")
HELPER = "module.exports = {};\n"
TEST_A = "test('a', () => {\n  expect(1).toBe(1);\n});\n"
FIX = UTIL.replace("return 1;", "return 10;").replace("return 3;", "return 30;")
FIX_IMPORT = FIX.replace("require('./helper')", "require('./helper2')")


class MirrorDiffCase(HotfixCase):
    repos = ("React",)

    def setUp(self) -> None:
        super().setUp()
        for alias, repo in self.repo_dirs.items():
            self.seed(alias, repo)
        self.start()

    def seed(self, alias, repo) -> None:
        commit_file(repo, "src/util.js", UTIL)
        commit_file(repo, "src/helper.js", HELPER)
        commit_file(repo, "tests/a.test.js", TEST_A)
        for t in ("homolog", "pack"):
            sh("git", "branch", "-f", t, "main", cwd=repo)
            sh("git", "update-ref", f"refs/remotes/origin/{t}", t, cwd=repo)

    def move_on_pack(self, alias="React") -> None:
        """`pack` moved src/util.js to lib/util.js (and its helper import with it)."""
        repo = self.repo_dirs[alias]
        sh("git", "checkout", "-q", "pack", cwd=repo)
        (repo / "lib").mkdir()
        sh("git", "mv", "src/util.js", "lib/util.js", cwd=repo)
        (repo / "lib" / "util.js").write_text(UTIL.replace("'./helper'", "'../src/helper'"), encoding="utf-8")
        sh("git", "add", "-A", cwd=repo)
        sh("git", "commit", "-q", "-m", "[Area] - move util", cwd=repo)
        sh("git", "checkout", "-q", "main", cwd=repo)
        sh("git", "update-ref", "refs/remotes/origin/pack", "pack", cwd=repo)

    def base_fix(self, alias="React", util=FIX, extra=None) -> None:
        wt = core.Path(core.run_load(self.rid)["repos"][alias]["worktree"])
        commit_file(wt, "src/util.js", util, "[Area] - fix")
        for rel, content in (extra or {}).items():
            commit_file(wt, rel, content, "[Area] - fix tests")

    def mirror_wt(self, alias="React"):
        """A mirror worktree on `pack`, registered in the run (no cherry-pick: the test writes what the builder would)."""
        repo = self.repo_dirs[alias]
        path = self.tmp / f"mirror-{alias}"
        sh("git", "worktree", "add", "-q", "--no-track", "-b", f"mirror-{alias}", str(path), "origin/pack", cwd=repo)

        def mutate(run):
            rec = run["repos"][alias]
            rec.setdefault("branches", {})["pack"] = f"mirror-{alias}"
            rec.setdefault("worktrees", {})["pack"] = str(path)
        core.run_update(self.rid, mutate)
        return path

    def report(self):
        run = core.run_load(self.rid)
        return mirrordiff.compute(run, core.workspace_load("ws"))

    def errors(self):
        return mirrordiff.errors(core.run_load(self.rid), core.workspace_load("ws"))

    def decide(self, text="mirror diff: tests live elsewhere on pack", by="user") -> int:
        args = ["run", "decide", self.rid, text, "--by", by]
        if by == "user":
            args += ["--user-said", "yes, the mirror may differ"]
        self.ok(*args)
        return len(core.run_load(self.rid)["decisions"]) - 1


class Comparison(MirrorDiffCase):
    def test_identical_mirror_passes(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        self.assertTrue(self.report()["equal"])
        self.assertEqual(self.errors(), [])
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 0, out)

    def test_extra_hunk_on_mirror_names_file_and_hunk(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX.replace("return 2;", "return 22;"), "[Area] - fix")
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 1, out)
        self.assertIn("React/pack", out)
        self.assertIn("src/util.js", out)
        self.assertIn("+  return 22;", out)
        self.assertIn("mirror only", out)
        errs = self.errors()
        self.assertTrue(any("src/util.js" in e and "+  return 22;" in e for e in errs), errs)

    def test_missing_hunk_on_mirror_fails(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", UTIL.replace("return 1;", "return 10;"), "[Area] - fix")
        rc, out = cli("run", "mirror-diff", self.rid, "--json")
        self.assertEqual(rc, 1, out)
        data = json.loads(out)
        f = data["entries"][0]["files"][0]
        self.assertEqual((f["file"], f["kind"], f["base_only_hunks"], f["mirror_only_hunks"]), ("src/util.js", "hunks", 1, 0))
        self.assertIn("+  return 30;", f["base_only"][0])

    def test_whole_file_reformat_on_mirror_fails(self):
        self.base_fix()
        mwt = self.mirror_wt()
        reformatted = FIX.replace("  return", "    return")  # a formatter run in write mode: indentation only
        commit_file(mwt, "src/util.js", reformatted, "[Area] - fix")
        self.assertTrue(self.errors())
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 1, out)
        self.assertIn("+    return 10;", out)

    def test_trailing_whitespace_only_difference_is_not_ignored(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX.replace("return 10;", "return 10; "), "[Area] - fix")
        self.assertTrue(self.errors())

    def test_context_and_offsets_do_not_matter(self):
        self.base_fix()
        repo = self.repo_dirs["React"]
        sh("git", "checkout", "-q", "pack", cwd=repo)  # pack gained lines on top: every hunk offset shifts
        commit_file(repo, "src/util.js", "// banner\n// banner 2\n" + UTIL, "[Area] - banner")
        sh("git", "checkout", "-q", "main", cwd=repo)
        sh("git", "update-ref", "refs/remotes/origin/pack", "pack", cwd=repo)
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", "// banner\n// banner 2\n" + FIX, "[Area] - fix")
        self.assertEqual(self.errors(), [])

    def test_file_moved_on_mirror_target_with_adjusted_imports_passes(self):
        self.move_on_pack()
        self.base_fix(util=FIX_IMPORT)
        mwt = self.mirror_wt()
        moved = FIX_IMPORT.replace("'./helper2'", "'../src/helper2'")
        commit_file(mwt, "lib/util.js", moved, "[Area] - fix")
        self.assertEqual(self.errors(), [])
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 0, out)

    def test_import_path_difference_is_not_forgiven_in_a_file_at_the_same_path(self):
        self.base_fix(util=FIX_IMPORT)
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX_IMPORT.replace("'./helper2'", "'../src/helper2'"), "[Area] - fix")
        self.assertTrue(self.errors())

    def test_moved_file_with_a_real_difference_still_fails(self):
        self.move_on_pack()
        self.base_fix(util=FIX_IMPORT)
        mwt = self.mirror_wt()
        moved = FIX_IMPORT.replace("'./helper2'", "'../src/helper2'").replace("return 2;", "return 22;")
        commit_file(mwt, "lib/util.js", moved, "[Area] - fix")
        errs = self.errors()
        self.assertTrue(any("lib/util.js" in e or "src/util.js" in e for e in errs), errs)
        self.assertTrue(any("return 22" in e for e in errs), errs)

    def test_tests_placed_in_a_different_file_fail(self):
        self.base_fix(extra={"tests/a.test.js": TEST_A + "test('b', () => {\n  expect(2).toBe(2);\n});\n"})
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        commit_file(mwt, "tests/b.test.js", "test('b', () => {\n  expect(2).toBe(2);\n});\n", "[Area] - tests")
        rc, out = cli("run", "mirror-diff", self.rid, "--json")
        self.assertEqual(rc, 1, out)
        files = {f["file"]: f["kind"] for f in json.loads(out)["entries"][0]["files"]}
        self.assertEqual(files, {"tests/a.test.js": "only_base", "tests/b.test.js": "only_mirror"})

    def test_excerpts_are_capped(self):
        many = "".join(f"line {i} " + "x" * 300 + "\n" for i in range(40))
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        commit_file(mwt, "src/big.js", many, "[Area] - big")
        pub = mirrordiff.public(self.report())
        ex = pub["entries"][0]["files"][0]["mirror_only"][0]
        self.assertLessEqual(len(ex.split("\n")), mirrordiff.EXCERPT_LINES + 1)
        self.assertTrue(all(len(l) <= mirrordiff.EXCERPT_WIDTH + 25 for l in ex.split("\n")))

    def test_no_mirror_yet_is_not_a_divergence(self):
        self.base_fix()
        self.assertIsNone(self.report())
        self.assertEqual(self.errors(), [])
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 2, out)


class Wiring(MirrorDiffCase):
    def test_build_gate_evaluator_reports_the_divergence(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX.replace("return 2;", "return 22;"), "[Area] - fix")
        run = core.run_load(self.rid)
        errs = gate._build(self.rid, run, core.workspace_load("ws"))
        self.assertTrue(any("mirror diff" in e and "src/util.js" in e for e in errs), errs)

    def test_build_gate_cli_fails_then_passes_with_the_user_exception(self):
        self.acs()
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        (core.run_dir(self.rid) / "diagnosis.md").write_text("## Root cause\nx\n\n## Evidence\ny\n\n## Ruled out\nz\n", encoding="utf-8")
        self.ok("prove", self.rid, "--repro")
        self.ok("gate", self.rid, "reproduce")
        self.fix()
        self.ok("gate", self.rid, "build")
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        commit_file(core.Path(core.run_load(self.rid)["repos"]["React"]["worktrees"]["pack"]), "extra.txt", "x\n", "[Area] - extra")
        self.ok("run", "rewind", self.rid, "build", "mirror review")
        rc, out = cli("gate", self.rid, "build")
        self.assertEqual(rc, 1, out)
        self.assertIn("extra.txt", out)
        n = self.decide("mirror diff: extra.txt is a pack-only changelog")
        rc, out = cli("gate", self.rid, "build", "--allow-mirror-divergence", str(n))
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.rid)["mirror_allow"]["decision"], n)

    def test_non_hotfix_run_is_unaffected(self):
        self.ok("run", "start", "ws", "FEAT-9")
        run = core.run_load("ws-feat-9")
        ws = core.workspace_load("ws")
        self.assertIsNone(mirrordiff.compute(run, ws))
        self.assertEqual(mirrordiff.errors(run, ws), [])
        rc, out = cli("run", "mirror-diff", "ws-feat-9")
        self.assertEqual(rc, 2, out)
        self.assertIn("hotfix", out)
        rc, out = cli("gate", "ws-feat-9", "intake", "--allow-mirror-divergence", "0")
        self.assertNotEqual(rc, 0, out)


class Exception_(MirrorDiffCase):
    """The user's explicit decision allows one named divergence."""

    def diverge(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        commit_file(mwt, "CHANGELOG.md", "pack only\n", "[Area] - changelog")
        return mwt

    def allow(self, n):
        run = core.run_load(self.rid)
        mirrordiff.record_allowance(run, core.workspace_load("ws"), n)
        core.run_save(run)

    def test_valid_user_decision_allows_and_is_stored(self):
        self.diverge()
        self.assertTrue(self.errors())
        n = self.decide("mirror diff: the changelog exists only on pack")
        self.allow(n)
        allow = core.run_load(self.rid)["mirror_allow"]
        self.assertEqual((allow["decision"], allow["reason"]), (n, "the changelog exists only on pack"))
        self.assertEqual(allow["files"], {"React/pack": ["CHANGELOG.md"]})
        self.assertEqual(self.errors(), [])
        rc, out = cli("run", "mirror-diff", self.rid, "--json")
        self.assertEqual(rc, 1, out)  # still divergent for readers, but flagged as allowed
        self.assertEqual(json.loads(out)["allowed_by_decision"], n)

    def test_non_user_decision_is_refused(self):
        self.diverge()
        n = self.decide("mirror diff: agent says fine", by="agent")
        with self.assertRaises(core.FactoryError) as cm:
            self.allow(n)
        self.assertIn("not a user decision", str(cm.exception))
        self.assertTrue(self.errors())

    def test_wrong_prefix_is_refused(self):
        self.diverge()
        n = self.decide("the mirror may differ")
        with self.assertRaises(core.FactoryError) as cm:
            self.allow(n)
        self.assertIn("mirror diff:", str(cm.exception))
        n = self.decide("mirror diff:   ")
        with self.assertRaises(core.FactoryError):
            self.allow(n)

    def test_missing_decision_is_refused(self):
        self.diverge()
        with self.assertRaises(core.FactoryError):
            self.allow(7)

    def test_nothing_to_allow_when_equal(self):
        self.base_fix()
        commit_file(self.mirror_wt(), "src/util.js", FIX, "[Area] - fix")
        n = self.decide()
        with self.assertRaises(core.FactoryError) as cm:
            self.allow(n)
        self.assertIn("no mirror divergence", str(cm.exception))

    def test_allowance_dies_when_the_divergence_changes(self):
        mwt = self.diverge()
        self.allow(self.decide("mirror diff: changelog only on pack"))
        self.assertEqual(self.errors(), [])
        commit_file(mwt, "src/helper.js", "module.exports = { x: 1 };\n", "[Area] - more")  # a new divergence
        errs = self.errors()
        self.assertTrue(any("helper.js" in e for e in errs), errs)
        self.assertTrue(any("no longer matches" in e for e in errs), errs)

    def test_allowance_dies_when_the_divergent_file_changes_again(self):
        mwt = self.diverge()
        self.allow(self.decide("mirror diff: changelog only on pack"))
        commit_file(mwt, "CHANGELOG.md", "pack only, edited\n", "[Area] - changelog 2")
        self.assertTrue(self.errors())

    def test_ship_plan_fails_then_passes_and_describes_the_divergence_on_the_mirror_mr(self):
        self.acs()
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        (core.run_dir(self.rid) / "diagnosis.md").write_text("## Root cause\nx\n\n## Evidence\ny\n\n## Ruled out\nz\n", encoding="utf-8")
        self.ok("prove", self.rid, "--repro")
        self.ok("gate", self.rid, "reproduce")
        self.fix()
        self.ok("gate", self.rid, "build")
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        pwt = core.Path(core.run_load(self.rid)["repos"]["React"]["worktrees"]["pack"])
        (pwt / "CHANGELOG.md").write_text("x\n", encoding="utf-8")  # not `git add -A`: prove leaves __pycache__ behind
        sh("git", "add", "CHANGELOG.md", cwd=pwt)
        sh("git", "commit", "-q", "--amend", "--no-edit", cwd=pwt)  # red-green takes HEAD as the fix commit
        self.ok("prove", self.rid)
        rc, out = cli("--json", "ship", "plan", self.rid)
        self.assertEqual(rc, 1, out)
        self.assertIn("CHANGELOG.md", out)
        n = self.decide("mirror diff: the changelog exists only on pack")
        bad = self.decide("mirror diff: ignored", by="agent")
        rc, out = cli("--json", "ship", "plan", self.rid, "--allow-mirror-divergence", str(bad))
        self.assertEqual(rc, 2, out)
        rc, out = cli("--json", "ship", "plan", self.rid, "--allow-mirror-divergence", str(n))
        self.assertEqual(rc, 0, out)
        plan = json.loads(out)
        base, mirror = plan["mrs"]
        self.assertNotIn("Mirror divergence", base["description"])
        self.assertIn("## Mirror divergence (approved by the user)", mirror["description"])
        self.assertIn("the changelog exists only on pack", mirror["description"])
        self.assertIn("- CHANGELOG.md", mirror["description"])
        rc, out = cli("--json", "ship", "plan", self.rid)  # stored once on the run: later plans honour it
        self.assertEqual(rc, 0, out)


class MultiRepo(MirrorDiffCase):
    repos = ("API", "React")

    def setUp(self) -> None:
        super().setUp()  # seeds both repos (HotfixCase seeded React only)
        self.ok("run", "branch", self.rid, "API", "--base", "homolog")

    def test_report_is_per_repo_and_target(self):
        self.base_fix("API")
        self.base_fix("React")
        api, react = self.mirror_wt("API"), self.mirror_wt("React")
        commit_file(react, "src/util.js", FIX, "[Area] - fix")
        commit_file(api, "src/util.js", FIX.replace("return 2;", "return 22;"), "[Area] - fix")
        report = self.report()
        by = {(e["repo"], e["target"]): e for e in report["entries"]}
        self.assertEqual(set(by), {("API", "pack"), ("React", "pack")})
        self.assertTrue(by[("API", "pack")]["files"])
        self.assertEqual(by[("React", "pack")]["files"], [])
        errs = self.errors()
        self.assertTrue(any(e.startswith("API/pack") for e in errs), errs)
        self.assertFalse(any(e.startswith("React/pack") for e in errs), errs)

    def test_a_repo_without_a_mirror_yet_is_skipped(self):
        self.base_fix("API")
        self.base_fix("React")
        commit_file(self.mirror_wt("React"), "src/util.js", FIX, "[Area] - fix")
        report = self.report()
        self.assertEqual([(e["repo"]) for e in report["entries"]], ["React"])
        self.assertTrue(report["equal"])


if __name__ == "__main__":
    unittest.main()
