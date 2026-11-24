"""F-28: change title/body content, reviewers, ticket modes, update-description, baseline and hotfix quality lines."""

from __future__ import annotations

import json
import unittest

from factory import core
from factory.commands import ship
from tests.integrity_support import IntegrityCase, cli, PASS
from tests.ticket_support import put_ticket

SUMMARY = "Ação de pagamento não funciona"


class ShipCase(IntegrityCase):
    ship_extra: dict = {}

    def setUp(self) -> None:
        super().setUp()
        self.ws["ship"].update({"ticket_comment": "change-links", "ticket_transition": "Done"})
        self.ws["ship"].update(self.ship_extra)
        self.save_ws()

    def ready(self, summary: str | None = SUMMARY, checks=None) -> None:
        self.to_prove(checks)
        if summary:
            put_ticket(self.rid, "DEV-1", title=summary)
        else:  # no ticket title known: drop the contract the fixture start wrote
            (self.rd / "contracts" / "ticket" / "DEV-1.json").unlink()
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")
        self.assertEqual(self.record("code")[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")

    def plan(self, *extra: str) -> dict:
        return json.loads(self.ok("--json", "ship", "plan", self.rid, *extra))


class TitleTests(ShipCase):
    def test_default_title_uses_ticket_summary_with_accents(self):
        self.ready()
        change = self.plan()["changes"][0]
        self.assertEqual(change["title"], f"[DEV-1] - {SUMMARY}")
        saved = (self.rd / "ship-plan.json").read_text(encoding="utf-8")
        self.assertIn(SUMMARY, saved)  # UTF-8 kept, not \u escapes

    def test_missing_summary_has_no_dangling_separator(self):
        self.ready(summary=None)
        self.assertEqual(self.plan()["changes"][0]["title"], "[DEV-1]")


class TitleFormatTests(ShipCase):
    ship_extra = {"title_format": "{repo}/{target}: {key} {summary}"}

    def test_custom_title_format(self):
        self.ready()
        self.assertEqual(self.plan()["changes"][0]["title"], f"React/main: DEV-1 {SUMMARY}")


class BadTitleFormatTests(ShipCase):
    ship_extra = {"title_format": "{key} {nope}"}

    def test_unknown_placeholder_is_a_clear_error(self):
        self.ready()
        rc, out = cli("ship", "plan", self.rid)
        self.assertNotEqual(rc, 0)
        self.assertIn("{nope}", out)
        self.assertIn("title_format", out)


class ReviewerTests(ShipCase):
    ship_extra = {"reviewers": [{"ticket_user": "ana@x.com", "scm_user": "ana"}, {"ticket_user": "bob@x.com"}]}

    def test_identity_without_scm_user_fails(self):
        self.ready()
        rc, out = cli("ship", "plan", self.rid)
        self.assertNotEqual(rc, 0)
        self.assertIn("scm_user", out)
        self.assertIn("bob@x.com", out)


class ReviewerOkTests(ShipCase):
    ship_extra = {"reviewers": [{"ticket_user": "ana@x.com", "scm_user": "ana"}, "carl"]}

    def test_reviewers_are_planned(self):
        self.ready()
        plan = self.plan()
        self.assertEqual(plan["changes"][0]["reviewers"], ["ana", "carl"])
        action = next(a for a in plan["actions"] if a["type"] == "change_request")
        self.assertEqual(action["reviewers"], ["ana", "carl"])


class TicketModeTests(ShipCase):
    def types(self, plan):
        return [a["type"] for a in plan["actions"] if a["type"].startswith("issue_")]

    def test_modes(self):
        self.ready()
        self.assertEqual(self.types(self.plan()), ["issue_comment", "issue_transition"])
        self.assertEqual(self.types(self.plan("--ticket", "none")), [])
        self.assertEqual(self.types(self.plan("--ticket", "comment")), ["issue_comment"])
        self.assertEqual(self.types(self.plan("--ticket", "transition")), ["issue_transition"])


class TicketWorkspaceModeTests(ShipCase):
    ship_extra = {"ticket_mode": "comment"}

    def test_workspace_default_and_flag_override(self):
        self.ready()
        self.assertEqual([a["type"] for a in self.plan()["actions"] if a["type"].startswith("issue_")], ["issue_comment"])
        self.assertEqual([a["type"] for a in self.plan("--ticket", "transition")["actions"] if a["type"].startswith("issue_")],
                         ["issue_transition"])


class DescriptionTests(ShipCase):
    def test_table_details_and_no_local_links(self):
        self.ready(checks=[{"id": "CHK-1", "type": "test", "repo": "React", "cmd": f'{PASS}'},
                           {"id": "CHK-H", "type": "human", "repo": "React", "text": "olhar a tela", "reason": "visual"}])
        desc = self.plan()["changes"][0]["body_md"]
        self.assertNotIn("Artifacts", desc)
        self.assertIn("| AC | Check | Status |", desc)
        self.assertIn("| AC-1: works | CHK-1 | PASS |", desc)
        self.assertIn("| AC-1: works | CHK-H | HUMAN |", desc)
        self.assertIn("<details><summary>CHK-1 (test): PASS</summary>", desc)
        for marker in ship.LOCAL_LINK_MARKERS:
            self.assertNotIn(marker, desc)
        self.assertIn(SUMMARY, desc)

    def test_check_output_lines_in_details(self):
        self.ready()
        ev = core.read_json(self.rd / "evidence" / "CHK-1.json")
        ev["details"]["key_lines"] = ["Ran 3 tests", "OK"]
        core.write_json(self.rd / "evidence" / "CHK-1.json", ev)
        desc = ship._description(core.run_load(self.rid), {}, "React", "main", [], core.read_json(self.rd / "acs.json"))
        self.assertIn("```\nRan 3 tests\nOK\n```", desc)

    def test_verify_rejects_local_links_in_plan_and_readback(self):
        self.ready()
        plan = self.plan()
        change = plan["changes"][0]
        self.readbacks(plan)
        self.ok("ship", "verify", self.rid)  # clean description passes
        rb = self.rd / change["readback"]
        data = core.read_json(rb)
        data["description_md"] = "see [log](evidence/CHK-1/out.txt)"
        core.write_json(rb, data)
        rc, out = cli("ship", "verify", self.rid)
        self.assertNotEqual(rc, 0)
        self.assertIn("local file", out)
        self.assertIn("readback", out)
        data["description_md"] = "ok"
        core.write_json(rb, data)
        plan_file = self.rd / "ship-plan.json"
        saved = core.read_json(plan_file)
        for a in saved["actions"]:
            if a["type"] == "change_request":
                a["body_md"] += "\n[x](file:///tmp/a)"
        core.write_json(plan_file, saved)
        rc, out = cli("ship", "verify", self.rid)
        self.assertNotEqual(rc, 0)
        self.assertIn("planned", out)


class BaselineTests(ShipCase):
    def test_failing_baseline_test_names_in_quality_section(self):
        self.ready()
        core.write_json(self.rd / "baseline" / "React.json", {"commands": {
            "test": {"cmd": "t", "exit": 1, "failures": ["test_old_one", "test_old_two"]}}})
        desc = self.plan()["changes"][0]["body_md"]
        self.assertIn("## Quality", desc)
        self.assertIn("test: 2", desc)
        self.assertIn("test_old_one; test_old_two", desc)


class HotfixQualityTests(ShipCase):
    def test_quality_per_target(self):
        self.ready()
        for t, st in (("homolog", "pass"), ("pack", "fail")):
            core.write_json(self.rd / "evidence" / f"QUALITY-React@{t}.json", {"status": st, "summary": f"lint {t}"})
        run = {"id": self.rid, "hotfix": {"base": "homolog"}}
        lines = ship._quality_lines(run, "React", ["homolog", "pack"])
        self.assertEqual(lines, ["- QUALITY-React@homolog: PASS - lint homolog", "- QUALITY-React@pack: FAIL - lint pack"])


class UpdateDescriptionTests(ShipCase):
    def test_rebuilds_from_readback_number(self):
        self.ready()
        plan = self.plan()
        self.readbacks(plan)
        core.write_json(self.rd / "baseline" / "React.json", {"commands": {"test": {"failures": ["test_late"]}}})
        out = json.loads(self.ok("--json", "ship", "update-description", self.rid, "--repo", "React", "--target", "main"))
        upd = next(a for a in out["actions"] if a["type"] == "change_request")
        self.assertEqual((upd["op"], upd["number"]), ("update", 1))
        self.assertIn("test_late", upd["body_md"])
        self.assertIn("test_late", core.read_json(self.rd / "ship-plan.json")["changes"][0]["body_md"])

    def test_missing_readback_prints_fetch(self):
        self.ready()
        self.plan()
        rc, out = cli("ship", "update-description", self.rid)
        self.assertNotEqual(rc, 0)
        self.assertIn("FETCH change React-main", out)
        self.assertIn("raw/change/React-main.json", out)
        self.assertIn("factory ingest", out)


if __name__ == "__main__":
    unittest.main()
