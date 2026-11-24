"""Epics: waves from depends_on, stacked branches from the dependency head, ready children under max_parallel, claims, and the epic ship."""
import json
import unittest
from pathlib import Path

from dev_factory import env as envmod, git as product_git, project as projmod
from dev_factory.run import Run
from tests.helpers import head_of, lesson
from tests.m2_support import M2Case
from tests.m3_support import ShipCase
from tests.m4_support import WorkspaceCase

CHILDREN = {"children": [{"ticket": "ABC-1", "repos": ["web"], "kind": "feature"}, {"ticket": "ABC-2", "repos": ["web"], "kind": "feature", "depends_on": ["ABC-1"]},
                         {"ticket": "ABC-3", "repos": ["web"], "kind": "feature"}, {"ticket": "ABC-4", "repos": ["web"], "kind": "feature", "depends_on": ["ABC-3"]}]}


class EpicCase(ShipCase):
    """Runs of one project; `switch` picks the run the CLI helpers talk to (every scoped command gets --run)."""
    cur: str | None = None

    def switch(self, ticket: str) -> None:
        self.cur, self.run_dir = ticket, self.proj / ".factory" / "runs" / ticket

    def cli(self, *args, **kw):
        free = args[:2] in (("run", "start"), ("review", "queue")) or args[0] in ("init", "check", "runs")
        if self.cur and not free and "--run" not in args:
            args += ("--run", self.cur)
        return super().cli(*args, **kw)

    def tree(self, repo: str = "web") -> Path:
        wts = [e for e in self.events(self.run_dir) if e["type"] == "worktree" and e["repo"] == repo and not e["throwaway"]]
        return Path(wts[-1]["path"])

    def epic(self, children=CHILDREN, extra: str = "") -> None:
        if extra:
            self.write("factory.toml", self.TOML + extra)
        self.start("EP-1", "--kind", "epic")
        self.switch("EP-1")
        self.write("epic.json", json.dumps(children), root=self.run_dir)
        self.ok("epic", "plan")

    def child_to_ship_plan(self, ticket: str) -> None:
        self.switch(ticket)
        self.ingest_ticket(self.run_dir, id=ticket, type="feature")
        self.ok("gate", "intake")
        self.ok("run", "branch")
        self.write_plan(self.run_dir, self.default_plan())
        self.ok("gate", "plan")
        self.commit("app.py" if ticket == "ABC-1" else "second.txt", "VALUE = 2\n" if ticket == "ABC-1" else "second\n", f"feat: {ticket}")
        self.ok("verify")
        self.ok("gate", "verify")
        self.approve_both()
        self.ok("gate", "review")
        self.ok("ship", "plan")

    def child_shipped(self) -> None:
        self.ingest_change()
        self.ok("confirm", "change:42", "--user-said", "yes, that is the change")
        self.ship_verify()
        self.ok("gate", "ship")


class EpicPlanTests(M2Case):
    def epic(self, children=CHILDREN, extra: str = "") -> Path:
        if extra:
            self.write("factory.toml", self.TOML + extra)
        run = self.start("EP-1", "--kind", "epic")
        self.write("epic.json", json.dumps(children), root=run)
        return run

    @lesson("L-185")
    def test_waves_come_from_depends_on_and_a_cycle_is_refused(self):
        """L-185: the epic plan computes waves from depends_on, starts one child run per ticket under the parent, and rejects cycles."""
        run = self.epic({"children": [{"ticket": "ABC-1", "depends_on": ["ABC-2"]}, {"ticket": "ABC-2", "depends_on": ["ABC-1"]}]})
        self.assertIn("depends_on cycle among: ABC-1, ABC-2", self.fails("epic", "plan", "--run", "EP-1"))
        self.assertEqual(sorted(p.name for p in (self.proj / ".factory" / "runs").iterdir()), ["EP-1"])
        self.write("epic.json", json.dumps({"children": [{"ticket": "ABC-1", "depends_on": ["NOPE"]}]}), root=run)
        self.assertIn("NOPE, which is not a child", self.fails("epic", "plan", "--run", "EP-1"))
        self.write("epic.json", json.dumps(CHILDREN), root=run)
        self.assertIn("[ABC-1, ABC-3] -> [ABC-2, ABC-4]", self.ok("epic", "plan", "--run", "EP-1"))
        kids = {p.name: [e for e in self.events(p) if e["type"] == "run_started"][0] for p in (self.proj / ".factory" / "runs").iterdir()}
        self.assertEqual({k: v.get("parent") for k, v in kids.items() if k != "EP-1"}, {"ABC-1": "EP-1", "ABC-2": "EP-1", "ABC-3": "EP-1", "ABC-4": "EP-1"})
        self.assertEqual(json.loads(self.ok("next", "EP-1", "--json"))["phase"], "children")
        self.assertIn("ABC-2 (intake)", self.ok("next", "EP-1"))

    @lesson("L-184")
    def test_next_lists_at_most_max_parallel_ready_children_and_a_claimed_run_refuses_another_session(self):
        """L-184: `next <epic>` hands out at most max_parallel ready children, counting the claimed ones; `next` from another session refuses without --takeover."""
        self.epic(extra="\n[orchestration]\nmax_parallel = 2\n")
        self.ok("epic", "plan", "--run", "EP-1")
        self.assertEqual(json.loads(self.ok("next", "EP-1", "--json"))["ready"], ["ABC-1", "ABC-3"])
        self.ok("claim", "ABC-1", "--session", "s1")
        self.assertEqual(json.loads(self.ok("next", "EP-1", "--session", "s2", "--json"))["ready"], ["ABC-3"])
        self.assertEqual(json.loads(self.ok("next", "EP-1", "--session", "s1", "--json"))["ready"], ["ABC-1", "ABC-3"])
        self.assertIn("claimed by session 's1'", self.fails("claim", "ABC-1", "--session", "s2"))
        self.assertIn("claimed by session 's1'", self.fails("next", "ABC-1", "--session", "s2"))
        self.assertIn("claimed by session 's1'", self.fails("next", "ABC-1"))
        self.ok("next", "ABC-1", "--session", "s1")
        self.ok("next", "ABC-1", "--session", "s2", "--takeover")
        claims = [e for e in self.events(self.proj / ".factory" / "runs" / "ABC-1") if e["type"] == "claim"]
        self.assertEqual([(c["session"], c["takeover"]) for c in claims], [("s1", False), ("s2", True)])
        self.ok("ask", "Which value?", "--run", "ABC-1")  # a parent answers for a child without owning it
        self.ok("answer", "Q-1", "--user-said", "two", "--run", "ABC-1")
        self.assertEqual(json.loads(self.ok("status", "EP-1", "--json"))["children"][0]["owner"], "s2")

    def test_with_max_parallel_one_nothing_more_starts_while_a_child_runs(self):
        self.epic()
        self.ok("epic", "plan", "--run", "EP-1")
        self.ok("claim", "ABC-1", "--session", "s1")
        n = json.loads(self.ok("next", "EP-1", "--session", "s2", "--json"))
        self.assertEqual((n["ready"], n["kind_of_action"]), ([], "none"))


