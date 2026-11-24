"""Runtime checks end to end through `verify`: binding to the served tree, heads, http, env failures, data and sessions."""
import json
import os
import unittest
from pathlib import Path

from dev_factory import env as envmod
from tests.helpers import PY, git, lesson
from tests.m4_support import M4Case, WorkspaceCase


class Runtime(M4Case):
    def http_plan(self, *checks, acs=None):
        ac = acs or [{"id": "AC-1", "text": "The API answers", "source": "ticket:description"}]
        return {"schema": 1, "kind": "feature", "repos": ["web"], "acs": ac,
                "checks": [{"type": "http", "repo": "web", "ac": ["AC-1"], "method": "GET", **c} for c in checks]}

    def locked(self, *checks, up=True, kind="feature", plan=None) -> Path:
        run = self.branch(kind)
        if up:
            self.up()
        self.write_plan(run, plan or self.http_plan(*checks))
        self.ok("gate", "plan")
        return run

    def verify(self, *flags, code=None):
        got, out, err = self.cli("verify", "--json", *flags)
        if code is not None:
            self.assertEqual(got, code, out + err)
        return json.loads(out) if out.strip().startswith("{") else {"error": err, "exit": got}


class HttpTests(Runtime):
    def test_an_http_check_passes_and_its_record_names_the_heads_and_the_served_process(self):
        run = self.locked({"id": "CHK-1", "path": "/api/overdue", "expect_status": 200, "expect_json": {"items": [{"status": "overdue"}]}})
        self.fix()
        self.verify(code=0)
        (rec,) = self.evidence(run)
        head = git("rev-parse", "HEAD", cwd=self.tree())
        self.assertEqual((rec["result"], rec["heads"], rec["cmd"]), ("pass", {"web": head}, "GET /api/overdue"))
        self.assertEqual(rec["served"]["web"]["sha"], head)
        self.assertEqual(rec["served"]["web"]["path"], self.tree().as_posix())
        self.assertEqual(self.gate("verify")[0], 0)

    @lesson("L-83")
    def test_a_failure_states_the_status_and_the_expectation_and_never_quotes_the_body(self):
        """L-83: an http failure names the status and the failed expectation, never the response body."""
        run = self.locked({"id": "CHK-1", "path": "/api/overdue", "expect_status": 200, "expect_json": {"items": [{"status": "paid"}]}},
                          {"id": "CHK-2", "path": "/boom", "expect_status": 200})
        self.fix()
        self.verify(code=1)
        recs = {r["check"]: r for r in self.evidence(run)}
        self.assertEqual(recs["CHK-1"]["reason"], "status 200; expect_json mismatch at $.items[0].status")
        self.assertEqual(recs["CHK-2"]["reason"], "status 500, expected 200")
        blob = (run / "events.jsonl").read_text(encoding="utf-8") + "".join((run / a).read_text(encoding="utf-8") for r in recs.values() for a in r["artifacts"])
        for secret in ("never-quote-this-body", "SSL Authentication Error"):
            self.assertNotIn(secret, blob)

    @lesson("L-49")
    def test_a_failure_output_matching_env_patterns_is_an_env_failure_not_a_product_one(self):
        """L-49: [repo.env] env_patterns turn a matching http failure into kind env."""
        flag = self.base / "degraded.flag"
        os.environ["FAKE_FLAG_FILE"] = str(flag)
        self.addCleanup(os.environ.pop, "FAKE_FLAG_FILE", None)
        self.configure(extra_env='env_patterns = ["SSL Authentication Error"]\n')
        run = self.locked({"id": "CHK-1", "path": "/maybe", "expect_status": 200})
        self.fix()
        flag.write_text("down")
        out = self.verify(code=3)
        rec = self.evidence(run)[0]
        self.assertEqual((rec["result"], rec["kind"]), ("env", "env"))
        self.assertIn("env_patterns", rec["reason"])
        self.assertNotIn("degraded", rec["reason"] + rec["tail"])
        self.assertEqual(out["exit"], 3)

    @lesson("L-82")
    def test_an_http_check_cannot_leave_the_env_origin(self):
        """L-82: the path is joined to the declared base_url only; a path that would change the origin is refused at the plan."""
        run = self.branch()
        self.up()
        self.write_plan(run, self.http_plan({"id": "CHK-1", "path": "//evil.test/x", "expect_status": 200}))
        self.assertIn("must start with a single /", self.failing("plan"))

    @lesson("L-52")
    def test_a_session_check_sends_the_login_headers_and_they_are_never_written_down(self):
        """L-52: `session` sends the headers login_http printed; the values stay in memory and out of every file."""
        script = self.write("headers.py", 'import json\nprint(json.dumps({"X-Token": "tok-123"}))\n', root=self.base)
        self.configure(extra_env=f"login_http = {json.dumps(chr(34) + PY + chr(34) + ' ' + chr(34) + script.as_posix() + chr(34))}\n")
        run = self.locked({"id": "CHK-1", "path": "/auth", "expect_status": 200, "session": "qa"},
                          {"id": "CHK-2", "path": "/auth", "expect_status": 401})
        self.fix()
        self.verify(code=0)
        blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in run.rglob("*") if p.is_file())
        self.assertNotIn("tok-123", blob)


