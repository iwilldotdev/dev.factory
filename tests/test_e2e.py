"""End to end: a scripted harness that only runs what `factory next` prints, from `run start` through the ship gate and the feedback phase."""
import json
import shlex
import unittest
from pathlib import Path

from tests.helpers import adapter_scenario, lesson, provider_toml, valid_ticket
from tests.m3_support import ShipCase

DIAG = ("## Root cause\nVALUE is 1 in app.py.\n\n## Evidence\ncheck.py prints VALUE=1 on the base.\n\n"
        "## Ruled out\nThe check itself.\n\n## Scenario\nTicket scenario used.\n")


class Harness:
    """The orchestrator, the builder, the reviewer and the user of one ticket, each doing only what `next` asks of its role."""

    def __init__(self, case: ShipCase, kind: str, changes_round: bool, stop: str = "feedback"):
        self.case, self.kind, self.changes_round, self.stop = case, kind, changes_round, stop
        self.trail: list[str] = []
        self.committed = self.asked_changes = False
        self.refreshes = 0

    def review_file(self, which: str, verdict: str) -> Path:
        data = self.case.review_input(which, verdict)
        if verdict == "CHANGES":
            data["findings"] = [{"id": "F1", "severity": "major", "text": "the check proves nothing about the edge case", "ac": "AC-1"}]
        return self.case.write_review(data, f"{which}-{verdict}.json")

    def step(self) -> dict:
        c = self.case
        n = json.loads(c.ok("next", "--json"))
        self.trail.append(n["phase"])
        cmd, phase = n["command"] or "", n["phase"]
        argv = shlex.split(cmd)[1:] if cmd.startswith("factory ") else []
        if phase in ("done", self.stop):
            return n
        if n["role"] == "builder" and argv[:1] == ["verify"]:
            if not self.committed:
                c.fix()
                self.committed = True
            elif "asked for changes" in n["action"] or "feedback round" in n["action"]:
                c.commit("extra.txt", f"change {len(self.trail)}\n")
            c.ok("verify", "--actor", "builder-1")
        elif phase == "plan" and cmd == "factory gate plan" and not (c.run_dir / "plan.json").is_file():
            c.write_plan(c.run_dir, c.default_plan(self.kind))
            c.ok("gate", "plan")
        elif phase == "reproduce" and argv[:1] == ["reproduce"]:
            c.ok("reproduce", "--actor", "builder-1")
        elif phase == "reproduce" and cmd == "factory gate reproduce":
            (c.run_dir / "diagnosis.md").write_text(DIAG, encoding="utf-8")
            c.ok("gate", "reproduce")
        elif phase == "review" and argv[:2] == ["review", "record"]:
            which = argv[argv.index("--kind") + 1]
            verdict = "CHANGES" if self.changes_round and which == "qa" and not self.asked_changes else "APPROVE"
            self.asked_changes = self.asked_changes or verdict == "CHANGES"
            c.ok("review", "record", "--kind", which, "--file", str(self.review_file(which, verdict)), "--actor", "reviewer-1")
        elif phase == "ship" and argv[:2] == ["ingest", "change"]:
            self.execute_intents(n)
        elif phase == "feedback" and argv[:2] == ["ingest", "change"]:
            self.read_back(n)
        elif argv:
            c.ok(*argv)
        else:
            c.fail(f"the harness does not know what to do with: {n['action']}")
        return n

    def execute_intents(self, n: dict) -> None:
        """The orchestrator runs the intents with its own tools after the user approved them, then reads the change back."""
        c = self.case
        assert n["kind_of_action"] == "user", "autonomy.ship = ask hands every intent to the user first"
        assert all(i["needs_user"] for i in c.sp()["intents"])
        raw, contract = (Path(f) for f in n["files"][:2])
        data = c.change_data()
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps({"payload": data}), encoding="utf-8")
        contract.write_text(json.dumps(data), encoding="utf-8")
        c.ok(*shlex.split(n["command"])[1:])
        c.ok("confirm", "change:42", "--user-said", "yes, that is the draft change on the board")

    def read_back(self, n: dict) -> None:
        """Once the reviewers and the CI had their turn: round 1 has an unresolved thread and a red pipeline, round 2 is clean and green."""
        c, first = self.case, self.refreshes == 0
        self.refreshes += 1
        head = c.sp()["heads"]["web"]
        pipe = {"id": f"P{self.refreshes}", "sha": head, "status": "failed" if first else "success", "created_at": f"2026-11-24T1{self.refreshes}:00:00Z",
                "jobs": [{"name": "lint", "status": "failed" if first else "success", "allow_failure": False}]}
        threads = [{"id": "T1", "author": "ana", "resolved": not first, "body_md": "Round the total before showing it", "file": "app.py", "line": 1}]
        data = c.change_data(threads=threads, pipelines=[pipe["id"]])
        for name, body in (("change", data), ("pipeline", pipe)):
            raw = c.run_dir / "feedback" / f"web-{name}.raw.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text(json.dumps({"payload": body}), encoding="utf-8")
            c.ok("ingest", name, "--raw", str(raw), "--stdin", "--refresh", stdin=json.dumps(body))
        c.ok("confirm", "change:42", "--user-said", "yes, that is the change as the board shows it")
        c.ok("confirm", f"pipeline:{pipe['id']}", "--user-said", "yes, that is the pipeline of the change")

    def run(self, limit: int = 80) -> list[str]:
        for _ in range(limit):
            if self.step()["phase"] in ("done", self.stop):
                return self.trail
        raise AssertionError(f"no end after {limit} steps: {self.trail}")


