"""Major 7: every evidence file prove writes has an `evidence` event with its sha256; gates compare."""

from __future__ import annotations

import unittest

from tests.integrity_support import IntegrityCase, cli, core


class EvidenceDigestTests(IntegrityCase):
    def test_prove_records_digest_event(self):
        self.to_prove()
        self.ok("prove", self.rid)
        ev = next(e for e in reversed(core.run_load(self.rid)["events"])
                  if e["kind"] == "evidence" and e["data"]["check"] == "CHK-1")
        self.assertEqual(ev["data"]["digest"], core.file_sha256(core.evidence_file(self.rid, "CHK-1")))

    def test_edited_evidence_fails_gate(self):
        self.to_prove([{"id": "CHK-1", "type": "test", "repo": "React", "cmd": "exit 3"}])
        cli("prove", self.rid)  # fails: exit 3
        path = core.evidence_file(self.rid, "CHK-1")
        rec = core.read_json(path)
        self.assertNotEqual(rec["status"], "pass")
        core.write_json(path, dict(rec, status="pass"))
        rc, out = self.gate("prove")
        self.assertEqual(rc, 1)
        self.assertIn("CHK-1: evidence file does not match the digest", out)

    def test_hand_written_evidence_fails_gate(self):
        self.to_prove()
        head = core.git_head(self.wt)
        core.write_json(core.evidence_file(self.rid, "CHK-1"), {
            "schemaVersion": 1, "check_id": "CHK-1", "type": "test", "status": "pass", "repo": "React",
            "git_sha": head, "dirty": False, "summary": "forged"})
        rc, out = self.gate("prove")
        self.assertEqual(rc, 1)
        self.assertIn("CHK-1: no evidence event in run.json", out)

    def test_one_byte_change_is_detected(self):
        from factory import integrity
        self.to_prove()
        self.ok("prove", self.rid)
        path = core.evidence_file(self.rid, "CHK-1")
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.assertIn("does not match", integrity.evidence_error(core.run_load(self.rid), "CHK-1"))


if __name__ == "__main__":
    unittest.main()
