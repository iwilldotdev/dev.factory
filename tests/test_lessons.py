"""Lesson mechanism: every @lesson tag points at a `test` row of LESSONS.md and is cited in the test's docstring."""
import ast
import re
import unittest

from tests.helpers import ROOT

# Lessons that M1 code implements and a test covers. A later change that drops one of these tests fails here.
M1_COVERED = {
    "L-26", "L-28", "L-29", "L-31", "L-42", "L-43", "L-44", "L-45", "L-80", "L-103", "L-106", "L-121", "L-122", "L-126", "L-129",
    "L-131", "L-134", "L-140", "L-141", "L-142", "L-143", "L-148", "L-150", "L-152", "L-156", "L-157", "L-158", "L-162", "L-163",
    "L-165", "L-166", "L-167", "L-168", "L-171", "L-172", "L-173", "L-174", "L-177",
}
# Lessons that M2 (plan, lock, worktrees, cmd checks, verify, reproduce, red-green, commit pattern, metrics) covers.
M2_COVERED = {
    "L-01", "L-02", "L-03", "L-04", "L-05", "L-06", "L-07", "L-08", "L-09", "L-10", "L-11", "L-14", "L-27", "L-33", "L-34", "L-35", "L-38", "L-47", "L-48", "L-58", "L-72", "L-89", "L-90", "L-132", "L-135", "L-136", "L-137", "L-139", "L-178", "L-180", "L-181",
}
# Lessons that M3 (review, ship, ship verify) covers; M2 lessons that M3 tests touch again (L-135 to L-137) stay in M2_COVERED.
M3_COVERED = {
    "L-18", "L-19", "L-20", "L-21", "L-22", "L-23", "L-39", "L-107", "L-108", "L-138", "L-144", "L-145", "L-146", "L-147", "L-151", "L-153", "L-154",
    "L-155",
}
# Lessons that M4 (workspace runs, env, http, browser, login, data, design, screenshots, UAT) covers; it includes the four deferred from M2.
M4_COVERED = {
    "L-12", "L-13", "L-37", "L-40", "L-46", "L-49", "L-50", "L-51", "L-52", "L-54", "L-55", "L-56", "L-57", "L-59", "L-64", "L-65", "L-66", "L-68", "L-69",
    "L-70", "L-71", "L-73", "L-74", "L-79", "L-81", "L-82", "L-83", "L-85", "L-86", "L-175", "L-186", "L-187", "L-188", "L-190",
}
# Lessons that M5 (human checks, quality baseline, the design note, estimates, token metrics) covers; the feedback tests also touch L-20, L-135, L-136
# and L-155, which stay in their first milestone.
M5_COVERED = {"L-24", "L-60", "L-61", "L-62", "L-176", "L-183", "L-189"}
# Lessons that M6 (hotfix mirror and the same-diff gate, epics, review runs, claims, slots) covers.
M6_COVERED = {"L-41", "L-93", "L-95", "L-96", "L-97", "L-98", "L-99", "L-100"}


def rows() -> dict[str, str]:
    """LESSONS.md id -> destination kind (`test` or `by-design`)."""
    out = {}
    for line in (ROOT / "LESSONS.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (L-\d+) \|.*\| `([a-z-]+)`", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def tags() -> list[tuple[str, str, tuple[str, ...], str]]:
    """(file, test name, lesson ids, docstring) for every function or class tagged with @lesson(...)."""
    found = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "lesson":
                        ids = tuple(a.value for a in dec.args if isinstance(a, ast.Constant))
                        found.append((path.name, node.name, ids, ast.get_docstring(node) or ""))
    return found


class LessonTests(unittest.TestCase):
    def test_the_lesson_table_is_parsed(self):
        r = rows()
        self.assertEqual(len(r), 190)
        self.assertEqual(sum(1 for v in r.values() if v == "test"), 143)
        self.assertEqual(sum(1 for v in r.values() if v == "by-design"), 47)

    def test_every_tag_points_at_a_test_row_and_is_cited_in_the_docstring(self):
        r, bad = rows(), []
        for file, name, ids, doc in tags():
            for lid in ids:
                if r.get(lid) != "test":
                    bad.append(f"{file}::{name}: {lid} is {r.get(lid, 'not in LESSONS.md')}, not a test row")
                if lid not in doc:
                    bad.append(f"{file}::{name}: docstring does not cite {lid}")
        self.assertEqual(bad, [])

    def test_the_m1_to_m6_lessons_are_all_covered_by_a_tagged_test(self):
        tagged = {lid for _, _, ids, _ in tags() for lid in ids}
        covered = M1_COVERED | M2_COVERED | M3_COVERED | M4_COVERED | M5_COVERED | M6_COVERED
        earlier = M1_COVERED | M2_COVERED | M3_COVERED | M4_COVERED | M5_COVERED
        self.assertEqual((M1_COVERED & M2_COVERED) | (M3_COVERED & (M1_COVERED | M2_COVERED)) | (M4_COVERED & (M2_COVERED | M3_COVERED)) | (M5_COVERED & (M1_COVERED | M2_COVERED | M3_COVERED | M4_COVERED)) | (M6_COVERED & earlier), set())
        self.assertEqual(sorted(covered - tagged), [])
        self.assertEqual(sorted(tagged - covered), [], "add new tagged lessons to the milestone set so they cannot silently disappear")


if __name__ == "__main__":
    unittest.main()
