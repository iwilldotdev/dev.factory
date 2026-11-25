"""The same-diff comparison on one small repo: relocated hunks, line endings, trailing spaces, reformatting and import specifiers."""
import functools
import unittest

from dev_factory import mirrordiff
from tests.helpers import git, lesson
from tests.m6_support import two_branches

BODY = "".join(f"line {i}\n" for i in range(1, 21))
START = {"a.txt": BODY, "c.txt": BODY, "d.txt": BODY, "i.ts": "import { x } from '../old'\nuse(x)\n", "k.ts": "const k = 1\n"}
BASE = {"a.txt": BODY.replace("line 2\n", "line 2\nadded\n"), "c.txt": BODY.replace("line 2\n", "line 2\nadded\n"),
        "d.txt": BODY.replace("line 2\n", "line 2\nadded\n"), "i.ts": "import { x } from '../old'\nuse(x)\nfix()\n", "k.ts": "const k = 2\n"}
MIRROR = {"a.txt": BODY.replace("line 15\n", "line 15\nadded\n"),  # the same hunk, elsewhere
          "c.txt": BODY.replace("line 2\n", "line 2\nadded   \n").replace("\n", "\r\n"),  # CRLF and trailing spaces
          "d.txt": "".join("  " + ln for ln in BASE["d.txt"].splitlines(True)),  # reindented as a whole
          "i.ts": "import { x } from '../../moved'\nuse(x)\nfix()\n", "k.ts": "const k = 3\n"}


@functools.cache
def compare() -> dict:
    repo = two_branches(START)
    start = git("rev-parse", "HEAD", cwd=repo)
    trees = {}
    for name, edit in (("b", BASE), ("m", MIRROR)):
        trees[name] = repo.parent / f"{name}tree"
        git("worktree", "add", "-q", "-b", name, str(trees[name]), start, cwd=repo)
        for rel, data in edit.items():
            (trees[name] / rel).write_bytes(data.encode())
        git("add", "-A", cwd=trees[name])
        git("commit", "-q", "-m", name, cwd=trees[name])
    return mirrordiff.compare(trees["b"], start, trees["m"], start)


class SignatureTests(unittest.TestCase):
    @lesson("L-96")
    def test_relocated_hunks_crlf_and_trailing_spaces_are_equal_but_a_reformat_is_not(self):
        """L-96: the diff is normalized (EOL, trailing whitespace) and hunks match wherever they landed; whole-file reindentation differs."""
        divergent = compare()["divergent"]
        self.assertNotIn("a.txt", divergent)
        self.assertNotIn("c.txt", divergent)
        self.assertEqual(divergent["d.txt"], "content")

    @lesson("L-97")
    def test_moved_module_import_specifiers_are_their_own_divergence_class(self):
        """L-97: files whose only difference is import lines are listed as `imports`, apart from content."""
        self.assertEqual({f: c for f, c in compare()["divergent"].items() if f.endswith(".ts")}, {"i.ts": "imports", "k.ts": "content"})
        self.assertFalse(compare()["equal"])


    def test_binary_and_empty_files_are_part_of_the_comparison(self):
        """Review m2: a binary change or a new empty file missing on the mirror used to look equal."""
        repo = two_branches(START)
        start = git("rev-parse", "HEAD", cwd=repo)
        trees = {}
        for name, files in (("b2", {"x.bin": b"\x00\x01\x02", "e.txt": b"", "same.bin": b"\x00\x09"}), ("m2", {"x.bin": b"\x00\x01\x03", "same.bin": b"\x00\x09"})):
            trees[name] = repo.parent / f"{name}tree"
            git("worktree", "add", "-q", "-b", name, str(trees[name]), start, cwd=repo)
            for rel, data in files.items():
                (trees[name] / rel).write_bytes(data)
            git("add", "-A", cwd=trees[name])
            git("commit", "-q", "-m", name, cwd=trees[name])
        got = mirrordiff.compare(trees["b2"], start, trees["m2"], start)
        self.assertEqual(got["divergent"], {"e.txt": "content", "x.bin": "content"})

    def test_a_deleted_binary_file_compares_without_crashing(self):
        """Review round 2, m2: numstat shows `-` for a deleted binary, whose blob no longer exists at the new commit."""
        repo = two_branches({"a.txt": "x\n"})
        (repo / "logo.bin").write_bytes(b"\x00\x01")
        git("add", "logo.bin", cwd=repo)
        git("commit", "-q", "-m", "logo", cwd=repo)
        start = git("rev-parse", "HEAD", cwd=repo)
        trees = {}
        for name, drop in (("b3", True), ("m3", True), ("n3", False)):
            trees[name] = repo.parent / f"{name}tree"
            git("worktree", "add", "-q", "-b", name, str(trees[name]), start, cwd=repo)
            if drop:
                git("rm", "-q", "logo.bin", cwd=trees[name])
            else:
                (trees[name] / "a.txt").write_text("y\n")
                git("add", "-A", cwd=trees[name])
            git("commit", "-q", "-m", name, cwd=trees[name])
        self.assertTrue(mirrordiff.compare(trees["b3"], start, trees["m3"], start)["equal"])
        self.assertEqual(mirrordiff.compare(trees["b3"], start, trees["n3"], start)["divergent"]["logo.bin"], "content")


if __name__ == "__main__":
    unittest.main()
