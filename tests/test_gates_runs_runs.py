"""Run lifecycle commands: close, retype, start derivation, status degradation, workspace list/show, naming."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.integrity_support import IntegrityCase, cli, core, sh

ROOT = Path(__file__).resolve().parents[1]


class CloseAndList(IntegrityCase):
    def test_close_hides_from_list_and_deletes_without_force(self):
        self.start()
        self.assertEqual(cli("run", "close", self.rid, "--reason", "  ")[0], 2)
        out = self.ok("run", "close", self.rid, "--reason", "superseded by DEV-2")
        self.assertIn("closed", out)
        run = core.run_load(self.rid)
        self.assertEqual(run["phase"], "closed")
        self.assertEqual(run["closed"]["reason"], "superseded by DEV-2")
        self.assertTrue(any(e["kind"] == "close" and e["data"]["reason"] == "superseded by DEV-2" for e in run["events"]))
        self.assertNotIn(self.rid, self.ok("run", "list"))
        self.assertIn(self.rid, self.ok("run", "list", "--all"))
        self.assertIn("run is closed", self.ok("run", "status", self.rid))
        self.assertEqual(cli("run", "close", self.rid, "--reason", "again")[0], 2)
        rc, out = cli("run", "delete", self.rid, "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(core.run_dir(self.rid).exists())

    def test_open_run_still_needs_force_to_delete(self):
        self.start()
        rc, out = cli("run", "delete", self.rid, "--yes")
        self.assertEqual(rc, 2)
        self.assertIn("--force", out)


class StatusDegrades(IntegrityCase):
    def test_missing_worktree_and_head_are_reported(self):
        self.start()
        run = core.run_load(self.rid)
        run["repos"]["React"]["head"] = "ab" * 20
        core.run_save(run)
        out = self.ok("run", "status", self.rid)
        self.assertIn("missing: React: recorded head abababab not found", out)
        sh("git", "worktree", "remove", "--force", str(self.wt), cwd=self.repo_dirs["React"])
        out = self.ok("run", "status", self.rid)
        self.assertIn(f"missing: React: worktree missing: {self.wt}", out)
        self.assertNotIn("Traceback", out)
        data = json.loads(self.ok("--json", "run", "status", self.rid))
        self.assertTrue(any("worktree missing" in g for g in data["gone"]))


class RetypeAndStart(IntegrityCase):
    def issue(self, rid_ticket: str, itype: str) -> None:
        core.write_json(core.run_dir(f"ws-{rid_ticket.lower()}") / "remote" / f"issue-{rid_ticket}.json",
                        {"key": rid_ticket, "fields": {"issuetype": {"name": itype}}})

    def types(self, rid: str) -> list[str]:
        return [t["type"] for t in core.run_load(rid)["tickets"]]

    def test_start_derives_bug_from_saved_issue_type(self):
        for ticket, itype, want in (("DEV-71", "Bug", "bug"), ("DEV-72", "defeito", "bug"), ("DEV-73", "ERRO", "bug"),
                                    ("DEV-74", "Task", "feature")):
            self.issue(ticket, itype)
            out = self.ok("run", "start", "ws", ticket)
            self.assertEqual(self.types(f"ws-{ticket.lower()}"), [want], ticket)
            if want == "bug":
                self.assertIn("derived from the Jira issuetype", out)

    def test_feature_flag_overrides_the_derivation(self):
        self.issue("DEV-75", "Bug")
        self.ok("run", "start", "ws", "DEV-75", "--feature")
        self.assertEqual(self.types("ws-dev-75"), ["feature"])

    def test_retype_moves_the_phase_machine_and_keeps_artifacts(self):
        self.start()
        self.acs()
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        self.assertEqual(core.run_load(self.rid)["phase"], "build")
        self.ok("run", "retype", self.rid, "--bug")
        run = core.run_load(self.rid)
        self.assertEqual((self.types(self.rid), run["phase"]), (["bug"], "reproduce"))
        self.assertTrue((self.rd / "acs.json").is_file())
        self.assertEqual(run["events"][-1]["kind"], "retype")
        self.ok("run", "retype", self.rid, "--feature")
        run = core.run_load(self.rid)
        self.assertEqual((self.types(self.rid), run["phase"]), (["feature"], "build"))

    def test_retype_hotfix_sets_targets(self):
        self.ws["ship"] = {"targets": ["pack", "homolog"], "hotfix": {"base": "homolog", "mirror": ["pack"]}}
        self.save_ws()
        self.start()
        self.ok("run", "retype", self.rid, "--hotfix")
        run = core.run_load(self.rid)
        self.assertEqual(run["hotfix"], {"base": "homolog", "mirror": ["pack"]})
        self.assertEqual(run["targets"], ["homolog", "pack"])
        self.assertEqual(self.types(self.rid), ["bug"])
        self.ok("run", "retype", self.rid, "--feature")
        self.assertNotIn("hotfix", core.run_load(self.rid))

    def test_retype_refused_after_gate_build(self):
        self.to_prove()
        rc, out = cli("run", "retype", self.rid, "--bug")
        self.assertEqual(rc, 2)
        self.assertIn("gate build already passed", out)
        self.assertEqual(self.types(self.rid), ["feature"])


class BlockersAndNaming(IntegrityCase):
    def test_decision_is_a_blocker_kind(self):
        self.start()
        self.ok("run", "block", self.rid, "decision", "needs the user's call")
        self.assertEqual(core.run_load(self.rid)["blockers"][0]["kind"], "decision")

    def test_infra_and_tooling_are_blocker_kinds_with_events(self):
        self.start()
        self.ok("run", "block", self.rid, "infra", "the registry is down")
        self.ok("run", "block", self.rid, "tooling", "factory cannot mirror env")
        run = core.run_load(self.rid)
        self.assertEqual([b["kind"] for b in run["blockers"]], ["infra", "tooling"])
        self.assertEqual([e["data"]["blocker_kind"] for e in run["events"] if e["kind"] == "block"], ["infra", "tooling"])
        rc, out = cli("run", "status", self.rid)
        self.assertEqual(rc, 0)
        self.assertIn("[infra]", out)
        self.assertIn("[tooling]", out)

    def test_invalid_blocker_kind_lists_valid_kinds_with_meaning(self):
        from factory.commands.run import BLOCKER_KIND_HELP
        self.start()
        try:
            rc, out = cli("run", "block", self.rid, "product", "x")
        except SystemExit as exc:
            rc, out = exc.code, ""
        self.assertEqual(rc, 2)
        self.assertEqual(core.run_load(self.rid)["blockers"], [])
        self.assertNotIn("product", BLOCKER_KIND_HELP)

    def test_invalid_blocker_kind_message(self):
        import argparse
        from factory.commands.run import BLOCKER_KIND_HELP, blocker_kind
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            blocker_kind("tooling2")
        msg = str(cm.exception)
        for kind, meaning in BLOCKER_KIND_HELP.items():
            self.assertIn(f"{kind}: {meaning}", msg)
        self.assertEqual(blocker_kind("infra"), "infra")

    def test_hotfix_mirror_name_never_equals_the_base_name(self):
        from factory.commands import run as run_cmd
        from factory.commands import workspace
        run = {"tickets": [{"key": "BUG-1", "type": "bug"}], "hotfix": {"base": "homolog", "mirror": ["pack"]},
               "repos": {"React": {"branch": "hotfix/REL_9/BUG-1"}}}
        ws = {"ship": {"package": "REL_9", "targets": ["homolog", "pack"], "hotfix": run["hotfix"]},
              "branch_format": {"hotfix": "hotfix/{package}/{key}"}}
        self.assertEqual(run_cmd.branch_name(run, ws, "React", "homolog"), "hotfix/REL_9/BUG-1")
        self.assertEqual(run_cmd.branch_name(run, ws, "React", "pack", mirror=True), "hotfix/REL_9/BUG-1-pack")
        self.assertTrue(any("branch_format.hotfix" in w for w in workspace.workspace_warnings(ws)))
        ws["branch_format"]["hotfix"] = "hotfix/{package}/{key}-{target_upper}"
        self.assertEqual(run_cmd.branch_name(run, ws, "React", "pack", mirror=True), "hotfix/REL_9/BUG-1-PACK")
        self.assertFalse(any("branch_format.hotfix" in w for w in workspace.workspace_warnings(ws)))


class WorkspaceListShow(IntegrityCase):
    def test_list_and_show_never_print_secrets(self):
        self.ws["repos"][0]["env"] = {"start": "x", "env_vars": {"DB_PASSWORD": "hunter2", "MODE": "dev"}}
        self.save_ws()
        sec = core.secrets_path("ws")
        sec.parent.mkdir(parents=True, exist_ok=True)
        sec.write_text("LOGIN_PASSWORD=s3cretvalue\n", encoding="utf-8")
        listing = self.ok("workspace", "list")
        self.assertIn("ws", listing)
        self.assertIn("React", listing)
        shown = self.ok("workspace", "show", "ws")
        self.assertNotIn("hunter2", shown)
        self.assertNotIn("s3cretvalue", shown)
        self.assertIn('"MODE": "dev"', shown)
        self.assertIn("1 key(s)", shown)
        self.assertEqual(cli("workspace", "show", "nope")[0], 2)


class SingleModuleRun(unittest.TestCase):
    def test_real_support_importers_run_alone(self):
        for mod in ("test_bug_flow", "test_hotfix_flow", "test_hotfix_ui_bug", "test_visual_evidence"):
            proc = subprocess.run([sys.executable, "-m", "unittest", f"tests/{mod}.py", "-k", "zz_matches_nothing"],
                                  cwd=str(ROOT), capture_output=True, text=True)
            self.assertNotIn("ModuleNotFoundError", proc.stderr, mod)
            self.assertIn(proc.returncode, (0, 5), f"{mod}: {proc.stderr[-400:]}")  # 5: no test matched the filter


if __name__ == "__main__":
    unittest.main()
