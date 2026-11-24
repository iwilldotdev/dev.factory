"""The feedback phase after ship: threads and pipeline jobs become findings, the run returns to build, and the gate needs resolved threads
and a green pipeline for the pushed head (or a waiver naming that pipeline)."""
import json
import unittest

from dev_factory.run import Run
from tests.helpers import adapter_scenario, lesson, provider_toml
from tests.m3_support import ShipCase

T1 = {"id": "T1", "author": "ana", "resolved": False, "body_md": "Round the total before showing it", "file": "app.py", "line": 1}
CONFIRM = ("confirm", "change:42", "--user-said", "yes, that is the change on the board")


class FeedbackCase(ShipCase):
    def shipped(self, extra_toml: str = "") -> None:
        """Both reviews approved, a change read back and confirmed, `ship verify` and the ship gate passed."""
        self.run_dir = self.proj / ".factory" / "runs" / "ABC-1"
        self.cached("shipped", lambda: self._shipped(extra_toml), extra_toml)

    def _shipped(self, extra_toml: str) -> None:
        self.to_ship(extra_toml=extra_toml)
        self.ship_plan()
        if "providers.change" in extra_toml:
            adapter_scenario(self, stdout=json.dumps({"contract": self.change_data(), "raw": {"k": 1}}))
        else:
            self.ingest_change()
            self.ok(*CONFIRM)
        self.ship_verify()
        self.ok("gate", "ship")

    def pipeline(self, pid: str = "P1", status: str = "success", sha: str | None = None, created: str = "2026-11-24T11:00:00Z", jobs=None) -> dict:
        return {"id": pid, "sha": sha or self.head(), "status": status, "created_at": created,
                "jobs": jobs if jobs is not None else [{"name": "lint", "status": "failed" if status == "failed" else "success", "allow_failure": False}]}

    def refresh(self, threads=(), pipe: dict | None = None, confirm: bool = True, **over) -> None:
        """What the harness does after the reviewers and the CI had their turn: read the change and its pipeline back, tagged as a refresh."""
        data = self.change_data(threads=list(threads), pipelines=[pipe["id"]] if pipe else [], **over)
        for name, body in (("change", data), *([("pipeline", pipe)] if pipe else [])):
            raw = self.run_dir / "feedback" / f"web-{name}.raw.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text(json.dumps({"payload": body}), encoding="utf-8")
            self.ok("ingest", name, "--raw", str(raw), "--stdin", "--refresh", stdin=json.dumps(body))
        if confirm:
            self.ok(*CONFIRM)
            if pipe:
                self.ok("confirm", f"pipeline:{pipe['id']}", "--user-said", "yes, that is the pipeline")

    def nxt(self) -> dict:
        return json.loads(self.ok("next", "--json"))

    def feedback_gate(self) -> tuple[int, str]:
        code, reasons = self.gate("feedback")
        return code, "\n".join(reasons)

    def rounds(self) -> list[dict]:
        return [e for e in self.reviews() if e["kind"] == "feedback"]


