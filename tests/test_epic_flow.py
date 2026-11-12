import json
import sys
from pathlib import Path

from factory import core
from tests.real_support import RealCase, cli, commit_file

EPIC = "ws-ep-1"
PASS = f'"{sys.executable}" -c "pass"'


def write_epic(tickets):
    core.write_json(core.run_dir(EPIC) / "epic.json", {"schemaVersion": 1, "tickets": tickets})


STD = [
    {"key": "DEV-1", "repos": ["API"], "summary": "Base API"},
    {"key": "DEV-2", "repos": ["React"], "depends_on": ["DEV-1"], "summary": "UI"},
    {"key": "DEV-3", "repos": ["API"], "depends_on": ["DEV-1"], "summary": "API follow-up"},
]


class EpicBase(RealCase):
    def start(self, keys=("DEV-1", "DEV-2", "DEV-3")):
        self.ok("epic", "start", "ws", "EP-1", "--tickets", *keys)

    def set_phase(self, run_id, phase):
        run = core.run_load(run_id)
        run["phase"] = phase
        core.run_save(run)


class TestEpicPlan(EpicBase):
    def test_start_cross_links(self):
        self.start()
        parent = core.run_load(EPIC)
        self.assertEqual(parent["kind"], "epic")
        self.assertEqual(parent["children"], ["ws-dev-1", "ws-dev-2", "ws-dev-3"])
        child = core.run_load("ws-dev-2")
        self.assertEqual((child["kind"], child["parent"]), ("ticket", EPIC))

    def test_waves_computed_and_children_updated(self):
        self.start()
        write_epic(STD)
        self.ok("epic", "plan", EPIC)
        parent = core.run_load(EPIC)
        self.assertEqual(parent["waves"], [["DEV-1"], ["DEV-2", "DEV-3"]])
        child = core.run_load("ws-dev-2")
        self.assertEqual(child["tickets"][0]["depends_on"], ["DEV-1"])
        self.assertEqual(child["tickets"][0]["summary"], "UI")
        self.assertEqual(child["wave"], 1)

    def test_cycle_rejected(self):
        self.start(("DEV-1", "DEV-2"))
        write_epic([{"key": "DEV-1", "repos": ["API"], "depends_on": ["DEV-2"]},
                    {"key": "DEV-2", "repos": ["API"], "depends_on": ["DEV-1"]}])
        rc, out = cli("epic", "plan", EPIC)
        self.assertEqual(rc, 1)
        self.assertIn("cycle", out)
        self.assertNotIn("waves", core.run_load(EPIC))

    def test_unknown_repo_rejected(self):
        self.start(("DEV-1",))
        write_epic([{"key": "DEV-1", "repos": ["Nope"]}])
        rc, out = cli("epic", "plan", EPIC)
        self.assertEqual(rc, 1)
        self.assertIn("Nope", out)

    def test_unknown_dep_and_missing_child_rejected(self):
        self.start(("DEV-1", "DEV-2"))
        write_epic([{"key": "DEV-1", "repos": ["API"], "depends_on": ["DEV-9"]}])
        rc, out = cli("epic", "plan", EPIC)
        self.assertEqual(rc, 1)
        self.assertIn("unknown ticket DEV-9", out)
        self.assertIn("child ws-dev-2 is missing", out)


class TestEpicStatus(EpicBase):
    def ready(self):
        rc, out = cli("--json", "epic", "status", EPIC)
        self.assertEqual(rc, 0, out)
        return json.loads(out)["ready"]

    def test_ready_children_follow_dependencies(self):
        self.start()
        write_epic(STD)
        self.ok("epic", "plan", EPIC)
        self.assertEqual(self.ready(), ["DEV-1"])
        self.set_phase("ws-dev-1", "ship")
        self.assertEqual(self.ready(), ["DEV-2", "DEV-3"])

    def test_blocked_child_does_not_block_independent(self):
        self.start()
        write_epic([{"key": "DEV-1", "repos": ["API"]}, {"key": "DEV-2", "repos": ["React"]},
                    {"key": "DEV-3", "repos": ["API"], "depends_on": ["DEV-2"]}])
        self.ok("epic", "plan", EPIC)
        self.set_phase("ws-dev-2", "blocked")
        self.assertEqual(self.ready(), ["DEV-1"])
        self.assertIn("ready: DEV-1", self.ok("epic", "status", EPIC))


