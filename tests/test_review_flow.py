import json
import sys
from pathlib import Path

from factory import contracts, core
from tests.real_support import cli, commit_file, sh
from tests.review_support import SRC, ReviewCase, change_payload, native_item, native_list
from tests.ticket_support import put_ticket

PASS = {"id": "CHK-1", "type": "test", "repo": "React", "cmd": f'"{sys.executable}" -c "import sys; sys.exit(0)"', "expect": "exit0"}
FAIL = {"id": "CHK-2", "type": "test", "repo": "React", "cmd": f'"{sys.executable}" -c "import sys; sys.exit(1)"', "expect": "exit0"}


class TestReviewStart(ReviewCase):
    def test_single_start_records_facts(self):
        rid = self.start_single()
        self.assertEqual(rid, "ws-dev-9431-review")
        run = core.run_load(rid)
        self.assertEqual(run["kind"], "review")
        self.assertEqual(run["phase"], "intake")
        change = run["changes"][0]
        self.assertEqual((change["repo"], change["number"], change["head_sha"], change["ticket"], change["target_branch"],
                          change["source_branch"]), ("React", 669, self.sha, "DEV-9431", "main", SRC))
        self.assertNotIn("mrs", run)
        contract, problems = contracts.load_contract(rid, "change", "React-669")
        self.assertEqual((problems, contract["number"], contract["head_sha"], contract["repo"]), ([], 669, self.sha, "React"))
        self.assertTrue((core.run_dir(rid) / "raw" / "change" / "React-669.json").is_file())
        self.assertFalse((core.run_dir(rid) / "remote").exists())
        self.assertEqual(run["targets"], [])

    def test_start_from_a_changes_list_file(self):
        item = native_item(number=669, sha=self.sha, project="group/react", author="alice")
        path = self.tmp / "list.json"
        core.write_json(path, native_list(item, native_item(number=5, sha=self.sha)))
        out = json.loads(self.ok("--json", "review", "start", "ws", "--change", "React#669", "--from", str(path)))
        contract, problems = contracts.load_contract(out["run"], "change", "React-669")
        prov = contract["provenance"]
        self.assertEqual((problems, contract["author"], prov.get("format")), ([], "alice", None))
        self.assertEqual((prov["profile"], prov["item"], prov["raw_path"]), ("acme", 0, "raw/change_list/list.json"))
        lst, lproblems = contracts.load_contract(out["run"], "change_list", "list")
        self.assertEqual((lproblems, len(lst["items"])), ([], 2))
        self.assertEqual(core.run_load(out["run"])["changes"][0]["author"], "alice")

    def test_start_by_change_url_resolves_the_repo_with_the_profile(self):
        self.save_change(sha=self.sha)
        url = "https://scm.example/group/react/changes/669"
        out = json.loads(self.ok("--json", "review", "start", "ws", "--change", url, "--from", str(self.inbox)))
        self.assertEqual(core.run_load(out["run"])["changes"][0]["repo"], "React")
        rc, text = cli("review", "start", "ws", "--change", "https://scm.example/other/proj/changes/3", "--from", str(self.inbox))
        self.assertEqual(rc, 2)
        self.assertIn("no workspace repo has scm_project 'other/proj'", text)
        rc, text = cli("review", "start", "ws", "--change", "https://elsewhere.example/x/3", "--from", str(self.inbox))
        self.assertEqual(rc, 2)
        self.assertIn("refs.change_url", text)
        rc, text = cli("review", "start", "ws", "--change", "React!669", "--from", str(self.inbox))
        self.assertEqual(rc, 2)
        self.assertIn("<alias>#<number>", text)

    def test_start_url_with_ticket_suffix(self):
        self.save_change(sha=self.sha, title="no key", source="fix-drawer")
        url = "https://scm.example/group/react/changes/669=ABC-7"
        out = json.loads(self.ok("--json", "review", "start", "ws", "--change", url, "--from", str(self.inbox)))
        self.assertEqual(core.run_load(out["run"])["changes"][0]["ticket"], "ABC-7")

    def test_missing_payload_prints_the_profile_fetch(self):
        rc, out = cli("--json", "review", "start", "ws", "--change", "React#669", "--from", str(self.inbox))
        self.assertEqual(rc, 2)
        item = json.loads(out.splitlines()[0])["fetch"][0]
        raw = (self.inbox / "React-669.json").as_posix()
        self.assertEqual((item["kind"], item["id"], item["raw_path"]), ("change", "React-669", raw))
        self.assertIn("acme-cli change get group/react 669", item["hint"])
        self.assertNotIn("tool", item)

    def test_missing_payload_and_ticket(self):
        rc, out = cli("review", "start", "ws", "--change", "React#669", "--from", str(self.inbox))
        self.assertEqual(rc, 2)
        self.assertIn("React-669.json", out)
        self.save_change(sha=self.sha, title="no key", source="fix-drawer")
        rc, out = cli("review", "start", "ws", "--change", "React#669", "--from", str(self.inbox))
        self.assertEqual(rc, 2)
        self.assertIn("ticket", out)
        self.ok("review", "start", "ws", "--change", "React#669", "--ticket", "dev-5", "--from", str(self.inbox))
        self.assertTrue((core.run_dir("ws-dev-5-review") / "run.json").is_file())

    def test_batch_has_parent_and_children(self):
        self.save_change("React", sha=self.sha)
        self.save_change("API", number=12, title="[DEV-9500] - api", source="feature/DEV-9500-api", sha=self.sha)
        out = json.loads(self.ok("--json", "review", "start", "ws", "--change", "React#669", "--change", "API#12",
                                 "--from", str(self.inbox)))
        parent = core.run_load(out["run"])
        self.assertEqual(parent["kind"], "review-batch")
        self.assertEqual(parent["children"], ["ws-dev-9431-review", "ws-dev-9500-review"])
        child = core.run_load("ws-dev-9500-review")
        self.assertEqual((child["kind"], child["parent"]), ("review", parent["id"]))
        self.assertEqual(child["changes"][0]["repo"], "API")

    def test_batch_done_when_every_child_done(self):
        self.save_change("React", sha=self.sha)
        self.save_change("API", number=12, title="[DEV-9500] - api", source="feature/DEV-9500-api", sha=self.sha)
        parent = json.loads(self.ok("--json", "review", "start", "ws", "--change", "React#669", "--change", "API#12",
                                    "--from", str(self.inbox)))["run"]
        self.ok("gate", parent, "intake")
        rc, out = cli("gate", parent, "report")
        self.assertEqual(rc, 1)
        self.assertIn("ws-dev-9431-review", out)
        for cid in core.run_load(parent)["children"]:
            run = core.run_load(cid)
            run["phase"] = "done"
            core.run_save(run)
        self.ok("gate", parent, "report")
        self.assertEqual(core.run_load(parent)["phase"], "done")


