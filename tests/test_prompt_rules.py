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


class ProductBehaviourVsChecksTests(unittest.TestCase):
    def test_builder_never_changes_product_behaviour_for_a_check(self):
        text = read(AGENTS / "factory-builder.md")
        self.assertIn("Never change product behaviour to fit a check", text)
        for needle in ("Durations", "timeouts", "retry counts", "check problem"):
            self.assertIn(needle, text)

    def test_auditor_looks_for_changed_values(self):
        text = read(AGENTS / "factory-auditor.md")
        self.assertIn("changed values", text)
        for needle in ("durations, timeouts, delays", "retry counts", "whether the ticket or the root cause requires it"):
            self.assertIn(needle, text)


class BlockedCapTests(unittest.TestCase):
    def test_two_consecutive_blocked_then_rescope(self):
        text = read(SKILL / "references" / "workers.md")
        self.assertIn("Cap on consecutive `blocked`: 2", text)
        for needle in ("re-scopes the task", "changes the approach", "asks the user", "never the same request"):
            self.assertIn(needle, text)


class OrchestratorDisciplineTests(unittest.TestCase):
    def test_language_and_no_hand_edits(self):
        text = read(SKILL / "SKILL.md")
        self.assertIn("talk to the user in the user's language", text)
        self.assertIn("Artifacts, commits, run files and CLI text keep their own conventions", text)
        self.assertIn("never edit specs, checks, `acs.json` or product code yourself", text)
        self.assertLessEqual(len(text.splitlines()), 135)


class HumanTypeDocsTests(unittest.TestCase):
    def test_skill_and_phases_name_the_types(self):
        for text in (read(SKILL / "SKILL.md"), read(SKILL / "references" / "phases.md")):
            self.assertIn("--type decision|approval|env-note|correction|other", text)


if __name__ == "__main__":
    unittest.main()
