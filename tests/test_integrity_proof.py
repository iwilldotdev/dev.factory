"""Blocker 2: commits after prove; blocker 3: untracked files count as dirty."""

from __future__ import annotations

import json
import os
import subprocess
import unittest

from tests.integrity_support import IntegrityCase, cli, commit_file, core, sh


class CommitAfterProveTests(IntegrityCase):
    def test_review_gate_rejects_commit_after_prove(self):
        self.proved()
        self.record("code")
        self.record("qa")
        commit_file(self.wt, "src/b.txt", "late\n", "[Area] - late")
        self.record("code")  # reviews re-bound to the new HEAD: still no proof for it
        self.record("qa")
        rc, out = self.gate("review")
        self.assertEqual(rc, 1)
        self.assertIn("evidence is stale", out)
        self.assertIn("run rewind", out)
        self.assertEqual(core.run_load(self.rid)["phase"], "review")  # not a CHANGES loop

    def test_ship_plan_and_gate_reject_commit_after_review(self):
        self.reviewed()
        commit_file(self.wt, "src/b.txt", "late\n", "[Area] - late")
        rc, out = cli("ship", "plan", self.rid)
        self.assertEqual(rc, 1)
        self.assertIn("evidence is stale", out)
        rc, out = self.gate("ship")
        self.assertEqual(rc, 1)
        self.assertIn("evidence is stale", out)

    def test_ship_verify_requires_readback_sha_equal_proven_head(self):
        self.reviewed()
        plan = json.loads(self.ok("--json", "ship", "plan", self.rid))
        head = core.git_head(self.wt)
        self.assertEqual(plan["mrs"][0]["head_sha"], head)
        self.readbacks(plan, sha="1" * 40)  # someone pushed another commit
        rc, out = cli("ship", "verify", self.rid)
        self.assertEqual(rc, 1)
        self.assertIn("!= proven HEAD", out)
        self.readbacks(plan)
        self.ok("ship", "verify", self.rid)
        # diff_refs.head_sha is accepted as the readback sha
        mr = plan["mrs"][0]
        core.write_json(self.rd / mr["readback"], {"merge_request": {
            "source_branch": mr["source_branch"], "target_branch": mr["target_branch"], "title": mr["title"],
            "draft": True, "iid": 1, "diff_refs": {"head_sha": head}}})
        self.ok("ship", "verify", self.rid)

    def test_ship_verify_rejects_branch_moved_after_plan(self):
        self.reviewed()
        plan = json.loads(self.ok("--json", "ship", "plan", self.rid))
        self.readbacks(plan)
        commit_file(self.wt, "src/b.txt", "late\n", "[Area] - late")
        rc, out = cli("ship", "verify", self.rid)
        self.assertEqual(rc, 1)
        self.assertIn("ship plan recorded", out)

    def test_prove_gate_records_proved_heads(self):
        self.proved()
        self.assertEqual(core.run_load(self.rid)["proved"], {"React": {"HEAD": core.git_head(self.wt)}})

    def test_rewind_to_prove_records_loop(self):
        self.reviewed()
        before = core.run_load(self.rid)["loops"].get("prove", 0)
        rc, out = cli("run", "rewind", self.rid, "prove", "rebased on main")
        self.assertEqual(rc, 0, out)
        run = core.run_load(self.rid)
        self.assertEqual(run["phase"], "prove")
        self.assertEqual(run["loops"]["prove"], before + 1)  # the loop of the phase it goes back to
        ev = next(e for e in reversed(run["events"]) if e["kind"] == "rewind")
        self.assertEqual((ev["data"]["from"], ev["data"]["to"], ev["data"]["reason"]), ("ship", "prove", "rebased on main"))
        self.assertEqual(cli("run", "rewind", self.rid, "prove", "again")[0], 2)  # not backwards
        self.assertEqual(cli("run", "rewind", self.rid, "build", "fix")[0], 0)

    def test_behind_message_points_to_rewind(self):
        self.reviewed()
        repo = self.repo_dirs["React"]
        commit_file(repo, "other.txt", "x\n", "[Area] - upstream")
        sh("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
        rc, out = cli("ship", "plan", self.rid)
        self.assertEqual(rc, 1)
        self.assertIn("factory run rewind", out)
        self.assertNotIn("then prove + review again", out)


class UntrackedDirtyTests(IntegrityCase):
    def test_untracked_file_is_dirty_and_gitignored_is_not(self):
        self.start()
        self.assertFalse(core.git_dirty(self.wt))
        (self.wt / "new.txt").write_text("x", encoding="utf-8")
        self.assertTrue(core.git_dirty(self.wt))
        (self.wt / "new.txt").unlink()
        commit_file(self.wt, ".gitignore", "build/\n", "[Area] - ignore")
        (self.wt / "build").mkdir()
        (self.wt / "build" / "out.js").write_text("x", encoding="utf-8")
        self.assertFalse(core.git_dirty(self.wt))

    def test_node_modules_link_is_not_dirty(self):
        self.start()
        target = self.tmp / "deps"
        target.mkdir()
        (target / "pkg.js").write_text("x", encoding="utf-8")
        link = self.wt / "node_modules"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("cannot create a link here")
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if not link.exists():
            self.skipTest("cannot create a link here")
        self.assertFalse(core.git_dirty(self.wt))

    def test_untracked_file_fails_prove_evidence_and_gate(self):
        self.to_prove()
        (self.wt / "stray.txt").write_text("x", encoding="utf-8")
        rc, out = cli("prove", self.rid)
        self.assertIn("dirty", out)
        rc, out = self.gate("prove")
        self.assertEqual(rc, 1)
        self.assertIn("dirty", out)


if __name__ == "__main__":
    unittest.main()