class E2E(ShipCase):
    def start_run(self, kind: str) -> None:
        out = json.dumps({"contract": valid_ticket(type=kind), "raw": {"k": 1}})
        self.write("factory.toml", provider_toml(adapter_scenario(self, stdout=out)))
        self.ok("run", "start", "ABC-1")
        self.run_dir = self.proj / ".factory" / "runs" / "ABC-1"

    def finish(self, kind: str, changes_round: bool, stop: str = "feedback"):
        trail = Harness(self, kind, changes_round, stop).run()
        events = self.events(self.run_dir)
        order = [p for i, p in enumerate(trail) if i == 0 or p != trail[i - 1]]
        return order, events, [e["name"] for e in events if e["type"] == "gate" and e["result"] == "pass"]


class FeatureTests(E2E):
    @lesson("L-26")
    def test_a_feature_runs_from_start_to_the_ship_gate_through_next_alone(self):
        """L-26: a harness that only follows `next` takes a feature through intake, plan, build, review and ship, with one CHANGES round."""
        self.start_run("feature")
        order, events, gates = self.finish("feature", changes_round=True)
        self.assertEqual(order, ["intake", "plan", "build", "review", "ship", "feedback"])
        self.assertEqual(gates, ["intake", "plan", "verify", "review", "ship"])
        self.assertEqual([(e["kind"], e["counts"]) for e in events if e["type"] == "loop"], [("review", True)])
        self.assertEqual([(e["kind"], e["round"], e["verdict"]) for e in events if e["type"] == "review"],
                         [("code", 1, "APPROVE"), ("qa", 1, "CHANGES"), ("code", 2, "APPROVE"), ("qa", 2, "APPROVE")])
        shipped = [e for e in events if e["type"] == "ship_verified"]
        self.assertEqual([(e["result"], e["mismatches"]) for e in shipped], [("pass", [])])
        self.assertEqual(self.head(), shipped[-1]["heads"]["web"])
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual((n["phase"], n["kind_of_action"]), ("feedback", "command"))
        self.assertIn("phase feedback", self.ok("status"))
        m = json.loads(self.ok("metrics", "--json"))
        self.assertEqual(m["loops"], {"amend": 0, "review": 1, "feedback": 0, "exempt": 0})
        self.assertTrue({"build", "review", "ship"} <= set(m["phases_s"]))

    def test_a_run_without_a_changes_round_takes_the_shortest_path(self):
        self.start_run("feature")
        order, events, _ = self.finish("feature", changes_round=False)
        self.assertEqual(order, ["intake", "plan", "build", "review", "ship", "feedback"])
        self.assertEqual([e for e in events if e["type"] == "loop"], [])

    @lesson("L-26", "L-20", "L-155")
    def test_the_feedback_round_returns_to_build_and_ends_with_a_clean_thread_list_and_a_green_pipeline(self):
        """L-26, L-20, L-155: an unresolved thread and a red pipeline become findings, the update goes through build, review and ship again,
        and the run ends only after a refresh shows no unresolved thread and a green pipeline for the pushed head."""
        self.start_run("feature")
        order, events, gates = self.finish("feature", changes_round=False, stop="done")
        self.assertEqual(order, ["intake", "plan", "build", "review", "ship", "feedback", "build", "review", "ship", "feedback", "done"])
        self.assertEqual(gates, ["intake", "plan", "verify", "review", "ship", "review", "ship", "feedback"])
        rounds = [e for e in events if e["type"] == "review" and e["kind"] == "feedback"]
        self.assertEqual([(e["round"], e["verdict"], sorted(f["source"] for f in e["findings"])) for e in rounds],
                         [(1, "CHANGES", ["job:P1/lint", "thread:T1"])])
        self.assertEqual([(e["kind"], e["counts"]) for e in events if e["type"] == "loop"], [("feedback", True)])
        updates = [e for e in events if e["type"] == "ship_planned"]
        self.assertEqual([e["update"] for e in updates], [False, True])
        self.assertEqual(self.sp()["intents"][1]["action"], "update")
        self.assertEqual(json.loads(self.ok("next", "--json"))["kind_of_action"], "none")
        self.assertEqual(json.loads(self.ok("metrics", "--json"))["loops"]["feedback"], 1)


class BugTests(E2E):
    def test_a_bug_runs_through_reproduce_red_green_review_and_ship(self):
        self.start_run("bug")
        order, events, gates = self.finish("bug", changes_round=False)
        self.assertEqual(order, ["intake", "plan", "reproduce", "build", "review", "ship", "feedback"])
        self.assertEqual(gates, ["intake", "plan", "reproduce", "verify", "review", "ship"])
        self.assertEqual({e["mode"] for e in events if e["type"] == "evidence"}, {"reproduce", "verify", "red", "green"})
        body = (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")
        self.assertNotIn("CHK-1", body)  # the change speaks to its reviewer; the AC x evidence table stays in the run
        self.assertEqual([e["result"] for e in events if e["type"] == "ship_verified"], ["pass"])


if __name__ == "__main__":
    unittest.main()
