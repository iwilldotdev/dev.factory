"""CLI prints fetch items (`{kind, id, raw_path, hint}`, raw_path absolute with forward slashes) for missing raw files."""

import contextlib
import io
import json
import shutil
import unittest

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from ops_helpers import FactoryCase, make_repo
from design_support import make_png

from factory import profiles
from factory.cli import main
from provider_support import FIXTURES


def png():
    return make_png(2, 3)


def png():
    return make_png(2, 3)


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class FetchHintTests(FactoryCase):
    def setUp(self):
        super().setUp()
        repo = make_repo(self.tmp / "react")
        self.write("workspaces/ws/workspace.json", {
            "schemaVersion": 1, "name": "ws",
            "providers": {"tickets": "acme", "scm": "acme", "design": "acme"},
            "repos": [{"alias": "React", "path": str(repo), "scm_project": "grp/react"}]})
        shutil.copytree(FIXTURES, self.home / "profiles")
        for pdir in (self.home / "profiles").iterdir():
            profiles.seal(pdir, "test fixture")
        self.make_run("ws-dev-1", phase="plan")
        self.rd = (self.home / "runs/ws-dev-1").as_posix()

    def test_design_check_prints_fetch_items(self):
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [],
                                              "checks": [{"id": "C1", "type": "visual", "node": "1:2", "source_file": "FK"}]})
        rc, out, _ = cli("design", "check", "ws-dev-1")
        self.assertEqual(rc, 1)
        img, frame = f"{self.rd}/raw/design_image/1-2.png", f"{self.rd}/raw/design_frame/1-2.json"
        self.assertIn(f"FETCH design_image 1:2 -> {img} | acme-cli design render FK 1:2 --scale 1 --out {img}", out)
        self.assertIn(f"FETCH design_frame 1:2 -> {frame} | acme-cli design node FK 1:2 --out {frame}", out)
        self.assertNotIn("\\", self.rd)

    def test_design_check_json_and_placeholder(self):
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [], "checks": [{"id": "C1", "type": "visual", "node": "1:2"}]})
        rc, out, _ = cli("--json", "design", "check", "ws-dev-1")
        data = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertFalse(data["passed"])
        first = data["fetch"][0]
        self.assertEqual((first["kind"], first["id"]), ("design_image", "1:2"))
        self.assertIn("<file>", first["hint"])
        self.assertTrue(first["raw_path"].endswith("/raw/design_image/1-2.png"))

    def test_saved_raw_is_not_fetched_again(self):
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [], "checks": [{"id": "C1", "type": "visual", "node": "1:2"}]})
        self.write("runs/ws-dev-1/raw/design_image/1-2.png", png())
        rc, out, _ = cli("design", "check", "ws-dev-1")
        self.assertEqual(rc, 1)
        self.assertNotIn("FETCH design_image", out)
        self.assertIn("FETCH design_frame", out)

    def test_gate_intake_issue(self):
        self.make_run("ws-dev-1", phase="intake")
        rc, out, _ = cli("gate", "ws-dev-1", "intake")
        self.assertEqual(rc, 1)
        raw = f"{self.rd}/raw/ticket/DEV-1.json"
        self.assertIn(f"FETCH ticket DEV-1 -> {raw} | acme-cli ticket get DEV-1 --out {raw}", out)

    def plan_v2(self, **change):
        c = {"repo": "React", "target": "pack", "source_branch": "b", "target_branch": "pack", "title": "t", "draft": False,
             "readback": "contracts/change/React-pack.json"}
        c.update(change)
        self.write("runs/ws-dev-1/ship-plan.json", {"schemaVersion": 2, "changes": [c], "actions": []})

    def test_ship_verify_readback(self):
        self.plan_v2()
        rc, out, _ = cli("ship", "verify", "ws-dev-1")
        self.assertEqual(rc, 1)
        raw = f"{self.rd}/raw/change/React-pack.json"
        self.assertIn(f"FETCH change React-pack -> {raw} | acme-cli change get grp/react <number> --with threads,approvals --out {raw}", out)
        self.assertIn(f"then: factory ingest ws-dev-1 change {raw} --id React-pack --repo React", out)

    def test_ship_verify_fetch_json_carries_the_ingest_line_and_known_number(self):
        from change_support import put_change
        self.plan_v2()
        put_change("ws-dev-1", "React", "pack", number=41, title="other")
        (self.home / "runs/ws-dev-1/raw/change/React-pack.json").write_text("{}", encoding="utf-8")  # stale contract
        rc, out, _ = cli("--json", "ship", "verify", "ws-dev-1")
        item = json.loads(out)["fetch"][0]
        self.assertEqual((rc, item["kind"], item["id"]), (1, "change", "React-pack"))
        self.assertIn("grp/react 41", item["hint"])
        self.assertTrue(item["ingest"].startswith("factory ingest ws-dev-1 change "))

    def test_ship_verify_accepts_a_matching_change_contract(self):
        from unittest import mock
        from change_support import put_change
        from factory.commands import ship
        sha = "a" * 40
        self.plan_v2(head_sha=sha)
        put_change("ws-dev-1", "React", "pack", source_branch="b", target_branch="pack", title="t", draft=False, head_sha=sha)
        with mock.patch.object(ship, "integrity_errors", return_value=[]), \
                mock.patch.object(ship, "review_errors", return_value=[]), \
                mock.patch.object(ship, "branch_head", return_value=sha):  # integrity: tests/test_integrity_*.py
            rc, out, _ = cli("ship", "verify", "ws-dev-1")
        self.assertEqual((rc, out.strip()), (0, "OK ship verified"))

    def test_feedback_ingest_lists_missing(self):
        from change_support import put_change
        self.plan_v2()
        put_change("ws-dev-1", "React", "pack", number=7, pipelines=[{"id": "9", "status": "running"}, {"id": "55", "status": "failed"}])
        rc, out, _ = cli("feedback", "ingest", "ws-dev-1")
        self.assertEqual(rc, 0)
        raw = f"{self.rd}/raw/pipeline/React-pack.json"
        self.assertIn(f"FETCH pipeline React-pack -> {raw} | acme-cli ci get grp/react 55 --out {raw}; jobs: acme-cli ci jobs grp/react 55 --out {raw}.jobs", out)
        self.assertIn(f"--raw jobs={raw}.jobs", out)
        self.assertNotIn("FETCH change", out)

    def test_feedback_ingest_reads_contracts_not_raw_saves(self):
        from change_support import put_change, put_pipeline
        self.plan_v2()
        put_change("ws-dev-1", "React", "pack", number=7, threads=[
            {"id": "t1", "resolved": False, "resolvable": True, "system": False,
             "notes": [{"author": "rev", "body_md": "fix", "system": False}]}],
            pipelines=[{"id": "55", "status": "failed"}])
        put_pipeline("ws-dev-1", "React", "pack", id="55", status="failed",
                     jobs=[{"id": "9", "name": "lint", "stage": "test", "status": "failed", "allow_failure": False}])
        rc, out, _ = cli("--json", "feedback", "ingest", "ws-dev-1")
        data = json.loads(out)
        self.assertEqual((rc, data["findings"], data["fetch"]), (0, 2, []))


if __name__ == "__main__":
    unittest.main()