class BindingTests(Runtime):
    @lesson("L-13", "L-69", "L-85")
    def test_a_new_commit_restarts_the_env_once_and_the_record_names_the_start_sha(self):
        """L-13 / L-85: the served process is judged by its recorded start SHA, and a stale one is restarted once; L-69: it serves the checked tree."""
        run = self.locked({"id": "CHK-1", "path": "/whoami", "expect_status": 200})
        first = self.lock_of()
        head = self.fix()
        self.assertNotEqual(first["sha"], head)
        self.verify(code=0)
        self.assertEqual(self.lock_of()["sha"], head)
        self.assertNotEqual(self.lock_of()["pid"], first["pid"])
        (rec,) = self.evidence(run)
        self.assertEqual((rec["served"]["web"]["sha"], rec["served"]["web"]["pid"]), (head, self.lock_of()["pid"]))
        self.assertEqual([e["action"] for e in self.events(run) if e["type"] == "env" and e["action"] != "down"], ["up", "restart"])

    def test_the_binding_names_what_differs(self):
        self.locked({"id": "CHK-1", "path": "/whoami", "expect_status": 200})
        self.fix()
        from dev_factory import project
        proj = project.discover(str(self.proj))
        problems, _ = envmod.binding(proj, proj.run("ABC-1").state(), "web")
        self.assertIn("started at", problems[0])
        self.assertIn("HEAD is", problems[0])

    @lesson("L-46")
    def test_after_an_env_failure_the_remaining_runtime_checks_are_skipped_and_the_exit_is_3(self):
        """L-46: once the env fails, the repo's other runtime checks are `skipped: env` instead of each waiting for its own timeout."""
        run = self.locked({"id": "CHK-1", "path": "/health", "expect_status": 200}, {"id": "CHK-2", "path": "/whoami", "expect_status": 200},
                          {"id": "CHK-3", "path": "/api/overdue", "expect_status": 200})
        self.fix()
        self.ok("env", "down", "web")
        os.environ["FAKE_DIE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_DIE", None)
        out = self.verify(code=3)
        by = {r["check"]: r for r in self.evidence(run)}
        self.assertEqual(sorted(r["result"] for r in by.values()), ["env", "skipped", "skipped"])
        self.assertTrue(all("[L-46]" in r["reason"] for r in by.values() if r["result"] == "skipped"))
        self.assertEqual(out["exit"], 3)
        self.assertNotEqual(self.gate("verify")[0], 0)


class WorkspaceTests(WorkspaceCase):
    @lesson("L-12")
    def test_a_commit_in_the_dependency_invalidates_a_runtime_pass_of_the_dependent(self):
        """L-12: web's http check exercises api too; a new api commit means the old pass is not reused and the gate asks again."""
        run = self.branch()
        self.up()
        plan = {"schema": 1, "kind": "feature", "repos": ["api", "web"],
                "acs": [{"id": "AC-1", "text": "web is served", "source": "ticket:description"}],
                "checks": [{"id": "CHK-1", "type": "http", "repo": "web", "ac": ["AC-1"], "method": "GET", "path": "/whoami", "expect_status": 200}]}
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.commit("web.txt", "w\n", tree=self.tree("web"))
        self.commit("api.txt", "a\n", tree=self.tree("api"))
        out = json.loads(self.ok("verify", "--json"))
        self.assertFalse(out["results"][0]["reused"])
        rec = self.evidence(run)[-1]
        self.assertEqual(sorted(rec["heads"]), ["api", "web"])
        self.assertTrue(json.loads(self.ok("verify", "--json"))["results"][0]["reused"])
        self.commit("api2.txt", "b\n", tree=self.tree("api"))
        self.assertIn("another HEAD", "\n".join(self.gate("verify")[1]))
        self.assertFalse(json.loads(self.ok("verify", "--json"))["results"][0]["reused"])


if __name__ == "__main__":
    unittest.main()
