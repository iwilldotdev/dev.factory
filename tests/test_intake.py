"""gate intake: ticket contract, run kind, media manifest, affected repos."""
import json
import unittest

from tests.helpers import FactoryCase, lesson

PNG = {"id": "att-1", "name": "screen.png", "mime": "image/png"}
MP4 = {"id": "att-2", "name": "demo.mp4", "mime": "Application/Octet-Stream"}
PDF = {"id": "att-3", "name": "spec.pdf", "mime": "application/pdf"}


class IntakeBase(FactoryCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.start("ABC-1")

    def gate(self):
        code, out, err = self.cli("gate", "intake")
        return code, out + err

    def media_file(self, name: str, text: str = "x") -> str:
        path = self.run_dir / "media" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return f"media/{name}"


class BasicTests(IntakeBase):
    def test_gate_passes_with_a_valid_contract_and_a_declared_repo(self):
        self.ingest_ticket(self.run_dir)
        self.assertIn("gate intake: pass", self.ok("gate", "intake"))
        gate = [e for e in self.events(self.run_dir) if e["type"] == "gate"][-1]
        self.assertEqual((gate["name"], gate["result"], gate["reasons"]), ("intake", "pass", []))

    def test_gate_fails_listing_every_reason_and_records_the_failure(self):
        self.write("factory.toml", self.TOML + '\n[[repo]]\npath = "../b"\nname = "b"\n')
        self.ok("run", "delete", "ABC-1", "--yes")
        self.ok("run", "start", "ABC-1")
        code, out = self.gate()
        self.assertEqual(code, 1)
        self.assertIn("ticket contract ABC-1 is missing", out)
        self.assertIn("no affected repo is declared", out)
        self.assertEqual([e["result"] for e in self.events(self.run_dir) if e["type"] == "gate"], ["fail"])

    def test_unknown_gates_are_refused_with_the_available_ones(self):
        self.assertIn("available: intake, plan, reproduce, verify, review, ship", self.fails("gate", "feedback"))

    @lesson("L-31")
    def test_a_bug_ticket_in_a_feature_run_fails_intake(self):
        """L-31: the run kind equals the ticket contract type; a bug-typed ticket in a feature run fails."""
        self.ok("run", "retype", "--kind", "feature")
        self.ingest_ticket(self.run_dir, type="bug")
        code, out = self.gate()
        self.assertEqual(code, 1)
        self.assertIn("run kind is feature but the ticket contract type is bug", out)
        self.ok("run", "retype", "--kind", "bug")
        self.assertEqual(self.gate()[0], 0)

    def test_hotfix_runs_take_bug_tickets_and_an_unset_kind_follows_the_contract(self):
        self.ingest_ticket(self.run_dir, type="bug")
        self.assertEqual(self.gate()[0], 0)
        self.assertEqual(json.loads(self.ok("status", "--json"))["kind"], "bug")
        self.ok("run", "retype", "--kind", "hotfix")
        self.assertEqual(self.gate()[0], 0)

    def test_the_gate_only_reads_and_never_writes_contracts_or_evidence(self):
        self.ingest_ticket(self.run_dir)
        before = sorted(str(p.relative_to(self.run_dir)) for p in self.run_dir.rglob("*") if p.is_file() and p.name != "events.jsonl")
        self.ok("gate", "intake")
        after = sorted(str(p.relative_to(self.run_dir)) for p in self.run_dir.rglob("*") if p.is_file() and p.name != "events.jsonl")
        self.assertEqual(before, after)

    def test_a_pass_is_invalidated_by_a_later_contract_change(self):
        self.ingest_ticket(self.run_dir)
        self.ok("gate", "intake")
        self.assertEqual(json.loads(self.ok("status", "--json"))["phase"], "plan")
        self.ingest_ticket(self.run_dir, title="Changed title")
        self.assertEqual(json.loads(self.ok("status", "--json"))["phase"], "intake")


class MediaTests(IntakeBase):
    def setUp(self):
        super().setUp()
        self.ingest_ticket(self.run_dir, attachments=[PNG, MP4, PDF])

    @lesson("L-28")
    def test_every_media_attachment_must_be_recorded(self):
        """L-28: the gate fails for each media attachment not recorded as seen or unseen; documents are not media."""
        code, out = self.gate()
        self.assertEqual(code, 1)
        self.assertIn("media att-1 (screen.png) is not recorded", out)
        self.assertIn("media att-2 (demo.mp4) is not recorded", out)
        self.assertNotIn("att-3", out)

    @lesson("L-28", "L-29")
    def test_seen_needs_one_existing_file_under_the_run_media_folder(self):
        """L-28, L-29: seen records one file under media/; each attachment id has its own entry."""
        a, b = self.media_file("a.png"), self.media_file("b.mp4")
        self.ok("media", "seen", "att-1", "--file", a)
        self.assertEqual(self.gate()[0], 1)
        self.ok("media", "seen", "att-2", "--file", b)
        self.assertEqual(self.gate()[0], 0)
        events = [e for e in self.events(self.run_dir) if e["type"] == "media"]
        self.assertEqual([(e["attachment"], e["state"], e["file"]) for e in events], [("att-1", "seen", a), ("att-2", "seen", b)])

    @lesson("L-29")
    def test_one_file_per_entry_and_files_must_exist_inside_media(self):
        """L-29: two attachments cannot share one file; a file outside media/ or a missing file is refused."""
        a = self.media_file("a.png")
        self.ok("media", "seen", "att-1", "--file", a)
        self.assertIn("one file per entry", self.fails("media", "seen", "att-2", "--file", a))
        self.assertIn("must be an existing file under", self.fails("media", "seen", "att-2", "--file", "media/missing.mp4"))
        outside = self.write("outside.png", "x", root=self.run_dir)
        self.assertIn("must be an existing file under", self.fails("media", "seen", "att-2", "--file", str(outside)))
        self.assertIn("not a media attachment", self.fails("media", "seen", "att-3", "--file", a))
        self.assertIn("not a media attachment", self.fails("media", "seen", "ghost", "--file", a))

    @lesson("L-29")
    def test_unseen_needs_a_question_whose_ref_names_the_attachment(self):
        """L-29, L-30: a question that merely mentions the file name does not count; the ref must be attachment:<id>."""
        self.ok("ask", "I cannot open demo.mp4 (att-2). Can you describe it?")
        self.assertIn("not a question about this attachment", self.fails("media", "unseen", "att-2", "--question", "Q-1"))
        self.ok("ask", "Please describe the video", "--ref", "attachment:att-2")
        self.assertIn("not a question about this attachment", self.fails("media", "unseen", "att-1", "--question", "Q-2"))
        self.ok("media", "unseen", "att-2", "--question", "Q-2")
        self.ok("media", "seen", "att-1", "--file", self.media_file("a.png"))
        self.assertEqual(self.gate()[0], 0, "an unseen entry with its question passes intake")

    def test_a_recorded_file_that_later_disappears_fails_the_gate(self):
        a = self.media_file("a.png")
        self.ok("media", "seen", "att-1", "--file", a)
        self.ok("media", "seen", "att-2", "--file", self.media_file("b.mp4"))
        (self.run_dir / "media" / "a.png").unlink()
        code, out = self.gate()
        self.assertEqual(code, 1)
        self.assertIn("recorded file media/a.png is missing", out)

    def test_the_manifest_is_the_events_not_a_file_named_manifest(self):
        """L-29: no manifest.json is ever read or written."""
        self.ok("media", "seen", "att-1", "--file", self.media_file("a.png"))
        self.assertFalse((self.run_dir / "media" / "manifest.json").exists())
        self.assertFalse((self.run_dir / "manifest.json").exists())


class RepoTests(FactoryCase):
    def test_workspace_runs_need_declared_repos_and_known_names(self):
        self.write("factory.toml", self.TOML + '\n[[repo]]\npath = "../b"\nname = "b"\n')
        run = self.start("ABC-1")
        self.ingest_ticket(run)
        self.assertIn("no affected repo is declared", self.fails("gate", "intake", code=1))
        self.ok("run", "repos", "web,b")
        self.ok("gate", "intake")
        self.assertIn("unknown repo ghost", self.fails("run", "repos", "ghost"))


if __name__ == "__main__":
    unittest.main()