class EpicRunTests(EpicCase):
    @lesson("L-91", "L-92")
    def test_the_epic_ships_the_children_in_order_with_stacked_branches_depends_on_links_and_one_ticket_comment(self):
        """L-91, L-92: the dependent's branch_point is the dependency's head and commit checks span branch_point..HEAD, so the dependency's commits are
        never the dependent's own; the epic ships the children in order with depends_on links and one ticket comment."""
        self.epic({"children": CHILDREN["children"][:2]}, extra="\n[ship]\nticket = \"comment\"\n")
        self.child_to_ship_plan("ABC-1")
        first = head_of(self.tree())
        self.assertEqual(json.loads(self.ok("next", "EP-1", "--json"))["phase"], "children")
        self.child_to_ship_plan("ABC-2")
        wt = [e for e in self.events(self.run_dir) if e["type"] == "worktree"][-1]
        self.assertEqual(wt["branch_point"], first)
        self.assertEqual(product_git.commit_problems(Path(wt["path"]), wt["branch_point"], "main", r"^feat: ABC-2$", []), [])
        self.assertEqual(len(product_git.commit_problems(Path(wt["path"]), head_of(self.proj), "main", r"^feat: ABC-2$", [])), 1)
        self.switch("EP-1")
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "ship")
        self.ok("epic", "ship", "plan")
        plan = json.loads((self.run_dir / "ship" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual([k["ticket"] for k in plan["children"]], ["ABC-1", "ABC-2"])
        crs = [i for i in plan["intents"] if i["intent"] == "change_request"]
        self.assertEqual([i["child"] for i in crs], ["ABC-1", "ABC-2"])
        self.assertEqual(crs[1]["depends_on_changes"][0]["ticket"], "ABC-1")
        self.assertNotIn("depends_on_changes", crs[0])
        self.assertEqual([i["intent"] for i in plan["intents"] if i["intent"].startswith("issue_")], ["issue_comment"])
        self.assertEqual(plan["intents"][-1]["ticket"], "EP-1")
        got, out, err = self.cli("epic", "ship", "verify")
        self.assertEqual(got, 1, out + err)
        self.assertIn("ABC-1: the ship gate has not passed", out)
        for t in ("ABC-1", "ABC-2"):
            self.switch(t)
            self.child_shipped()
        self.switch("EP-1")
        self.ok("epic", "ship", "verify")
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "done")
        status = self.ok("status", "EP-1")
        self.assertIn("ABC-2", status)


class SiblingTreeTests(WorkspaceCase):
    @lesson("L-88")
    def test_a_dependency_without_a_worktree_in_the_run_is_served_from_the_sibling_child_else_from_the_main_path(self):
        """L-88: web's env depends on api; the child run that only lists web serves api from the sibling child's api worktree, and warns when there is none."""
        self.write("epic.json", json.dumps({"children": [{"ticket": "ABC-1", "repos": ["api"], "kind": "feature"}, {"ticket": "ABC-2", "repos": ["web"], "kind": "feature"}]}), root=self.start("EP-1", "--kind", "epic"))
        self.ok("epic", "plan", "--run", "EP-1")
        proj = projmod.discover(str(self.proj))
        web = Run(proj.runs_dir, "ABC-2").state()
        path, from_main = envmod.target(proj, web, "api")
        self.assertTrue(from_main)
        self.assertEqual(Path(path).resolve(), (self.base / "api").resolve())
        data = {"id": "ABC-1", "title": "Show overdue invoices", "type": "feature", "status": "Open", "description_md": "x", "attachments": []}
        raw = self.write("raw.json", json.dumps({"payload": data}), root=self.base)
        self.ok("ingest", "ticket", "--raw", str(raw), "--stdin", "--run", "ABC-1", stdin=json.dumps(data))
        self.ok("gate", "intake", "--run", "ABC-1")
        self.ok("run", "branch", "--run", "ABC-1")
        sib = [e for e in Run(proj.runs_dir, "ABC-1").read() if e["type"] == "worktree"][-1]["path"]
        path, from_main = envmod.target(proj, Run(proj.runs_dir, "ABC-2").state(), "api")
        self.assertEqual((Path(path).resolve(), from_main), (Path(sib).resolve(), False))


if __name__ == "__main__":
    unittest.main()
