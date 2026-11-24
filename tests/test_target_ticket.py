"""`factory run target-ticket`: the change of a hotfix target is titled with the ticket chosen for it."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_support import cli, core  # noqa: E402
from test_hotfix_flow import HotfixCase  # noqa: E402


class TargetTicketTests(HotfixCase):
    def titles(self):
        rc, out = cli("--json", "ship", "plan", self.rid)
        self.assertEqual(rc, 0, out)
        return {m["target"]: m["title"] for m in json.loads(out)["changes"]}

    def test_each_target_mr_is_titled_with_its_ticket(self):
        self.through_build()
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        self.ok("prove", self.rid)
        core.run_update(self.rid, lambda r: r["tickets"].append({"key": "BUG-2", "summary": "Clone summary", "area": "Docs"}))
        before = self.titles()
        self.assertTrue(all("BUG-1" in t or "BUG-2" not in t for t in before.values()))
        self.ok("run", "target-ticket", self.rid, "pack", "bug-2")
        self.assertEqual(core.run_load(self.rid)["hotfix"]["tickets"], {"pack": "BUG-2"})
        titles = self.titles()
        self.assertEqual(titles["homolog"], before["homolog"])
        self.assertIn("Clone summary", titles["pack"])
        self.assertNotEqual(titles["pack"], before["pack"])
        self.assertTrue(any(e.get("kind") == "target_ticket" for e in core.run_load(self.rid)["events"]))

    def test_usage_errors(self):
        self.start()
        for args in (("nope", "BUG-1"), ("pack", "BUG-9")):
            rc, out = cli("run", "target-ticket", self.rid, *args)
            self.assertEqual(rc, 2, out)

    def test_title_falls_back_to_the_first_ticket_when_the_chosen_one_left_the_run(self):
        self.through_build()
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        self.ok("prove", self.rid)
        before = self.titles()
        core.run_update(self.rid, lambda r: r["hotfix"].setdefault("tickets", {}).update({"pack": "GONE-7"}))
        self.assertEqual(self.titles(), before)

    def test_refused_when_the_run_is_done_or_blocked(self):
        self.start()
        for phase in ("done", "blocked"):
            core.run_update(self.rid, lambda r, p=phase: r.update(phase=p))
            rc, out = cli("run", "target-ticket", self.rid, "pack", "BUG-1")
            self.assertEqual(rc, 2, out)
            self.assertIn(phase, out)
            self.assertNotIn("tickets", core.run_load(self.rid)["hotfix"])


if __name__ == "__main__":
    unittest.main()
