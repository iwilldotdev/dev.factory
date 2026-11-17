"""Tests for install.py using temporary profile directories."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install  # noqa: E402

HARNESS_CLAUDE_MD = """# Global instructions

## Delegation to workers

Workers table and rules.

| a | b |

## dev.harness flows

When a dev-* skill is active, read the delegation file.

## My own notes

Keep me.
"""


def make_dir_link(link: Path, target: Path) -> str | None:
    """Create a directory symlink, or a junction on Windows. Returns the kind or None."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if proc.returncode == 0:
            return "junction"
    return None


def run_install(*args: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.main(list(args))
    return code, out.getvalue()


class InstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.profile = self.tmp / "profile"
        self.fh = self.tmp / "fhome"
        self.profile.mkdir()
        self.running = False
        patcher = mock.patch.object(install, "claude_running", lambda: self.running)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        # Remove links first so cleanup never follows them into targets.
        for root, dirs, files in os.walk(self.tmp, topdown=True):
            for d in list(dirs):
                p = Path(root) / d
                if install.is_link(p):
                    install.remove_link(p)
                    dirs.remove(d)
        self._tmp.cleanup()

    def base(self, *extra):
        return ("--profile", str(self.profile), "--factory-home", str(self.fh), *extra)

    def harness_profile(self):
        p = self.profile
        (p / "skills" / "dev-cycle").mkdir(parents=True)
        (p / "skills" / "dev-cycle" / "SKILL.md").write_text("harness", encoding="utf-8")
        (p / "skills" / "other").mkdir()
        (p / "agents").mkdir()
        (p / "agents" / "dev-scout.md").write_text("x", encoding="utf-8")
        (p / "agents" / "mine.md").write_text("keep", encoding="utf-8")
        (p / "hooks").mkdir()
        (p / "hooks" / "dev-worker-guard.js").write_text("//", encoding="utf-8")
        (p / "hooks" / "other.js").write_text("//", encoding="utf-8")
        (p / "dev-harness-delegation.md").write_text("deleg", encoding="utf-8")
        (p / "CLAUDE.md").write_text(HARNESS_CLAUDE_MD, encoding="utf-8")
        (p / "settings.json").write_text(json.dumps({
            "agentPushNotifEnabled": False,
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "node other.js"}]}]},
        }), encoding="utf-8")

    def test_fresh_install(self):
        code, out = run_install(*self.base())
        self.assertEqual(code, 0, out)
        p = self.profile
        self.assertTrue((p / "skills" / "factory" / "SKILL.md").is_file())
        self.assertTrue((p / "skills" / "factory" / "references").is_dir())
        for role in ("scout", "author", "builder", "auditor"):
            text = (p / "agents" / f"factory-{role}.md").read_text(encoding="utf-8")
            self.assertNotIn("{{", text)
            self.assertIn(f'node "{install.fwd(p / "hooks")}/worker-guard.js" {role}', text)
            self.assertIn(install.fwd(self.fh), text)
            if role != "scout":
                self.assertEqual(text.count(f'node "{install.fwd(p / "hooks")}/failfast.js"'), 3, role)
                for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
                    self.assertIn(f"  {event}:", text)
        self.assertIn("failfast.js", install.HOOK_FILES)
        for name in install.HOOK_FILES:
            self.assertTrue((p / "hooks" / name).is_file())
        settings = json.loads((p / "settings.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertEqual(len(cmds), 1)
        self.assertIn("protect.js", cmds[0])
        self.assertEqual(settings["env"]["FACTORY_HOME"], install.fwd(self.fh))
        md = (p / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(install.MARK_START, md)
        self.assertIn("factory-scout", md)
        self.assertTrue((p / "bin" / "factory").is_file())
        self.assertIn("python -m factory", (p / "bin" / "factory.cmd").read_text(encoding="utf-8"))
        for sub in install.FACTORY_DIRS:
            self.assertTrue((self.fh / sub).is_dir())

    def test_idempotent_and_keeps_other_settings(self):
        self.harness_profile()
        run_install(*self.base())
        first_md = (self.profile / "CLAUDE.md").read_text(encoding="utf-8")
        code, _ = run_install(*self.base())
        self.assertEqual(code, 0)
        settings = json.loads((self.profile / "settings.json").read_text(encoding="utf-8"))
        self.assertFalse(settings["agentPushNotifEnabled"])
        pre = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 2)
        self.assertEqual(sum("protect.js" in h["command"] for e in pre for h in e["hooks"]), 1)
        md = (self.profile / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(md, first_md)
        self.assertEqual(md.count(install.MARK_START), 1)

    def test_snippet_block_is_replaced_in_place(self):
        text = f"# Top\n\n{install.MARK_START}\nold\n{install.MARK_END}\n\n## After\n"
        new = install.upsert_block(text, "fresh")
        self.assertIn("fresh", new)
        self.assertNotIn("old", new)
        self.assertTrue(new.startswith("# Top"))
        self.assertIn("## After", new)

    def test_dry_run_changes_nothing(self):
        self.harness_profile()
        before = sorted(str(p) for p in self.profile.rglob("*"))
        md_before = (self.profile / "CLAUDE.md").read_text(encoding="utf-8")
        code, out = run_install(*self.base("--remove-harness", "--dry-run"))
        self.assertEqual(code, 0, out)
        self.assertIn("DRY", out)
        self.assertNotIn("DO   ", out)
        self.assertEqual(before, sorted(str(p) for p in self.profile.rglob("*")))
        self.assertEqual(md_before, (self.profile / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertFalse(self.fh.exists())

    def test_remove_harness(self):
        self.harness_profile()
        code, out = run_install(*self.base("--remove-harness"))
        self.assertEqual(code, 0, out)
        p = self.profile
        self.assertFalse((p / "skills" / "dev-cycle").exists())
        self.assertTrue((p / "skills" / "other").is_dir())
        self.assertFalse((p / "agents" / "dev-scout.md").exists())
        self.assertTrue((p / "agents" / "mine.md").is_file())
        self.assertFalse((p / "hooks" / "dev-worker-guard.js").exists())
        self.assertTrue((p / "hooks" / "other.js").is_file())
        self.assertFalse((p / "dev-harness-delegation.md").exists())
        md = (p / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertNotIn("## Delegation to workers", md)
        self.assertNotIn("## dev.harness flows", md)
        self.assertIn("## My own notes", md)
        self.assertIn("Keep me.", md)
        self.assertIn(install.MARK_START, md)
        backups = list((p / "backups").glob("harness-*"))
        self.assertEqual(len(backups), 1)
        b = backups[0]
        self.assertTrue((b / "skills" / "dev-cycle" / "SKILL.md").is_file())
        self.assertTrue((b / "agents" / "dev-scout.md").is_file())
        self.assertTrue((b / "hooks" / "dev-worker-guard.js").is_file())
        self.assertTrue((b / "dev-harness-delegation.md").is_file())
        self.assertIn("## Delegation to workers", (b / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_remove_harness_unlinks_linked_skill_without_touching_target(self):
        shared = self.tmp / "shared-skills" / "dev-ship"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("shared content", encoding="utf-8")
        (self.profile / "skills").mkdir()
        kind = make_dir_link(self.profile / "skills" / "dev-ship", shared)
        if not kind:
            self.skipTest("cannot create directory links on this system")
        self.assertTrue(install.is_link(self.profile / "skills" / "dev-ship"))
        code, out = run_install(*self.base("--remove-harness"))
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.lexists(self.profile / "skills" / "dev-ship"), kind)
        self.assertEqual((shared / "SKILL.md").read_text(encoding="utf-8"), "shared content")
        links = list((self.profile / "backups").glob("harness-*/links.json"))
        self.assertEqual(len(links), 1)
        self.assertIn("dev-ship", links[0].read_text(encoding="utf-8"))

    def test_shared_skills_folder_needs_yes(self):
        shared = self.tmp / "shared"
        (shared / "dev-cycle").mkdir(parents=True)
        (shared / "dev-cycle" / "SKILL.md").write_text("x", encoding="utf-8")
        (shared / "grill-me").mkdir()
        if not make_dir_link(self.profile / "skills", shared):
            self.skipTest("cannot create directory links on this system")
        code, out = run_install(*self.base("--remove-harness"))
        self.assertEqual(code, 1, out)
        self.assertIn("SKIP", out)
        self.assertTrue((shared / "dev-cycle").is_dir())
        self.assertFalse((shared / "factory").exists())
        code, out = run_install(*self.base("--remove-harness", "--yes"))
        self.assertEqual(code, 0, out)
        self.assertFalse((shared / "dev-cycle").exists())
        self.assertTrue((shared / "grill-me").is_dir())
        self.assertTrue((shared / "factory" / "SKILL.md").is_file())
        self.assertTrue(install.is_link(self.profile / "skills"))

    def test_invalid_settings_is_not_clobbered(self):
        (self.profile / "settings.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            run_install(*self.base())
        self.assertEqual((self.profile / "settings.json").read_text(encoding="utf-8"), "{not json")

    def mcp_file(self, servers, name=".claude.json"):
        path = self.tmp / name
        path.write_text(json.dumps({"numStartups": 3, "mcpServers": servers}, indent=2), encoding="utf-8")
        return path

    def test_mcp_config_merges_env_without_overwriting(self):
        path = self.mcp_file({
            "dev.mcp": {"command": "node", "env": {"JIRA_TOKEN": "secret", "DEV_MCP_SPILL_KB": "128"}},
            "other": {"command": "x"},
        })
        code, out = run_install(*self.base("--mcp-config", str(path)))
        self.assertEqual(code, 0, out)
        data = json.loads(path.read_text(encoding="utf-8"))
        env = data["mcpServers"]["dev.mcp"]["env"]
        self.assertEqual(env["JIRA_TOKEN"], "secret")
        self.assertEqual(env["DEV_MCP_SPILL_KB"], "128")
        self.assertEqual(env["DEV_MCP_SAVE_ALLOW_DOT"], ".factory")
        roots = env["DEV_MCP_SAVE_ROOTS"].split(os.pathsep)
        self.assertEqual(roots[0], install.fwd(self.fh / "runs"))
        self.assertTrue(roots[1].endswith("/dev.mcp"))
        self.assertNotIn("env", data["mcpServers"]["other"])
        self.assertEqual(data["numStartups"], 3)
        self.assertEqual(len(list((self.profile / "backups").glob("factory-*/.claude.json"))), 1)
        code, out = run_install(*self.base("--mcp-config", str(path)))
        self.assertIn("already has the save_to settings", out)

    def test_mcp_config_dry_run_prints_diff_and_keeps_file(self):
        path = self.mcp_file({"dev_mcp": {"command": "node"}})
        before = path.read_text(encoding="utf-8")
        code, out = run_install(*self.base("--mcp-config", str(path), "--dry-run"))
        self.assertEqual(code, 0, out)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertIn("+        \"DEV_MCP_SPILL_KB\": \"64\"", out)
        self.assertFalse((self.profile / "backups").exists())

    def test_mcp_config_without_entry_or_file(self):
        path = self.mcp_file({"other": {"command": "x"}})
        before = path.read_text(encoding="utf-8")
        code, out = run_install(*self.base("--mcp-config", str(path)))
        self.assertEqual(code, 0, out)
        self.assertIn("no dev.mcp entry", out)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        code, out = run_install(*self.base())
        self.assertEqual(code, 0, out)
        self.assertIn("default MCP config", out)
        code, out = run_install(*self.base("--mcp-config", str(self.tmp / "nope.json")))
        self.assertEqual(code, 1)

    def test_mcp_config_defaults_to_profile_file(self):
        (self.profile / ".claude.json").write_text(json.dumps({"mcpServers": {"dev.mcp": {}}}), encoding="utf-8")
        code, out = run_install(*self.base())
        self.assertEqual(code, 0, out)
        env = json.loads((self.profile / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["dev.mcp"]["env"]
        self.assertEqual(env["DEV_MCP_SPILL_KB"], "64")

    def test_mcp_diff_masks_secrets(self):
        path = self.tmp / "c.json"
        path.write_text(json.dumps({"mcpServers": {"dev.mcp": {"env": {
            "JIRA_API_TOKEN": "tok-123", "Gitlab_Password": "pw-456", "AUTH_HEADER": "Bearer zzz"}}}}, indent=2),
            encoding="utf-8")
        for extra in ("--dry-run", None):
            args = ("--mcp-config", str(path)) + ((extra,) if extra else ())
            code, out = run_install(*self.base(*args))
            for secret in ("tok-123", "pw-456", "Bearer zzz"):
                self.assertNotIn(secret, out)
            self.assertIn("***", out)
        self.assertEqual(install.mask_secrets("MY_SECRET=abc\nPATH=x"), "MY_SECRET=***\nPATH=x")

    def test_install_backs_up_what_it_overwrites(self):
        (self.profile / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8")
        (self.profile / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
        (self.profile / "agents").mkdir()
        (self.profile / "agents" / "factory-builder.md").write_text("old agent", encoding="utf-8")
        (self.profile / "hooks").mkdir()
        (self.profile / "hooks" / "protect.js").write_text("old hook", encoding="utf-8")
        (self.profile / "skills" / "factory").mkdir(parents=True)
        (self.profile / "skills" / "factory" / "SKILL.md").write_text("old skill", encoding="utf-8")
        code, out = run_install(*self.base())
        self.assertEqual(code, 0, out)
        (b,) = list((self.profile / "backups").glob("factory-*"))
        self.assertEqual((b / "settings.json").read_text(encoding="utf-8"), '{"theme": "dark"}')
        self.assertEqual((b / "CLAUDE.md").read_text(encoding="utf-8"), "# mine\n")
        self.assertEqual((b / "agents" / "factory-builder.md").read_text(encoding="utf-8"), "old agent")
        self.assertEqual((b / "hooks" / "protect.js").read_text(encoding="utf-8"), "old hook")
        self.assertEqual((b / "skills" / "factory" / "SKILL.md").read_text(encoding="utf-8"), "old skill")

    def test_running_claude_blocks_mcp_edit_without_yes(self):
        path = self.mcp_file({"dev.mcp": {}})
        before = path.read_text(encoding="utf-8")
        self.running = True
        code, out = run_install(*self.base("--mcp-config", str(path)))
        self.assertEqual(code, 1, out)
        self.assertIn("running Claude", out)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        code, out = run_install(*self.base("--mcp-config", str(path), "--yes"))
        self.assertEqual(code, 0, out)
        self.assertIn("DEV_MCP_SPILL_KB", path.read_text(encoding="utf-8"))

    def test_running_claude_does_not_block_dry_run_or_unrelated_install(self):
        self.running = True
        code, out = run_install(*self.base())
        self.assertEqual(code, 0, out)

    def test_path_hint_is_printed_not_executed(self):
        with mock.patch.dict(os.environ, {"PATH": os.defpath}):
            code, out = run_install(*self.base("--dry-run"))
        self.assertEqual(code, 0, out)
        if os.name == "nt":
            self.assertIn("SetEnvironmentVariable('Path'", out)
            self.assertNotIn("setx PATH", out)
        else:
            self.assertIn("export PATH=", out)
        self.assertIn(str(self.profile / "bin").replace("'", "''"), out.replace("/", "\\") if os.name == "nt" else out)

    def test_protect_matcher_covers_mcp_tools(self):
        run_install(*self.base())
        settings = json.loads((self.profile / "settings.json").read_text(encoding="utf-8"))
        matcher = settings["hooks"]["PreToolUse"][0]["matcher"]
        self.assertTrue(re.fullmatch(matcher, "mcp__dev_mcp__figma_get_design_context"))
        self.assertTrue(re.fullmatch(matcher, "Write"))

    def test_strip_harness_sections_without_sections(self):
        text = "# Title\n\nbody\n"
        self.assertEqual(install.strip_harness_sections(text), text)


if __name__ == "__main__":
    unittest.main()
