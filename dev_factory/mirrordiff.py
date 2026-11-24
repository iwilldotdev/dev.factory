"""The same-diff gate: the fix on a mirror target compared with the fix on the base, file by file. A cherry-pick conflict can be
"resolved" by moving tests or reformatting a file, and every other gate still passes [L-95]."""
from __future__ import annotations

import re
from pathlib import Path

from . import git

IMPORT = re.compile(r"^\s*(import\b|from\s+\S+\s+import\b|export\b.*\bfrom\b|(?:const|let|var)\b.*=\s*require\(|#include\b)")


def signature(path: Path, old: str, new: str) -> dict[str, dict[str, int]]:
    """{file: {line: net count}} of `git diff old..new`. Lines are compared without line ending and trailing whitespace, and a
    line removed in one place and added in another cancels, so relocated hunks and whitespace-only rewrites are equal [L-96]."""
    out: dict[str, dict[str, int]] = {}
    file, in_hunk = None, False
    for raw in git.git(path, "diff", "--no-color", "--no-renames", "-U0", f"{old}..{new}").out.splitlines():
        if raw.startswith("diff --git "):
            file, in_hunk = raw.rsplit(" b/", 1)[-1], False
        elif raw.startswith("@@"):
            in_hunk = True
        elif in_hunk and file and raw[:1] in "+-":
            net = out.setdefault(file, {})
            net[raw[1:].rstrip()] = net.get(raw[1:].rstrip(), 0) + (1 if raw[0] == "+" else -1)
    return {f: {k: v for k, v in net.items() if v} for f, net in out.items() if any(net.values())}


def compare(base_tree: Path, base_point: str, mirror_tree: Path, mirror_point: str) -> dict:
    """`divergent`: file -> `content` (a changed line differs, or the file is only on one side) or `imports` (only import specifiers
    differ, e.g. after a module moved; listed apart so a user decision can accept them) [L-97]."""
    a, b = signature(base_tree, base_point, git.head(base_tree)), signature(mirror_tree, mirror_point, git.head(mirror_tree))
    divergent = {}
    for f in sorted({*a, *b}):
        if a.get(f) != b.get(f):
            lines = {k for k in {*a.get(f, {}), *b.get(f, {})} if a.get(f, {}).get(k) != b.get(f, {}).get(k)}
            divergent[f] = "imports" if f in a and f in b and all(IMPORT.match(k) for k in lines) else "content"
    return {"equal": not divergent, "divergent": divergent}