class PhaseTests(FeedbackCase):
    def test_the_ship_gate_opens_the_feedback_phase_and_next_asks_for_a_refresh(self):
        self.shipped()
        n = self.nxt()
        self.assertEqual((n["phase"], n["gate"], n["kind_of_action"]), ("feedback", "feedback", "command"))
        self.assertIn("--refresh", n["command"])
        self.assertEqual([f.rsplit("/", 2)[-2:] for f in n["files"]], [["feedback", "web.raw.json"], ["feedback", "web.json"]])
        code, text = self.feedback_gate()
        self.assertEqual(code, 1)
        self.assertIn("web: the change was not read back after the ship gate", text)

    def test_feedback_is_refused_before_the_ship_gate_passed(self):
        self.to_ship()
        self.assertIn("feedback starts after the ship gate passed", self.fails("feedback", "--no-fetch", code=2))

    def test_a_clean_refresh_passes_the_gate_and_the_run_is_done(self):
        self.shipped()
        self.refresh(pipe=self.pipeline())
        self.assertEqual(self.nxt()["command"], "factory gate feedback")
        self.assertEqual(self.feedback_gate()[0], 0)
        n = self.nxt()
        self.assertEqual((n["phase"], n["kind_of_action"]), ("done", "none"))
        self.assertEqual(self.rounds(), [])

    def test_a_refresh_that_a_person_changed_does_not_unpass_the_ship_gate(self):
        self.shipped()
        self.refresh(draft=False, title="[ABC-1] - renamed by the reviewer", state="open")
        self.assertEqual(self.nxt()["phase"], "feedback")
        self.assertEqual(self.feedback_gate()[0], 0)

    def test_a_merged_change_needs_no_thread_fix_and_a_closed_one_fails(self):
        self.shipped()
        self.refresh(threads=[T1], state="merged")
        self.assertEqual(self.feedback_gate()[0], 0)
        self.refresh(threads=[T1], state="closed")
        self.assertIn("is closed", self.feedback_gate()[1])


class FindingTests(FeedbackCase):
    @lesson("L-20")
    def test_an_unresolved_thread_fails_the_gate_becomes_a_finding_and_sends_the_run_back_to_build(self):
        """L-20: unresolved threads are findings with source thread:<id> in a feedback review; a resolved thread is not."""
        self.shipped()
        self.refresh(threads=[T1, {**T1, "id": "T2", "resolved": True}], pipe=self.pipeline())
        code, text = self.feedback_gate()
        self.assertEqual(code, 1)
        self.assertIn("thread:T1 is unresolved (ana: Round the total before showing it)", text)
        self.assertNotIn("T2", text)
        n = self.nxt()
        self.assertEqual((n["phase"], n["command"]), ("feedback", "factory feedback --no-fetch"))
        out = self.ok("feedback", "--no-fetch")
        self.assertIn("feedback round 1: 1 finding(s)", out)
        rec = self.rounds()[0]
        self.assertEqual((rec["verdict"], rec["round"], rec["repos"]), ("CHANGES", 1, {"web": self.head()}))
        self.assertEqual([(f["source"], f["severity"], f["file"], f["repo"]) for f in rec["findings"]], [("thread:T1", "major", "app.py:1", "web")])
        self.assertEqual([(e["kind"], e["counts"], e["round"]) for e in self.loops()], [("feedback", True, 1)])
        n = self.nxt()
        self.assertEqual((n["phase"], n["role"], n["command"]), ("build", "builder", "factory verify"))
        self.assertIn("feedback round 1", n["action"])
        self.assertIn("thread:T1", n["action"])
        self.assertIn("no new unresolved thread", self.ok("feedback", "--no-fetch"))
        self.assertEqual(len(self.rounds()), 1, "the same source at the same commits is not a new finding")

    @lesson("L-155", "L-20")
    def test_a_failed_job_of_the_newest_pipeline_for_the_pushed_head_becomes_a_finding(self):
        """L-155, L-20: the pipeline must be for the pushed head; its failed jobs are findings (job:<pipeline>/<name>), an allowed failure is minor."""
        self.shipped()
        jobs = [{"name": "lint", "status": "failed", "allow_failure": False}, {"name": "e2e", "status": "failed", "allow_failure": True},
                {"name": "unit", "status": "success", "allow_failure": False}]
        self.refresh(pipe=self.pipeline("P7", "failed", jobs=jobs))
        self.assertIn("pipeline P7 is failed", self.feedback_gate()[1])
        self.ok("feedback", "--no-fetch")
        found = {f["source"]: f["severity"] for f in self.rounds()[0]["findings"]}
        self.assertEqual(found, {"job:P7/lint": "major", "job:P7/e2e": "minor"})
        self.assertEqual(len(self.loops()), 1)

    @lesson("L-155")
    def test_a_green_pipeline_for_another_commit_never_satisfies_the_feedback_gate(self):
        """L-155: the pipeline sha must equal the pushed head."""
        self.shipped()
        self.refresh(pipe=self.pipeline("P3", "success", sha="a" * 40))
        code, text = self.feedback_gate()
        self.assertEqual(code, 1)
        self.assertIn(f"the newest pipeline P3 ran for aaaaaaa, not the pushed head {self.head()[:7]}", text)

    @lesson("L-20")
    def test_a_waiver_names_one_pipeline_and_a_newer_failed_one_needs_its_own(self):
        """L-20: the user's waiver covers the red pipeline it names; a newer pipeline is not covered."""
        self.shipped()
        self.refresh(pipe=self.pipeline("P1", "failed"))
        self.assertEqual(self.feedback_gate()[0], 1)
        self.ok("waive", "pipeline:P1", "--user-said", "the lint job is known to be broken on main")
        self.assertEqual(self.feedback_gate()[0], 0)
        self.refresh(pipe=self.pipeline("P2", "failed", created="2026-11-24T12:00:00Z"))
        self.assertIn("pipeline P2 is failed", self.feedback_gate()[1])
        self.assertEqual(self.nxt()["phase"], "feedback")