class TestRunBranchChange(ReviewCase):
    def test_detached_worktree_at_sha(self):
        rid = self.start_single()
        self.ok("run", "branch", rid, "React", "--change", "669")
        run = core.run_load(rid)
        wt = Path(run["repos"]["React"]["worktree"])
        self.assertTrue(wt.is_dir())
        self.assertEqual(core.git_head(wt), self.sha)
        self.assertEqual(sh("git", "branch", "--show-current", cwd=wt), "")  # detached
        self.assertEqual(run["repos"]["React"]["base"], "main")

    def test_sha_not_local_tells_to_fetch(self):
        rid = self.start_single(sha="a" * 40)
        rc, out = cli("run", "branch", rid, "React", "--change", "669")
        self.assertEqual(rc, 2)
        self.assertIn(f"git -C {self.repo_dirs['React']} fetch origin {SRC}", out)
        self.assertEqual(core.run_load(rid)["repos"], {})

    def test_ticket_run_rejects_change(self):
        self.ok("run", "start", "ws", "DEV-1")
        rc, out = cli("run", "branch", "ws-dev-1", "React", "--change", "1")
        self.assertEqual(rc, 2)
        rc, out = cli("run", "branch", "ws-dev-1", "React")
        self.assertEqual(rc, 2)
        self.assertIn("--base", out)


