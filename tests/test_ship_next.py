"""ship phase through `next`, and the adapter route of ship verify."""
import json
import unittest

from tests.helpers import adapter_scenario, provider_toml
from tests.m3_support import ShipCase

OTHER_SHA = "a" * 40
CONFIRM = ("confirm", "change:42", "--user-said", "yes, that is the change on the board")


class NextTests(ShipCase):
    def setUp(self):
        super().setUp()
        self.to_ship()

    def nxt(self):
        return json.loads(self.ok("next", "--json"))

    def test_next_walks_plan_execute_verify_gate_and_prints_the_briefing(self):
        n = self.nxt()
        self.assertEqual((n["phase"], n["command"], n["role"]), ("ship", "factory ship plan", "orchestrator"))
        self.ship_plan()
        n = self.nxt()
        self.assertEqual(n["kind_of_action"], "user", "autonomy.ship = ask hands the intents to the user first")
        self.assertIn("asking the user before each intent that needs_user", n["action"])
        self.assertTrue(n["command"].startswith("factory ingest change --raw"))
        self.assertEqual([f.rsplit("/", 1)[-1] for f in n["files"]], ["web.raw.json", "web.json", "body.md"])
        self.assertTrue(any(ln.startswith("Gate S briefing for ABC-1") for ln in n["briefing"]))
        self.assertTrue(any("AC-1 x CHK-1: pass" in ln for ln in n["briefing"]))
        self.assertIn("Gate S briefing for ABC-1", self.ok("next"))
        self.ingest_change()
        self.ok(*CONFIRM)
        self.assertEqual(self.nxt()["command"], "factory ship verify")
        self.ship_verify()
        self.assertEqual(self.nxt()["command"], "factory gate ship")
        self.ok("gate", "ship")
        n = self.nxt()
        self.assertEqual((n["phase"], n["kind_of_action"]), ("feedback", "command"))

    def test_a_mismatch_sends_the_orchestrator_back_to_fix_the_change(self):
        self.ship_plan()
        self.ingest_change(draft=False)
        self.ok(*CONFIRM)
        n = self.nxt()
        self.assertIn("differs from the ship plan", n["action"])
        self.assertTrue(any("draft is False" in r for r in n["reasons"]))

    def test_a_moved_head_sends_next_back_to_plan_with_update_once_a_change_exists(self):
        self.ship_plan()
        self.ingest_change()
        self.ok(*CONFIRM)
        self.ship_verify()
        self.commit("more.txt", "x\n")
        self.ok("verify")
        self.ok("gate", "verify")
        self.approve_both()
        self.ok("gate", "review")
        self.assertEqual(self.nxt()["command"], "factory ship plan --update")


class AdapterTests(ShipCase):
    def setUp(self):
        super().setUp()
        template = adapter_scenario(self, stdout="{}", dump=(self.base / "dump.json").as_posix()) + " {branch} {sha}"
        self.template = template
        self.to_ship(extra_toml=provider_toml(template, "change")[len(self.TOML):])
        self.ship_plan()

    def test_the_configured_adapter_reads_the_change_back_and_needs_no_confirmation(self):
        it = next(i for i in self.sp()["intents"] if i["intent"] == "change_request")
        self.assertTrue(next(i for i in self.sp()["intents"] if i["intent"] == "readback")["adapter"])
        out = json.dumps({"contract": self.change_data(), "raw": {"k": 1}})
        adapter_scenario(self, stdout=out, dump=(self.base / "dump.json").as_posix())
        res = self.ship_verify()
        self.assertEqual((res["result"], res["mismatches"]), ("pass", []))
        dump = json.loads((self.base / "dump.json").read_text(encoding="utf-8"))
        self.assertEqual(dump["argv"][-3:], [it["source_branch"], it["source_branch"], self.head()])
        self.assertEqual(json.loads((self.run_dir / "contracts" / "change" / "42.json").read_text(encoding="utf-8"))["source"], "adapter")
        self.assertEqual(self.gate("ship")[0], 0)
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "feedback")

    def test_an_adapter_failure_is_a_mismatch_not_a_crash(self):
        adapter_scenario(self, stdout="not json")
        res = self.ship_verify(code=1)
        self.assertIn("reading the change back through the adapter failed", res["mismatches"][0])

    def test_next_offers_ship_verify_to_an_adapter_project_instead_of_an_ingest(self):
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual(n["command"], "factory ship verify")
        self.assertIn("let the configured adapter do it", n["action"])


if __name__ == "__main__":
    unittest.main()
