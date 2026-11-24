"""Mirror diff gate (DESIGN section 9): a hotfix mirror carries the SAME CHANGES as the base fix, with a user exception.

Real temporary git repositories: the base target `homolog` and the mirror target `pack`, where `pack` may have moved
files (the mirror then has to adapt paths, nothing else).
"""

from __future__ import annotations

import json
import subprocess
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


def commit_bytes(repo: Path, rel: str, data: bytes, msg: str = "[Area] - change") -> None:
    """Like commit_file but byte-exact (write_text would translate newlines on Windows)."""
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_bytes(data)
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", msg, cwd=repo)


class Placement(MirrorDiffCase):
    """Same lines in another function are a divergence (placement = git function context, not the neighbour lines)."""

    GUARD = "  if (x == null) return null;\n"

    def guarded(self, after: str) -> str:
        return UTIL.replace(f"function {after}() {{\n", f"function {after}() {{\n{self.GUARD}")

    def base_wt(self):
        return core.Path(core.run_load(self.rid)["repos"]["React"]["worktree"])

    def test_same_line_added_in_another_function_fails(self):
        self.base_fix(util=self.guarded("a"))
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", self.guarded("b"), "[Area] - fix")
        errs = self.errors()
        self.assertTrue(any("src/util.js" in e and "same lines, different place/context" in e for e in errs), errs)
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 1, out)
        self.assertIn("same lines, different place/context", out)

    def test_without_any_function_context_the_changed_lines_alone_count(self):
        """No git function context at all (every line indented): the place cannot be told, so the same changed lines
        pass wherever they sit (the neighbours are not part of the comparison), and different ones still fail."""
        text = "".join(f"  row {i}\n" for i in range(8))
        repo = self.repo_dirs["React"]
        for t in ("homolog", "pack"):
            sh("git", "checkout", "-q", t, cwd=repo)
            commit_file(repo, "data.txt", text, "[Area] - data")
            sh("git", "update-ref", f"refs/remotes/origin/{t}", t, cwd=repo)
        sh("git", "checkout", "-q", "main", cwd=repo)
        wt = self.base_wt()
        sh("git", "reset", "-q", "--hard", "homolog", cwd=wt)
        commit_file(wt, "data.txt", text.replace("  row 1\n", "  row 1\n  added\n"), "[Area] - fix")
        mwt = self.mirror_wt()
        commit_file(mwt, "data.txt", text.replace("  row 5\n", "  row 5\n  added\n"), "[Area] - fix")
        self.assertEqual(self.errors(), [])
        commit_file(mwt, "data.txt", text.replace("  row 5\n", "  row 5\n  added!\n"), "[Area] - fix again")
        errs = self.errors()
        self.assertTrue(any("data.txt" in e for e in errs), errs)

    def test_shifted_offsets_with_the_same_neighbours_still_pass(self):
        self.base_fix(util=self.guarded("a"))
        repo = self.repo_dirs["React"]
        sh("git", "checkout", "-q", "pack", cwd=repo)
        commit_file(repo, "src/util.js", "// banner\n" * 5 + UTIL, "[Area] - banner")
        sh("git", "checkout", "-q", "main", cwd=repo)
        sh("git", "update-ref", "refs/remotes/origin/pack", "pack", cwd=repo)
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", "// banner\n" * 5 + self.guarded("a"), "[Area] - fix")
        self.assertEqual(self.errors(), [])


