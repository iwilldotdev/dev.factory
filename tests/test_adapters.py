"""Adapters of the chat harness: skill and worker files, user scope, hook entries and the hook itself [L-160, L-161]."""
import json
import os
import unittest
from unittest import mock

from dev_factory import harness, hook
from dev_factory.util import FactoryError
from tests.helpers import FactoryCase, lesson

SKILL, WORKER = ".claude/skills/factory/SKILL.md", ".claude/agents/factory-worker.md"


class Adapters(FactoryCase):
    def setUp(self):
        super().setUp()
        self.root = self.base / "target"
        self.root.mkdir()

    def read(self, rel, root=None):
        return ((root or self.root) / rel).read_text(encoding="utf-8")

    def test_the_chat_harness_writes_a_skill_a_worker_and_a_short_block_with_at_most_100_lines_each(self):
        self.ok("init", "--project", str(self.root), "--harness", "claude-code", project=False)
        files = [SKILL, WORKER, "CLAUDE.md"]
        for rel in files:
            self.assertLessEqual(len(self.read(rel).splitlines()), 100, rel)
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in (self.root / ".claude").rglob("*") if p.is_file()), sorted(files[:2]))
        self.assertTrue(self.read(SKILL).startswith("---\nname: factory\n"))
        self.assertTrue(self.read(WORKER).startswith("---\nname: factory-worker\n"))
        self.assertTrue((self.root / "factory.toml").is_file())

    def test_init_on_a_project_that_has_its_config_only_writes_the_adapter(self):
        self.ok("init", "--project", str(self.root), project=False)
        toml = self.read("factory.toml")
        out = self.ok("init", "--project", str(self.root), "--harness", "claude-code", "--hooks", project=False)
        self.assertEqual(self.read("factory.toml"), toml)
        self.assertIn("hook entries", out)
        self.assertTrue((self.root / SKILL).is_file() and (self.root / ".claude/settings.json").is_file())

    def test_the_prose_covers_the_loop_the_roles_and_every_composite_command(self):
        skill, agents = harness.text("skill.md"), harness.text("agents.md")
        both = ("factory next", "--user-said", "factory confirm CHK-n", "reviewer-<n>", "never a pass", "language", "factory review queue", "factory claim",
                "--takeover", "--dismiss", "--slot", "factory mirror", "factory epic plan", "factory wait")
        for must in both:
            self.assertIn(must, skill, must)
            self.assertIn(must, agents, must)
        self.assertIn("/factory session", skill)

    def test_a_second_run_changes_nothing_and_a_hand_edited_generated_file_is_replaced_after_a_backup(self):
        harness.write(self.root, "claude-code")
        self.assertTrue(all(m.startswith("unchanged") for m in harness.write(self.root, "claude-code")))
        self.assertEqual([p.name for p in self.root.rglob("*.factory-bak-*")], [])
        path = self.root / SKILL
        path.write_text(path.read_text(encoding="utf-8") + "my edit\n", encoding="utf-8")
        done = harness.write(self.root, "claude-code")
        self.assertIn("SKILL.md.factory-bak-1", done[1])
        self.assertIn("my edit", (path.parent / "SKILL.md.factory-bak-1").read_text(encoding="utf-8"))
        self.assertNotIn("my edit", path.read_text(encoding="utf-8"))

    def test_a_file_at_the_skill_path_that_the_adapter_does_not_own_is_left_alone(self):
        path = self.root / SKILL
        path.parent.mkdir(parents=True)
        path.write_text("# my own skill\n", encoding="utf-8")
        done = harness.write(self.root, "claude-code")
        self.assertIn("skipped", done[1])
        self.assertEqual(path.read_text(encoding="utf-8"), "# my own skill\n")
        self.assertTrue((self.root / WORKER).is_file())

    def test_user_scope_writes_into_the_host_home_creates_no_project_and_keeps_a_user_file(self):
        home = self.base / "host home"
        home.mkdir()
        (home / "CLAUDE.md").write_text("# Global notes\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {harness.HOME_ENV: str(home)}):
            self.ok("init", "--project", str(self.root), "--harness", "claude-code", "--scope", "user", project=False)
        self.assertTrue((home / "skills/factory/SKILL.md").is_file() and (home / "agents/factory-worker.md").is_file())
        self.assertTrue((home / "CLAUDE.md").read_text(encoding="utf-8").startswith("# Global notes\n"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn("no user scope", self.fails("init", "--project", str(self.root), "--harness", "agents-md", "--scope", "user", project=False))
        self.assertIn("name one", self.fails("init", "--project", str(self.root), "--scope", "user", project=False))

    @lesson("L-161")
    def test_hooks_merge_into_the_settings_idempotently_keep_other_entries_and_keys_and_leave_a_backup(self):
        """L-161: `init --harness --hooks` merges one entry into the host settings, keeps other keys, backs up, and refuses a bad or locked file."""
        settings = self.root / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-hook"}]}
        settings.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [mine], "Stop": [mine]}}), encoding="utf-8")
        self.ok("init", "--project", str(self.root), "--harness", "claude-code", "--hooks", project=False)
        doc = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(doc["model"], "x")
        self.assertEqual(doc["hooks"]["Stop"], [mine])
        self.assertEqual(doc["hooks"]["PreToolUse"], [mine, hook.ENTRY])
        self.assertEqual(json.loads((settings.parent / "settings.json.factory-bak-1").read_text(encoding="utf-8"))["hooks"]["PreToolUse"], [mine])
        self.assertIn("unchanged " + settings.as_posix(), " ".join(harness.write(self.root, "claude-code", hooks=True)))
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")), doc)
        self.assertFalse((settings.parent / "settings.json.factory-bak-2").exists())
        self.assertEqual(hook.ENTRY["hooks"][0]["command"], "factory hook pre-tool")  # the entry point, never a path into a working tree [L-149]
        settings.write_text("{not json", encoding="utf-8")
        self.assertRaisesRegex(FactoryError, "not valid JSON", harness.write, self.root, "claude-code", "project", True)
        self.assertEqual(settings.read_text(encoding="utf-8"), "{not json")
        settings.write_text("{}", encoding="utf-8")
        with mock.patch("pathlib.Path.write_text", side_effect=PermissionError("locked by another program")):
            self.assertRaisesRegex(FactoryError, "cannot write", harness.write, self.root, "claude-code", "project", True)
        self.assertRaisesRegex(FactoryError, "no user scope and no hooks", harness.write, self.root, "agents-md", "project", True)

    def test_the_hook_refuses_hand_edits_of_events_and_evidence_and_allows_everything_else(self):
        runs = ".factory/runs/ABC-1/"
        deny = [{"tool_name": "Write", "tool_input": {"file_path": f"C:\\p\\{runs.replace('/', chr(92))}events.jsonl"}},
                {"tool_name": "Edit", "tool_input": {"file_path": f"/p/{runs}artifacts/CHK-1/out.txt"}},
                {"tool_name": "Bash", "tool_input": {"command": f"echo x >> {runs}events.jsonl"}},
                {"tool_name": "Bash", "tool_input": {"command": f"rm -rf {runs}reviews"}},
                {"tool_name": "PowerShell", "tool_input": {"command": f"Set-Content -Path '{runs}contracts/ticket.json' -Value x"}},
                {"tool_name": "mcp__dev_mcp__any", "tool_input": {"save_to": f"/p/{runs}raw/a.json"}}]
        allow = [{"tool_name": "Write", "tool_input": {"file_path": f"/p/{runs}plan.json"}},
                 {"tool_name": "Write", "tool_input": {"file_path": f"/p/{runs}notes/design.md"}},
                 {"tool_name": "Bash", "tool_input": {"command": f"cat {runs}events.jsonl | head"}},
                 {"tool_name": "Bash", "tool_input": {"command": f"factory log && grep fail {runs}artifacts/x.txt"}},
                 {"tool_name": "Write", "tool_input": {"file_path": "/p/src/events.jsonl"}}]
        for p in deny:
            self.assertIsNotNone(hook.reason(p), p)
        for p in allow:
            self.assertIsNone(hook.reason(p), p)
        out = json.loads(hook.run(json.dumps(deny[0])))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(hook.run(json.dumps(allow[0])), "")
        self.assertIn("deny", hook.run("not json"))

    def test_the_hook_normalises_paths_and_follows_cd_and_variables_but_lets_reads_through(self):
        """Review m1: spelling tricks do not dodge the guard; reads by an interpreter or a word in a comment are not refused."""
        r = ".factory/runs/R/"
        deny = [{"file_path": f"/p/{r}./events.jsonl"}, {"file_path": f"/p/{r}../R/events.jsonl"}, {"file_path": "/p/.factory//runs/R/events.jsonl"},
                {"file_path": f"C:\\p\\.FACTORY\\runs\\R\\Events.jsonl"},
                {"command": f"cd {r[:-1]} && echo x >> events.jsonl"}, {"command": f"cd {r} && rm -rf artifacts"},
                {"command": f"P=/p/{r}; echo x >> $P/events.jsonl"},
                {"command": f"python -c \"open('{r}events.jsonl', 'a').write('x')\""}]
        allow = [{"command": f"python -m json.tool /p/{r}artifacts/CHK-1/1/output.txt"},
                 {"command": f"less /p/{r}artifacts/CHK-1/1/output.txt # copy later"},
                 {"command": f"cd {r} && cat events.jsonl | head"}, {"command": f"node show.js /p/{r}raw/ticket/a.json"}]
        for key, items in (("deny", deny), ("allow", allow)):
            for args in items:
                tool = "Write" if "file_path" in args else "Bash"
                got = hook.reason({"tool_name": tool, "tool_input": args})
                self.assertEqual(got is not None, key == "deny", (key, args))

    def test_the_hook_command_reads_the_tool_call_on_stdin(self):
        code, out, _ = self.cli("hook", "pre-tool", stdin=json.dumps({"tool_input": {"file_path": ".factory/runs/a/events.jsonl"}}), project=False, cwd=self.root)
        self.assertEqual((code, json.loads(out)["hookSpecificOutput"]["permissionDecision"]), (0, "deny"))
        self.assertEqual(self.cli("hook", "pre-tool", stdin="{}", project=False, cwd=self.root)[:2], (0, ""))

    def test_the_package_ships_the_adapter_text_as_data(self):
        data = (harness.Path(harness.__file__).parent / "adapters")
        self.assertEqual(sorted(p.name for p in data.glob("*.md")), ["agents.md", "host.md", "skill.md", "worker.md"])
        self.assertIn("adapters/*.md", (data.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
        for name in ("skill.md", "worker.md", "agents.md", "host.md"):
            self.assertLessEqual(len((data / name).read_text(encoding="utf-8").splitlines()), 100, name)


if __name__ == "__main__":
    unittest.main()