class TestReviewGates(ReviewCase):
    def prepare(self, checks):
        rid = self.start_single()
        put_ticket(rid, "DEV-9431")
        self.write_acs(rid, checks)
        return rid

    def test_sequence_without_build_or_ship(self):
        rid = self.prepare([PASS])
        self.ok("gate", rid, "intake")
        rc, out = cli("gate", rid, "plan")
        self.assertEqual(rc, 1)
        self.assertIn("--change 669", out)  # plan needs the change worktree
        self.ok("run", "branch", rid, "React", "--change", "669")
        self.ok("gate", rid, "plan")
        self.assertEqual(core.run_load(rid)["phase"], "prove")
        self.ok("prove", rid)
        self.ok("gate", rid, "prove")
        for bad in ("build", "ship"):
            rc, out = cli("gate", rid, bad)
            self.assertEqual(rc, 2, out)
        rc, out = cli("gate", rid, "review")
        self.assertEqual(rc, 1)
        self.record(rid, "code")
        self.record(rid, "qa")
        self.ok("gate", rid, "review")
        rc, out = cli("gate", rid, "report")
        self.assertEqual(rc, 1)
        self.assertIn("review-report.json missing", out)
        self.ok("review", "report", rid)
        rep = json.loads((core.run_dir(rid) / "review-report.json").read_text(encoding="utf-8"))
        self.assertEqual(list(rep["profiles"]), ["acme"])
        self.assertTrue(rep["profiles"]["acme"].startswith("sha256:"))
        self.assertIn("- acme: sha256:", (core.run_dir(rid) / "review-report.md").read_text(encoding="utf-8"))
        self.ok("gate", rid, "report")
        self.assertEqual(core.run_load(rid)["phase"], "done")
        phases = [e["data"]["to"] for e in core.run_load(rid)["events"] if e["kind"] == "phase"]
        self.assertEqual(phases, ["intake", "plan", "prove", "review", "report", "done"])

    def test_intake_needs_a_fresh_change_contract(self):
        rid = self.prepare([PASS])
        (core.run_dir(rid) / "raw" / "change" / "React-669.json").write_text("{}", encoding="utf-8")  # stale
        rc, out = cli("gate", rid, "intake")
        self.assertEqual(rc, 1)
        self.assertIn("change/React-669", out)
        self.assertIn("FETCH change React-669 -> ", out)
        self.assertIn("acme-cli change get group/react 669", out)
        self.assertNotIn("remote", out)

    def test_intake_without_changes_in_the_run(self):
        rid = self.prepare([PASS])
        run = core.run_load(rid)
        run["changes"] = []
        core.run_save(run)
        rc, out = cli("gate", rid, "intake")
        self.assertEqual(rc, 1)
        self.assertIn("no changes in run", out)

    def test_changes_verdict_does_not_loop_to_build(self):
        rid = self.prepare([PASS])
        self.ok("run", "branch", rid, "React", "--change", "669")
        self.lock_and_prove(rid)
        run = core.run_load(rid)
        run["phase"] = "review"
        core.run_save(run)
        self.record(rid, "code", "CHANGES", [{"id": "F1", "severity": "blocker", "file": "a.ts:1", "text": "bug", "ac": "AC-1"}])
        self.record(rid, "qa")
        self.ok("gate", rid, "review")
        self.assertEqual(core.run_load(rid)["phase"], "report")

    def test_prove_gate_binds_to_change_head(self):
        rid = self.prepare([PASS])
        self.ok("run", "branch", rid, "React", "--change", "669")
        self.ok("prove", rid)
        wt = Path(core.run_load(rid)["repos"]["React"]["worktree"])
        sh("git", "checkout", "-q", "--detach", "main", cwd=wt)
        run = core.run_load(rid)
        run["phase"] = "prove"
        core.run_save(run)
        rc, out = cli("gate", rid, "prove")
        self.assertEqual(rc, 1)
        self.assertIn("change head", out)

    def test_ticket_run_has_no_report_gate(self):
        self.ok("run", "start", "ws", "DEV-1")
        rc, out = cli("gate", "ws-dev-1", "report")
        self.assertEqual(rc, 2)


