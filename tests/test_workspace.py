"""A workspace run over two member repos, end to end: branch, env with depends_on, verify and the gates per affected repo, ship plan."""
import json
import unittest

from tests.helpers import git
from tests.m2_support import check_cmd
from tests.m3_support import M3Case
from tests.m4_support import WorkspaceCase


class WorkspaceRunTests(WorkspaceCase, M3Case):
    def plan(self):
        return {"schema": 1, "kind": "feature", "repos": ["api", "web"], "acs": [
            {"id": "AC-1", "text": "The api value is 2", "source": "ticket:description"}, {"id": "AC-2", "text": "The web app is served", "source": "ticket:description"}],
            "checks": [{"id": "CHK-1", "type": "cmd", "repo": "api", "ac": ["AC-1"], "run": check_cmd(2), "expect_pattern": "checks passed: VALUE=2"},
                       {"id": "CHK-2", "type": "http", "repo": "web", "ac": ["AC-2"], "method": "GET", "path": "/whoami", "expect_status": 200,
                        "expect_json": {"cwd": ""}}]}

    def test_a_two_repo_feature_runs_from_branch_to_the_ship_plan_with_every_gate_per_repo(self):
        """Each affected repo needs its own commit past the branch point; dependencies served by env only are never gated for commits."""
        run = self.branch()
        self.assertEqual(sorted(w["repo"] for w in self.events(run) if w["type"] == "worktree"), ["api", "web"])
        out = self.up()
        self.assertLess(out.index("api: up"), out.index("web: up"))
        plan = self.plan()
        plan["checks"][1]["expect_json"] = {"cwd": self.tree("web").as_posix()}
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.assertIn("no commit past the branch point", self.failing("verify"))
        self.commit("app.py", "VALUE = 2\n", "fix: api value", tree=self.tree("api"))
        self.assertIn("repo web: no commit past the branch point", self.failing("verify"))
        self.commit("web.txt", "web change\n", "feat: web change", tree=self.tree("web"))
        self.ok("verify")
        self.assertEqual(self.gate("verify")[0], 0)
        by = {e["check"]: e for e in self.evidence(run)}
        self.assertEqual(by["CHK-1"]["sha"], git("rev-parse", "HEAD", cwd=self.tree("api")))
        self.assertEqual(sorted(by["CHK-2"]["heads"]), ["api", "web"])
        self.assertEqual(by["CHK-2"]["served"]["api"]["path"], self.tree("api").as_posix())
        self.run_dir = run
        heads = {"api": git("rev-parse", "HEAD", cwd=self.tree("api"))[:7], "web": git("rev-parse", "HEAD", cwd=self.tree("web"))[:7]}
        exercises = [{"ac": "AC-1", "exercises": True, "note": "CHK-1 asserts it"}, {"ac": "AC-2", "exercises": True, "note": "CHK-2 asserts it"}]
        self.record("code", repos=heads)
        self.record("qa", repos=heads, exercises=exercises)
        self.ok("gate", "review")
        self.ok("ship", "plan")
        intents = json.loads((run / "ship" / "plan.json").read_text(encoding="utf-8"))["intents"]
        self.assertEqual(sorted((i["intent"], i["repo"]) for i in intents if i["intent"] in ("push", "change_request")),
                         [("change_request", "api"), ("change_request", "web"), ("push", "api"), ("push", "web")])
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "ship")

    def test_a_new_commit_in_one_repo_invalidates_only_its_evidence(self):
        run = self.branch()
        self.up()
        plan = self.plan()
        plan["checks"][1]["expect_json"] = {"cwd": self.tree("web").as_posix()}
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.commit("app.py", "VALUE = 2\n", "fix: api value", tree=self.tree("api"))
        self.commit("web.txt", "web change\n", "feat: web change", tree=self.tree("web"))
        self.ok("verify")
        self.commit("more.txt", "x\n", "feat: more web", tree=self.tree("web"))
        text = "\n".join(self.gate("verify")[1])
        self.assertIn("CHK-2", text)
        self.assertNotIn("CHK-1:", text)
        out = json.loads(self.ok("verify", "--json"))
        self.assertEqual({r["check"]: r["reused"] for r in out["results"]}, {"CHK-1": True, "CHK-2": False})


if __name__ == "__main__":
    unittest.main()
