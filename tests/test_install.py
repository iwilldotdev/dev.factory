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
            self.assertNotIn("disallowedTools", text)
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
        self.assertIn("factory-dist", (p / "bin" / "factory.cmd").read_text(encoding="utf-8"))
        self.assertTrue((p / "factory-dist" / "provenance.json").is_file())
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

    def mcp_file(self, servers, name=".claude.json", extra=None):
        path = self.tmp / name
        path.write_text(json.dumps({"numStartups": 3, "mcpServers": servers, **(extra or {})}, indent=2), encoding="utf-8")
        return path

    def tools_line(self, role="builder"):
        text = (self.profile / "agents" / f"factory-{role}.md").read_text(encoding="utf-8")
        (line,) = [ln for ln in text.splitlines() if ln.startswith("tools:")]
        return line

    def test_agent_tools_list_has_one_entry_per_configured_server(self):
        path = self.mcp_file({
            "acme": {"command": "node", "env": {"ACME_TOKEN": "tok-9f3a"}},
            "my-other_srv": {"url": "https://x.example"},
        })
        before = path.read_text(encoding="utf-8")
        code, out = run_install(*self.base("--mcp-config", str(path)))
        self.assertEqual(code, 0, out)
        for role, base in (("scout", "Read, Grep, Glob, Bash, Write"), ("author", "Read, Grep, Glob, Write, Edit, Bash"),
                           ("builder", "Read, Grep, Glob, Write, Edit, Bash"), ("auditor", "Read, Grep, Glob, Write, Bash")):
            self.assertEqual(self.tools_line(role), f"tools: {base}, mcp__acme__*, mcp__my-other_srv__*", role)
        self.assertIn("agent tools use servers: acme, my-other_srv", out)
        self.assertNotIn("tok-9f3a", out)
        # the MCP config is only read: no edit, no backup of it, no env keys added
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(list((self.profile / "backups").glob("factory-*/.claude.json")), [])

    def test_agent_tools_list_reads_project_scoped_servers_once(self):
        path = self.mcp_file({"acme": {}}, extra={"projects": {"/p1": {"mcpServers": {"acme": {}, "beta": {}}},
                                                               "/p2": {"mcpServers": {"gamma.x": {}}}, "/p3": {}}})
        code, out = run_install(*self.base("--mcp-config", str(path)))
        self.assertEqual(code, 0, out)
        self.assertTrue(self.tools_line().endswith("Bash, mcp__acme__*, mcp__beta__*, mcp__gamma_x__*"), self.tools_line())

    def test_agent_tools_list_without_servers_has_no_mcp_entries(self):
        for servers in ({}, ):
            path = self.mcp_file(servers)
            code, out = run_install(*self.base("--mcp-config", str(path)))
            self.assertEqual(code, 0, out)
            self.assertEqual(self.tools_line("builder"), "tools: Read, Grep, Glob, Write, Edit, Bash")
            self.assertEqual(self.tools_line("scout"), "tools: Read, Grep, Glob, Bash, Write")
            self.assertIn("no MCP servers", out)
        # no config file at all (default location absent): note, exit 0, same result
        code, out = run_install(*self.base())
        self.assertEqual(code, 0, out)
        self.assertIn("default MCP config", out)
        self.assertEqual(self.tools_line("auditor"), "tools: Read, Grep, Glob, Write, Bash")
        for role in ("scout", "author", "builder", "auditor"):
            text = (self.profile / "agents" / f"factory-{role}.md").read_text(encoding="utf-8")
            self.assertNotIn("mcp__", text.split("hooks:")[0])
            self.assertNotIn("MCP_TOOLS", text)

    def test_mcp_servers_flag_overrides_the_config(self):
        path = self.mcp_file({"acme": {}})
        code, out = run_install(*self.base("--mcp-config", str(path), "--mcp-servers", "one, two-x ,,three"))
        self.assertEqual(code, 0, out)
        self.assertTrue(self.tools_line().endswith("Bash, mcp__one__*, mcp__two-x__*, mcp__three__*"), self.tools_line())
        self.assertNotIn("mcp__acme__", self.tools_line())
        self.assertIn("one, two-x, three", out)
        code, out = run_install(*self.base("--mcp-config", str(path), "--mcp-servers", ""))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tools_line(), "tools: Read, Grep, Glob, Write, Edit, Bash")

    def test_mcp_config_defaults_to_profile_file(self):
        (self.profile / ".claude.json").write_text(json.dumps({"mcpServers": {"acme": {}}}), encoding="utf-8")
        code, out = run_install(*self.base())
        self.assertEqual(code, 0, out)
        self.assertTrue(self.tools_line().endswith("mcp__acme__*"), self.tools_line())

    def test_missing_or_broken_mcp_config_is_a_skip_not_a_crash(self):
        code, out = run_install(*self.base("--mcp-config", str(self.tmp / "nope.json")))
        self.assertEqual(code, 1, out)
        self.assertEqual(self.tools_line(), "tools: Read, Grep, Glob, Write, Edit, Bash")
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, out = run_install(*self.base("--mcp-config", str(bad)))
        self.assertEqual(code, 1, out)
        self.assertEqual(self.tools_line(), "tools: Read, Grep, Glob, Write, Edit, Bash")

    def test_dry_run_prints_the_servers_and_writes_nothing(self):
        path = self.mcp_file({"acme": {}})
        code, out = run_install(*self.base("--mcp-config", str(path), "--dry-run"))
        self.assertEqual(code, 0, out)
        self.assertIn("agent tools use servers: acme", out)
        self.assertFalse((self.profile / "agents").exists())

    def test_render_helpers(self):
        self.assertEqual(install.mcp_tool_server("my.server name"), "my_server_name")
        self.assertEqual(install.mcp_server_names([1]), [])
        self.assertEqual(install.mcp_server_names({"mcpServers": {"a": {}}, "projects": {"x": {"mcpServers": {"b": {}, "a": {}}}}}),
                         ["a", "b"])
        text = "tools: Read, Bash, {{MCP_TOOLS}}\nnext"
        self.assertEqual(install.render_mcp_tools(text, ["a", "b"]), "tools: Read, Bash, mcp__a__*, mcp__b__*\nnext")
        self.assertEqual(install.render_mcp_tools(text, []), "tools: Read, Bash\nnext")

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

    def test_installing_while_claude_runs_is_not_special(self):
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
        self.assertTrue(re.fullmatch(matcher, "mcp__acme__get_node"))
        self.assertTrue(re.fullmatch(matcher, "Write"))

    def test_strip_harness_sections_without_sections(self):
        text = "# Title\n\nbody\n"
        self.assertEqual(install.strip_harness_sections(text), text)


if __name__ == "__main__":
    unittest.main()
