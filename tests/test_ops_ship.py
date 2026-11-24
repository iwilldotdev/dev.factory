import json
import unittest
from unittest import mock

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from change_support import put_change, put_for_plan, put_pipeline
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
RUN = "ws-dev-1"


class ShipBase(FactoryCase):
    """Plan/verify formatting. The integrity layer (plan lock, proof at HEAD, branch heads) is covered by
    tests/test_integrity_*.py and stubbed here."""

    def setUp(self):
        super().setUp()
        for name, value in (("integrity_errors", []), ("review_errors", []), ("branch_head", HEAD)):
            patcher = mock.patch.object(ship, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def setup_ws(self, autonomy="ask", draft=True, targets=("pack",), tickets=None, ticket_transition="Code Review",
                 ship_extra=None):
        self.react = make_repo(self.tmp / "react")
        self.api = make_repo(self.tmp / "api")
        for r in (self.react, self.api):
            for t in ("pack", "homolog"):
                git(r, "update-ref", f"refs/remotes/origin/{t}", "HEAD")
        ws = {
            "schemaVersion": 1, "name": "ws",
            "repos": [
                {"alias": "React", "path": str(self.react), "order": 2, "scm_project": "team/react"},
                {"alias": "API", "path": str(self.api), "order": 1, "scm_project": "team/api"},
            ],
            "ship": {"targets": ["pack", "homolog"], "draft_default": draft, "title_format": "[{key} | {target}] - {summary}",
                     "ticket_comment": "change-links", "ticket_transition": ticket_transition, **(ship_extra or {})},
            "autonomy": {"ship": autonomy},
            "epics": [{"key": "DEV-0", "ship_comment": "c.md"}],
        }
        self.write("workspaces/ws/workspace.json", ws)
        tk = tickets or [{"key": "DEV-1", "type": "feature", "summary": "Do thing", "area": "Obras", "parent": "DEV-0"}]
        self.make_run(RUN, tickets=tk, targets=list(targets),
                      repos={"React": {"branch": "feature/x", "base": "pack"}, "API": {"branch": "feature/y", "base": "pack"}})
        self.write(f"runs/{RUN}/acs.json", ACS)
        self.write(f"runs/{RUN}/evidence/C1.json", {"check_id": "C1", "status": "pass", "artifacts": ["evidence/C1/a.png"]})


class PlanTests(ShipBase):
    def test_plan_ask(self):
        self.setup_ws(targets=("pack", "homolog"))
        plan = ship.build_plan(RUN)
        self.assertEqual(plan["schemaVersion"], 2)
        order = [(m["repo"], m["target"]) for m in plan["changes"]]
        self.assertEqual(order, [("API", "pack"), ("API", "homolog"), ("React", "pack"), ("React", "homolog")])
        react_pack = plan["changes"][2]
        self.assertEqual(react_pack["title"], "[DEV-1 | pack] - Do thing")  # draft-free: `draft` is its own field
        self.assertTrue(react_pack["draft"])
        self.assertEqual(react_pack["source_branch"], "feature/x")
        self.assertEqual(plan["changes"][3]["source_branch"], "feature/x-homolog")
        self.assertIn("Depends on {change_url:API:pack}", react_pack["body_md"])
        self.assertIn("{change_url:React:pack}", plan["changes"][3]["body_md"])
        self.assertIn("| C1 | PASS |", react_pack["body_md"])
        self.assertIn("| C2 | HUMAN |", react_pack["body_md"])
        self.assertIn("Native picker", react_pack["body_md"])
        self.assertIn("Shows \\| thing", react_pack["body_md"])
        self.assertTrue(all(a["needs_user"] for a in plan["actions"]))
        kinds = [a["type"] for a in plan["actions"]]
        self.assertEqual(kinds[:3], ["push", "change_request", "readback"])
        self.assertEqual(kinds[-2:], ["issue_comment", "issue_transition"])
        comment = plan["actions"][-2]
        self.assertEqual(comment["body_md"].count("{change_url:"), 4)
        self.assertEqual(comment["epic_ship_comment"], "c.md")
        self.assertEqual(plan["actions"][-1]["to_status"], "Code Review")
        self.assertTrue((self.home / f"runs/{RUN}/ship-plan.json").is_file())

    def test_intents_are_neutral_markdown_no_rich_text_tree(self):
        self.setup_ws(targets=("pack", "homolog"))
        plan = ship.build_plan(RUN)
        text = json.dumps(plan)
        for banned in ('"type": "doc"', '"version": 1', '"mrs"'):  # no rich-text tree, no legacy run-state key
            self.assertNotIn(banned, text)
        known = {"push", "change_request", "set_draft", "readback", "issue_comment", "issue_transition", "pipeline_watch"}
        self.assertLessEqual({a["type"] for a in plan["actions"]}, known)
        cr = next(a for a in plan["actions"] if a["type"] == "change_request")
        self.assertEqual(cr["op"], "create")
        self.assertEqual({"repo", "target", "title", "body_md", "source", "target_branch", "draft"} - set(cr), set())
        self.assertIsInstance(cr["body_md"], str)
        rb = next(a for a in plan["actions"] if a["type"] == "readback")
        self.assertEqual((rb["kind"], rb["id"]), ("change", "API-pack"))
        self.assertIn("raw/change/API-pack.json", rb["save_to"])
        self.assertIn("factory ingest ws-dev-1 change", rb["ingest"])

    def test_plan_draft_auto(self):
        self.setup_ws(autonomy="draft-auto")
        plan = ship.build_plan(RUN)
        by = {}
        for a in plan["actions"]:
            by.setdefault(a["type"], []).append(a["needs_user"])
        self.assertEqual(by["push"], [False, False])
        self.assertEqual(by["change_request"], [False, False])
        self.assertEqual(by["readback"], [False, False])
        self.assertEqual(by["issue_comment"], [True])
        self.assertEqual(by["issue_transition"], [True])

    def test_not_draft_needs_user_for_change_request(self):
        self.setup_ws(autonomy="draft-auto", draft=False)
        plan = ship.build_plan(RUN)
        self.assertEqual(plan["changes"][0]["title"], "[DEV-1 | pack] - Do thing")
        self.assertFalse(plan["changes"][0]["draft"])
        self.assertTrue(all(a["needs_user"] for a in plan["actions"] if a["type"] == "change_request"))

    def test_no_transition(self):
        self.setup_ws(ticket_transition=None)
        plan = ship.build_plan(RUN)
        self.assertNotIn("issue_transition", [a["type"] for a in plan["actions"]])

    def test_pipeline_watch_is_opt_in_and_carries_the_sha(self):
        self.setup_ws()
        self.assertNotIn("pipeline_watch", [a["type"] for a in ship.build_plan(RUN)["actions"]])
        ws = json.loads((self.home / "workspaces/ws/workspace.json").read_text())
        ws["ship"]["pipeline_watch"] = True
        ws["autonomy"]["ship"] = "draft-auto"
        self.write("workspaces/ws/workspace.json", ws)
        plan = ship.build_plan(RUN)
        watches = [a for a in plan["actions"] if a["type"] == "pipeline_watch"]
        self.assertEqual([(w["repo"], w["sha"], w["needs_user"]) for w in watches], [("API", HEAD, False), ("React", HEAD, False)])
        self.assertIn("--raw jobs=", watches[0]["ingest"])

    def test_existing_change_makes_an_update_and_a_draft_flip_a_set_draft(self):
        self.setup_ws(draft=False)
        put_change(RUN, "API", "pack", number=41, draft=True, source_branch="feature/y")
        plan = ship.build_plan(RUN)
        api = [a for a in plan["actions"] if a.get("repo") == "API"]
        self.assertEqual([a["type"] for a in api], ["push", "change_request", "set_draft", "readback"])
        self.assertEqual((api[1]["op"], api[1]["number"]), ("update", 41))
        self.assertEqual((api[2]["draft"], api[2]["needs_user"]), (False, True))
        react = [a for a in plan["actions"] if a.get("repo") == "React"]
        self.assertEqual(react[1]["op"], "create")

    def test_behind_fails(self):
        self.setup_ws()
        git(self.react, "commit", "-q", "--allow-empty", "-m", "c2")
        git(self.react, "update-ref", "refs/remotes/origin/pack", "HEAD")
        git(self.react, "reset", "-q", "--hard", "HEAD~1")
        with self.assertRaises(GateFailed) as cm:
            ship.build_plan(RUN)
        self.assertIn("1 commit(s) behind origin/pack", cm.exception.reasons[0])
        self.assertEqual(len(ship.build_plan(RUN, allow_behind=True)["changes"]), 2)

    def test_missing_origin_ref(self):
        self.setup_ws()
        git(self.api, "update-ref", "-d", "refs/remotes/origin/pack")
        with self.assertRaises(GateFailed) as cm:
            ship.build_plan(RUN)
        self.assertIn("git fetch", cm.exception.reasons[0])

    def test_dependency_needs_a_merged_change_contract_naming_the_ticket(self):
        tk = [{"key": "DEV-1", "summary": "s", "depends_on": ["DEV-9"]}]
        self.setup_ws(tickets=tk)
        with self.assertRaises(GateFailed) as cm:
            ship.build_plan(RUN)
        self.assertIn("DEV-9", cm.exception.reasons[0])
        put_change(RUN, "API", "dep", state="open", title="DEV-9 thing")
        with self.assertRaises(GateFailed):
            ship.build_plan(RUN)
        put_change(RUN, "API", "dep", state="merged", title="unrelated", source_branch="feature/other")
        with self.assertRaises(GateFailed):  # no key match anywhere in title/description/branch
            ship.build_plan(RUN)
        put_change(RUN, "API", "dep", state="merged", title="unrelated", source_branch="feature/dev-9-thing")
        self.assertEqual(len(ship.build_plan(RUN)["changes"]), 2)
        put_change(RUN, "API", "dep", state="merged", title="x", description_md="closes DEV-90")  # not DEV-9
        with self.assertRaises(GateFailed):
            ship.build_plan(RUN)

    def test_stale_dependency_contract_does_not_count(self):
        tk = [{"key": "DEV-1", "summary": "s", "depends_on": ["DEV-9"]}]
        self.setup_ws(tickets=tk)
        put_change(RUN, "API", "dep", state="merged", title="DEV-9 thing")
        (self.home / f"runs/{RUN}/raw/change/API-dep.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(GateFailed) as cm:
            ship.build_plan(RUN)
        self.assertIn("unusable change contract", cm.exception.reasons[0])


class VerifyTests(ShipBase):
    def planned(self, **ship_extra):
        self.setup_ws(ship_extra=ship_extra or None)
        return ship.build_plan(RUN)

    def readback(self, plan, repo, repo_field=None, **over):
        c = next(x for x in plan["changes"] if x["repo"] == repo)
        kw = dict(number=1, source_branch=c["source_branch"], target_branch=c["target_branch"], title=c["title"],
                  draft=c["draft"], head_sha=HEAD)
        kw.update(over)
        if repo_field:
            kw["contract_repo"] = repo_field
        put_change(RUN, repo, c["target"], **kw)

    def test_verify_passes_on_matching_change_contracts_and_logs_once(self):
        plan = self.planned()
        self.assertTrue(any("change readback unusable" in e and "missing contract" in e for e in ship.verify(RUN)))
        put_for_plan(RUN, plan)
        self.assertEqual(ship.verify(RUN), [])
        run = json.loads((self.home / f"runs/{RUN}/run.json").read_text())
        self.assertEqual(run["events"][-1]["kind"], "ship")
        self.assertEqual(run["events"][-1]["data"]["verified"][0]["number"], 1)
        self.assertEqual(ship.verify(RUN), [])
        run = json.loads((self.home / f"runs/{RUN}/run.json").read_text())
        self.assertEqual(sum(1 for e in run["events"] if e["kind"] == "ship"), 1)

    def test_draft_mismatch_uses_the_bool_not_the_title(self):
        plan = self.planned()
        self.readback(plan, "React")
        self.readback(plan, "API", draft=False)
        errs = ship.verify(RUN)
        self.assertEqual(len(errs), 1)
        self.assertIn("draft is False", errs[0])
        self.readback(plan, "API", title="Draft: " + plan["changes"][0]["title"])  # a marker in the title is a title mismatch
        self.assertTrue(any("title" in e for e in ship.verify(RUN)))

    def test_branch_title_sha_and_repo_mismatches(self):
        plan = self.planned()
        self.readback(plan, "React")
        self.readback(plan, "API", target_branch="homolog", source_branch="other")
        errs = ship.verify(RUN)
        self.assertTrue(any("target_branch" in e for e in errs))
        self.assertTrue(any("source_branch" in e for e in errs))
        self.readback(plan, "API", title="wrong")
        self.assertTrue(any("title" in e for e in ship.verify(RUN)))
        self.readback(plan, "API", head_sha="a" * 40)
        self.assertTrue(any("change head sha aaaaaaaa != proven HEAD" in e for e in ship.verify(RUN)))
        self.readback(plan, "API", repo_field="React")
        self.assertTrue(any("readback is for repo 'React'" in e for e in ship.verify(RUN)))
        self.readback(plan, "API")
        self.assertEqual(ship.verify(RUN), [])

    def test_local_file_links_fail_in_readback_and_plan(self):
        plan = self.planned()
        self.readback(plan, "React")
        self.readback(plan, "API", description_md="see [log](evidence/C1/out.txt)")
        self.assertTrue(any("(readback)" in e and "local file" in e for e in ship.verify(RUN)))

    def test_reviewers_must_be_on_the_change(self):
        plan = self.planned(reviewers=[{"scm_user": "ana", "ticket_user": "u-1"}, "carl"])
        self.assertEqual(plan["changes"][0]["reviewers"], ["ana", "carl"])
        self.readback(plan, "React", reviewers=["ana", "carl"])
        self.readback(plan, "API", reviewers=["ana"])
        errs = ship.verify(RUN)
        self.assertEqual(len(errs), 1)
        self.assertIn("lack planned ['carl']", errs[0])
        self.readback(plan, "API")  # no reviewer data at all is unknown, never assumed
        self.assertTrue(any("no reviewers (unknown)" in e for e in ship.verify(RUN)))
        self.readback(plan, "API", reviewers=["carl", "ana", "zoe"])
        self.assertEqual(ship.verify(RUN), [])

    def test_stale_change_contract_is_not_evidence(self):
        plan = self.planned()
        put_for_plan(RUN, plan)
        raw = self.home / f"runs/{RUN}/raw/change/API-pack.json"
        raw.write_text('{"edited": true}', encoding="utf-8")
        errs = ship.verify(RUN)
        self.assertTrue(any("API/pack" in e and "changed since ingest" in e for e in errs), errs)

    def test_a_schema_one_plan_is_refused(self):
        plan = self.planned()
        put_for_plan(RUN, plan)
        path = self.home / f"runs/{RUN}/ship-plan.json"
        data = json.loads(path.read_text())
        data["schemaVersion"] = 1
        data["mrs"] = data.pop("changes")
        path.write_text(json.dumps(data), encoding="utf-8")
        errs = ship.verify(RUN)
        self.assertEqual(len(errs), 1)
        self.assertIn("re-run ship plan", errs[0])
        with self.assertRaises(FactoryError) as cm:
            ship.update_description(RUN)
        self.assertIn("re-run ship plan", str(cm.exception))

    def test_pipeline_watch_readback(self):
        plan = self.planned(pipeline_watch=True)
        put_for_plan(RUN, plan)
        errs = ship.verify(RUN)
        self.assertEqual(len(errs), 2)
        self.assertTrue(all("pipeline readback unusable" in e for e in errs))
        put_pipeline(RUN, "API", "pack", id="77", sha=HEAD, status="running")
        put_pipeline(RUN, "React", "pack", id="78", sha="b" * 40, status="success")
        errs = ship.verify(RUN)
        self.assertEqual(len(errs), 1)
        self.assertIn("pipeline 78 ran on bbbbbbbb, watched sha is ffffffff", errs[0])
        put_pipeline(RUN, "React", "pack", id="78", sha=HEAD, status="failed")
        self.assertEqual(ship.verify(RUN), [])  # the status is reported, judged by the feedback gate
        run = json.loads((self.home / f"runs/{RUN}/run.json").read_text())
        pipes = {v["repo"]: v["pipeline"] for v in run["events"][-1]["data"]["verified"]}
        self.assertEqual(pipes, {"API": {"id": "77", "status": "running"}, "React": {"id": "78", "status": "failed"}})


class UpdateDescriptionTests(ShipBase):
    def test_update_uses_the_number_of_the_change_contract(self):
        self.setup_ws()
        plan = ship.build_plan(RUN)
        with self.assertRaises(GateFailed) as cm:
            ship.update_description(RUN)
        self.assertIn("no number", cm.exception.reasons[0])
        self.assertEqual(cm.exception.fetch[0]["kind"], "change")
        put_for_plan(RUN, plan)
        self.write(f"runs/{RUN}/evidence/C1.json", {"check_id": "C1", "status": "fail"})
        out = ship.update_description(RUN, repo="React")
        self.assertEqual(out["schemaVersion"], 2)
        self.assertEqual([(a["type"], a.get("number")) for a in out["actions"]], [("change_request", 2), ("readback", None)])
        self.assertEqual(out["actions"][0]["op"], "update")
        self.assertIn("| C1 | FAIL |", out["actions"][0]["body_md"])
        saved = json.loads((self.home / f"runs/{RUN}/ship-plan.json").read_text())
        stored = next(a for a in saved["actions"] if a["type"] == "change_request" and a["repo"] == "React")
        self.assertIn("| C1 | FAIL |", stored["body_md"])
        self.assertIn("| C1 | FAIL |", next(c for c in saved["changes"] if c["repo"] == "React")["body_md"])


if __name__ == "__main__":
    unittest.main()
