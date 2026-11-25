"""Bug runs: reproduce on the base, the diagnosis gate, red-green at the fix commit, and flows driven only through `next`."""
import json
import shlex
import unittest

from tests.helpers import git, lesson
from tests.m2_support import M2Case, cmd

DIAG = ("## Root cause\nVALUE is 1 in app.py.\n\n## Evidence\ncheck.py prints VALUE=1 on the base.\n\n"
        "## Ruled out\nThe check itself: it passes once VALUE is 2.\n\n## Scenario\nTicket scenario used.\n")
STRICT_CHECK = '''import sys
import app
want = int(sys.argv[1])
if app.VALUE != want:
    print(f"AssertionError: VALUE={app.VALUE} expected {want} (strict)")
    sys.exit(1)
print(f"checks passed: VALUE={app.VALUE} (strict)")
'''


class BugCase(M2Case):
    def setUp(self):
        super().setUp()
        self.run_dir = self.prepare("bug")

    def diagnosis(self, text=DIAG):
        (self.run_dir / "diagnosis.md").write_text(text, encoding="utf-8")

    def reproduce(self, *flags, code=None):
        got, out, err = self.cli("reproduce", "--json", *flags)
        if code is not None:
            self.assertEqual(got, code, out + err)
        return json.loads(out)

    def throwaways(self):
        return [line for line in git("worktree", "list", cwd=self.proj).splitlines() if "_throwaway" in line]


class NoReproductionTests(unittest.TestCase):
    def test_a_bug_plan_with_every_ac_reproduce_false_needs_the_users_recorded_decision(self):
        """Review M1 / L-33: nothing reproduced is not a reproduction."""
        from dev_factory import gates
        acs = [{"id": "AC-1", "reproduce": False, "reason": "copy"}]
        st = {"decisions": {}}
        self.assertIn("nothing is reproduced", gates.no_reproduction({"kind": "bug", "acs": acs}, st)[0])
        self.assertEqual(gates.no_reproduction({"kind": "bug", "acs": acs + [{"id": "AC-2"}]}, st), [])
        self.assertEqual(gates.no_reproduction({"kind": "feature", "acs": acs}, st), [])
        said = [{**acs[0], "reason": "cannot reproduce, user decided in D-1"}]
        self.assertEqual(gates.no_reproduction({"kind": "bug", "acs": said}, {"decisions": {"D-1": {}}}), [])
        self.assertTrue(gates.no_reproduction({"kind": "bug", "acs": said}, st))


