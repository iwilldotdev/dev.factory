"""Loop counters say what happened: amendments are not replans, a replan records what it changed, a rewind counts in the
loop of the phase it goes back to."""

from __future__ import annotations

import json

from tests.integrity_support import PASS, IntegrityCase, cli, core


def user_decision(case, text: str = "user asked") -> int:
    out = case.ok("--json", "run", "decide", case.rid, text, "--by", "user", "--user-said", "ok, go ahead")
    return json.loads(out)["decisions"] - 1


class ReplanChangedTests(IntegrityCase):
    def test_replan_event_lists_what_the_replan_changed(self):
        self.proved()
        self.ok("run", "replan", self.rid, "second check needed")
        ev = next(e for e in reversed(core.run_load(self.rid)["events"]) if e["kind"] == "replan")
        self.assertEqual(ev["data"]["changed"], [])  # nothing edited yet
        checks = [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": PASS},
                  {"id": "CHK-2", "type": "test", "repo": "React", "cmd": PASS}]
        self.acs(checks, [{"id": "AC-1", "ticket": "DEV-1", "text": "works", "kind": "logic", "checks": ["CHK-1"]},
                          {"id": "AC-2", "ticket": "DEV-1", "text": "new", "kind": "logic", "checks": ["CHK-2"]}])
        (self.rd / "checks" / "note.txt").write_text("v2\n", encoding="utf-8")
        self.ok("gate", self.rid, "plan")
        run = core.run_load(self.rid)
        data = next(e for e in reversed(run["events"]) if e["kind"] == "replan")["data"]
        self.assertEqual(data["changed_checks"], ["CHK-2"])
        self.assertEqual(data["changed_acs"], ["AC-2"])
        self.assertIn("acs.json", data["changed"])
        self.assertIn("checks/note.txt", data["changed"])
        self.assertNotIn("replan_from", run)

    def test_old_run_without_replan_from_still_relocks(self):
        self.proved()
        self.ok("run", "replan", self.rid, "x")
        run = core.run_load(self.rid)
        run.pop("replan_from", None)
        core.run_save(run)
        self.ok("gate", self.rid, "plan")


class AmendIsNotAReplanTests(IntegrityCase):
    def test_amend_counts_against_the_cap_but_not_as_a_replan(self):
        self.set_autonomy(max_replans=1)
        chk = {"id": "CHK-1", "type": "test", "repo": "React", "cmd": PASS, "timeout_s": 30}
        self.start()
        self.acs([chk])
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        chk["timeout_s"] = 60
        self.acs([chk])
        self.ok("run", "amend-check", self.rid, "CHK-1", "--kind", "timing", "--reason", "slow")
        loops = core.run_load(self.rid)["loops"]
        self.assertEqual(loops.get("amend"), 1)
        self.assertEqual(loops.get("replan", 0), 0)
        rc, out = cli("run", "replan", self.rid, "again")  # the amend used the single allowed replan
        self.assertNotEqual(rc, 0)
        self.assertIn("max_replans (1)", out)


class RewindLoopTests(IntegrityCase):
    def test_rewind_counts_in_the_loop_of_its_target_phase(self):
        self.reviewed()
        self.ok("run", "rewind", self.rid, "prove", "rebased")
        loops = core.run_load(self.rid)["loops"]
        self.assertEqual(loops.get("prove"), 1)
        self.assertEqual(loops.get("build", 0), 0)
        ev = next(e for e in reversed(core.run_load(self.rid)["events"]) if e["kind"] == "rewind")
        self.assertEqual(ev["data"]["loop"], 1)

    def test_rewind_with_a_user_decision_is_counted_but_not_against_the_cap(self):
        self.reviewed()
        n = user_decision(self, "rebase on main")
        self.ok("run", "rewind", self.rid, "prove", "rebased", "--decision", str(n))
        loops = core.run_load(self.rid)["loops"]
        self.assertEqual((loops.get("prove"), loops.get("prove_approved")), (1, 1))


if __name__ == "__main__":
    import unittest
    unittest.main()
