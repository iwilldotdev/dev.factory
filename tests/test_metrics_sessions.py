"""Metrics find transcripts only through registered sessions, in the profiles the run or the environment names."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from core_support import FactoryCase, cli, core  # noqa: F401
from factory.commands import metrics


def rec(text):
    return json.dumps({"type": "user", "timestamp": "2026-02-01T00:00:00Z", "message": {"role": "user", "content": text}}) + "\n"


class TranscriptLookupTests(FactoryCase):
    def profile(self, name, sid=None, marker=None):
        proj = self.tmp / name / "projects" / "p"
        proj.mkdir(parents=True)
        if sid:
            (proj / f"{sid}.jsonl").write_text(rec("hi"), encoding="utf-8")
        if marker:
            (proj / "marked.jsonl").write_text(rec(f"factory-run: {marker}"), encoding="utf-8")
        return self.tmp / name

    def env(self, **kv):
        base = {"FACTORY_TRANSCRIPT_ROOTS": "", "CLAUDE_CONFIG_DIR": "", "HOME": str(self.tmp / "nohome"),
                "USERPROFILE": str(self.tmp / "nohome")}
        p = mock.patch.dict(os.environ, {**base, **kv})
        p.start()
        self.addCleanup(p.stop)

    def test_no_session_finds_nothing_even_with_a_marker(self):
        prof = self.profile("prof", marker="ws-dev-1")
        self.env(CLAUDE_CONFIG_DIR=str(prof))
        run = {"id": "ws-dev-1", "created": "2026-01-01T00:00:00+00:00", "sessions": []}
        self.assertEqual(metrics.find_transcripts(run), [])

    def test_registered_session_found_in_config_dir_profile(self):
        prof = self.profile("prof", sid="abc")
        self.env(CLAUDE_CONFIG_DIR=str(prof))
        run = {"id": "ws-dev-1", "sessions": ["abc"]}
        self.assertEqual([p.name for p in metrics.find_transcripts(run)], ["abc.jsonl"])

    def test_roots_come_from_env_list_and_run_not_from_a_home_glob(self):
        a = self.profile("alpha", sid="s1")
        b = self.profile("beta", sid="s2")
        home = self.tmp / "nohome"
        (home / ".claude-stray" / "projects" / "p").mkdir(parents=True)
        (home / ".claude-stray" / "projects" / "p" / "s3.jsonl").write_text(rec("x"), encoding="utf-8")
        self.env(FACTORY_TRANSCRIPT_ROOTS=os.pathsep.join([str(a), str(b / "projects")]))
        run = {"id": "r", "sessions": ["s1", "s2", "s3"], "transcript_roots": []}
        self.assertEqual(sorted(p.name for p in metrics.find_transcripts(run)), ["s1.jsonl", "s2.jsonl"])
        c = self.profile("gamma", sid="s4")
        run = {"id": "r", "sessions": ["s4"], "transcript_roots": [str(c)]}
        self.assertEqual([p.name for p in metrics.find_transcripts(run)], ["s4.jsonl"])


class RegisterSessionTests(FactoryCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-aaaa1111", "CLAUDE_CONFIG_DIR": str(self.tmp / "prof")})
        p.start()
        self.addCleanup(p.stop)

    def test_run_start_registers_the_session_without_claiming(self):
        rid = self.make_run(branch=False)
        run = core.run_load(rid)
        self.assertEqual(run["sessions"], ["sess-aaaa1111"])
        self.assertEqual(run["transcript_roots"], [str(self.tmp / "prof")])
        self.assertEqual(core.claims(run), [])

    def test_run_session_is_idempotent_and_takes_an_explicit_id(self):
        rid = self.make_run(branch=False)
        rc, out = cli("run", "session", rid)
        self.assertEqual(rc, 0)
        self.assertIn("already registered", out)
        rc, out = cli("run", "session", rid, "sess-bbbb2222")
        self.assertIn("registered", out)
        cli("run", "session", rid, "sess-bbbb2222")
        self.assertEqual(core.run_load(rid)["sessions"], ["sess-aaaa1111", "sess-bbbb2222"])

    def test_run_session_without_any_id_is_a_usage_error(self):
        rid = self.make_run(branch=False)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_ID": ""}):
            self.assertEqual(cli("run", "session", rid)[0], 2)


if __name__ == "__main__":
    unittest.main()
