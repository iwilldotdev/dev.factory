"""factory next: the one required action, with absolute paths and the reasons a gate would fail."""
import json
import os
import re
import shlex
import unittest

from tests.helpers import FactoryCase, adapter_scenario, lesson, provider_toml, valid_ticket
from dev_factory.run import Run

ATTS = [{"id": "att-1", "name": "screen.png", "mime": "image/png"}, {"id": "att-2", "name": "demo.mp4", "mime": "video/mp4"}]


class NextTests(FactoryCase):
    def nxt(self, *extra, **kw):
        return json.loads(self.ok("next", "--json", *extra, **kw))

    @lesson("L-158")
    def test_files_are_absolute_forward_slash_paths_and_nothing_is_relayed_through_chat(self):
        """L-158: every file `next` expects is absolute with forward slashes; the command takes files, not pasted payloads."""
        run = self.start("ABC-1")
        n = self.nxt()
        self.assertEqual((n["phase"], n["kind_of_action"], n["role"]), ("intake", "command", "any"))
        self.assertEqual(n["files"], [(run / "raw" / "ticket" / "ABC-1.json").as_posix(), (run / "input" / "ticket-ABC-1.json").as_posix()])
        for f in n["files"]:
            self.assertTrue(os.path.isabs(f) and "\\" not in f, f)
        self.assertIn(f'--raw "{n["files"][0]}"', n["command"])
        self.assertIn(f'--contract "{n["files"][1]}"', n["command"])
        self.assertNotRegex(n["command"], r"<[a-z-]+\.json>")
        text = self.ok("next")
        self.assertIn(f"expects file: {n['files'][0]}", text)
        self.assertIn("ticket contract ABC-1 is missing", text)

    def test_with_an_adapter_the_action_is_fetch(self):
        self.write("factory.toml", provider_toml(adapter_scenario(self, stdout="{}")))
        self.start("ABC-1")
        self.assertEqual(self.nxt()["command"], "factory fetch ticket ABC-1")

    def test_project_flag_is_added_only_when_the_cwd_is_outside_the_project(self):
        self.start("ABC-1")
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertNotIn("--project", self.nxt("--project", str(self.proj), cwd=self.proj, project=False)["command"])
        self.assertIn(f'--project "{self.proj.as_posix()}"', self.nxt("--project", str(self.proj), cwd=elsewhere, project=False)["command"])

    def test_reasons_media_and_gate_steps(self):
        run = self.start("ABC-1")
        self.ingest_ticket(run, attachments=ATTS)
        n = self.nxt()
        self.assertIn("media seen <attachment-id>", n["command"])
        self.assertEqual(len(n["reasons"]), 2)
        self.assertTrue(all("not recorded" in r for r in n["reasons"]))
        for att, name in (("att-1", "a.png"), ("att-2", "b.mp4")):
            (run / "media").mkdir(exist_ok=True)
            (run / "media" / name).write_text("x")
            self.ok("media", "seen", att, "--file", f"media/{name}")
        n = self.nxt()
        self.assertEqual(n["command"], "factory gate intake")
        self.assertEqual(n["reasons"], [])
        self.ok("gate", "intake")
        n = self.nxt()
        self.assertEqual((n["phase"], n["kind_of_action"]), ("plan", "none"))

    def test_declare_repos_before_the_gate_in_a_workspace(self):
        self.write("factory.toml", self.TOML + '\n[[repo]]\npath = "../b"\nname = "b"\n')
        run = self.start("ABC-1")
        self.ingest_ticket(run)
        self.assertIn("factory run repos", self.nxt()["command"])

    def test_open_questions_come_before_everything_and_name_the_answer_command(self):
        self.start("ABC-1")
        self.ok("ask", "Which environment?")
        n = self.nxt()
        self.assertEqual((n["kind_of_action"], n["role"]), ("user", "orchestrator"))
        self.assertIn("Q-1", n["action"])
        self.assertIn("Which environment?", n["action"])
        self.assertTrue(n["command"].startswith('factory answer Q-1 --user-said "'))
        self.ok("answer", "Q-1", "--user-said", "staging")
        self.assertNotEqual(self.nxt()["kind_of_action"], "user")
        self.assertEqual(self.nxt()["decisions"], ["Q-1"])

    @lesson("L-168")
    def test_two_identical_env_failures_block_the_run_and_stop_suggesting_the_command(self):
        """L-168: after two identical env-kind failures `next` reports the run blocked and refuses the same command."""
        run = self.start("ABC-1")
        r = Run(run.parent, "ABC-1")
        fail = {"type": "env", "repo": "web", "action": "fail", "kind": "health", "reason": "no answer on :5173"}
        r.append(fail)
        self.assertEqual(self.nxt()["refused"], [])
        r.append(dict(fail, kind="start", reason="other"))
        self.assertEqual(self.nxt()["refused"], [])
        r.append(dict(fail, kind="start", reason="other"))
        n = self.nxt()
        self.assertEqual(n["refused"], ["factory env up"])
        self.assertIn("blocked: the same environment failure happened 2 times", n["action"])
        self.assertEqual(n["kind_of_action"], "user")
        self.assertIn("do not run: factory env up", self.ok("next"))
        r.append({"type": "env", "repo": "web", "action": "up", "pid": 1})
        self.assertEqual(self.nxt()["refused"], [])

    def test_a_closed_run_has_no_action(self):
        self.start("ABC-1")
        self.ok("run", "close", "--reason", "abandoned")
        n = self.nxt("ABC-1")
        self.assertEqual((n["phase"], n["kind_of_action"]), ("closed", "none"))


