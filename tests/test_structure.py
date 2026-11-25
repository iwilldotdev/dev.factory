"""Structural tests: provider-name grep ban, size ceilings, git safety, no network, no dynamic imports."""
import ast
import re
import subprocess
import unittest
from pathlib import Path

from tests.helpers import ROOT, lesson

PKG = ROOT / "dev_factory"
MODULE_CAP, CORE_CAP = 400, 8500
# Built from fragments so this file passes its own scan. Provider, vendor and tool names are banned everywhere.
BANNED = ["".join(p) for p in (
    ("ji", "ra"), ("git", "lab"), ("git", "hub"), ("bit", "bucket"), ("fig", "ma"), ("conflu", "ence"), ("atlas", "sian"),
    ("cla", "ude"), ("anthro", "pic"), ("open", "ai"), ("cod", "ex"), ("copi", "lot"), ("cur", "sor"), ("gem", "ini"),
    ("play", "wright"), ("pupp", "eteer"), ("sele", "nium"), ("cypr", "ess"), ("jenk", "ins"), ("sonar", "qube"), ("sla", "ck"),
    ("az", "ure"), ("n", "pm"), ("ya", "rn"), ("pn", "pm"), ("dot", "net"), ("pyt", "est"), ("je", "st"), ("es", "lint"), ("dock", "er"),
    ("kuber", "netes"), ("mav", "en"), ("grad", "le"))]
# Decision 13: a harness adapter id may appear in the module that generates its file and in the test of that file only.
HARNESS_ALLOW: dict[str, set[str]] = {"".join(("cla", "ude")): {"dev_factory/harness.py", "tests/test_harness.py", "tests/test_adapters.py", "docs/install.md", "docs/transition-from-v0.md"}}
# Decision 14: the bundled runner is a Node project with its own dependencies; its tool names may appear under browser/ only.
RUNNER_TOOLS = {"".join(("n", "pm")), "".join(("play", "wright"))}
CI_DIR = "." + "git" + "hub"
SKIP_DIRS = {".git", CI_DIR, "__pycache__", ".tmp", "node_modules", "dist"}


def repo_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True,
                             stdin=subprocess.DEVNULL, timeout=60, check=True).stdout.decode()
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]
    return [ROOT / n for n in names if not (set(Path(n).parts) & SKIP_DIRS) and (ROOT / n).is_file()]


def source_files() -> list[Path]:
    return sorted(PKG.rglob("*.py"))


class GrepBanTests(unittest.TestCase):
    @lesson("L-172")
    def test_no_provider_vendor_or_tool_names_anywhere_in_the_repo(self):
        """L-172: a grep over code, tests and docs rejects provider, vendor and tool names (version-control and CI folders excluded)."""
        pattern = re.compile(r"\b(" + "|".join(BANNED) + r")\b", re.I)
        hits = []
        files = repo_files()
        self.assertGreater(len(files), 20)
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for m in pattern.finditer(line):
                    rel = path.relative_to(ROOT).as_posix()
                    if rel not in HARNESS_ALLOW.get(m.group(1).lower(), set()) and not (rel.startswith("browser/") and m.group(1).lower() in RUNNER_TOOLS):
                        hits.append(f"{path.relative_to(ROOT).as_posix()}:{n}: {m.group(1)}")
        self.assertEqual(hits, [], "banned names found")

    def test_the_ban_scan_covers_docs_and_tests_and_skips_vcs_and_ci_folders(self):
        names = {p.relative_to(ROOT).as_posix() for p in repo_files()}
        for expected in ("DESIGN.md", "LESSONS.md", "docs/parity-map.md", "tests/test_structure.py", "dev_factory/cli.py"):
            self.assertIn(expected, names)
        self.assertFalse([n for n in names if n.startswith((".git/", CI_DIR + "/"))])

    def test_the_pattern_actually_matches(self):
        pattern = re.compile(r"\b(" + "|".join(BANNED) + r")\b", re.I)
        self.assertTrue(pattern.search("see the " + "Ji" + "ra ticket"))
        self.assertFalse(pattern.search("a generic tracker"))