class TestReviewReport(ReviewCase):
    def prepare(self, checks, prove=True):
        rid = self.start_single()
        self.ok("run", "branch", rid, "React", "--change", "669")
        self.write_acs(rid, checks)
        if prove:
            cli("prove", rid)
        return rid

    def report(self, rid) -> dict:
        return json.loads(self.ok("--json", "review", "report", rid))

    def test_ok_when_evidence_passes_and_reviews_approve(self):
        rid = self.prepare([PASS])
        self.record(rid, "code")
        self.record(rid, "qa")
        rep = self.report(rid)
        self.assertEqual(rep["verdict"], "ok")
        change = rep["changes"][0]
        self.assertEqual(change["acs"][0]["status"], "pass")
        self.assertEqual(change["freshness"]["behind"], 0)
        self.assertTrue(all(a["needs_user"] for a in rep["remote_actions"]))
        self.assertIn("change_approve", [a["type"] for a in rep["remote_actions"]])
        self.assertTrue((core.run_dir(rid) / "review-report.md").is_file())

    def test_report_actions_are_write_intents_with_markdown_bodies(self):
        rid = self.prepare([PASS])
        self.record(rid, "code", "CHANGES", [{"id": "F1", "severity": "major", "file": "src/x.ts:42", "text": "crash", "ac": "AC-1"}])
        self.record(rid, "qa")
        rep = self.report(rid)
        acts = {a["type"]: a for a in rep["remote_actions"]}
        self.assertEqual(sorted(acts), ["change_comment", "issue_comment"])  # not ok: no approval
        comment, issue = acts["change_comment"], acts["issue_comment"]
        self.assertEqual((comment["repo"], comment["number"], comment["needs_user"]), ("React", 669, True))
        self.assertIn("- [major] F1 src/x.ts:42 (AC-1): crash", comment["body_md"])
        self.assertIn("Review of React#669", comment["body_md"])
        self.assertEqual((issue["issues"], issue["needs_user"]), (["DEV-9431"], True))
        self.assertIn("**not-ok**", issue["body_md"])
        self.assertIn("Open blocker/major findings: 1.", issue["body_md"])
        for a in rep["remote_actions"]:
            self.assertLessEqual(set(a), {"type", "repo", "number", "issues", "body_md", "needs_user"})
        md = (core.run_dir(rid) / "review-report.md").read_text(encoding="utf-8")
        self.assertIn("| React#669 |", md)
        self.assertIn("- change_comment React#669", md)
        self.assertIn("- issue_comment DEV-9431", md)

    def test_approval_intent_only_when_everything_is_ok(self):
        rid = self.prepare([PASS])
        self.record(rid, "code")
        self.record(rid, "qa")
        acts = {a["type"]: a for a in self.report(rid)["remote_actions"]}
        self.assertEqual(sorted(acts), ["change_approve", "issue_comment"])
        self.assertEqual((acts["change_approve"]["repo"], acts["change_approve"]["number"]), ("React", 669))
        self.assertNotIn("body_md", acts["change_approve"])

    def test_not_ok_with_blocker_finding(self):
        rid = self.prepare([PASS])
        self.record(rid, "code", "CHANGES", [{"id": "F1", "severity": "blocker", "file": "src/x.ts:42", "text": "crash", "ac": "AC-1"}])
        self.record(rid, "qa")
        rep = self.report(rid)
        self.assertEqual(rep["verdict"], "not-ok")
        self.assertEqual(rep["changes"][0]["findings"][0]["file"], "src/x.ts:42")
        self.assertNotIn("change_approve", [a["type"] for a in rep["remote_actions"]])
        self.assertIn("src/x.ts:42", (core.run_dir(rid) / "review-report.md").read_text(encoding="utf-8"))

    def test_not_ok_when_evidence_fails(self):
        rid = self.prepare([PASS, FAIL])
        self.record(rid, "code")
        self.record(rid, "qa")
        self.assertEqual(self.report(rid)["verdict"], "not-ok")

    def test_incomplete_when_check_has_no_evidence(self):
        rid = self.prepare([PASS], prove=False)
        self.ok("prove", rid)
        self.write_acs(rid, [PASS, dict(PASS, id="CHK-9")])
        self.record(rid, "code")
        self.record(rid, "qa")
        rep = self.report(rid)
        self.assertEqual(rep["verdict"], "incomplete")
        self.assertTrue(any("CHK-9" in g for g in rep["changes"][0]["gaps"]))

    def test_incomplete_without_reviews_and_human_check_listed(self):
        human = {"id": "CHK-H", "type": "human", "text": "picker", "reason": "native"}
        rid = self.prepare([PASS, human])
        rep = self.report(rid)
        self.assertEqual(rep["verdict"], "incomplete")
        self.assertEqual(rep["changes"][0]["human_checks"][0]["id"], "CHK-H")
        self.assertEqual(rep["changes"][0]["acs"][1]["status"], "human")

    def test_batch_report_aggregates_children(self):
        self.save_change("React", sha=self.sha)
        self.save_change("API", number=12, title="[DEV-9500] - api", source="feature/DEV-9500-api", sha=self.sha)
        parent = json.loads(self.ok("--json", "review", "start", "ws", "--change", "React#669", "--change", "API#12",
                                    "--from", str(self.inbox)))["run"]
        rep = self.report(parent)
        self.assertEqual(rep["verdict"], "incomplete")
        self.assertEqual(len(rep["changes"]), 2)
        self.assertTrue((core.run_dir("ws-dev-9500-review") / "review-report.json").is_file())


