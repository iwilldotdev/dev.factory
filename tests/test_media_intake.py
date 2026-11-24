"""Intake gate: ticket images/videos must be downloaded and recorded, or raised as a question to the user."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from core_support import FactoryCase, cli, core  # noqa: F401
from ticket_support import put_ticket

PNG = {"id": "11", "name": "bug.png", "mime": "image/png", "url": "https://acme.example/files/11"}
MP4 = {"id": "12", "name": "screen 16-11-53", "mime": "video/mp4", "url": "https://acme.example/files/12"}


class MediaIntakeTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.rid = self.make_run(branch=False)
        self.rd = core.run_dir(self.rid)

    def issue(self, *atts, fields=True):
        put_ticket(self.rid, "DEV-1", attachments=list(atts) if fields else None)

    def manifest(self, *items):
        core.write_json(self.rd / "raw" / "attachment" / "manifest.json", {"items": list(items)})

    def gate(self):
        return cli("gate", self.rid, "intake")

    def test_old_pack_without_attachment_list_passes(self):
        self.issue(fields=False)
        self.assertEqual(self.gate()[0], 0)

    def test_text_attachment_is_ignored(self):
        self.issue({"id": "9", "name": "log.txt", "mime": "text/plain", "url": "https://acme.example/files/9"})
        self.assertEqual(self.gate()[0], 0)

    def test_image_not_downloaded_fails_with_fetch(self):
        self.issue(PNG)
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("bug.png", out)
        self.assertIn("attachment 11 -> ", out)
        self.assertIn("https://acme.example/files/11", out)
        self.assertIn("raw/attachment/11-bug.png", out)

    def test_image_downloaded_and_seen_passes(self):
        self.issue(PNG)
        (self.rd / "raw" / "attachment").mkdir(parents=True)
        (self.rd / "raw" / "attachment" / "bug.png").write_bytes(b"\x89PNG0")
        self.manifest({"name": "bug.png", "id": "11", "type": "image/png", "source": "u", "seen": True,
                       "file": "raw/attachment/bug.png"})
        self.assertEqual(self.gate()[0], 0)

    def test_same_filename_two_ids_need_two_entries(self):
        a = {"id": "21", "name": "image.png", "mime": "image/png", "url": "u"}
        b = {"id": "22", "name": "image.png", "mime": "image/png", "url": "u"}
        self.issue(a, b)
        (self.rd / "raw" / "attachment").mkdir(parents=True)
        (self.rd / "raw" / "attachment" / "21-image.png").write_bytes(b"x")
        self.manifest({"name": "image.png", "id": "21", "seen": True, "file": "raw/attachment/21-image.png"})
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("is not in raw/attachment/manifest.json"), 1)
        self.assertIn("22-image.png", out)
        from factory import media
        run = core.run_load(self.rid)
        self.assertEqual(len(media.fetches(self.rid, run)), 1)
        self.assertTrue(media.fetches(self.rid, run)[0]["raw_path"].endswith("22-image.png"))
        self.assertNotEqual(media.attachments(self.rid, run)[0]["file"], media.attachments(self.rid, run)[1]["file"])

    def test_mime_case_and_generic_mime_by_extension(self):
        self.issue({"id": "31", "name": "a.png", "mime": "Image/PNG", "url": "u"},
                   {"id": "32", "name": "b.MOV", "mime": "application/octet-stream", "url": "u"},
                   {"id": "33", "name": "c.jpg", "mime": "", "url": "u"},
                   {"id": "34", "name": "notes.txt", "mime": "application/octet-stream", "url": "u"})
        from factory import media
        atts = media.attachments(self.rid, core.run_load(self.rid))
        self.assertEqual([a["id"] for a in atts], ["31", "32", "33"])
        self.assertEqual(self.gate()[0], 1)

    def test_manifest_cannot_be_an_entry_file(self):
        self.issue(PNG)
        (self.rd / "raw" / "attachment").mkdir(parents=True)
        self.manifest({"name": "bug.png", "id": "11", "seen": True, "file": "raw/attachment/manifest.json"})
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("missing or empty", out)

    def test_two_seen_entries_cannot_share_a_file(self):
        a = {"id": "21", "name": "a.png", "mime": "image/png", "url": "u"}
        b = {"id": "22", "name": "b.png", "mime": "image/png", "url": "u"}
        self.issue(a, b)
        (self.rd / "raw" / "attachment").mkdir(parents=True)
        (self.rd / "raw" / "attachment" / "x.png").write_bytes(b"x")
        self.manifest({"name": "a.png", "id": "21", "seen": True, "file": "raw/attachment/x.png"},
                      {"name": "b.png", "id": "22", "seen": True, "file": "raw/attachment/x.png"})
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("own downloaded file", out)

    def test_seen_without_file_fails(self):
        self.issue(PNG)
        self.manifest({"name": "bug.png", "seen": True, "file": "raw/attachment/bug.png"})
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("missing or empty", out)

    def test_seen_file_outside_media_dir_fails(self):
        self.issue(PNG)
        self.manifest({"name": "bug.png", "seen": True, "file": "contracts/ticket/DEV-1.json"})
        self.assertEqual(self.gate()[0], 1)

    def test_video_without_entry_fails_then_question_passes(self):
        self.issue(MP4)
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("question", out)
        self.manifest({"name": "screen 16-11-53", "id": "12", "type": "video/mp4", "seen": False})
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("needs a `question` blocker", out)
        cli("run", "block", self.rid, "question", "attachment:12 Video 'screen 16-11-53': send the file or describe the steps")
        cli("run", "unblock", self.rid, "0")  # answered; the record of the question stays
        self.assertEqual(self.gate()[0], 0)

    def test_unrelated_question_naming_the_file_does_not_pass(self):
        self.issue(MP4)
        self.manifest({"name": "screen 16-11-53", "id": "12", "seen": False})
        cli("run", "block", self.rid, "question", "does 'screen 16-11-53' mean we need video support?")
        cli("run", "unblock", self.rid, "0")
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("attachment:12", out)

    def test_open_question_still_blocks(self):
        self.issue(MP4)
        self.manifest({"name": "screen 16-11-53", "seen": False})
        cli("run", "block", self.rid, "question", "attachment:12 Video 'screen 16-11-53': send it")
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("open question blocker", out)


class MediaDocsTests(unittest.TestCase):
    def test_procedure_is_written(self):
        root = Path(__file__).resolve().parent.parent
        phases = (root / "claude/skills/factory/references/phases.md").read_text(encoding="utf-8")
        author = (root / "claude/agents/factory-author.md").read_text(encoding="utf-8")
        for text in (phases, author):
            self.assertIn("seen: false", text) if text is author else self.assertIn('"seen": false', text)
        self.assertIn("raw/attachment/manifest.json", phases)
        self.assertIn("attachment:<id>", phases)
        self.assertIn("attachment:<id>", author)
        self.assertIn("question", phases)


if __name__ == "__main__":
    unittest.main()
