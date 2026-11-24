"""`factory migrate apply`: a synthetic legacy run directory plus a map file gives the expected tree, a re-sealed
plan lock and a recorded event."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.provider_support import ProviderCase, cli  # noqa: E402

from factory import core, integrity  # noqa: E402
from factory.commands import migrate  # noqa: E402

OPS = [
    {"op": "ws_rename_key", "path": "repos[*].scm_legacy", "to": "scm_project"},
    {"op": "rename_key", "file": "run.json", "path": "legacy_changes", "to": "changes"},
    {"op": "rename_key", "file": "run.json", "path": "changes[*].num_old", "to": "number"},
    {"op": "move_file", "from": "remote/issue-*.json", "to": "raw/ticket/{1}.json"},
    {"op": "ingest", "kind": "ticket", "raw": "raw/ticket/*.json"},
    {"op": "replace_text", "file": "acs.json", "from": "legacy_ref", "to": "source_file"},
]


class MigrateCase(ProviderCase):
    def setUp(self):
        super().setUp()
        core.write_json(core.workspace_path("ws"), {
            "schemaVersion": 1, "name": "ws", "repos": [{"alias": "api", "scm_legacy": "team/api"}],
            "providers": {"tickets": "acme", "scm": "acme", "design": "acme"}})
        self.map_path = self.tmp / "map.json"
        self.map_path.write_text(json.dumps(OPS), encoding="utf-8")

    def legacy_run(self, run_id="r1", locked=True) -> Path:
        rd = self.make_run(run_id, phase="plan", legacy_changes=[{"alias": "api", "num_old": 4}])
        core.write_json(rd / "acs.json", {"schemaVersion": 1, "acs": [{"id": "AC-1", "text": "t"}],
                                          "checks": [{"id": "CHK-1", "type": "visual", "legacy_ref": "abc", "node": "1:1"}]})
        core.write_json(rd / "checks" / "style" / "s.json", {"legacy_ref": "x", "node": "1:1"})
        (rd / "remote").mkdir()
        (rd / "remote" / "issue-ACME-12.json").write_bytes(self.fixture("acme", "ticket", "1.json").read_bytes())
        if locked:
            run = core.run_load(run_id)
            run["phase"] = "build"
            integrity.lock_plan(run, "test fixture")
            core.run_save(run)
        return rd

    def map_sha(self) -> str:
        return hashlib.sha256(self.map_path.read_bytes()).hexdigest()

    def apply(self, *args):
        return cli("migrate", "apply", *args, "--digest", self.map_sha(), "--user-said", "ok, apply that map")


class MigrateTests(MigrateCase):
    def test_full_migration_tree_lock_and_event(self):
        rd = self.legacy_run()
        before = core.run_load("r1")
        old_lock = before["plan_lock"]["digest"]
        sha = hashlib.sha256(self.map_path.read_bytes()).hexdigest()
        rc, out = self.apply("r1", str(self.map_path))
        self.assertEqual(rc, 0, out)

        run = core.run_load("r1")
        self.assertNotIn("legacy_changes", run)
        self.assertEqual(run["changes"], [{"alias": "api", "number": 4}])
        self.assertEqual(run["contracts_schema"], 2)
        self.assertEqual(run["schemaVersion"], 1)  # run_load still accepts it
        self.assertEqual(self.read(rd, "acs.json")["checks"][0]["source_file"], "abc")
        self.assertNotIn("legacy_ref", (rd / "acs.json").read_text(encoding="utf-8"))
        self.assertFalse((rd / "remote" / "issue-ACME-12.json").exists())
        self.assertTrue((rd / "raw" / "ticket" / "ACME-12.json").is_file())
        ticket = self.read(rd, "contracts/ticket/ACME-12.json")
        self.assertEqual(ticket["key"], "ACME-12")
        self.assertEqual(ticket["provenance"]["source_hint"], "migrate")
        ws = json.loads(core.workspace_path("ws").read_text(encoding="utf-8"))
        self.assertEqual(ws["repos"][0], {"alias": "api", "scm_project": "team/api"})

        ev = [e for e in run["events"] if e["kind"] == "migrate"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["data"]["map_sha256"], sha)
        self.assertEqual(ev[0]["data"]["old_lock"], old_lock)
        self.assertEqual(ev[0]["data"]["new_lock"], run["plan_lock"]["digest"])
        self.assertNotEqual(old_lock, run["plan_lock"]["digest"])
        # the lock was re-sealed over the migrated plan files and the migrated workspace; evidence order is kept
        self.assertEqual(integrity.lock_errors(run), [])
        self.assertEqual(run["plan_lock"]["seq"], before["plan_lock"]["seq"])
        self.assertTrue(any(e["kind"] == "plan_lock" and e["data"]["reason"] == "migrate" for e in run["events"]))

    def test_without_the_reseal_the_lock_would_object(self):
        rd = self.legacy_run()
        text = (rd / "acs.json").read_text(encoding="utf-8").replace("legacy_ref", "source_file")
        (rd / "acs.json").write_text(text, encoding="utf-8")
        self.assertTrue(integrity.lock_errors(core.run_load("r1")))

    def test_rerun_is_idempotent(self):
        rd = self.legacy_run()
        self.assertEqual(self.apply("r1", str(self.map_path))[0], 0)
        first = core.run_load("r1")
        rc, out = self.apply("r1", str(self.map_path))
        self.assertEqual(rc, 0, out)
        second = core.run_load("r1")
        self.assertEqual(first["plan_lock"]["digest"], second["plan_lock"]["digest"])
        self.assertEqual(second["changes"], first["changes"])
        self.assertEqual(len([e for e in second["events"] if e["kind"] == "migrate"]), 2)
        self.assertTrue((rd / "contracts" / "ticket" / "ACME-12.json").is_file())

    def test_unlocked_run_has_no_seal(self):
        self.legacy_run(locked=False)
        rc, out = self.apply("r1", str(self.map_path))
        self.assertEqual(rc, 0, out)
        run = core.run_load("r1")
        self.assertNotIn("plan_lock", run)
        data = [e for e in run["events"] if e["kind"] == "migrate"][0]["data"]
        self.assertEqual((data["old_lock"], data["new_lock"]), (None, None))

    def test_all_migrates_every_run_and_reports_failures(self):
        self.legacy_run("r1")
        self.legacy_run("r2", locked=False)
        rc, out = self.apply("--all", str(self.map_path))
        self.assertEqual(rc, 0, out)
        self.assertIn("migrated r1", out)
        self.assertIn("migrated r2", out)
        self.assertEqual(core.run_load("r2")["changes"][0]["number"], 4)
        rc, out = cli("--json", "migrate", "apply", "--all", str(self.map_path), "--digest", self.map_sha(), "--user-said", "ok")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(r["run"] for r in json.loads(out)["migrated"]), ["r1", "r2"])

    def test_bad_map_is_rejected_before_anything_changes(self):
        rd = self.legacy_run()
        before = (rd / "run.json").read_bytes()
        for bad in ([{"op": "explode"}], [{"op": "rename_key", "file": "run.json"}], {"nope": 1}, [], "text"):
            self.map_path.write_text(json.dumps(bad), encoding="utf-8")
            rc, out = self.apply("r1", str(self.map_path))
            self.assertEqual(rc, 2, bad)
        self.map_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.apply("r1", str(self.map_path))[0], 2)
        self.assertEqual(self.apply("r1", str(self.tmp / "absent.json"))[0], 2)
        self.assertEqual((rd / "run.json").read_bytes(), before)

    def test_failing_op_leaves_run_json_unsaved(self):
        rd = self.legacy_run()
        (rd / "raw" / "ticket").mkdir(parents=True)
        (rd / "raw" / "ticket" / "ACME-12.json").write_text("different", encoding="utf-8")
        before = (rd / "run.json").read_bytes()
        rc, out = self.apply("r1", str(self.map_path))
        self.assertEqual(rc, 2)
        self.assertIn("exists with different content", out)
        self.assertEqual((rd / "run.json").read_bytes(), before)

    def test_files_outside_the_run_are_refused(self):
        self.legacy_run()
        for op in ({"op": "replace_text", "file": "../outside.json", "from": "a", "to": "b"},
                   {"op": "rename_key", "file": "../../x.json", "path": "a", "to": "b"}):
            self.map_path.write_text(json.dumps([op]), encoding="utf-8")
            rc, out = self.apply("r1", str(self.map_path))
            self.assertEqual(rc, 2)
            self.assertIn("outside the run directory", out)

    def test_user_said_is_required_and_recorded_as_a_decision(self):
        self.legacy_run()
        rc, out = cli("migrate", "apply", "r1", str(self.map_path))
        self.assertEqual(rc, 2)
        self.assertIn("--user-said", out)
        self.assertNotIn("migrate", [e["kind"] for e in core.run_load("r1")["events"]])
        sha = hashlib.sha256(self.map_path.read_bytes()).hexdigest()
        rc, out = cli("migrate", "apply", "r1", str(self.map_path), "--digest", sha, "--user-said", "approve migration map")
        self.assertEqual(rc, 0, out)
        run = core.run_load("r1")
        dec = [d for d in run["decisions"] if sha in d["text"]]
        self.assertEqual((len(dec), dec[0]["by"], dec[0]["user_said"]), (1, "user", "approve migration map"))
        ev = [e for e in run["events"] if e["kind"] == "migrate"][0]["data"]
        self.assertEqual((ev["map_sha256"], ev["user_said"]), (sha, "approve migration map"))

    def test_digest_is_required_and_must_match_the_map(self):
        rd = self.legacy_run()
        before = (rd / "run.json").read_bytes()
        rc, out = cli("migrate", "apply", "r1", str(self.map_path), "--user-said", "ok")
        self.assertEqual(rc, 2)
        self.assertIn("--digest", out)
        rc, out = cli("migrate", "apply", "r1", str(self.map_path), "--digest", "0" * 64, "--user-said", "ok")
        self.assertEqual(rc, 2)
        self.assertIn("--digest", out)
        self.assertEqual((rd / "run.json").read_bytes(), before)
        rc, out = cli("migrate", "apply", "r1", str(self.map_path), "--digest", "sha256:" + self.map_sha(), "--user-said", "ok")
        self.assertEqual(rc, 0, out)

    def test_dry_run_lists_every_operation_and_target_and_writes_nothing(self):
        rd = self.legacy_run()
        before = (rd / "run.json").read_bytes()
        rc, out = cli("migrate", "apply", "r1", str(self.map_path), "--dry-run")  # no digest, no user-said needed
        self.assertEqual(rc, 0, out)
        self.assertIn(self.map_sha(), out)
        for needle in ("[0] ws_rename_key", "[1] rename_key", "run.json", "[3] move_file", "issue-ACME-12.json",
                       "[5] replace_text", "acs.json"):
            self.assertIn(needle, out)
        self.assertEqual((rd / "run.json").read_bytes(), before)
        self.assertTrue((rd / "remote" / "issue-ACME-12.json").is_file())
        self.assertNotIn("migrate", [e["kind"] for e in core.run_load("r1")["events"]])

    def test_cli_owned_targets_are_refused(self):
        rd = self.legacy_run()
        before = (rd / "run.json").read_bytes()
        bad_ops = [
            {"op": "replace_text", "file": "evidence/CHK-1.json", "from": "pass", "to": "fail"},
            {"op": "rename_key", "file": "reviews/code-1.json", "path": "verdict", "to": "v"},
            {"op": "rename_key", "file": "contracts/ticket/ACME-12.json", "path": "title", "to": "t"},
            {"op": "move_file", "from": "remote/issue-*.json", "to": "contracts/ticket/{1}.json"},
            {"op": "move_file", "from": "baseline/*.png", "to": "raw/{1}.png"},
            {"op": "move_file", "from": "*/x.json", "to": "raw/{1}.json"},
            {"op": "ingest", "kind": "ticket", "raw": "evidence/*.json"},
            {"op": "rename_key", "file": "run.json", "path": "events", "to": "history"},
            {"op": "rename_key", "file": "run.json", "path": "plan_lock.digest", "to": "d"},
            {"op": "replace_text", "file": "run.json", "from": "a", "to": "b"},
        ]
        for op in bad_ops:
            self.map_path.write_text(json.dumps([op]), encoding="utf-8")
            rc, out = self.apply("r1", str(self.map_path))
            self.assertEqual(rc, 2, op)
            self.assertTrue("refused" in out or "CLI-owned" in out, (op, out))
        self.assertEqual((rd / "run.json").read_bytes(), before)

    def test_a_lock_that_already_fails_is_not_resealed(self):
        rd = self.legacy_run()
        (rd / "acs.json").write_text((rd / "acs.json").read_text(encoding="utf-8").replace('"AC-1"', '"AC-9"', 1), encoding="utf-8")
        before = (rd / "run.json").read_bytes()
        rc, out = self.apply("r1", str(self.map_path))
        self.assertEqual(rc, 2)
        self.assertIn("already fails before the migration", out)
        self.assertEqual((rd / "run.json").read_bytes(), before)
        self.assertFalse((rd / "raw" / "ticket" / "ACME-12.json").exists())  # nothing was applied

    def test_usage_errors(self):
        self.legacy_run()
        self.assertEqual(self.apply(str(self.map_path))[0], 2)
        self.assertEqual(self.apply("--all", "r1", str(self.map_path))[0], 2)
        self.assertEqual(self.apply("ghost", str(self.map_path))[0], 2)


class HelperTests(unittest.TestCase):
    def test_rename_key_keeps_order_and_refuses_collisions(self):
        data = {"a": 1, "old": 2, "z": 3, "list": [{"old": 1}, {"other": 1}, {"old": 2}]}
        self.assertEqual(migrate.rename_key(data, "old", "new"), 1)
        self.assertEqual(list(data), ["a", "new", "z", "list"])
        self.assertEqual(migrate.rename_key(data, "list[*].old", "n"), 2)
        self.assertEqual(data["list"], [{"n": 1}, {"other": 1}, {"n": 2}])
        self.assertEqual(migrate.rename_key(data, "missing.deeper", "x"), 0)
        with self.assertRaises(core.FactoryError):
            migrate.rename_key({"a": 1, "b": 2}, "a", "b")
        with self.assertRaises(core.FactoryError):
            migrate.rename_key({"a": [1]}, "a[0]", "b")

    def test_replace_keys_touches_keys_only(self):
        data = {"fk": 1, "nested": [{"fk_two": "fk"}], "v": "fk"}
        self.assertEqual(migrate.replace_keys(data, "fk", "sf"), 2)
        self.assertEqual(data, {"sf": 1, "nested": [{"sf_two": "fk"}], "v": "fk"})


if __name__ == "__main__":
    unittest.main()
