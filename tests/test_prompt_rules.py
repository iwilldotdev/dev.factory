"""Text contracts of the skill, its references and the agent briefs: each rule below failed in a real run and has no
other mechanism, so the words are pinned here (a rule removed from the text fails its test)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "claude" / "skills" / "factory"
AGENTS = ROOT / "claude" / "agents"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class SkillSizeTests(unittest.TestCase):
    def test_skill_stays_under_the_cap(self):
        self.assertLessEqual(len(read(SKILL / "SKILL.md").splitlines()), 150)


class ReproductionScenarioTests(unittest.TestCase):
    def test_first_attempt_uses_the_tickets_own_scenario(self):
        phases = read(SKILL / "references" / "phases.md")
        self.assertIn("The ticket's own scenario first", phases)
        for needle in ("same user/profile", "same entity", "same steps", "same data shape", "state each deviation"):
            self.assertIn(needle, phases)
        author = read(AGENTS / "factory-author.md")
        self.assertIn("first reproduction attempt uses the ticket's own scenario", author)
        self.assertIn("every deviation is stated with its reason", author)


if __name__ == "__main__":
    unittest.main()