class TestReviewFix(ReviewCase):
    def test_converts_kind_and_phase(self):
        rid = self.start_single()
        self.ok("run", "branch", rid, "React", "--change", "669")
        self.write_acs(rid, [PASS])
        self.ok("prove", rid)
        self.ok("review", "fix", rid)
        run = core.run_load(rid)
        self.assertEqual((run["kind"], run["phase"]), ("ticket", "build"))
        self.assertNotIn("changes", run)
        rec = run["repos"]["React"]
        wt = Path(rec["worktree"])
        self.assertEqual(sh("git", "branch", "--show-current", cwd=wt), SRC)
        self.assertEqual(core.git_head(wt), self.sha)
        self.assertEqual(run["targets"], ["main"])
        self.assertTrue((core.run_dir(rid) / "evidence" / "CHK-1.json").is_file())
        commit_file(wt, "src/fix.txt", "fix\n", "[DEV-9431] - fix")
        self.ok("gate", rid, "build")

    def test_dirty_worktree_refused(self):
        rid = self.start_single()
        self.ok("run", "branch", rid, "React", "--change", "669")
        wt = Path(core.run_load(rid)["repos"]["React"]["worktree"])
        (wt / "README.md").write_text("dirty\n", encoding="utf-8")
        rc, out = cli("review", "fix", rid)
        self.assertEqual(rc, 2)
        self.assertEqual(core.run_load(rid)["kind"], "review")

    def test_only_review_runs(self):
        self.ok("run", "start", "ws", "DEV-1")
        rc, _ = cli("review", "fix", "ws-dev-1")
        self.assertEqual(rc, 2)
