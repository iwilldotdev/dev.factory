"""Profile loading, digest, validity, lint, hints, the `profile` commands and `workspace check` providers."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.provider_support import FIXTURES, ProviderCase, cli  # noqa: E402

from factory import contracts, mapping, profiles  # noqa: E402
from factory.commands import workspace as ws_cmd  # noqa: E402
from factory.core import FactoryError  # noqa: E402


class ProfileTests(ProviderCase):
    def edit(self, name, fn):
        path = self.home / "profiles" / name / "profile.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        fn(doc)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        profiles.seal(path.parent, "test: edited")

    def test_load_and_roles(self):
        doc = profiles.load_profile("acme")
        self.assertEqual(profiles.roles_of(doc), ["tickets", "scm", "design"])
        self.assertEqual(profiles.declared_kinds(doc), list(contracts.KINDS))
        self.assertEqual(profiles.kind_inputs(doc, "pipeline"), ["main", "jobs"])
        self.assertEqual(profiles.kind_ext(doc, "design_image"), "png")

    def test_load_errors(self):
        with self.assertRaises(FactoryError) as cm:
            profiles.load_profile("nope")
        self.assertIn("acme", str(cm.exception))
        with self.assertRaises(FactoryError):
            profiles.load_profile("../etc")
        self.edit("zeta", lambda d: d.update(name="other"))
        with self.assertRaises(FactoryError):
            profiles.load_profile("zeta")

    def test_digest_is_canonical_and_ignores_samples(self):
        doc = profiles.load_profile("acme")
        d1 = profiles.digest(doc)
        self.assertRegex(d1, r"^sha256:[0-9a-f]{64}$")
        shuffled = dict(reversed(list(doc.items())))
        self.assertEqual(profiles.digest(shuffled), d1)
        self.fixture("acme", "ticket", "1.json").write_text("{}", encoding="utf-8")  # samples are outside profile.json
        self.assertEqual(profiles.digest(profiles.load_profile("acme")), d1)
        self.assertEqual(profiles.digest({**doc, "samples": [1]}), d1)
        doc2 = copy.deepcopy(doc)
        doc2["hints"]["ticket"] += " "
        self.assertNotEqual(profiles.digest(doc2), d1)

    def test_valid_profiles(self):
        for name in ("acme", "zeta"):
            with self.subTest(name):
                rep = profiles.check_profile(name)
                self.assertTrue(rep["valid"], rep["problems"])
                self.assertEqual(rep["lint"], [])
                for kind, k in rep["kinds"].items():
                    self.assertGreaterEqual(k["ok"], 1, kind)

    def test_every_sample_matches_its_golden(self):
        for name in ("acme", "zeta"):
            doc = profiles.load_profile(name)
            for kind in profiles.declared_kinds(doc):
                if kind == "design_image":
                    continue
                for p in profiles.sample_files(name, kind):
                    with self.subTest(profile=name, kind=kind, sample=p.name):
                        docs = {"main": profiles.load_raw_json(p)}
                        for n, ip in profiles.sample_inputs(p, profiles.kind_inputs(doc, kind)).items():
                            docs[n] = profiles.load_raw_json(ip)
                        got = profiles.map_docs(doc, kind, docs, profiles.sample_ctx(p))["contract"]
                        gp = p.with_name(f"{p.stem}.expected.json")
                        if gp.is_file():
                            self.assertEqual(got, json.loads(gp.read_text(encoding="utf-8")))

    def test_every_kind_has_a_golden_in_each_profile(self):
        for name, kinds in (("acme", contracts.KINDS), ("zeta", ("ticket", "change", "pipeline", "design_frame"))):
            for kind in kinds:
                if kind != "design_image":
                    self.assertTrue(self.fixture(name, kind, "1.expected.json").is_file(), (name, kind))

    def test_zeta_shapes_differ_from_acme_and_unknowns_are_populated(self):
        z = json.loads(self.fixture("zeta", "ticket", "1.expected.json").read_text(encoding="utf-8"))
        self.assertEqual(z["labels"], ["crash", "data"])  # array of pairs
        self.assertIn("1. **open the file**", z["description_md"])
        reasons = {u["reason"] for u in z["unknowns"] if u["field"] == "description_md"}
        self.assertEqual(reasons, {"rich-text mark 'u' ignored", "rich-text node type 'embed' descended without config"})
        self.assertEqual(z["comments"][0]["created"], "2023-11-14T22:13:20Z")
        a = json.loads(self.fixture("acme", "ticket", "1.expected.json").read_text(encoding="utf-8"))
        self.assertIn({"field": "links", "reason": "not mapped"}, a["unknowns"])
        a2 = json.loads(self.fixture("acme", "ticket", "2.expected.json").read_text(encoding="utf-8"))
        self.assertIn({"field": "labels", "reason": "null"}, a2["unknowns"])
        self.assertEqual(a2["description_md"], "")

    def test_required_field_null_is_an_error_naming_expression(self):
        doc = profiles.load_profile("acme")
        raw = profiles.load_raw_json(self.fixture("acme", "ticket", "1.json"))
        raw["head"]["title"] = None
        with self.assertRaises(mapping.MappingFailure) as cm:
            profiles.map_docs(doc, "ticket", {"main": raw}, {})
        self.assertIn("contract field title required; expression", cm.exception.errors[0])
        self.assertIn('{"path":"head.title"}', cm.exception.errors[0])
        self.assertIn("gave null", cm.exception.errors[0])

    def test_schema_violation_after_mapping_lists_every_one(self):
        def bad(d):
            d["kinds"]["ticket"]["map"]["is_bug"] = {"lit": "yes"}
            d["kinds"]["ticket"]["map"]["status"] = {"lit": 5}
        self.edit("acme", bad)
        doc = profiles.load_profile("acme")
        raw = profiles.load_raw_json(self.fixture("acme", "ticket", "1.json"))
        with self.assertRaises(mapping.MappingFailure) as cm:
            profiles.map_docs(doc, "ticket", {"main": raw}, {})
        self.assertEqual(len(cm.exception.errors), 2)

    def test_invalid_when_a_kind_has_no_working_sample(self):
        shutil.rmtree(self.home / "profiles" / "acme" / "samples" / "pipeline")
        rep = profiles.check_profile("acme")
        self.assertFalse(rep["valid"])
        self.assertTrue(any("pipeline: no samples" in p for p in rep["problems"]))

    def test_invalid_when_samples_fail_to_map(self):
        self.fixture("acme", "ticket", "1.json").write_text('{"id": "ACME-1"}', encoding="utf-8")
        self.fixture("acme", "ticket", "2.json").write_text('[]', encoding="utf-8")
        rep = profiles.check_profile("acme")
        self.assertFalse(rep["valid"])
        self.assertEqual(rep["kinds"]["ticket"]["ok"], 0)

    def test_one_good_sample_is_enough(self):
        self.fixture("acme", "ticket", "1.json").write_text('{"id": "ACME-1"}', encoding="utf-8")
        self.assertTrue(profiles.check_profile("acme")["valid"])  # sample 2 still maps

    def test_bad_png_sample_makes_design_image_invalid(self):
        self.fixture("acme", "design_image", "1.png").write_bytes(b"nope")
        self.assertFalse(profiles.check_profile("acme")["valid"])

    # ----- lint -----

    def lint(self, mutate) -> list[str]:
        doc = copy.deepcopy(profiles.load_profile("acme"))
        mutate(doc)
        return profiles.lint_profile(doc, "acme")

    def test_lint_clean(self):
        self.assertEqual(self.lint(lambda d: None), [])

    def test_lint_unknown_operator(self):
        errs = self.lint(lambda d: d["kinds"]["ticket"]["map"].update(title={"frob": {"path": "x"}}))
        self.assertTrue(any("unknown operator 'frob'" in e for e in errs), errs)

    def test_lint_hint_placeholders(self):
        errs = self.lint(lambda d: d["hints"].update(ticket="get {key} {bogus} {raw_path}"))
        self.assertTrue(any("bogus" in e for e in errs), errs)

    def test_lint_regex_does_not_compile_or_is_unsafe(self):
        errs = self.lint(lambda d: d["kinds"]["change"]["map"].update(title={"regex_sub": [{"path": "t"}, "(", ""]}))
        self.assertTrue(any("invalid regex" in e for e in errs), errs)
        errs = self.lint(lambda d: d["cli_write_patterns"].append("(unclosed"))
        self.assertTrue(any("cli_write_patterns[1]" in e for e in errs), errs)
        errs = self.lint(lambda d: d["kinds"]["change"]["map"].update(title={"regex_sub": [{"path": "t"}, "(a+)+", ""]}))
        self.assertTrue(any("repeat" in e for e in errs), errs)

    def test_lint_refs_need_named_groups(self):
        errs = self.lint(lambda d: d["refs"].update(ticket_key="[A-Z]+-\\d+"))
        self.assertTrue(any("named group" in e and "key" in e for e in errs), errs)
        errs = self.lint(lambda d: d["refs"].update(change_url="(?P<project>.+)/(\\d+)"))
        self.assertTrue(any("number" in e for e in errs), errs)
        errs = self.lint(lambda d: d["refs"].update(other="x"))
        self.assertTrue(any("unknown ref" in e for e in errs), errs)

    def test_lint_structure(self):
        self.assertTrue(any("not a field" in e for e in self.lint(lambda d: d["kinds"]["ticket"]["map"].update(summary={"path": "x"}))))
        self.assertTrue(any("required field 'title'" in e for e in self.lint(lambda d: d["kinds"]["ticket"]["map"].pop("title"))))
        self.assertTrue(any("role" in e for e in self.lint(lambda d: d.update(role="weird"))))
        self.assertTrue(any("outside the profile role" in e for e in self.lint(lambda d: d.update(role="tickets"))))
        self.assertTrue(any("unknown kind" in e for e in self.lint(lambda d: d["kinds"].update(bogus={}))))
        self.assertTrue(any("optional" in e for e in self.lint(lambda d: d["kinds"]["ticket"].update(optional=["title"]))))
        self.assertTrue(any("only json" in e for e in self.lint(lambda d: d["kinds"]["ticket"].update(ext="xml"))))
        self.assertTrue(any("top-level" in e for e in self.lint(lambda d: d.update(extra=1))))

    # ----- hints -----

    def test_render_hint(self):
        doc = profiles.load_profile("acme")
        ctx = {"key": "ACME-12", "raw_path": "C:\\runs\\r1\\raw\\ticket\\ACME-12.json"}
        self.assertEqual(profiles.render_hint(doc, "ticket", ctx), "acme-cli ticket get ACME-12 --out C:/runs/r1/raw/ticket/ACME-12.json")
        out = profiles.render_hint(doc, "change", {"number": 4, "raw_path": "/x/y.json"})
        self.assertEqual(out, "acme-cli change get <scm_project> 4 --with threads,approvals --out /x/y.json")
        doc["hints"]["ticket"] = "run {nothing} {key}"
        self.assertEqual(profiles.render_hint(doc, "ticket", {"key": "K-1"}), "run <nothing> K-1")
        self.assertIsNone(profiles.render_hint({"hints": {}}, "ticket", {}))

    # ----- workspace providers -----

    def test_provider_errors(self):
        ok = {"providers": {"tickets": "acme", "scm": "acme", "design": "zeta"}}
        self.assertEqual(profiles.provider_errors(ok), [])
        self.assertEqual(profiles.provider_errors({}), [])
        self.assertTrue(any("not found" in e for e in profiles.provider_errors({"providers": {"tickets": "ghost"}})))
        self.assertTrue(any("unknown role" in e for e in profiles.provider_errors({"providers": {"chat": "acme"}})))
        self.assertTrue(profiles.provider_errors({"providers": []}))
        self.edit("zeta", lambda d: d.update(role="tickets"))
        errs = profiles.provider_errors({"providers": {"scm": "zeta"}})
        self.assertTrue(any("role" in e for e in errs), errs)
        shutil.rmtree(self.home / "profiles" / "acme" / "samples" / "ticket")
        errs = profiles.provider_errors({"providers": {"tickets": "acme"}})
        self.assertTrue(any("invalid" in e for e in errs), errs)

    def test_workspace_check_reports_providers(self):
        errs = ws_cmd.check_workspace({"providers": {"tickets": "ghost"}})
        self.assertTrue(any(e.startswith("providers.tickets") for e in errs))
        errs = ws_cmd.check_workspace({"providers": {"tickets": "acme"}})
        self.assertFalse(any(e.startswith("providers") for e in errs))
        self.make_workspace({"tickets": "ghost"})
        rc, out = cli("workspace", "check", "ws")
        self.assertEqual(rc, 1)
        self.assertIn("providers.tickets", out)


class ProfileCommandTests(ProviderCase):
    def test_new_scaffolds_and_prints_workflow(self):
        rc, out = cli("profile", "new", "fresh", "--role", "scm")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.home / "profiles" / "fresh").exists())  # drafts only: profiles/ is the user's
        doc = json.loads((self.home / "profile-drafts" / "fresh" / "profile.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["schema"], doc["name"], doc["role"], doc["kinds"]), ("profile.v1", "fresh", "scm", {}))
        for kind in ("change", "change_list", "pipeline"):
            self.assertTrue((self.home / "profile-drafts" / "fresh" / "samples" / kind).is_dir())
        self.assertIn("never invent a sample", out)
        self.assertIn("profile approve <name> --digest", out)
        rc, out = cli("profile", "new", "fresh", "--role", "scm")
        self.assertEqual(rc, 2)
        rc, _ = cli("profile", "new", "bad name", "--role", "scm")
        self.assertEqual(rc, 2)
        rc, out = cli("profile", "check", "fresh", "--draft")
        self.assertEqual(rc, 2)
        self.assertIn("declares no kinds", out)

    # ----- drafts -> approve -----

    def draft_of(self, name="acme"):
        shutil.copytree(self.home / "profiles" / name, self.home / "profile-drafts" / name)
        (self.home / "profile-drafts" / name / "approved.json").unlink(missing_ok=True)

    def test_approve_promotes_a_valid_draft_with_approved_json_and_backup(self):
        self.draft_of()
        edited = self.home / "profile-drafts" / "acme" / "profile.json"
        doc = json.loads(edited.read_text(encoding="utf-8"))
        doc["notes"] = "second version"
        edited.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli("profile", "approve", "acme", "--digest", "x")  # argparse: --user-said is required
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli("profile", "approve", "acme", "--user-said", "yes")  # argparse: --digest is required
        dg = profiles.digest(profiles.load_profile("acme", True))
        rc, out = cli("profile", "approve", "acme", "--digest", "sha256:" + "0" * 64, "--user-said", "yes")
        self.assertEqual(rc, 2)
        self.assertIn("--digest", out)
        self.assertNotIn("notes", profiles.load_profile("acme"))  # a wrong digest promotes nothing
        rc, out = cli("profile", "approve", "acme", "--digest", dg, "--user-said", "yes, ship the acme profile")
        self.assertEqual(rc, 0, out)
        self.assertIn("guard fields now in force", out)
        self.assertIn("cli_write_patterns", out)
        self.assertIn("mcp_read_allow", out)
        self.assertIn("save_args", out)
        self.assertIn('+  "notes": "second version"', out)  # unified diff against the previous approved profile.json
        live = self.home / "profiles" / "acme"
        rec = json.loads((live / "approved.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["file_sha256"], hashlib.sha256((live / "profile.json").read_bytes()).hexdigest())
        self.assertEqual(rec["digest"], profiles.digest(profiles.load_profile("acme")))
        self.assertEqual(rec["user_said"], "yes, ship the acme profile")
        self.assertRegex(rec["approved_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(profiles.load_profile("acme")["notes"], "second version")
        backups = list((self.home / "profiles" / ".backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].name.startswith("acme-"))
        self.assertNotIn("notes", json.loads((backups[0] / "profile.json").read_text(encoding="utf-8")))
        self.assertIsNone(profiles.approval_error("acme"))
        self.assertEqual(profiles.profile_for({"providers": {"tickets": "acme"}}, "ticket")[0], "acme")

    def test_approve_refuses_an_invalid_draft(self):
        self.draft_of()
        shutil.rmtree(self.home / "profile-drafts" / "acme" / "samples" / "pipeline")
        before = (self.home / "profiles" / "acme" / "approved.json").read_bytes()
        dg = profiles.digest(profiles.load_profile("acme", True))
        rc, out = cli("profile", "approve", "acme", "--digest", dg, "--user-said", "ok")
        self.assertEqual(rc, 2)
        self.assertIn("is invalid", out)
        self.assertEqual((self.home / "profiles" / "acme" / "approved.json").read_bytes(), before)
        rc, out = cli("profile", "approve", "ghost", "--digest", dg, "--user-said", "ok")
        self.assertEqual(rc, 2)
        self.assertIn("no draft", out)

    def test_loading_for_gates_refuses_an_edited_profile(self):
        path = self.home / "profiles" / "acme" / "profile.json"
        path.write_text(path.read_text(encoding="utf-8").replace('"acme-cli', '"acme2-cli', 1), encoding="utf-8")
        with self.assertRaises(FactoryError) as cm:
            profiles.profile_for({"providers": {"tickets": "acme"}}, "ticket")
        self.assertIn("profile approve acme", str(cm.exception))
        self.assertIsNotNone(profiles.load_profile("acme"))  # `profile test|check` still read it
        errs = profiles.provider_errors({"providers": {"tickets": "acme"}})
        self.assertTrue(any("changed since it was approved" in e for e in errs), errs)

    def test_test_and_check_work_on_drafts_and_approved_read_only(self):
        self.draft_of()
        rc, out = cli("profile", "check", "acme", "--draft")
        self.assertEqual(rc, 0, out)
        rc, out = cli("profile", "test", "acme", "--draft", "--kind", "ticket", "--sample", "1")
        self.assertEqual(rc, 0, out)
        rc, out = cli("profile", "test", "acme", "--kind", "pipeline", "--write-golden")
        self.assertEqual(rc, 2)
        self.assertIn("read-only", out)

    def test_check_draft_prints_digest_and_guard_fields_and_warns(self):
        self.draft_of("zeta")
        rc, out = cli("profile", "check", "zeta", "--draft")
        self.assertEqual(rc, 0, out)
        self.assertIn("DIGEST " + profiles.digest(profiles.load_profile("zeta", True)), out)
        self.assertIn("mcp_read_allow:", out)
        self.assertIn("*_peek", out)
        self.assertIn("warning: cli_write_patterns is empty", out)

    def test_approve_new_profile_prints_new_profile_diff(self):
        self.draft_of("zeta")
        shutil.rmtree(self.home / "profiles" / "zeta")
        dg = profiles.digest(profiles.load_profile("zeta", True))
        rc, out = cli("profile", "approve", "zeta", "--digest", dg.removeprefix("sha256:"), "--user-said", "ok")
        self.assertEqual(rc, 0, out)
        self.assertIn("new profile", out)

    def test_lint_rejects_broad_mcp_read_allow(self):
        def lint_with(allow):
            doc = profiles.load_profile("acme")
            doc["mcp_read_allow"] = [allow]
            return profiles.lint_profile(doc, "acme")
        for bad in ("*", "**", "mcp__*", "mcp__srv__*", "mcp__*__*", "*e*", ""):
            errs = lint_with(bad)
            self.assertTrue(any("mcp_read_allow[0]" in e for e in errs), (bad, errs))
        for good in ("*_peek", "get_*", "list_issues"):
            self.assertEqual(lint_with(good), [], good)

    def test_check_exit_codes(self):
        rc, out = cli("profile", "check", "acme")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK profile acme", out)
        shutil.rmtree(self.home / "profiles" / "acme" / "samples" / "pipeline")
        rc, out = cli("profile", "check", "acme")
        self.assertEqual(rc, 2)
        self.assertIn("INVALID", out)
        rc, _ = cli("profile", "check", "ghost")
        self.assertEqual(rc, 2)

    def test_test_prints_contract_unknowns_and_sources(self):
        rc, out = cli("profile", "test", "acme", "--kind", "ticket", "--sample", "1")
        self.assertEqual(rc, 0, out)
        self.assertIn('"schema": "ticket.v1"', out)
        self.assertIn("unknowns:", out)
        self.assertIn("links (not mapped)", out)
        self.assertIn('title <- {"path":"head.title"}', out)

    def test_test_json_output(self):
        rc, out = cli("--json", "profile", "test", "zeta", "--kind", "change")
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(data["failed"], 0)
        self.assertEqual(data["results"][0]["contract"]["number"], 5)

    def test_golden_match_and_mismatch(self):
        rc, out = cli("profile", "test", "acme", "--golden")
        self.assertEqual(rc, 0, out)
        self.assertIn("golden: match", out)
        gp = self.fixture("acme", "change", "1.expected.json")
        gold = json.loads(gp.read_text(encoding="utf-8"))
        gold["title"] = "something else"
        gp.write_text(json.dumps(gold), encoding="utf-8")
        rc, out = cli("profile", "test", "acme", "--kind", "change", "--golden")
        self.assertEqual(rc, 2)
        self.assertIn("golden differs", out)
        self.assertIn("something else", out)
        gp.unlink()
        rc, out = cli("profile", "test", "acme", "--kind", "change", "--golden")
        self.assertEqual(rc, 2)
        self.assertIn("no golden file", out)

    def test_write_golden_roundtrip(self):
        shutil.copytree(self.home / "profiles" / "acme", self.home / "profile-drafts" / "acme")
        (self.home / "profile-drafts" / "acme" / "samples" / "pipeline" / "1.expected.json").unlink()
        rc, out = cli("profile", "test", "acme", "--draft", "--kind", "pipeline", "--write-golden")
        self.assertEqual(rc, 0, out)
        rc, out = cli("profile", "test", "acme", "--draft", "--kind", "pipeline", "--golden")
        self.assertEqual(rc, 0, out)

    def test_errors_exit_2(self):
        self.fixture("acme", "ticket", "1.json").write_text('{"id": "ACME-1"}', encoding="utf-8")
        rc, out = cli("profile", "test", "acme", "--kind", "ticket", "--sample", "1")
        self.assertEqual(rc, 2)
        self.assertIn("required; expression", out)
        rc, out = cli("profile", "test", "acme", "--kind", "ticket", "--sample", "99")
        self.assertEqual(rc, 2)
        rc, out = cli("profile", "test", "zeta", "--kind", "change_list")
        self.assertEqual(rc, 2)
        self.assertIn("declares no kind", out)


if __name__ == "__main__":
    unittest.main()
