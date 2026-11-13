"""Validation of the optional `orchestration` and workspace-level `env` blocks."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory.commands.workspace import orchestration_config_errors  # noqa: E402


class OrchestrationConfigTest(unittest.TestCase):
    def test_absent_blocks_are_valid(self):
        self.assertEqual(orchestration_config_errors({}), [])

    def test_valid_blocks(self):
        ws = {"orchestration": {"sessions": True, "per": "ticket"}, "env": {"lease": True, "lease_ttl_s": 900}}
        self.assertEqual(orchestration_config_errors(ws), [])

    def test_bad_orchestration(self):
        errs = orchestration_config_errors({"orchestration": {"sessions": "yes", "per": "mr", "x": 1}})
        self.assertTrue(any("sessions must be" in e for e in errs))
        self.assertTrue(any("per must be" in e for e in errs))
        self.assertTrue(any("unknown key" in e for e in errs))
        self.assertTrue(orchestration_config_errors({"orchestration": []}))

    def test_bad_env(self):
        errs = orchestration_config_errors({"env": {"lease": 1, "lease_ttl_s": 10}})
        self.assertTrue(any("env.lease must be" in e for e in errs))
        self.assertTrue(any("lease_ttl_s" in e for e in errs))
        self.assertTrue(orchestration_config_errors({"env": {"lease_ttl_s": True}}))
        self.assertTrue(orchestration_config_errors({"env": "on"}))


if __name__ == "__main__":
    unittest.main()