class TestStackedBranch(EpicBase):
    def test_from_run_records_base_branch(self):
        self.start()
        write_epic(STD)
        self.ok("epic", "plan", EPIC)
        self.ok("run", "branch", "ws-dev-1", "API", "--base", "main")
        wt1 = Path(core.run_load("ws-dev-1")["repos"]["API"]["worktree"])
        sha1 = commit_file(wt1, "a.txt", "1")
        self.ok("run", "branch", "ws-dev-3", "API", "--base", "main", "--from-run", "ws-dev-1")
        rec = core.run_load("ws-dev-3")["repos"]["API"]
        self.assertEqual(rec["base_branch"], core.run_load("ws-dev-1")["repos"]["API"]["branch"])
        self.assertEqual(rec["head"], sha1)

    def test_from_run_requires_same_repo_branch(self):
        self.start()
        rc, out = cli("run", "branch", "ws-dev-3", "API", "--base", "main", "--from-run", "ws-dev-1")
        self.assertEqual(rc, 2)

    def test_for_target_records_branch(self):
        self.start()
        self.ok("run", "branch", "ws-dev-1", "API", "--base", "main")
        self.ok("run", "branch", "ws-dev-1", "API", "--base", "main", "--for-target", "homolog")
        rec = core.run_load("ws-dev-1")["repos"]["API"]
        self.assertEqual(rec["branches"]["homolog"], rec["branch"] + "-homolog")


class TestEpicShip(EpicBase):
    def prepare(self):
        self.start()
        write_epic(STD)
        self.ok("epic", "plan", EPIC)
        for cid, alias in (("ws-dev-1", "API"), ("ws-dev-2", "React"), ("ws-dev-3", "API")):
            extra = ["--from-run", "ws-dev-1"] if cid == "ws-dev-3" else []
            self.ok("run", "branch", cid, alias, "--base", "main", *extra)
            wt = Path(core.run_load(cid)["repos"][alias]["worktree"])
            commit_file(wt, f"{cid}.txt", cid)
            core.write_json(core.run_dir(cid) / "acs.json", {"schemaVersion": 1, "acs": [
                {"id": "AC-1", "text": "works", "kind": "logic", "checks": ["C1"]}],
                "checks": [{"id": "C1", "type": "test", "repo": alias, "cmd": PASS}]})
            self.lock_and_prove(cid)
            self.set_phase(cid, "ship")

    def test_aggregated_order_and_depends_on(self):
        self.prepare()
        plan = json.loads(self.ok("--json", "epic", "ship", "plan", EPIC))
        self.assertEqual([(m["child"], m["repo"]) for m in plan["mrs"]],
                         [("ws-dev-1", "API"), ("ws-dev-3", "API"), ("ws-dev-2", "React")])
        by_child = {m["child"]: m for m in plan["mrs"]}
        self.assertEqual([d["child"] for d in by_child["ws-dev-3"]["depends_on"]], ["ws-dev-1"])
        self.assertIn("Depends on {mr_url:ws-dev-1:API:main}", by_child["ws-dev-3"]["description"])
        self.assertIn("{mr_url:ws-dev-1:API:main}", by_child["ws-dev-2"]["description"])
        self.assertEqual([a["type"] for a in plan["actions"]].count("jira_comment"), 1)
        self.assertEqual([a["child"] for a in plan["actions"] if a["type"] == "push"],
                         ["ws-dev-1", "ws-dev-3", "ws-dev-2"])

    def test_plan_refused_until_children_reviewed(self):
        self.prepare()
        self.set_phase("ws-dev-2", "review")
        rc, out = cli("epic", "ship", "plan", EPIC)
        self.assertEqual(rc, 1)
        self.assertIn("ws-dev-2", out)

    def test_verify_requires_every_child(self):
        self.prepare()
        plan = json.loads(self.ok("--json", "epic", "ship", "plan", EPIC))
        rc, _out = cli("epic", "ship", "verify", EPIC)
        self.assertEqual(rc, 1)
        for m in plan["mrs"]:
            core.write_json(core.run_dir(m["child"]) / m["readback"], {
                "source_branch": m["source_branch"], "target_branch": m["target_branch"],
                "title": m["title"], "draft": True, "web_url": "u", "iid": 1, "sha": m["head_sha"]})
        self.ok("epic", "ship", "verify", EPIC)


class TestEpicGate(EpicBase):
    def test_build_gate_requires_children_review_passed(self):
        self.start()
        write_epic(STD)
        self.ok("epic", "plan", EPIC)
        self.set_phase(EPIC, "build")
        self.set_phase("ws-dev-1", "ship")
        self.set_phase("ws-dev-2", "review")
        self.set_phase("ws-dev-3", "done")
        rc, out = cli("gate", EPIC, "build")
        self.assertEqual(rc, 1)
        self.assertIn("ws-dev-2", out)
        self.assertNotIn("ws-dev-3", out)
        self.set_phase("ws-dev-2", "ship")
        self.ok("gate", EPIC, "build")
        self.assertEqual(core.run_load(EPIC)["phase"], "ship")

    def test_plan_gate_requires_children_planned(self):
        self.start()
        write_epic(STD)
        self.ok("epic", "plan", EPIC)
        self.set_phase(EPIC, "plan")
        rc, out = cli("gate", EPIC, "plan")
        self.assertEqual(rc, 1)
        self.assertIn("plan gate not passed", out)
        for c in ("ws-dev-1", "ws-dev-2", "ws-dev-3"):
            self.set_phase(c, "build")
        self.ok("gate", EPIC, "plan")
        self.assertEqual(core.run_load(EPIC)["phase"], "build")
