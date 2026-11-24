"""`factory ingest` end to end through the CLI entry: mapping, provenance, stale detection, native format, ids."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.provider_support import ProviderCase, cli  # noqa: E402
from tests.test_contracts import golden, png  # noqa: E402

from factory import contracts, core, profiles  # noqa: E402


class IngestCase(ProviderCase):
    def setUp(self):
        super().setUp()
        self.make_workspace()
        self.rd = self.make_run("r1")
        self.drop = self.tmp / "drop"
        self.drop.mkdir()

    def edit_profile(self, name, fn):
        path = self.home / "profiles" / name / "profile.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        fn(doc)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        profiles.seal(path.parent, "test: edited")

    def ingest(self, *argv, **kw):
        return cli("ingest", "r1", *argv, **kw)

    def raw(self, profile, kind, n="1", ext="json", name=None) -> Path:
        dst = self.drop / (name or f"{kind}-{n}.{ext}")
        dst.write_bytes(self.fixture(profile, kind, f"{n}.{ext}").read_bytes())
        return dst

    def contract(self, kind, ident):
        return self.read(self.rd, f"contracts/{kind}/{ident}.json")

    def without_prov(self, doc):
        return {k: v for k, v in doc.items() if k != "provenance"}


class IngestMappedTests(IngestCase):
    def test_ticket_end_to_end(self):
        raw = self.raw("acme", "ticket")
        rc, out = self.ingest("ticket", str(raw), "--id", "ACME-12", "--source-hint", "acme-cli ticket get ACME-12")
        self.assertEqual(rc, 0, out)
        self.assertIn("INGEST OK ticket/ACME-12", out)
        self.assertIn("unknown links: not mapped", out)
        doc = self.contract("ticket", "ACME-12")
        self.assertEqual(self.without_prov(doc), golden("acme", "ticket"))
        prov = doc["provenance"]
        self.assertEqual(prov["profile"], "acme")
        self.assertEqual(prov["raw_path"], "raw/ticket/ACME-12.json")
        self.assertEqual(prov["raw_sha256"], contracts.sha256_file(self.rd / prov["raw_path"]))
        self.assertEqual(prov["raw_sha256"], contracts.sha256_file(raw))
        self.assertEqual(prov["source_hint"], "acme-cli ticket get ACME-12")
        self.assertEqual(prov["profile_digest"][:7], "sha256:")
        self.assertRegex(prov["ingested_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(contracts.validate("ticket", doc), [])
        self.assertEqual(list((self.rd / "contracts" / "ticket").glob("*.tmp")), [])

    def test_default_id_is_the_raw_stem_and_raw_inside_the_run_stays_put(self):
        inside = self.rd / "raw" / "ticket" / "ACME-12.json"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(self.fixture("acme", "ticket", "1.json").read_bytes())
        rc, out = self.ingest("ticket", str(inside))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.contract("ticket", "ACME-12")["provenance"]["raw_path"], "raw/ticket/ACME-12.json")
        rc, out = self.ingest("ticket", "raw/ticket/ACME-12.json")  # run-relative works too
        self.assertEqual(rc, 0, out)

    def test_same_raw_same_profile_same_contract_bytes(self):
        raw = self.raw("acme", "ticket")
        self.ingest("ticket", str(raw), "--id", "A-1")
        first = self.contract("ticket", "A-1")
        self.ingest("ticket", str(raw), "--id", "A-1")
        second = self.contract("ticket", "A-1")
        for d in (first, second):
            d["provenance"].pop("ingested_at")
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_second_profile_via_workspace_and_override(self):
        raw = self.raw("zeta", "ticket")
        rc, out = self.ingest("ticket", str(raw), "--id", "ZED-7", "--profile", "zeta")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.contract("ticket", "ZED-7")["provenance"]["profile"], "zeta")
        self.make_workspace({"tickets": "zeta", "scm": "zeta", "design": "zeta"})
        rc, out = self.ingest("ticket", str(raw), "--id", "ZED-8")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.contract("ticket", "ZED-8")["provenance"]["profile"], "zeta")

    def test_change_without_threads_is_unknown_and_empty_list_is_none(self):
        data = json.loads(self.fixture("acme", "change", "1.json").read_text(encoding="utf-8"))
        for field, key in (("threads", "threads"), ("reviewers", "reviewers"), ("pipelines", "ci")):
            payload = {k: v for k, v in data.items() if k != key}
            raw = self.drop / "c-missing.json"
            raw.write_text(json.dumps(payload), encoding="utf-8")
            rc, out = self.ingest("change", str(raw), "--id", "api-42", "--repo", "api")
            self.assertEqual(rc, 0, out)
            doc = self.contract("change", "api-42")
            self.assertNotIn(field, doc, field)  # absent payload data stays unknown
            self.assertIn({"field": field, "reason": "null"}, doc["unknowns"], field)
            raw.write_text(json.dumps({**payload, key: []}), encoding="utf-8")
            rc, out = self.ingest("change", str(raw), "--id", "api-42", "--repo", "api")
            self.assertEqual(rc, 0, out)
            doc = self.contract("change", "api-42")
            self.assertEqual(doc[field], [], field)  # an explicit empty list is a real "none"
            self.assertNotIn(field, [u["field"] for u in doc["unknowns"]])

    def test_change_uses_repo_context(self):
        raw = self.raw("acme", "change")
        rc, out = self.ingest("change", str(raw), "--id", "api-42", "--repo", "api")
        self.assertEqual(rc, 0, out)
        doc = self.contract("change", "api-42")
        self.assertEqual((doc["repo"], doc["number"], doc["draft"], doc["title"]), ("api", 42, True, "Fix empty list spinner"))
        self.assertEqual(self.without_prov(doc), golden("acme", "change"))

    def test_change_without_repo_fails_on_required_context(self):
        raw = self.raw("acme", "change")
        rc, out = self.ingest("change", str(raw), "--id", "api-42")
        self.assertEqual(rc, 2)
        self.assertIn("contract field repo required", out)
        self.assertFalse((self.rd / "contracts" / "change").exists())

    def test_change_list(self):
        raw = self.raw("acme", "change_list")
        rc, out = self.ingest("change_list", str(raw), "--id", "open", "--repo", "api")
        self.assertEqual(rc, 0, out)
        self.assertEqual([i["number"] for i in self.contract("change_list", "open")["items"]], [7, 8])

    def test_optional_input_may_be_left_out(self):
        """acme declares inputs ["main", "jobs?"]: the profile finds the jobs in the main raw when `jobs` is not passed."""
        def embed(d):
            m = d["kinds"]["pipeline"]["map"]
            each = m["jobs"]
            d["kinds"]["pipeline"]["map"]["jobs"] = {"coalesce": [
                each, {"each": {**each["each"], "over": {"path": "embedded"}}}]}
        self.edit_profile("acme", embed)
        raw = json.loads(self.fixture("acme", "pipeline", "1.json").read_text(encoding="utf-8"))
        raw["embedded"] = [{"id": "j1", "name": "lint", "stage": "check", "state": "ok", "soft": False}]
        main = self.drop / "pipeline-1.json"
        main.write_text(json.dumps(raw), encoding="utf-8")
        rc, out = self.ingest("pipeline", str(main), "--id", "p8")
        self.assertEqual(rc, 0, out)
        doc = self.contract("pipeline", "p8")
        self.assertEqual([j["name"] for j in doc["jobs"]], ["lint"])
        self.assertNotIn("inputs", doc["provenance"])

    def test_optional_input_absent_without_a_fallback_names_the_null_expression(self):
        main = self.raw("acme", "pipeline")
        rc, out = self.ingest("pipeline", str(main), "--id", "p8")
        self.assertEqual(rc, 2)
        self.assertIn("contract field jobs required", out)
        self.assertIn("gave null", out)

    def test_pipeline_needs_and_records_named_raw(self):
        self.edit_profile("acme", lambda d: d["kinds"]["pipeline"].update(inputs=["main", "jobs"]))
        main = self.raw("acme", "pipeline")
        jobs = self.drop / "jobs.json"
        jobs.write_bytes(self.fixture("acme", "pipeline", "1.jobs.json").read_bytes())
        rc, out = self.ingest("pipeline", str(main), "--id", "p9")
        self.assertEqual(rc, 2)
        self.assertIn("--raw jobs=", out)
        rc, out = self.ingest("pipeline", str(main), "--id", "p9", "--raw", f"jobs={jobs}", "--raw", f"extra={jobs}")
        self.assertEqual(rc, 2)
        self.assertIn("no input", out)
        rc, out = self.ingest("pipeline", str(main), "--id", "p9", "--raw", f"jobs={jobs}")
        self.assertEqual(rc, 0, out)
        doc = self.contract("pipeline", "p9")
        self.assertEqual(self.without_prov(doc), golden("acme", "pipeline"))
        rec = doc["provenance"]["inputs"]["jobs"]
        self.assertEqual(rec["raw_path"], "raw/pipeline/p9.jobs.json")
        # the jobs raw is part of freshness
        (self.rd / rec["raw_path"]).write_text('{"items": []}', encoding="utf-8")
        rc, out = self.ingest("--check")
        self.assertEqual(rc, 1)
        self.assertIn("p9.jobs.json changed", out)

    def test_stdin_writes_raw_then_maps(self):
        data = self.fixture("acme", "ticket", "1.json").read_bytes()
        rc, out = self.ingest("ticket", "--stdin", "--id", "ACME-12", stdin=data)
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.rd / "raw" / "ticket" / "ACME-12.json").read_bytes(), data)
        rc, out = self.ingest("ticket", "--stdin", stdin=data)
        self.assertEqual(rc, 2)
        self.assertIn("--id", out)

    def test_design_frame_with_context_and_image(self):
        raw = self.raw("acme", "design_frame")
        img = self.drop / "frame.png"
        img.write_bytes(png(1440, 900))
        rc, out = self.ingest("design_image", str(img), "--id", "3544:5710")
        self.assertEqual(rc, 0, out)
        image = self.contract("design_image", "3544-5710")
        self.assertEqual((image["width"], image["height"], image["path"]), (1440, 900, "raw/design_image/3544-5710.png"))
        self.assertEqual(image["sha256"], contracts.sha256_file(self.rd / image["path"]))
        rc, out = self.ingest("design_frame", str(raw), "--id", "3544:5710", "--ctx", "file=file-ref")
        self.assertEqual(rc, 0, out)
        doc = self.contract("design_frame", "3544-5710")
        self.assertEqual(doc["source"]["file"], "file-ref")
        self.assertEqual(doc["frame"]["image"], {"path": "raw/design_image/3544-5710.png", "width": 1440, "height": 900, "scale": 1})
        self.assertEqual(doc["provenance"]["id"], "3544:5710")
        self.assertEqual(contracts.validate("design_frame", doc), [])

    def test_bad_png_is_rejected(self):
        bad = self.drop / "x.png"
        bad.write_bytes(b"PNG? no")
        rc, out = self.ingest("design_image", str(bad), "--id", "x")
        self.assertEqual(rc, 2)
        self.assertIn("not a PNG", out)
        self.assertFalse((self.rd / "contracts" / "design_image" / "x.json").exists())


class IngestFailureTests(IngestCase):
    def test_unknown_kind_lists_declared_kinds_and_samples(self):
        self.make_workspace({"tickets": "zeta", "scm": "zeta", "design": "zeta"})
        raw = self.raw("acme", "change_list")
        rc, out = self.ingest("change_list", str(raw), "--id", "x", "--repo", "api")
        self.assertEqual(rc, 2)
        self.assertIn("declares no kind 'change_list'", out)
        self.assertIn("ticket, change, pipeline, design_frame, design_image", out)
        self.assertIn("samples", out)
        rc, out = self.ingest("bogus", str(raw))
        self.assertEqual(rc, 2)
        self.assertIn("kinds: ticket", out)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.ingest("ticket", str(raw), "--format", "bogus")  # argparse usage error
        self.assertEqual(cm.exception.code, 2)

    def test_reserved_ctx_keys_are_refused(self):
        raw = self.raw("acme", "change")
        for key in ("repo", "scm_project", "id", "key", "number"):
            rc, out = self.ingest("change", str(raw), "--id", "api-42", "--repo", "api", "--ctx", f"{key}=evil")
            self.assertEqual(rc, 2, key)
            self.assertIn("reserved", out)
        self.assertFalse((self.rd / "contracts" / "change").exists())

    def test_unapproved_or_edited_profile_is_refused(self):
        raw = self.raw("acme", "ticket")
        path = self.home / "profiles" / "acme" / "profile.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")  # any byte change after approval
        rc, out = self.ingest("ticket", str(raw), "--id", "ACME-12")
        self.assertEqual(rc, 2)
        self.assertIn("changed since it was approved", out)
        self.assertIn("factory profile approve acme --user-said", out)
        (path.parent / "approved.json").unlink()
        rc, out = self.ingest("ticket", str(raw), "--id", "ACME-12")
        self.assertEqual(rc, 2)
        self.assertIn("is not approved", out)

    def test_catastrophic_regex_is_killed_by_the_mapping_timeout(self):
        import time
        self.edit_profile("acme", lambda d: d["kinds"]["ticket"]["map"].update(
            title={"regex_sub": [{"path": "head.title"}, "^(?:a|a)+$", "x"]}))
        data = json.loads(self.fixture("acme", "ticket", "1.json").read_text(encoding="utf-8"))
        data["head"]["title"] = "a" * 60 + "!"
        raw = self.drop / "t.json"
        raw.write_text(json.dumps(data), encoding="utf-8")
        ws = json.loads(core.workspace_path("ws").read_text(encoding="utf-8"))
        ws["limits"] = {"mapping_timeout_s": 2}
        core.write_json(core.workspace_path("ws"), ws)
        started = time.monotonic()
        rc, out = self.ingest("ticket", str(raw), "--id", "ACME-12")
        self.assertEqual(rc, 2, out)
        self.assertIn("exceeded the 2s limit", out)
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse((self.rd / "contracts" / "ticket").exists())

    def test_no_provider_configured(self):
        self.make_workspace({})
        raw = self.raw("acme", "ticket")
        rc, out = self.ingest("ticket", str(raw), "--id", "A-1")
        self.assertEqual(rc, 2)
        self.assertIn("providers.tickets", out)

    def test_mapping_errors_are_all_listed_and_nothing_is_written(self):
        raw = self.drop / "t.json"
        raw.write_text(json.dumps({"id": "A-1", "head": {"kind": "Bug"}, "files": [{"id": 1}]}), encoding="utf-8")
        rc, out = self.ingest("ticket", str(raw), "--id", "A-1")
        self.assertEqual(rc, 2)
        self.assertIn("contract field title required; expression", out)
        self.assertIn("contract field status required", out)
        self.assertFalse((self.rd / "contracts").exists())

    def test_raw_problems(self):
        rc, out = self.ingest("ticket", str(self.drop / "missing.json"))
        self.assertEqual(rc, 2)
        self.assertIn("not found", out)
        bad = self.drop / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        rc, out = self.ingest("ticket", str(bad))
        self.assertEqual(rc, 2)
        self.assertIn("not valid JSON", out)
        rc, out = self.ingest("ticket")
        self.assertEqual(rc, 2)

    def test_missing_run(self):
        rc, out = cli("ingest", "ghost", "--check")
        self.assertEqual(rc, 2)

    def test_id_collision_names_both_ids(self):
        raw = self.raw("acme", "design_frame")
        rc, out = self.ingest("design_frame", str(raw), "--id", "3544:5710", "--ctx", "file=f")
        self.assertEqual(rc, 0, out)
        rc, out = self.ingest("design_frame", str(raw), "--id", "3544/5710", "--ctx", "file=f")
        self.assertEqual(rc, 2)
        self.assertIn("'3544:5710'", out)
        self.assertIn("'3544/5710'", out)
        rc, out = self.ingest("design_frame", str(raw), "--id", "3544:5710", "--ctx", "file=f")
        self.assertEqual(rc, 0, out)  # the same id may be re-ingested


class StaleTests(IngestCase):
    def test_check_reports_stale_and_recovers_on_reingest(self):
        raw = self.raw("acme", "ticket")
        self.ingest("ticket", str(raw), "--id", "ACME-12")
        rc, out = self.ingest("--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK 1 contract(s) fresh", out)
        stored = self.rd / "raw" / "ticket" / "ACME-12.json"
        stored.write_text(stored.read_text(encoding="utf-8").replace("Bug", "Story"), encoding="utf-8")
        rc, out = self.ingest("--check")
        self.assertEqual(rc, 1)
        self.assertIn("stale contracts/ticket/ACME-12.json", out)
        self.assertIn("changed since ingest", out)
        rc, out = cli("--json", "ingest", "r1", "--check")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["stale"][0]["contract"], "contracts/ticket/ACME-12.json")
        rc, out = self.ingest("ticket", str(stored), "--id", "ACME-12")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.contract("ticket", "ACME-12")["is_bug"])
        self.assertEqual(self.ingest("--check")[0], 0)

    def test_missing_raw_is_stale_and_corrupt_contract_is_invalid(self):
        raw = self.raw("acme", "ticket")
        self.ingest("ticket", str(raw), "--id", "ACME-12")
        (self.rd / "raw" / "ticket" / "ACME-12.json").unlink()
        rc, out = self.ingest("--check")
        self.assertEqual((rc, "is missing" in out), (1, True))
        path = self.rd / "contracts" / "ticket" / "ACME-12.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.pop("title")
        path.write_text(json.dumps(doc), encoding="utf-8")
        rc, out = self.ingest("--check")
        self.assertEqual(rc, 1)
        self.assertIn("invalid contracts/ticket/ACME-12.json", out)

    def test_profile_change_is_advisory_only(self):
        raw = self.raw("acme", "ticket")
        self.ingest("ticket", str(raw), "--id", "ACME-12")
        path = self.home / "profiles" / "acme" / "profile.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["hints"]["ticket"] += " --verbose"
        path.write_text(json.dumps(doc), encoding="utf-8")
        profiles.seal(path.parent, "test: reapproved")  # the new version is approved again
        rc, out = self.ingest("--check")
        self.assertEqual(rc, 0, out)
        self.assertIn("note contracts/ticket/ACME-12.json: profile changed since ingest (advisory)", out)
        loaded, problems = contracts.load_contract("r1", "ticket", "ACME-12")
        self.assertEqual(problems, [])  # gates use the contract as is
        from factory.commands import gate
        warns = gate.profile_warnings("r1")
        self.assertEqual(len(warns), 1)
        self.assertIn("profile changed since ingest", warns[0])
        self.assertIn("re-ingest", warns[0])

    def test_load_contract_flags_a_changed_raw(self):
        raw = self.raw("acme", "ticket")
        self.ingest("ticket", str(raw), "--id", "ACME-12")
        (self.rd / "raw" / "ticket" / "ACME-12.json").write_text("{}", encoding="utf-8")
        _, problems = contracts.load_contract("r1", "ticket", "ACME-12")
        self.assertTrue(any("changed since ingest" in p for p in problems))


class NativeFormatTests(IngestCase):
    def frame(self, fidelity):
        return {"schema": "design_frame.v1", "fidelity": fidelity, "frame": {"id": "a:1", "name": "Mock"},
                "root": {"id": "a:1", "name": "Mock", "type": "frame", "box": {"x": 0, "y": 0, "w": 100, "h": 80}}}

    def native(self, doc, name="a-1.json"):
        p = self.drop / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def approx_frame(self):
        doc = self.frame("approx")
        doc["root"]["estimated"] = {"box": True}
        return doc

    def test_exact_native_frame_is_refused(self):
        rc, out = self.ingest("design_frame", str(self.native(self.frame("exact"))), "--id", "a:1", "--format", "native")
        self.assertEqual(rc, 2)
        self.assertIn("accepted only for fidelity approx", out)
        self.assertFalse((self.rd / "contracts" / "design_frame").exists())

    def test_approx_native_frame_is_accepted(self):
        rc, out = self.ingest("design_frame", str(self.native(self.approx_frame())), "--id", "a:1", "--format", "native")
        self.assertEqual(rc, 0, out)
        prov = self.contract("design_frame", "a-1")["provenance"]
        self.assertEqual((prov["profile"], prov["format"]), ("native", "native"))
        self.assertEqual(self.contract("design_frame", "a-1")["unknowns"], [])

    def test_native_is_refused_for_provider_kinds(self):
        for kind in ("ticket", "change", "change_list", "pipeline"):
            with self.subTest(kind):
                raw = self.native({"schema": f"{kind}.v1"}, f"{kind}.json")
                rc, out = self.ingest(kind, str(raw), "--id", "x1", "--format", "native")
                self.assertEqual(rc, 2)
                self.assertIn("--format native is not allowed", out)

    def test_a_native_marked_contract_of_a_provider_kind_is_invalid_for_gates(self):
        raw = self.raw("acme", "ticket")
        self.ingest("ticket", str(raw), "--id", "ACME-12")
        path = self.rd / "contracts" / "ticket" / "ACME-12.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["provenance"]["format"] = "native"
        path.write_text(json.dumps(doc), encoding="utf-8")
        _, problems = contracts.load_contract("r1", "ticket", "ACME-12")
        self.assertTrue(any("native" in p for p in problems), problems)

    def test_approx_rejects_unflagged_values(self):
        doc = self.frame("approx")
        rc, out = self.ingest("design_frame", str(self.native(doc)), "--id", "a:1", "--format", "native")
        self.assertEqual(rc, 2)
        self.assertIn("not flagged in `estimated`", out)
        self.assertFalse((self.rd / "contracts" / "design_frame").exists())
        doc["root"]["estimated"] = {"box": True}
        rc, out = self.ingest("design_frame", str(self.native(doc)), "--id", "a:1", "--format", "native")
        self.assertEqual(rc, 0, out)

    def test_native_needs_no_workspace_providers(self):
        self.make_workspace({})
        rc, out = self.ingest("design_frame", str(self.native(self.approx_frame())), "--id", "p9", "--format", "native")
        self.assertEqual(rc, 0, out)

    def test_native_violation(self):
        bad = self.approx_frame()
        bad["root"].pop("box")
        rc, out = self.ingest("design_frame", str(self.native(bad)), "--id", "a:1", "--format", "native")
        self.assertEqual(rc, 2)
        self.assertIn("box: required", out)


if __name__ == "__main__":
    unittest.main()
