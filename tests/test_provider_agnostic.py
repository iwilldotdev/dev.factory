"""Grep regression test: the repo carries no provider, vendor or concrete tool names.

Provider knowledge is data (FACTORY_HOME/profiles). The banned patterns are built
from fragments so this file does not match itself.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Synthetic or host-owned server names allowed inside mcp__<server>__<tool> strings.
ALLOWED_SERVERS = {
    # synthetic names used by hook/installer tests
    "acme", "acme-tickets", "x", "a-b", "a-b_c", "other", "my_server", "my-server", "my_long_server_name", "srv2",
    "plugin_acme_tickets", "get_server", "list_server", "read-only-server",
    # host-owned session and browser tools named in references/sessions.md (not providers)
    "ccd_session", "claude_browser",
}

EXCLUDED_PREFIXES = (
    "docs/research/",
    ".claude/worktrees/",
    ".tmp/",
    "node_modules/",
    "browser/dist/",
    "handoffs/",
    "tests/test_provider_agnostic.py",
)
EXCLUDED_NAMES = {"changelog.md"}
EXCLUDED_PARTS = ("/node_modules/", "/__pycache__/")


def _banned_fragments():
    return [
        r"dev[._]" + "mcp",
        "DEV_" + "MCP",
        "j" + "ira",
        "git" + "lab",
        "git" + "hub",
        "fi" + "gma",
        "conflu" + "ence",
        "atlas" + "sian",
        r"\b" + "AD" + "F" + r"\b",
        r"\b" + "gl" + "ab" + r"\b",
        r"\b" + "g" + "h" + r"\b",
        r"merge[ _-]" + "request",
        r"\b" + "M" + "R" + r"\b",
        r"\b" + "m" + "r[_-]",
        r"\b" + "i" + "id" + r"\b",
        "file" + "Key",
        "inline" + "Card",
        "linear" + r"\.app",
        "dev" + r"\.azure",
        "azure" + r"[ _-]" + "devops",
    ]


BANNED = re.compile("|".join(f"(?:{p})" for p in _banned_fragments()), re.IGNORECASE)
TOOL_NAME = re.compile(r"mcp__([a-z0-9_-]+?)__[a-z0-9_]+", re.IGNORECASE)


def repo_files():
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    files = []
    for rel in filter(None, out.split("\0")):
        low = rel.lower()
        if low.startswith(EXCLUDED_PREFIXES) or Path(low).name in EXCLUDED_NAMES:
            continue
        if any(part in "/" + low for part in EXCLUDED_PARTS):
            continue
        p = ROOT / rel
        if p.is_file():
            files.append((rel, p))
    return files


def scan(rel, path):
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        m = BANNED.search(line)
        if m:
            hits.append(f"{rel}:{n}: banned term {m.group(0)!r}: {line.strip()[:120]}")
        for t in TOOL_NAME.finditer(line):
            if t.group(1).lower() not in ALLOWED_SERVERS:
                hits.append(f"{rel}:{n}: concrete tool name {t.group(0)!r}: {line.strip()[:120]}")
    return hits


class ProviderAgnosticTests(unittest.TestCase):
    def test_no_banned_terms(self):
        hits = []
        for rel, path in repo_files():
            hits.extend(scan(rel, path))
        self.assertEqual(hits, [], "provider-specific terms found:\n" + "\n".join(hits))

    def test_no_profile_json_outside_fixtures(self):
        bad = [rel for rel, _ in repo_files()
               if Path(rel).name == "profile.json" and not rel.startswith("tests/fixtures/profiles/")]
        self.assertEqual(bad, [], "profile.json is data and must not ship in the repo:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
