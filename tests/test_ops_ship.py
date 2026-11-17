import json
import unittest
from unittest import mock

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, git, make_repo

from factory.commands import ship
from factory.core import FactoryError, GateFailed

ACS = {
    "schemaVersion": 1,
    "acs": [{"id": "AC-1", "ticket": "DEV-1", "text": "Shows | thing", "kind": "ui", "checks": ["C1", "C2"]}],
    "checks": [
        {"id": "C1", "type": "test", "repo": "React", "cmd": "x"},
        {"id": "C2", "type": "human", "text": "Native picker", "reason": "not automatable"},
    ],
}


HEAD = "f" * 40


class ShipBase(FactoryCase):
    """Plan/verify formatting. The integrity layer (plan lock, proof at HEAD, branch heads) is covered by
    tests/test_integrity_*.py and stubbed here."""

    def setUp(self):
        super().setUp()
        for name, value in (("integrity_errors", []), ("review_errors", []), ("branch_head", HEAD)):
            patcher = mock.patch.object(ship, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def setup_ws(self, autonomy="ask", draft=True, targets=("pack",), tickets=None, jira_transition="Code Review"):
        self.react = make_repo(self.tmp / "react")
        self.api = make_repo(self.tmp / "api")
        for r in (self.react, self.api):
            for t in ("pack", "homolog"):
                git(r, "update-ref", f"refs/remotes/origin/{t}", "HEAD")
        ws = {
            "schemaVersion": 1, "name": "ws",
            "repos": [
                {"alias": "React", "path": str(self.react), "order": 2},
                {"alias": "API", "path": str(self.api), "order": 1},
            ],
            "ship": {"targets": ["pack", "homolog"], "draft_default": draft, "mr_title_format": "[{area} | {target_upper}] - {summary}",
                     "jira_comment": "mr-links", "jira_transition": jira_transition},
            "autonomy": {"ship": autonomy},
            "epics": [{"key": "DEV-0", "ship_comment": "c.md"}],
        }
        self.write("workspaces/ws/workspace.json", ws)
        tk = tickets or [{"key": "DEV-1", "type": "feature", "summary": "Do thing", "area": "Obras", "parent": "DEV-0"}]
        self.make_run("ws-dev-1", tickets=tk, targets=list(targets),
                      repos={"React": {"branch": "feature/x", "base": "pack"}, "API": {"branch": "feature/y", "base": "pack"}})
        self.write("runs/ws-dev-1/acs.json", ACS)
        self.write("runs/ws-dev-1/evidence/C1.json", {"check_id": "C1", "status": "pass", "artifacts": ["evidence/C1/a.png"]})


class PlanTests(ShipBase):
    def test_plan_ask(self):
        self.setup_ws(targets=("pack", "homolog"))
        plan = ship.build_plan("ws-dev-1")
        order = [(m["repo"], m["target"]) for m in plan["mrs"]]
        self.assertEqual(order, [("API", "pack"), ("API", "homolog"), ("React", "pack"), ("React", "homolog")])
        react_pack = plan["mrs"][2]
        self.assertEqual(react_pack["title"], "Draft: [Obras | PACK] - Do thing")
        self.assertTrue(react_pack["draft"])
        self.assertEqual(react_pack["source_branch"], "feature/x")
        self.assertEqual(plan["mrs"][3]["source_branch"], "feature/x-homolog")
        self.assertIn("Depends on {mr_url:API:pack}", react_pack["description"])
        self.assertIn("{mr_url:React:pack}", plan["mrs"][3]["description"])
        self.assertIn("| C1 | PASS |", react_pack["description"])
        self.assertIn("| C2 | HUMAN |", react_pack["description"])
        self.assertIn("Native picker", react_pack["description"])
        self.assertIn("Shows \\| thing", react_pack["description"])
        self.assertTrue(all(a["needs_user"] for a in plan["actions"]))
        kinds = [a["type"] for a in plan["actions"]]
        self.assertEqual(kinds[:3], ["push", "mr", "readback"])
        self.assertEqual(kinds[-2:], ["jira_comment", "jira_transition"])
        self.assertEqual(len(plan["jira_comment"]["adf"]["content"]), 4)
        self.assertEqual(plan["jira_comment"]["epic_ship_comment"], "c.md")
        self.assertTrue((self.home / "runs/ws-dev-1/ship-plan.json").is_file())

    def test_plan_draft_auto(self):
        self.setup_ws(autonomy="draft-auto")
        plan = ship.build_plan("ws-dev-1")
        by = {}
        for a in plan["actions"]:
            by.setdefault(a["type"], []).append(a["needs_user"])
        self.assertEqual(by["push"], [False, False])
        self.assertEqual(by["mr"], [False, False])
        self.assertEqual(by["jira_comment"], [True])
        self.assertEqual(by["jira_transition"], [True])

    def test_not_draft_needs_user_for_mr(self):
        self.setup_ws(autonomy="draft-auto", draft=False)
        plan = ship.build_plan("ws-dev-1")
        self.assertEqual(plan["mrs"][0]["title"], "[Obras | PACK] - Do thing")
        self.assertTrue(all(a["needs_user"] for a in plan["actions"] if a["type"] == "mr"))

    def test_no_transition(self):
        self.setup_ws(jira_transition=None)
        plan = ship.build_plan("ws-dev-1")
        self.assertNotIn("jira_transition", [a["type"] for a in plan["actions"]])

    def test_behind_fails(self):
        self.setup_ws()
        git(self.react, "commit", "-q", "--allow-empty", "-m", "c2")
        git(self.react, "update-ref", "refs/remotes/origin/pack", "HEAD")
        git(self.react, "reset", "-q", "--hard", "HEAD~1")
        with self.assertRaises(GateFailed) as cm:
            ship.build_plan("ws-dev-1")
        self.assertIn("1 commit(s) behind origin/pack", cm.exception.reasons[0])
        self.assertEqual(len(ship.build_plan("ws-dev-1", allow_behind=True)["mrs"]), 2)

    def test_missing_origin_ref(self):
        self.setup_ws()
        git(self.api, "update-ref", "-d", "refs/remotes/origin/pack")
        with self.assertRaises(GateFailed) as cm:
            ship.build_plan("ws-dev-1")
        self.assertIn("git fetch", cm.exception.reasons[0])

    def test_dependency_not_merged(self):
        tk = [{"key": "DEV-1", "summary": "s", "depends_on": ["DEV-9"]}]
        self.setup_ws(tickets=tk)
        with self.assertRaises(GateFailed) as cm:
            ship.build_plan("ws-dev-1")
        self.assertIn("DEV-9", cm.exception.reasons[0])
        self.write("runs/ws-dev-1/remote/mr-API-pack-dep.json", {"state": "opened", "title": "DEV-9 thing"})
        with self.assertRaises(GateFailed):
            ship.build_plan("ws-dev-1")
        self.write("runs/ws-dev-1/remote/mr-API-pack-dep.json", {"state": "merged", "title": "DEV-9 thing"})
        self.assertEqual(len(ship.build_plan("ws-dev-1")["mrs"]), 2)


class VerifyTests(ShipBase):
    def readback(self, alias, **over):
        data = {"source_branch": "feature/" + ("x" if alias == "React" else "y"), "target_branch": "pack",
                "title": "Draft: [Obras | PACK] - Do thing", "draft": True, "web_url": "http://x/1", "iid": 1,
                "sha": HEAD}
        data.update(over)
        self.write(f"runs/ws-dev-1/remote/mr-{alias}-pack.json", data)

    def test_verify(self):
        self.setup_ws()
        ship.build_plan("ws-dev-1")
        self.assertTrue(any("missing readback" in e for e in ship.verify("ws-dev-1")))
        self.readback("React")
        self.readback("API", draft=False, title="[Obras | PACK] - Do thing")
        errs = ship.verify("ws-dev-1")
        self.assertEqual(len(errs), 1)
        self.assertIn("draft is False", errs[0])
        self.readback("API", target_branch="homolog", source_branch="other")
        errs = ship.verify("ws-dev-1")
        self.assertTrue(any("target_branch" in e for e in errs))
        self.assertTrue(any("source_branch" in e for e in errs))
        self.readback("API", title="wrong")
        self.assertTrue(any("title" in e for e in ship.verify("ws-dev-1")))
        self.readback("API")
        self.assertEqual(ship.verify("ws-dev-1"), [])
        run = json.loads((self.home / "runs/ws-dev-1/run.json").read_text())
        self.assertEqual(run["events"][-1]["kind"], "ship")


if __name__ == "__main__":
    unittest.main()
