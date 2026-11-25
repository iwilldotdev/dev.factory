"""UAT in the browser: a change with a screen needs a third review, by a fresh reviewer who explored it through `factory uat run`."""
import json
import unittest

from tests.test_ui import UiCase


class UatTests(UiCase):
    def review(self, kind, reviewer, head, **over):
        data = {"verdict": "APPROVE", "reviewer": reviewer, "repos": {"web": head[:7]}, "findings": [], "resolved": [],
                "completeness": {"done": 1, "gap": 0, "not_checked": 0}, **over}
        if kind == "code":
            data.setdefault("changed_values", [])
        if kind == "qa":
            data.setdefault("exercises", [{"ac": "AC-1", "exercises": True, "note": "CHK-1 opens the page"}])
        path = self.base / f"review-{kind}-{reviewer}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return self.cli("review", "record", "--kind", kind, "--file", str(path))

    def test_a_change_with_a_screen_needs_a_uat_review_that_cites_exploration_runs_at_head(self):
        run = self.locked()
        self.ok("verify")
        self.ok("gate", "verify")
        head = self.evidence(run)[-1]["sha"]
        self.assertEqual(self.review("code", "rev-a", head)[0], 0)
        self.assertEqual(self.review("qa", "rev-a", head)[0], 0)
        self.assertIn("no uat review", "\n".join(self.gate("review")[1]))
        self.assertEqual(json.loads(self.ok("next", "--json"))["command"].split(" --file")[0], "factory review record --kind uat")
        explored = [{"flow": "open the page at a narrow width", "observed": "no overflow", "evidence": ["UAT-narrow"]}]
        code, out, err = self.review("uat", "rev-b", head, explored=explored)
        self.assertIn("no exploration run at the current HEAD", out + err)
        self.assertNotEqual(code, 0)
        (run / "uat").mkdir()
        (run / "uat" / "narrow.json").write_text(json.dumps({"actions": [{"goto": "/index.html"}]}), encoding="utf-8")
        self.assertIn("under", self.fails("uat", "run", "checks/s1.json"))
        self.assertIn("UAT-narrow attempt 1: pass", self.ok("uat", "run", "uat/narrow.json"))
        self.assertIn("also wrote the code or qa review", "".join(self.review("uat", "rev-a", head, explored=explored)[1:]))
        bad = [{"flow": "x", "observed": "y", "evidence": ["UAT-other"]}]
        self.assertIn("explored cites UAT-other", "".join(self.review("uat", "rev-b", head, explored=bad)[1:]))
        self.assertIn("explored:", "".join(self.review("uat", "rev-b", head)[1:]))  # a uat review without explored entries
        self.assertEqual(self.review("uat", "rev-b", head, explored=explored)[0], 0)
        self.assertEqual(self.gate("review")[0], 0)
        self.assertEqual([e["mode"] for e in self.evidence(run) if e["check"] == "UAT-narrow"], ["uat"])

    def test_a_change_without_a_screen_takes_no_uat(self):
        from dev_factory import review
        plan = {"checks": [{"id": "CHK-1", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": "x"}], "repos": ["web"]}
        cfg = {"repo": [{"name": "web", "check": []}], "checks": {"cmd_timeout_s": 60}}
        self.assertEqual(review.kinds(plan, cfg), ("code", "qa"))
        self.assertEqual(review.kinds(plan, None), ("code", "qa"))


if __name__ == "__main__":
    unittest.main()
