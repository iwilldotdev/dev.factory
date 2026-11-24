"""The metrics report is recomputed when a run closes or reaches done (it used to stay at the last manual call)."""

from __future__ import annotations

import unittest

from core_support import FactoryCase, cli, core  # noqa: F401


class RefreshAtFinalStateTests(FactoryCase):
    def report(self, rid):
        return core.run_dir(rid) / ("metrics" + ".json")

    def test_run_close_writes_the_report(self):
        rid = self.make_run(branch=False)
        self.assertFalse(self.report(rid).exists())
        rc, out = cli("run", "close", rid, "--reason", "superseded")
        self.assertEqual(rc, 0, out)
        data = core.read_json(self.report(rid))
        self.assertEqual(data["phase"], "closed")
        self.assertIn("blocked_hours", data)
        self.assertIn("idle_hours", data)

    def test_a_broken_report_never_fails_the_command(self):
        from unittest import mock
        from factory.commands import metrics
        rid = self.make_run(branch=False)
        with mock.patch.object(metrics, "compute", side_effect=RuntimeError("boom")):
            self.assertEqual(cli("run", "close", rid, "--reason", "x")[0], 0)


if __name__ == "__main__":
    unittest.main()