class ReproduceTests(BugCase):
    @lesson("L-33")
    def test_every_bug_ac_must_fail_on_the_base_with_its_expected_pattern(self):
        """L-33: `reproduce` runs the check on a throwaway worktree at the branch point; gate reproduce needs the reproduced record."""
        self.diagnosis()
        self.assertIn("CHK-1: no reproduced record at the base", self.failing("reproduce"))
        out = self.reproduce(code=0)
        self.assertEqual([(r["check"], r["result"]) for r in out["results"]], [("CHK-1", "reproduced")])
        (rec,) = self.evidence(self.run_dir, mode="reproduce")
        bp = [e for e in self.events(self.run_dir) if e["type"] == "plan_locked"][0]["branch_points"]["web"]
        self.assertEqual((rec["sha"], rec["result"], rec["clean"]), (bp, "reproduced", True))
        self.assertIn("_throwaway", rec["worktree"])
        self.assertNotEqual(rec["worktree"], self.tree().as_posix())
        self.assertEqual(self.throwaways(), [], "the throwaway worktree is removed afterwards")
        self.assertEqual(self.gate("reproduce")[0], 0)

    def test_the_run_worktree_is_never_touched_by_reproduce(self):
        self.reproduce(code=0)
        self.assertEqual(git("status", "--porcelain", cwd=self.tree()), "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.tree()), [e for e in self.events(self.run_dir) if e["type"] == "plan_locked"][0]["branch_points"]["web"])

    def test_a_second_reproduce_is_reused_and_fresh_runs_again(self):
        self.reproduce(code=0)
        again = self.reproduce(code=0)
        self.assertTrue(again["results"][0]["reused"])
        self.assertEqual(len(self.evidence(self.run_dir, mode="reproduce")), 1)
        self.reproduce("--fresh", code=0)
        self.assertEqual(len(self.evidence(self.run_dir, mode="reproduce")), 2)

    @lesson("L-05")
    def test_a_failure_without_the_expected_text_is_not_reproduced_and_fails_the_gate(self):
        """L-05: a different failure (or none) never counts as the bug reproducing."""
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["reproduce"] = {"expect_fail_pattern": "AssertionError: VALUE=9 expected"}
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "pattern")
        self.diagnosis()
        out = self.reproduce(code=1)
        self.assertEqual(out["results"][0]["result"], "not_reproduced")
        self.assertIn("does not match expect_fail_pattern", out["results"][0]["reason"])
        self.assertIn("CHK-1: no reproduced record", self.failing("reproduce"))

    @lesson("L-03")
    def test_a_setup_failure_is_never_a_reproduction(self):
        """L-03: a missing command on the base is `setup`, whatever the expected pattern says."""
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["run"] = "factory-test-no-such-command"
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "command")
        out = self.reproduce(code=1)
        self.assertEqual((out["results"][0]["result"], out["results"][0]["kind"]), ("setup", "setup"))
        self.diagnosis()
        self.assertIn("no reproduced record", self.failing("reproduce"))

    @lesson("L-06")
    def test_a_failing_setup_in_the_throwaway_worktree_blocks_the_reproduction(self):
        """L-06: when the base worktree cannot be set up, the check is not run and nothing is reproduced."""
        probe = cmd("import os, sys; sys.exit(5 if '_throwaway' in os.getcwd() else 0)")
        self.write("factory.toml", self.TOML.replace('name = "web"\n', f'name = "web"\nsetup = {json.dumps(probe)}\n'))
        self.ok("plan", "amend", "--reason", "setup command")
        out = self.reproduce(code=1)
        self.assertEqual(out["results"][0]["result"], "setup")
        self.assertIn("worktree setup failed (exit 5)", out["results"][0]["reason"])
        self.assertEqual(self.evidence(self.run_dir, mode="reproduce")[0]["exit"], 5)

class FeatureTests(M2Case):
    def test_reproduce_is_for_bug_runs(self):
        self.prepare("feature")
        self.assertIn("reproduce is for bug runs", self.fails("reproduce"))
        self.assertIn("gate reproduce is for bug runs", self.failing("reproduce"))


