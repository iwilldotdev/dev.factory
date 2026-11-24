import json

from factory import core
from tests.real_support import cli
from tests.review_support import ReviewCase, native_item, native_list


class TestReviewQueue(ReviewCase):
    def queue(self, doc, *extra):
        path = self.tmp / "queue.json"
        core.write_json(path, doc)
        return json.loads(self.ok("--json", "review", "queue", "ws", "--list", str(path), *extra))

    def test_filters_drafts_own_and_approved_by_me(self):
        doc = native_list(
            native_item(1, title="[DEV-1] - ok"),
            native_item(2, title="[DEV-2] - draft", draft=True),
            native_item(3, title="[DEV-3] - mine", author="me"),
            native_item(4, title="[DEV-4] - approved", approved=["me"]),
            native_item(5, title="[DEV-5] - approved by other", approved=["bob"]),
            native_item(6, title="[DEV-6] - merged", state="merged"),
            native_item(7, title="[DEV-7] - unknown approvals", approved=None),
        )
        out = self.queue(doc, "--me", "me")
        self.assertEqual([q["number"] for q in out["queue"]], [1, 5, 7])
        reasons = {s["change"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(reasons["React#2"], "draft")
        self.assertEqual(reasons["React#3"], "authored by you")
        self.assertEqual(reasons["React#4"], "already approved by you")
        self.assertEqual(reasons["React#6"], "state merged")

    def test_me_matches_case_insensitively_and_defaults_to_the_workspace_scm_user(self):
        self.ws["scm_user"] = "Me"
        self.save_ws()
        doc = native_list(native_item(1, author="ME"), native_item(2, approved=["me"]), native_item(3, author="alice"))
        out = self.queue(doc)
        self.assertEqual([q["number"] for q in out["queue"]], [3])
        out = self.queue(doc, "--me", "alice")  # the flag wins over the workspace value
        self.assertEqual([q["number"] for q in out["queue"]], [1])

    def test_without_me_only_drafts_and_closed_are_dropped(self):
        out = self.queue(native_list(native_item(1, author="me"), native_item(2, draft=True), native_item(3, approved=["me"])))
        self.assertEqual([q["number"] for q in out["queue"]], [1, 3])

    def test_maps_ticket_and_repo(self):
        doc = native_list(
            native_item(1, title="Fix drawer", source="feature/dev-77-drawer", project="group/api", repo="?"),
            native_item(2, title="[ABC-12] - x"),
            native_item(3, title="no key", source="main-fix"))
        by_number = {q["number"]: q for q in self.queue(doc)["queue"]}
        self.assertEqual((by_number[1]["ticket"], by_number[1]["repo"]), ("DEV-77", "API"))  # by scm_project, not by name
        self.assertEqual((by_number[2]["ticket"], by_number[2]["repo"]), ("ABC-12", "React"))
        self.assertIsNone(by_number[3]["ticket"])

    def test_raw_list_is_mapped_with_the_scm_profile(self):
        path = self.tmp / "api-open.json"
        core.write_json(path, {"results": [
            {"n": 7, "link": "https://scm.example/group/api/changes/7", "title": "Bump deps", "text": "Routine.",
             "from": "chore/dev-3-deps", "into": "main", "tip": "t1", "status": "OPEN", "wip": False, "approvals": [{"login": "ana"}]},
            {"n": 8, "link": "https://scm.example/group/api/changes/8", "title": "WIP: split", "text": None,
             "from": "wip/split", "into": "main", "tip": "t2", "status": "OPEN", "wip": True},
            {"n": 9, "link": "https://scm.example/group/api/changes/9", "title": "Old", "text": None,
             "from": "old", "into": "main", "tip": "t3", "status": "DONE", "wip": False},
            {"n": 10, "link": "https://scm.example/group/api/changes/10", "title": "Seen", "text": None,
             "from": "seen", "into": "main", "tip": "t4", "status": "OPEN", "wip": False, "approvals": [{"login": "Me"}]}]})
        out = json.loads(self.ok("--json", "review", "queue", "ws", "--list", str(path), "--repo", "API", "--me", "me"))
        self.assertEqual([(q["change"], q["ticket"], q["head_sha"]) for q in out["queue"]], [("API#7", "DEV-3", "t1")])
        reasons = {s["change"]: s["reason"] for s in out["skipped"]}
        self.assertEqual(reasons, {"API#8": "draft", "API#9": "state merged", "API#10": "already approved by you"})

    def test_profile_ticket_refs(self):
        self.use_profile("zeta")
        out = self.queue(native_list(native_item(1, title="DEV-1 ZED-5 thing")))
        self.assertEqual(out["queue"][0]["ticket"], "ZED-5")

    def test_text_output(self):
        path = self.tmp / "q.json"
        core.write_json(path, native_list(native_item(1, title="[DEV-9431] - a"), native_item(2, draft=True)))
        rc, out = cli("review", "queue", "ws", "--list", str(path))
        self.assertEqual(rc, 0)
        self.assertIn("React#1\tDEV-9431", out)
        self.assertIn("skipped React#2: draft", out)

    def test_missing_list_prints_the_profile_fetch(self):
        path = self.tmp / "q.json"
        rc, out = cli("--json", "review", "queue", "ws", "--list", str(path), "--repo", "API")
        self.assertEqual(rc, 2)
        item = json.loads(out.splitlines()[0])["fetch"][0]
        self.assertEqual((item["kind"], item["raw_path"]), ("change_list", path.as_posix()))
        self.assertEqual(item["hint"], f"acme-cli change list group/api --state open --out {path.as_posix()}")
        rc, text = cli("review", "queue", "ws", "--list", str(path), "--repo", "API")
        self.assertIn(f"FETCH change_list q -> {path.as_posix()} | acme-cli change list group/api", text)

    def test_a_profile_without_change_list_is_reported(self):
        self.ws["providers"] = {"tickets": "zeta", "scm": "zeta"}
        self.save_ws()
        path = self.tmp / "q.json"
        core.write_json(path, {"results": []})
        rc, out = cli("review", "queue", "ws", "--list", str(path), "--repo", "API")
        self.assertEqual(rc, 2)
        self.assertIn("declares no kind 'change_list'", out)
