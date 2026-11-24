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
    "L-01", "L-04", "L-06", "L-58", "L-72", "L-89", "L-132", "L-135", "L-136", "L-137",
}


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

    def test_the_m1_and_m2_lessons_are_all_covered_by_a_tagged_test(self):
        tagged = {lid for _, _, ids, _ in tags() for lid in ids}
        covered = M1_COVERED | M2_COVERED
        self.assertEqual(M1_COVERED & M2_COVERED, set())
        self.assertEqual(sorted(covered - tagged), [])
        self.assertEqual(sorted(tagged - covered), [], "add new tagged lessons to M2_COVERED so they cannot silently disappear")


if __name__ == "__main__":
    unittest.main()