class FlowTests(FactoryCase):
    """A scripted harness that only ever runs what `factory next` prints."""

    def drive(self, answers=None, max_steps=20):
        trail = []
        for _ in range(max_steps):
            n = json.loads(self.ok("next", "--json"))
            trail.append(n["command"] or n["action"])
            if n["phase"] != "intake":
                return n, trail
            cmd = n["command"]
            if n["kind_of_action"] == "user":
                qid = re.match(r"factory answer (Q-\d+)", cmd).group(1)
                self.ok("answer", qid, "--user-said", (answers or {}).get(qid, "go ahead"))
            elif cmd.startswith("factory fetch"):
                self.ok(*shlex.split(cmd)[1:])
            elif cmd.startswith("factory media seen"):
                run = self.proj / ".factory/runs/ABC-1"
                ticket = json.loads((run / "contracts/ticket/ABC-1.json").read_text())["data"]
                done = {e["attachment"] for e in self.events(run) if e["type"] == "media"}
                for a in ticket["attachments"]:
                    if a["id"] not in done:
                        (run / "media").mkdir(exist_ok=True)
                        (run / "media" / a["name"]).write_text("x")
                        self.ok("media", "seen", a["id"], "--file", f"media/{a['name']}")
            elif cmd.startswith("factory gate"):
                self.ok(*shlex.split(cmd)[1:])
            else:
                self.fail(f"driver does not know how to run: {cmd}")
        self.fail(f"no end after {max_steps} steps: {trail}")

    @lesson("L-26")
    def test_intake_is_driven_to_the_end_through_next_alone(self):
        """L-26 (intake part): a harness that only follows `next` takes a run from start to the end of intake via the adapter."""
        out = json.dumps({"contract": valid_ticket(attachments=ATTS), "raw": {"k": 1}})
        self.write("factory.toml", provider_toml(adapter_scenario(self, stdout=out)))
        self.ok("run", "start", "ABC-1")
        n, trail = self.drive()
        self.assertEqual(n["phase"], "plan")
        self.assertEqual(trail[0], "factory fetch ticket ABC-1")
        self.assertEqual(trail[-2], "factory gate intake")
        gates = [e for e in self.events(self.proj / ".factory/runs/ABC-1") if e["type"] == "gate"]
        self.assertEqual([g["result"] for g in gates], ["pass"])

    def test_the_agent_route_and_a_user_question_in_the_middle(self):
        self.ok("run", "start", "ABC-1")
        run = self.proj / ".factory/runs/ABC-1"
        self.ok("ask", "Is the staging data fine?")
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual(n["kind_of_action"], "user")
        self.ok("answer", "Q-1", "--user-said", "yes")
        n = json.loads(self.ok("next", "--json"))
        raw, data = n["files"]
        os.makedirs(os.path.dirname(raw), exist_ok=True)
        os.makedirs(os.path.dirname(data), exist_ok=True)
        open(raw, "w").write("{}")
        open(data, "w").write(json.dumps(valid_ticket()))
        self.ok(*shlex.split(n["command"])[1:])
        n = json.loads(self.ok("next", "--json"))
        self.assertEqual(n["command"], "factory gate intake")
        self.ok("gate", "intake")
        self.assertEqual(json.loads(self.ok("next", "--json"))["phase"], "plan")
        self.assertTrue((run / "contracts" / "ticket" / "ABC-1.json").is_file())


if __name__ == "__main__":
    unittest.main()