class BudgetTests(FeedbackCase):
    @lesson("L-135", "L-136")
    def test_a_feedback_round_spends_the_single_loop_budget_and_a_decision_exempts_one(self):
        """L-135, L-136: feedback rounds share autonomy.max_loops; past it the command refuses and blocks the run until the user decides."""
        self.shipped("\n[autonomy]\nmax_loops = 1\n")
        Run(self.proj / ".factory" / "runs", "ABC-1").append({"type": "loop", "kind": "amend", "decision": None, "counts": True})
        self.refresh(threads=[T1], pipe=self.pipeline())
        self.assertIn("loop budget exhausted: 1 of 1", self.fails("feedback", "--no-fetch", code=1))
        self.assertEqual(self.rounds(), [])
        self.assertEqual(len([e for e in self.events(self.run_dir) if e["type"] == "blocker"]), 1)
        n = self.nxt()
        self.assertEqual((n["kind_of_action"], n["phase"]), ("user", "feedback"))
        self.ok("ask", "One more feedback round?")
        self.ok("answer", "Q-1", "--user-said", "yes, fix the thread")
        self.ok("feedback", "--no-fetch", "--decision", "Q-1")
        self.assertEqual([(e["kind"], e["counts"]) for e in self.loops()], [("amend", True), ("feedback", False)])
        self.assertEqual(json.loads(self.ok("metrics", "--json"))["loops"], {"amend": 1, "review": 0, "feedback": 0, "exempt": 1})
        self.assertEqual(json.loads(self.ok("metrics", "--json"))["escaped_findings"], 1)


class AdapterTests(FeedbackCase):
    def test_the_configured_adapter_refreshes_the_change_so_feedback_needs_no_ingest_or_confirmation(self):
        template = adapter_scenario(self, stdout="{}")
        self.shipped(provider_toml(template, "change")[len(self.TOML):])
        adapter_scenario(self, stdout=json.dumps({"contract": self.change_data(threads=[T1]), "raw": {"k": 2}}))
        n = self.nxt()
        self.assertEqual((n["phase"], n["command"]), ("feedback", "factory feedback"))
        out = self.ok("feedback")
        self.assertIn("refreshed change 42", out)
        self.assertEqual([f["source"] for f in self.rounds()[0]["findings"]], ["thread:T1"])
        contracts = [e for e in self.events(self.run_dir) if e["type"] == "contract" and e["kind"] == "change"]
        self.assertEqual((contracts[-1]["source"], contracts[-1]["refresh"]), ("adapter", True))


if __name__ == "__main__":
    unittest.main()
