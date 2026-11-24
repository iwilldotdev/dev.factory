"""Major 11 + docs: guidance matches the mechanisms (red-green fix_commit, rewind instead of rebase at ship, auditor role)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFS = ROOT / "claude" / "skills" / "factory" / "references"


def read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class GuidanceTests(unittest.TestCase):
    def test_red_green_fix_commit_matches_collector(self):
        for text in (read("claude", "skills", "factory", "references", "phases.md"), read("claude", "agents", "factory-author.md")):
            self.assertIn("commit that contains the fix", text)
            self.assertIn("test_files", text)
            self.assertIn("expect_fail_pattern", text)
        self.assertNotIn("writes the red test, commits it, then fixes", read("claude", "skills", "factory", "references", "phases.md"))
        self.assertIn("may skip `red-green`", read("claude", "agents", "factory-author.md"))

    def test_ship_rebase_goes_through_rewind(self):
        ship = (REFS / "ship.md").read_text(encoding="utf-8")
        self.assertIn("factory run rewind <run> prove", ship)
        self.assertNotIn("then prove + review again, or ask", ship)

    def test_auditor_records_with_role(self):
        self.assertIn("FACTORY_ROLE=auditor factory review record", read("claude", "agents", "factory-auditor.md"))
        self.assertIn("FACTORY_ROLE=auditor", (REFS / "phases.md").read_text(encoding="utf-8"))

    def test_design_documents_integrity(self):
        design = read("DESIGN.md")
        for needle in ("### 2.6 Proof integrity", "run replan", "run rewind", "expect_fail_pattern", "max_tolerance_px",
                       "quality.setup", "FACTORY_LOGIN_PASS", "no scaling"):
            self.assertIn(needle, design)
        self.assertNotIn("scale the capture to the design image size", design)


if __name__ == "__main__":
    unittest.main()
