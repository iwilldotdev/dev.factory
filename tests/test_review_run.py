"""Review runs: the queue, intake from the change contract, the detached worktree at the change head, the report with neutral intents, and `review fix`."""
import json
import unittest
from pathlib import Path

from tests.helpers import PY, git, head_of, lesson
from tests.m3_support import M3Case

ITEMS = {"items": [
    {"id": "41", "url": "https://scm.example/c/41", "repo": "web", "title": "Draft", "author": "ana", "draft": True},
    {"id": "42", "url": "https://scm.example/c/42", "repo": "web", "title": "Fix the value", "author": "ana", "draft": False, "ticket": "ABC-9"},
    {"id": "43", "url": "https://scm.example/c/43", "repo": "web", "title": "Mine", "author": "me", "draft": False, "own": True},
    {"id": "44", "url": "https://scm.example/c/44", "repo": "web", "title": "Approved already", "author": "bo", "draft": False, "approved": True}]}


class ReviewRunCase(M3Case):
    def setUp(self):
        super().setUp()
        self.run_dir = self.proj / ".factory" / "runs" / "review-42"

    def change_branch(self) -> str:
        """The change under review: a branch of the repo with the fix commit; returns its head."""
        git("checkout", "-q", "-b", "feature/fix-value", cwd=self.proj)
        (self.proj / "app.py").write_text("VALUE = 2" + chr(10), newline=chr(10))
        git("add", "app.py", cwd=self.proj)
        git("commit", "-q", "-m", "fix: value", cwd=self.proj)
        sha = head_of(self.proj)
        git("checkout", "-q", "main", cwd=self.proj)
        return sha

    def ingest_change(self, sha: str, **over) -> None:
        data = {"id": "42", "url": "https://scm.example/c/42", "repo": "web", "title": "Fix the value", "description_md": "Fixes the value.",
                "source_branch": "feature/fix-value", "target_branch": "main", "head_sha": sha, "state": "open", "draft": False, **over}
        raw = self.write("raw.json", json.dumps({"payload": data}), root=self.base)
        self.ok("ingest", "change", "--raw", str(raw), "--stdin", "--run", "review-42", stdin=json.dumps(data))

    def started(self) -> str:
        sha = self.change_branch()
        self.ok("review", "start", "42", "--repo", "web")
        self.ingest_change(sha)
        self.ok("gate", "intake")
        self.ok("run", "branch")
        return sha

    def plan(self) -> None:
        self.write_plan(self.run_dir, {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [{"id": "AC-1", "text": "VALUE is 2", "source": "ticket:description"}],
                                       "checks": [{"id": "CHK-1", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": f'"{PY}" check.py 2',
                                                   "expect_pattern": "checks passed: VALUE=2"}]})
        self.ok("gate", "plan")


class ReviewRunTests(ReviewRunCase):
    def phase(self) -> str:
        return json.loads(self.ok("next", "--json"))["phase"]

    def test_the_queue_drops_drafts_own_and_already_approved_changes(self):
        path = self.write("queue.json", json.dumps(ITEMS), root=self.base)
        out = json.loads(self.ok("review", "queue", "--file", str(path), "--json"))
        self.assertEqual([i["id"] for i in out], ["42"])
        self.assertIn("no change list", self.fails("review", "queue"))

    @lesson("L-185")
    def test_a_review_run_goes_through_intake_plan_verify_review_and_report_and_nothing_is_run_by_itself(self):
        """L-185: intake needs the change contract; the worktree is detached at the change head; the report writes neutral intents that are never auto-run."""
        sha = self.change_branch()
        self.ok("review", "start", "42", "--repo", "web")
        self.assertEqual(self.phase(), "intake")
        self.assertIn("no change contract", json.loads(self.ok("next", "--json"))["reasons"][0])
        self.ingest_change(sha)
        self.ok("gate", "intake")
        self.ok("run", "branch")
        wt = [e for e in self.events(self.run_dir) if e["type"] == "worktree"][-1]
        self.assertEqual((wt["branch"], head_of(Path(wt["path"])), wt["branch_point"]), (None, sha, head_of(self.proj)))
        self.assertEqual(self.phase(), "plan")
        self.plan()
        self.assertEqual(self.phase(), "verify")
        self.ok("verify")
        self.ok("gate", "verify")
        self.assertEqual(self.phase(), "review")
        self.record("code", "CHANGES", findings=[{"id": "F1", "severity": "minor", "file": "app.py:1", "text": "name the constant", "ac": "AC-1"}])
        self.record("qa", "APPROVE")
        self.assertEqual(self.phase(), "report")
        bad = self.write("report.json", json.dumps({"verdict": "ok", "summary": "fine"}), root=self.base)
        self.assertIn("contradicts the reviews", self.fails("review", "report", "--file", str(bad)))
        good = self.write("report2.json", json.dumps({"verdict": "not-ok", "summary": "one naming issue", "comment_md": "Please name the constant."}), root=self.base)
        self.ok("review", "report", "--file", str(good))
        plan = json.loads((self.run_dir / "report" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual([(a["intent"], a["needs_user"], a["auto"]) for a in plan["remote_actions"]], [("change_comment", True, False)])
        self.assertEqual(self.phase(), "done")
        self.assertEqual(self.gate("report")[0], 0)
        self.assertEqual([e["type"] for e in self.events(self.run_dir) if e["type"] in ("ship_planned", "ship_verified")], [])
        self.assertEqual(json.loads(self.ok("next", "--json"))["kind_of_action"], "none")

    def test_an_ok_report_plans_the_approval_and_a_head_that_moves_asks_for_a_new_report(self):
        self.started()
        self.plan()
        self.ok("verify")
        self.ok("gate", "verify")
        self.approve_both()
        self.ok("review", "report", "--file", str(self.write("r.json", json.dumps({"verdict": "ok", "summary": "good", "ticket_comment_md": "Reviewed."}), root=self.base)))
        plan = json.loads((self.run_dir / "report" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual([a["intent"] for a in plan["remote_actions"]], ["change_comment", "change_approve", "issue_comment"])

    def test_review_fix_turns_the_run_into_a_ticket_run_on_the_change_branch_keeping_the_plan_and_evidence(self):
        sha = self.started()
        self.plan()
        self.ok("verify")
        before = len(self.evidence(self.run_dir))
        self.ok("review", "fix")
        wt = [e for e in self.events(self.run_dir) if e["type"] == "worktree"][-1]
        self.assertEqual(wt["branch"], "feature/fix-value")
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=Path(wt["path"])), "feature/fix-value")
        self.assertEqual(self.events(self.run_dir)[-1]["kind"], "feature")
        self.assertEqual((len(self.evidence(self.run_dir)), head_of(Path(wt["path"]))), (before, sha))
        self.assertEqual(self.phase(), "intake")


if __name__ == "__main__":
    unittest.main()
