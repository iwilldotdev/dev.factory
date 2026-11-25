"""Harness adapters: one generated section per host file, replaced in place, never more than 100 lines (decision 13)."""
import unittest

from dev_factory import harness
from tests.helpers import FactoryCase, lesson

HOSTS = {"agents-md": "AGENTS.md", "claude-code": "CLAUDE.md"}


class HarnessFileTests(FactoryCase):
    @lesson("L-171")
    def test_every_harness_id_writes_its_own_host_file_with_a_generated_section_of_at_most_100_lines(self):
        """L-171: each harness id writes its own file with its generated section, at most 100 lines; AGENTS.md holds the full prose."""
        for hid, name in HOSTS.items():
            root = self.base / hid
            root.mkdir()
            self.ok("init", "--project", str(root), "--harness", hid, project=False)
            text = (root / name).read_text(encoding="utf-8")
            self.assertEqual(text.count(harness.BEGIN), 1)
            self.assertLessEqual(len(text.splitlines()), 100)
            self.assertIn(harness.text(harness.SECTION[hid]).strip(), text)
        self.assertIn("factory ship verify", (self.base / "agents-md" / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("factory review record", (self.base / "agents-md" / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertFalse((self.base / "agents-md" / "CLAUDE.md").exists())
        self.assertFalse((self.base / "agents-md" / ".claude").exists())

    def test_an_existing_host_file_keeps_its_own_text_and_a_second_write_changes_nothing(self):
        root = self.base / "h"
        root.mkdir()
        (root / "CLAUDE.md").write_text("# Mine\n\nKeep this.\n", encoding="utf-8")
        harness.write(root, "claude-code")
        first = (root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(first.startswith("# Mine\n\nKeep this.\n"))
        self.assertEqual(harness.write(root, "claude-code")[0].split()[0], "unchanged")
        self.assertEqual((root / "CLAUDE.md").read_text(encoding="utf-8"), first)

    def test_the_unknown_harness_message_lists_every_id(self):
        msg = self.fails("init", "--project", str(self.base / "x"), "--harness", "nope", project=False)
        for hid in HOSTS:
            self.assertIn(hid, msg)


if __name__ == "__main__":
    unittest.main()