class DiagnosisTests(BugCase):
    def test_every_empty_section_is_named_at_once(self):
        self.assertIn("diagnosis.md is missing", self.failing("reproduce"))
        self.diagnosis("## Root cause\n\n## Evidence\nx\n\n## Scenario\nticket scenario used\n")
        text = self.failing("reproduce")
        self.assertIn("## Root cause is missing or empty", text)
        self.assertIn("## Ruled out is missing or empty", text)
        self.assertNotIn("## Evidence is missing", text)

    @lesson("L-38")
    def test_the_three_sections_are_required(self):
        """L-38: non-empty Root cause, Evidence and Ruled out are what stop fixes that start from a guess."""
        self.reproduce(code=0)
        for section in ("Root cause", "Evidence", "Ruled out"):
            body = DIAG.split(f"## {section}\n")
            rest = body[1].split("\n\n", 1)[1]
            self.diagnosis(body[0] + f"## {section}\n\n" + rest)
            self.assertIn(f"## {section} is missing or empty", self.failing("reproduce"))
        self.diagnosis()
        self.assertEqual(self.gate("reproduce")[0], 0)

    @lesson("L-27")
    def test_the_scenario_says_ticket_scenario_used_or_lists_each_deviation_with_its_reason(self):
        """L-27: a reproduction that left the ticket's scenario must say how and why."""
        self.reproduce(code=0)
        for scenario, ok in (("Ticket scenario used.", True), ("- used admin instead of a viewer: the viewer cannot open the page\n", True),
                             ("- used admin instead of a viewer\n", False), ("The scenario was close enough.\n", False), ("", False)):
            self.diagnosis(DIAG.replace("Ticket scenario used.\n", scenario))
            code, reasons = self.gate("reproduce")
            self.assertEqual(code == 0, ok, (scenario, reasons))
            if not ok:
                self.assertIn("## Scenario must say 'ticket scenario used'", "\n".join(reasons))

    def test_a_diagnosis_edit_after_the_gate_is_drift_until_the_gate_passes_again(self):
        self.reproduce(code=0)
        self.diagnosis()
        self.assertEqual(self.gate("reproduce")[0], 0)
        self.diagnosis(DIAG + "\nAnother thought.\n")
        self.assertIn("diagnosis.md changed after gate reproduce passed", self.failing("verify"))
        self.assertEqual(self.gate("reproduce")[0], 0)
        self.assertNotIn("diagnosis.md changed", "\n".join(self.gate("verify")[1]))


class RedGreenTests(BugCase):
    def ready(self):
        self.reproduce(code=0)
        self.diagnosis()
        self.ok("gate", "reproduce")

    @lesson("L-34")
    def test_red_fails_before_the_fix_and_green_passes_at_it(self):
        """L-34: red runs at fix~1 and must be reproduced; green runs at the fix and must pass; the gate needs both."""
        self.ready()
        fix = self.fix()
        out = json.loads(self.cli("verify", "--json")[1])
        self.assertEqual([(r["check"], r["mode"], r["result"]) for r in out["results"]],
                         [("CHK-1", "verify", "pass"), ("CHK-1", "red", "reproduced"), ("CHK-1", "green", "pass")])
        red, green = self.evidence(self.run_dir, mode="red")[0], self.evidence(self.run_dir, mode="green")[0]
        self.assertEqual((red["fix"], green["fix"], green["sha"]), (fix, fix, fix))
        self.assertEqual(red["sha"], git("rev-parse", f"{fix}^", cwd=self.tree()))
        self.assertEqual(self.gate("verify"), (0, []))
        self.assertEqual(self.throwaways(), [])
        again = json.loads(self.cli("verify", "--json")[1])
        self.assertTrue(all(r["reused"] for r in again["results"]))
        self.assertEqual(len(self.evidence(self.run_dir)), 4)

    @lesson("L-34")
    def test_a_fix_commit_whose_test_already_passed_before_it_is_not_red_green(self):
        """L-34: when the check passes at fix~1 the test proves nothing; the gate refuses and says why."""
        self.ready()
        self.fix()
        self.commit("notes.txt", "later\n", "docs: later")
        out = json.loads(self.cli("verify", "--json")[1])
        self.assertEqual([r["mode"] + ":" + r["result"] for r in out["results"] if r["mode"] != "verify"], ["red:not_reproduced"])
        self.assertIn("red-green not proven at fix", self.failing("verify"))
        plan = self.read_plan(self.run_dir)
        plan["fix_commit"] = "HEAD~1"
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "the fix is the previous commit")
        self.cli("verify", "--json")
        self.assertEqual(self.gate("verify"), (0, []))

    @lesson("L-35")
    def test_the_red_side_takes_its_test_files_from_git_at_the_fix_commit(self):
        """L-35: the test file of the red run is the one committed with the fix, not whatever sits in the base."""
        self.ready()
        fix = self.commit_many({"app.py": "VALUE = 2\n", "check.py": STRICT_CHECK}, "fix: value with a strict test")
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["reproduce"] = {"expect_fail_pattern": r"expected 2 \(strict\)"}
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "the strict test came with the fix")
        self.assertEqual(self.reproduce("--fresh", code=1)["results"][0]["result"], "not_reproduced", "the base test has no strict marker")
        self.cli("verify", "--json")
        red = self.evidence(self.run_dir, mode="red")[-1]
        self.assertEqual((red["result"], red["fix"]), ("reproduced", fix))
        self.assertIn("(strict)", red["tail"])
        self.assertEqual(self.evidence(self.run_dir, mode="green")[-1]["result"], "pass")

    def test_a_test_file_missing_from_the_fix_commit_is_setup(self):
        self.ready()
        self.fix()
        plan = self.read_plan(self.run_dir)
        plan["checks"][0]["red_green"] = {"test_files": ["tests/missing_test.py"]}
        self.write_plan(self.run_dir, plan)
        self.ok("plan", "amend", "--reason", "wrong path")
        self.cli("verify", "--json")
        red = self.evidence(self.run_dir, mode="red")[0]
        self.assertEqual((red["result"], red["kind"]), ("setup", "setup"))
        self.assertIn("test file missing from the fix commit", red["reason"])
        self.assertEqual(self.evidence(self.run_dir, mode="green"), [])