class SizeTests(unittest.TestCase):
    def counts(self):
        return {p.name: len(p.read_text(encoding="utf-8").splitlines()) for p in source_files()}

    def test_every_module_is_at_most_400_lines(self):
        """Decision 16: module cap 400 lines."""
        over = {k: v for k, v in self.counts().items() if v > MODULE_CAP}
        self.assertEqual(over, {})

    def test_the_python_core_stays_under_8500_lines(self):
        """Decision 16: core cap 8,500 lines."""
        total = sum(self.counts().values())
        self.assertLessEqual(total, CORE_CAP)

    def test_the_node_runner_stays_under_900_lines(self):
        """Decision 16: the bundled runner has its own cap of 900 lines (tests excluded)."""
        total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in (ROOT / "browser" / "src").glob("*.js"))
        self.assertGreater(total, 100)
        self.assertLessEqual(total, 900)

    def test_adapter_text_is_at_most_100_lines(self):
        from dev_factory import harness
        self.assertLessEqual(len(harness.agents_md_section().splitlines()), 100)
        self.assertEqual(sorted(harness.FILES) + ["none"], sorted(harness.IDS))


class SafetyTests(unittest.TestCase):
    GIT_DANGER = re.compile(r"\bgit\b.*\b(reset|stash|clean)\b|--force\b|--force-with-lease|\bpush\b.*\s-f\b")

    @lesson("L-106")
    def test_no_git_call_site_runs_reset_stash_clean_or_force_push(self):
        """L-106: a grep-style scan over the CLI source for destructive git verbs."""
        hits = [f"{p.name}:{n}" for p in source_files() for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if self.GIT_DANGER.search(line)]
        self.assertEqual(hits, [])
        self.assertTrue(self.GIT_DANGER.search('run(["git", "reset", "--hard"])'))
        self.assertTrue(self.GIT_DANGER.search('git push --force origin x'))
        self.assertFalse(self.GIT_DANGER.search('run(["git", "worktree", "add"])'))

    def test_the_cli_makes_no_network_call(self):
        """Invariant: no network module is imported by the CLI (adapters are the user's code)."""
        banned = {"socket", "http", "urllib.request", "ftplib", "smtplib", "ssl", "xmlrpc", "requests", "asyncio"}
        seen = set()
        for p in (q for q in source_files() if q.name != "localhttp.py"):  # the loopback-only client, tested below
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for n in names:
                    if n in banned or n.split(".")[0] in banned:
                        seen.add(f"{p.name}: {n}")
        self.assertEqual(seen, set())

    @lesson("L-82")
    def test_the_only_network_module_refuses_every_origin_but_loopback(self):
        """L-82: localhttp.py is the one place that opens sockets, and it refuses a foreign origin or a path that changes the origin."""
        from dev_factory import localhttp
        from dev_factory.util import FactoryError
        for url in ("http://example.com/x", "http://10.0.0.5:80/", "ftp://127.0.0.1/", "http://127.0.0.1.evil.test/"):
            with self.assertRaises(FactoryError, msg=url):
                localhttp.request("GET", url)
        for path in ("//evil.test/x", "http://evil.test/", "/a" + chr(92) + "b", "x"):
            with self.assertRaises(FactoryError, msg=path):
                localhttp.join("http://127.0.0.1:1", path)
        self.assertEqual(localhttp.join("http://127.0.0.1:1/", "/a?b=1"), "http://127.0.0.1:1/a?b=1")
        importers = [p.name for p in source_files() if re.search(r"^\s*(import http|from http|import socket)", p.read_text(encoding="utf-8"), re.M)]
        self.assertEqual(importers, ["localhttp.py"])

    @lesson("L-148")
    def test_the_cli_never_imports_code_by_name_or_from_the_cwd(self):
        """L-148: no dynamic import, exec/eval or sys.path edit in the CLI, so nothing can load from the cwd."""
        bad = []
        for p in source_files():
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                    if name in {"__import__", "import_module", "exec", "eval", "run_path", "run_module"}:
                        bad.append(f"{p.name}:{node.lineno}: {name}")
                if isinstance(node, ast.Attribute) and node.attr == "path" and isinstance(node.value, ast.Name) and node.value.id == "sys":
                    bad.append(f"{p.name}:{node.lineno}: sys.path")
        self.assertEqual(bad, [])

    def test_child_processes_are_only_started_in_proc_and_close_stdin(self):
        """L-44: the only spawn site is proc.py, which sets stdin to the null device."""
        spawners = [p.name for p in source_files() if re.search(r"subprocess\.(Popen|run|call|check_output)", p.read_text(encoding="utf-8"))]
        self.assertEqual(sorted(spawners), ["proc.py"])
        text = (PKG / "proc.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("stdin=subprocess.DEVNULL"), text.count("subprocess.Popen(") + text.count("subprocess.run("))


if __name__ == "__main__":
    unittest.main()
