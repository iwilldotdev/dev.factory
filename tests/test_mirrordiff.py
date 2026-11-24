"""The same-diff comparison on small repos: relocated hunks, line endings, trailing spaces, reformatting and import specifiers."""
import unittest

from dev_factory import mirrordiff
from tests.helpers import git, lesson
from tests.m6_support import two_branches

BODY = "".join(f"line {i}\n" for i in range(1, 21))


def compare(base_edit: dict, mirror_edit: dict, files: dict) -> dict:
    repo = two_branches(files)
    start = git("rev-parse", "HEAD", cwd=repo)
    trees = {}
    for name, edit in (("b", base_edit), ("m", mirror_edit)):
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
        base = {"a.txt": BODY.replace("line 2\n", "line 2\nadded\n")}
        moved = {"a.txt": BODY.replace("line 15\n", "line 15\nadded\n")}
        self.assertTrue(compare(base, moved, {"a.txt": BODY})["equal"])
        crlf = {"a.txt": BODY.replace("line 2\n", "line 2\nadded   \n").replace("\n", "\r\n")}
        self.assertTrue(compare(base, crlf, {"a.txt": BODY})["equal"])
        indented = {"a.txt": "".join("  " + ln for ln in base["a.txt"].splitlines(True))}
        self.assertEqual(compare(base, indented, {"a.txt": BODY})["divergent"], {"a.txt": "content"})

    @lesson("L-97")
    def test_moved_module_import_specifiers_are_their_own_divergence_class(self):
        """L-97: files whose only difference is import lines are listed as `imports`, apart from content."""
        files = {"a.ts": "import { x } from '../old'\nuse(x)\n", "b.ts": "const k = 1\n"}
        base = {"a.ts": "import { x } from '../old'\nuse(x)\nfix()\n", "b.ts": "const k = 2\n"}
        mirror = {"a.ts": "import { x } from '../../moved'\nuse(x)\nfix()\n", "b.ts": "const k = 3\n"}
        self.assertEqual(compare(base, mirror, files)["divergent"], {"a.ts": "imports", "b.ts": "content"})


if __name__ == "__main__":
    unittest.main()
