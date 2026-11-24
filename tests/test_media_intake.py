"""Intake gate: ticket images/videos must be downloaded and recorded, or raised as a question to the user."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from core_support import FactoryCase, cli, core  # noqa: F401

PNG = {"id": "11", "filename": "bug.png", "mimeType": "image/png", "content": "https://x/attachment/content/11"}
MP4 = {"id": "12", "filename": "screen 16-11-53", "mimeType": "video/mp4", "content": "https://x/attachment/content/12"}


class MediaIntakeTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.rid = self.make_run(branch=False)
        self.rd = core.run_dir(self.rid)

    def issue(self, *atts, fields=True):
        body = {"key": "DEV-1", "fields": {"attachment": list(atts)} if fields else {}}
        core.write_json(self.rd / "remote" / "issue-DEV-1.json", body)

    def manifest(self, *items):
        core.write_json(self.rd / "remote" / "media" / "manifest.json", {"items": list(items)})

    def gate(self):
        return cli("gate", self.rid, "intake")

    def test_old_pack_without_attachment_list_passes(self):
        self.issue(fields=False)
        self.assertEqual(self.gate()[0], 0)

    def test_text_attachment_is_ignored(self):
        self.issue({"id": "9", "filename": "log.txt", "mimeType": "text/plain"})
        self.assertEqual(self.gate()[0], 0)

    def test_image_not_downloaded_fails_with_fetch(self):
        self.issue(PNG)
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("bug.png", out)
        self.assertIn("jira_download_issue_attachment", out)
        self.assertIn("remote/media/11-bug.png", out)

    def test_image_downloaded_and_seen_passes(self):
        self.issue(PNG)
        (self.rd / "remote" / "media").mkdir(parents=True)
        (self.rd / "remote" / "media" / "bug.png").write_bytes(b"\x89PNG0")
        self.manifest({"name": "bug.png", "id": "11", "type": "image/png", "source": "u", "seen": True,
                       "file": "remote/media/bug.png"})
        self.assertEqual(self.gate()[0], 0)

    def test_same_filename_two_ids_need_two_entries(self):
        a = {"id": "21", "filename": "image.png", "mimeType": "image/png"}
        b = {"id": "22", "filename": "image.png", "mimeType": "image/png"}
        self.issue(a, b)
        (self.rd / "remote" / "media").mkdir(parents=True)
        (self.rd / "remote" / "media" / "21-image.png").write_bytes(b"x")
        self.manifest({"name": "image.png", "id": "21", "seen": True, "file": "remote/media/21-image.png"})
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertEqual(out.count("is not in remote/media/manifest.json"), 1)
        self.assertIn("22-image.png", out)
        from factory import media
        run = core.run_load(self.rid)
        self.assertEqual(len(media.fetches(self.rid, run)), 1)
        self.assertTrue(media.fetches(self.rid, run)[0]["args"]["save_to"].endswith("22-image.png"))
        self.assertNotEqual(media.attachments(self.rid, run)[0]["file"], media.attachments(self.rid, run)[1]["file"])

    def test_seen_without_file_fails(self):
        self.issue(PNG)
        self.manifest({"name": "bug.png", "seen": True, "file": "remote/media/bug.png"})
        rc, out = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("missing or empty", out)

    def test_seen_file_outside_media_dir_fails(self):
        self.issue(PNG)
        (self.rd / "remote" / "issue-DEV-1.json").stat()
        self.manifest({"name": "bug.png", "seen": True, "file": "remote/issue-DEV-1.json"})
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
            self.assertIn("remote/media/manifest.json", text)
            self.assertIn("seen: false", text) if text is author else self.assertIn('"seen": false', text)
        self.assertIn("jira_download_issue_attachment", phases)
        self.assertIn("attachment:<id>", phases)
        self.assertIn("attachment:<id>", author)
        self.assertIn("question", phases)


if __name__ == "__main__":
    unittest.main()