class ContextAndImports(MirrorDiffCase):
    """Changed lines decide; placement is the function context; imports of a moved module are masked."""

    CONSUMER = ('import a from "./a";\nimport b from "./b";\nimport { old } from "./mod/compare";\n\n'
                'export function run(a) {\n  return a;\n}\n')
    MODULE = "export const old = 1;\nexport const shouldSkip = () => false;\n"
    GUARD = "  if (x == null) return null;\n"

    def prepare(self, base_files, pack_files, moves=()):
        repo = self.repo_dirs["React"]
        for t, files in (("homolog", base_files), ("pack", pack_files)):
            sh("git", "checkout", "-q", t, cwd=repo)
            for rel, content in files.items():
                commit_file(repo, rel, content, "[Area] - seed")
            if t == "pack":
                for old, new in moves:
                    (repo / Path(new).parent).mkdir(parents=True, exist_ok=True)
                    sh("git", "mv", old, new, cwd=repo)
                    sh("git", "commit", "-q", "-m", "[Area] - move", cwd=repo)
            sh("git", "update-ref", f"refs/remotes/origin/{t}", t, cwd=repo)
        sh("git", "checkout", "-q", "main", cwd=repo)
        wt = core.Path(core.run_load(self.rid)["repos"]["React"]["worktree"])
        sh("git", "reset", "-q", "--hard", "homolog", cwd=wt)
        return wt

    def pack_consumer(self, spec="@shared/compare"):
        """The consumer on `pack`: another second import, another specifier, another line in the function."""
        return (self.CONSUMER.replace('import b from "./b";', 'import b2 from "./b2";')
                .replace('"./mod/compare"', f'"{spec}"').replace("  return a;", "  const t = 1;\n  return a;"))

    @staticmethod
    def fixed(text: str) -> str:
        return (text.replace("{ old }", "{ old, shouldSkip }")
                .replace("run(a) {\n", "run(a) {\n  if (shouldSkip(a)) return null;\n"))

    def test_moved_module_via_another_specifier_other_neighbours_other_import_context_pass(self):
        wt = self.prepare({"src/consumer.js": self.CONSUMER, "src/mod/compare.js": self.MODULE},
                          {"src/consumer.js": self.pack_consumer(), "src/mod/compare.js": self.MODULE},
                          moves=[("src/mod/compare.js", "shared/compare.js")])
        commit_file(wt, "src/consumer.js", self.fixed(self.CONSUMER), "[Area] - fix")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/consumer.js", self.fixed(self.pack_consumer()), "[Area] - fix")
        self.assertEqual(self.errors(), [])
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 0, out)

    def test_specifier_change_not_backed_by_a_moved_module_fails(self):
        wt = self.prepare({"src/consumer.js": self.CONSUMER, "src/mod/compare.js": self.MODULE},
                          {"src/consumer.js": self.pack_consumer(), "src/mod/compare.js": self.MODULE})  # nothing moved
        commit_file(wt, "src/consumer.js", self.fixed(self.CONSUMER), "[Area] - fix")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/consumer.js", self.fixed(self.pack_consumer()), "[Area] - fix")
        errs = self.errors()
        self.assertTrue(any("src/consumer.js" in e and "@shared/compare" in e for e in errs), errs)

    def test_a_different_module_is_not_masked_by_another_moved_one(self):
        wt = self.prepare({"src/consumer.js": self.CONSUMER, "src/mod/compare.js": self.MODULE},
                          {"src/consumer.js": self.pack_consumer("@shared/other"), "src/mod/compare.js": self.MODULE},
                          moves=[("src/mod/compare.js", "shared/compare.js")])
        commit_file(wt, "src/consumer.js", self.fixed(self.CONSUMER), "[Area] - fix")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/consumer.js", self.fixed(self.pack_consumer("@shared/other")), "[Area] - fix")
        self.assertTrue(self.errors())

    def test_same_function_with_different_neighbour_lines_passes(self):
        base = UTIL.replace("function a() {\n", "function a() {\n  const k = 0;\n")
        pack = UTIL.replace("function a() {\n  return 1;", "function a() {\n  const q = 5;\n  return 1;")
        wt = self.prepare({"src/util.js": base}, {"src/util.js": pack})
        commit_file(wt, "src/util.js", base.replace("function a() {\n", "function a() {\n" + self.GUARD), "[Area] - fix")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", pack.replace("function a() {\n", "function a() {\n" + self.GUARD), "[Area] - fix")
        self.assertEqual(self.errors(), [])

    def test_same_lines_in_another_function_shows_both_contexts(self):
        wt = self.prepare({}, {})
        commit_file(wt, "src/util.js", UTIL.replace("function a() {\n", "function a() {\n" + self.GUARD), "[Area] - fix")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", UTIL.replace("function c() {\n", "function c() {\n" + self.GUARD), "[Area] - fix")
        errs = self.errors()
        self.assertTrue(any("different place" in e and "base in `function a() {`" in e and "mirror in `function c() {`" in e
                            for e in errs), errs)

    def test_one_of_two_changed_lines_moved_to_another_function_fails(self):
        wt = self.prepare({}, {})
        base = UTIL.replace("function a() {\n", "function a() {\n" + self.GUARD).replace("function b() {\n", "function b() {\n" + self.GUARD)
        mirror = UTIL.replace("function a() {\n", "function a() {\n" + self.GUARD).replace("function c() {\n", "function c() {\n" + self.GUARD)
        commit_file(wt, "src/util.js", base, "[Area] - fix")
        commit_file(self.mirror_wt(), "src/util.js", mirror, "[Area] - fix")
        self.assertTrue(any("different place" in e for e in self.errors()))

    def test_different_changed_lines_in_the_same_function_still_fail(self):
        wt = self.prepare({}, {})
        commit_file(wt, "src/util.js", UTIL.replace("return 1;", "return 10;"), "[Area] - fix")
        commit_file(self.mirror_wt(), "src/util.js", UTIL.replace("return 1;", "return 11;"), "[Area] - fix")
        self.assertTrue(self.errors())


class ImportDetection(unittest.TestCase):
    def test_import_like_lines_across_languages(self):
        for line in ('import x from "y";', '} from "y";', 'from "y"', 'import "y";', "const x = require('y');",
                     "from pkg.mod import thing", "import pkg.mod", '#include "y.h"', 'export * from "y";'):
            self.assertTrue(mirrordiff._is_import(line), line)
        for line in ("using System.Text;", "return 1;", "const important = 2;", "  one,"):
            self.assertFalse(mirrordiff._is_import(line), line)

    def test_masking_needs_a_moved_module(self):
        moved = frozenset({"compare"})
        a = mirrordiff._norm_line('} from "../helpers/compare";', False, moved)
        b = mirrordiff._norm_line('} from "@shared/compare";', False, moved)
        self.assertEqual(a, b)
        self.assertNotEqual(mirrordiff._norm_line('} from "../helpers/other";', False, moved),
                            mirrordiff._norm_line('} from "@shared/other";', False, moved))
        self.assertEqual(mirrordiff._norm_line("from a.compare import x", False, moved),
                         mirrordiff._norm_line("from b.compare import x", False, moved))
        self.assertEqual(mirrordiff._norm_line("  return '../helpers/compare';", False, moved), "  return '../helpers/compare';")


class ExactBytes(MirrorDiffCase):
    def base_wt(self):
        return core.Path(core.run_load(self.rid)["repos"]["React"]["worktree"])

    def test_crlf_versus_lf_is_a_divergence(self):
        self.base_fix()
        commit_bytes(self.base_wt(), "src/new.js", b"a = 1\nb = 3\n")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        commit_bytes(mwt, "src/new.js", b"a = 1\r\nb = 3\r\n")
        errs = self.errors()
        self.assertTrue(any("src/new.js" in e and "line endings differ" in e for e in errs), errs)
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertEqual(rc, 1, out)
        self.assertIn("\\r", out)

    def test_trailing_space_on_the_last_added_line_is_a_divergence(self):
        self.base_fix()
        commit_bytes(self.base_wt(), "src/new.js", b"a = 1\nb = 3\n")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        commit_bytes(mwt, "src/new.js", b"a = 1\nb = 3 \n")
        self.assertTrue(any("src/new.js" in e for e in self.errors()))

    def test_autocrlf_checkout_with_identical_committed_changes_passes(self):
        """core.autocrlf=true (a Windows checkout): git commits LF blobs and diffs the blobs, so a CRLF working file on
        one side and an LF one on the other are the same committed change."""
        sh("git", "config", "core.autocrlf", "true", cwd=self.repo_dirs["React"])
        self.base_fix()
        wt = self.base_wt()
        commit_bytes(wt, "src/new.js", b"a = 1\r\nb = 3\r\n")
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        commit_bytes(mwt, "src/new.js", b"a = 1\nb = 3\n")
        blob = subprocess.run(["git", "cat-file", "blob", "HEAD:src/new.js"], cwd=str(wt), capture_output=True).stdout
        self.assertEqual(blob, b"a = 1\nb = 3\n")  # committed as LF
        self.assertEqual(self.errors(), [])


class FilesAndPaths(MirrorDiffCase):
    def test_mode_only_change_on_one_side_is_ignored(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        sh("git", "update-index", "--chmod=+x", "tests/a.test.js", cwd=mwt)
        sh("git", "commit", "-q", "-m", "[Area] - chmod", cwd=mwt)
        self.assertIn("100755", sh("git", "diff", "--raw", "HEAD~1", "HEAD", cwd=mwt))
        self.assertEqual(self.errors(), [])
        self.assertTrue(self.report()["equal"])

    def test_non_ascii_and_spaced_paths_are_matched_to_their_own_hunks(self):
        a, b = "src/ação.js", "src/my file.js"
        repo = self.repo_dirs["React"]
        for t in ("homolog", "pack"):
            sh("git", "checkout", "-q", t, cwd=repo)
            commit_file(repo, a, "one\n", "[Area] - a")
            commit_file(repo, b, "two\n", "[Area] - b")
            sh("git", "update-ref", f"refs/remotes/origin/{t}", t, cwd=repo)
        sh("git", "checkout", "-q", "main", cwd=repo)
        wt = core.Path(core.run_load(self.rid)["repos"]["React"]["worktree"])
        sh("git", "reset", "-q", "--hard", "homolog", cwd=wt)
        commit_file(wt, a, "one\nuno\n", "[Area] - fix a")
        commit_file(wt, b, "two\ndos\n", "[Area] - fix b")
        mwt = self.mirror_wt()
        commit_file(mwt, a, "one\nuno\n", "[Area] - fix a")
        commit_file(mwt, b, "two\ndos\n", "[Area] - fix b")
        self.assertEqual(self.errors(), [])
        commit_file(mwt, b, "two\ndos!\n", "[Area] - fix b again")
        files = [f["file"] for f in self.report()["entries"][0]["files"]]
        self.assertEqual(files, [b])


class ComparisonErrors(MirrorDiffCase):
    def test_a_comparison_error_cannot_be_covered_by_the_allowance(self):
        self.base_fix()
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX, "[Area] - fix")
        commit_file(mwt, "CHANGELOG.md", "pack only\n", "[Area] - changelog")
        n = self.decide("mirror diff: changelog only on pack")
        run = core.run_load(self.rid)
        mirrordiff.record_allowance(run, core.workspace_load("ws"), n)
        core.run_save(run)
        self.assertEqual(self.errors(), [])
        repo = self.repo_dirs["React"]  # the mirror target ref disappears: nothing can be compared
        sh("git", "update-ref", "-d", "refs/remotes/origin/pack", cwd=repo)
        sh("git", "branch", "-D", "pack", cwd=repo)
        errs = self.errors()
        self.assertTrue(any("cannot be computed" in e for e in errs), errs)
        self.assertFalse(mirrordiff.allowance_valid(core.run_load(self.rid), self.report()))
        n = self.decide("mirror diff: second try")
        with self.assertRaises(core.FactoryError) as cm:
            mirrordiff.record_allowance(core.run_load(self.rid), core.workspace_load("ws"), n)
        self.assertIn("could not be computed", str(cm.exception))

    def test_refs_and_merge_bases_are_printed_with_a_fetch_hint_when_local_and_origin_differ(self):
        self.base_fix()
        repo = self.repo_dirs["React"]
        sh("git", "checkout", "-q", "pack", cwd=repo)
        commit_file(repo, "NEWS.md", "news\n", "[Area] - news")
        sh("git", "update-ref", "refs/remotes/origin/pack", "pack", cwd=repo)
        sh("git", "reset", "-q", "--hard", "main", cwd=repo)  # the local `pack` is now behind origin/pack
        sh("git", "checkout", "-q", "main", cwd=repo)
        mwt = self.mirror_wt()
        commit_file(mwt, "src/util.js", FIX.replace("return 2;", "return 22;"), "[Area] - fix")
        rc, out = cli("run", "mirror-diff", self.rid, "--json")
        self.assertEqual(rc, 1, out)
        refs = json.loads(out)["entries"][0]["refs"]
        self.assertEqual((refs["mirror"]["ref"], refs["base"]["ref"]), ("origin/pack", "origin/homolog"))
        self.assertEqual(refs["mirror"]["merge_base"], sh("git", "rev-parse", "origin/pack", cwd=repo))
        self.assertTrue(refs["mirror"]["stale_hint"])
        self.assertFalse(refs["base"]["stale_hint"])
        rc, out = cli("run", "mirror-diff", self.rid)
        self.assertIn("compared base origin/homolog@", out)
        self.assertIn("merge-base", out)
        self.assertIn("git fetch", out)
        self.assertTrue(any("compared base origin/homolog@" in e for e in self.errors()))


class GateProveFlow(MirrorDiffCase):
    def test_gate_prove_enforces_the_mirror_diff_without_a_rewind(self):
        self.acs()
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        (core.run_dir(self.rid) / "diagnosis.md").write_text("## Root cause\nx\n\n## Evidence\ny\n\n## Ruled out\nz\n", encoding="utf-8")
        self.ok("prove", self.rid, "--repro")
        self.ok("gate", self.rid, "reproduce")
        self.fix()
        self.ok("gate", self.rid, "build")  # no mirror yet: nothing to compare
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        pwt = core.Path(core.run_load(self.rid)["repos"]["React"]["worktrees"]["pack"])
        (pwt / "CHANGELOG.md").write_text("x\n", encoding="utf-8")
        sh("git", "add", "CHANGELOG.md", cwd=pwt)
        sh("git", "commit", "-q", "--amend", "--no-edit", cwd=pwt)
        self.ok("prove", self.rid)
        rc, out = cli("gate", self.rid, "prove")
        self.assertEqual(rc, 1, out)
        self.assertIn("mirror diff", out)
        self.assertIn("CHANGELOG.md", out)
        self.assertEqual(core.run_load(self.rid)["phase"], "prove")
        bad = self.decide("mirror diff: ignored", by="agent")
        rc, out = cli("gate", self.rid, "prove", "--allow-mirror-divergence", str(bad))
        self.assertEqual(rc, 2, out)
        n = self.decide("mirror diff: the changelog exists only on pack")
        rc, out = cli("gate", self.rid, "prove", "--allow-mirror-divergence", str(n))
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load(self.rid)["mirror_allow"]["decision"], n)


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