class FlowTests(M2Case):
    """A scripted harness that only runs what `factory next` prints, doing the agent's file work when it is asked for."""

    def drive(self, kind: str):
        run = self.start("ABC-1")
        trail = []
        for _ in range(40):
            n = json.loads(self.ok("next", "--json"))
            trail.append((n["phase"], n["command"]))
            ph, command = n["phase"], n["command"] or ""
            if ph == "review":
                return n, trail
            if command.startswith("factory ingest"):
                raw, data = n["files"]
                self.ingest_ticket(run, id="ABC-1", type=kind)
            elif command == "factory run branch" or (command.startswith("factory gate") and ph in ("intake", "build")):
                self.ok(*shlex.split(command)[1:])
            elif ph == "plan":
                if not (run / "plan.json").exists():
                    self.write_plan(run, self.default_plan(kind))
                self.ok(*shlex.split(command)[1:])
            elif ph == "reproduce":
                if command == "factory reproduce":
                    self.ok("reproduce")
                else:
                    (run / "diagnosis.md").write_text(DIAG, encoding="utf-8")
                    self.ok(*shlex.split(command)[1:])
            elif ph == "build":
                self.fix()
                self.ok("verify")
            else:
                self.fail(f"driver does not know this step: {n}")
        self.fail(f"no end after 40 steps: {trail}")

    @lesson("L-26")
    def test_a_feature_run_goes_from_intake_to_a_verified_build_through_next_alone(self):
        """L-26: following only `next`, a feature run reaches review with every gate recorded and a locked plan."""
        n, trail = self.drive("feature")
        phases = [p for p, _ in trail]
        self.assertEqual(phases[0], "intake")
        self.assertEqual([p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p], ["intake", "plan", "build", "review"])
        gates = {e["name"] for e in self.events(self.proj / ".factory/runs/ABC-1") if e["type"] == "gate" and e["result"] == "pass"}
        self.assertEqual(gates, {"intake", "plan", "verify"})

    @lesson("L-26")
    def test_a_bug_run_adds_the_reproduce_phase_and_red_green(self):
        """L-26: a bug run also reproduces on the base and proves red-green, all through `next`."""
        n, trail = self.drive("bug")
        phases = [p for p, _ in trail]
        self.assertEqual([p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p], ["intake", "plan", "reproduce", "build", "review"])
        events = self.events(self.proj / ".factory/runs/ABC-1")
        self.assertEqual({e["name"] for e in events if e["type"] == "gate" and e["result"] == "pass"}, {"intake", "plan", "reproduce", "verify"})
        self.assertEqual({e["mode"] for e in events if e["type"] == "evidence"}, {"reproduce", "verify", "red", "green"})


if __name__ == "__main__":
    unittest.main()
