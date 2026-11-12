import json

from factory import core
from tests.real_support import cli
from tests.review_support import ReviewCase, mr_payload


class TestReviewQueue(ReviewCase):
    def queue(self, items, *extra):
        path = self.tmp / "queue.json"
        core.write_json(path, items)
        return json.loads(self.ok("--json", "review", "queue", "ws", "--mrs-json", str(path), *extra))

    def test_filters_drafts_own_and_approved(self):
        items = [
            mr_payload(1, title="[DEV-1] - ok"),
            mr_payload(2, title="[DEV-2] - draft", draft=True),
            mr_payload(3, title="[DEV-3] - mine", author="me"),
            mr_payload(4, title="[DEV-4] - approved", approved=["me"]),
            mr_payload(5, title="[DEV-5] - approved by other", approved=["bob"]),
            mr_payload(6, title="Draft: [DEV-6] - titled draft", draft=False),
        ]
        out = self.queue(items, "--me", "me")
        self.assertEqual([q["iid"] for q in out["queue"]], [1, 5])
        reasons = {s["mr"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(reasons["React!2"], "draft")
        self.assertEqual(reasons["React!3"], "authored by you")
        self.assertEqual(reasons["React!4"], "already approved by you")
        self.assertEqual(reasons["React!6"], "draft")

    def test_without_me_only_drafts_are_dropped(self):
        out = self.queue([mr_payload(1, author="me"), mr_payload(2, draft=True)])
        self.assertEqual([q["iid"] for q in out["queue"]], [1])

    def test_maps_ticket_and_repo(self):
        items = [mr_payload(1, title="Fix drawer", source="feature/dev-77-drawer", project="group/api"),
                 mr_payload(2, title="[ABC-12] - x"), mr_payload(3, title="no key", source="main-fix")]
        by_iid = {q["iid"]: q for q in self.queue(items)["queue"]}
        self.assertEqual((by_iid[1]["ticket"], by_iid[1]["repo"]), ("DEV-77", "API"))
        self.assertEqual((by_iid[2]["ticket"], by_iid[2]["repo"]), ("ABC-12", "React"))
        self.assertIsNone(by_iid[3]["ticket"])

    def test_workspace_ticket_regex_and_envelope(self):
        self.ws["ticket_regex"] = r"PRJ-\d+"
        self.save_ws()
        wrapped = {"content": [{"type": "text", "text": json.dumps({"merge_requests": [
            mr_payload(1, title="DEV-1 PRJ-5 thing")]})}]}
        out = self.queue(wrapped)
        self.assertEqual(out["queue"][0]["ticket"], "PRJ-5")

    def test_text_output(self):
        path = self.tmp / "q.json"
        core.write_json(path, [mr_payload(1), mr_payload(2, draft=True)])
        rc, out = cli("review", "queue", "ws", "--mrs-json", str(path))
        self.assertEqual(rc, 0)
        self.assertIn("React!1\tDEV-9431", out)
        self.assertIn("skipped React!2: draft", out)
