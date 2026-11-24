"""Contract validators, approx rule, PNG header, safe ids, provenance and freshness."""

from __future__ import annotations

import copy
import json
import struct
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.provider_support import FIXTURES, ProviderCase  # noqa: E402

from factory import contracts  # noqa: E402
from factory.core import FactoryError  # noqa: E402

PROV = {"profile": "acme", "profile_digest": "sha256:" + "a" * 64, "raw_path": "raw/ticket/x.json",
        "raw_sha256": "b" * 64, "ingested_at": "2026-11-24T10:00:00Z"}


def golden(profile: str, kind: str, n: str = "1") -> dict:
    return json.loads((FIXTURES / profile / "samples" / kind / f"{n}.expected.json").read_text(encoding="utf-8"))


def png(w: int, h: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (contracts.PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" * (3 * w * h + h))) + chunk(b"IEND", b""))


class ValidatorTests(unittest.TestCase):
    def test_goldens_of_every_profile_validate(self):
        for profile in ("acme", "zeta"):
            for p in sorted((FIXTURES / profile / "samples").glob("*/*.expected.json")):
                kind = p.parent.name
                with self.subTest(profile=profile, kind=kind, sample=p.name):
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    self.assertEqual(contracts.validate(kind, doc, provenance=False), [])
                    self.assertEqual(contracts.validate(kind, {**doc, "provenance": PROV}), [])

    def test_missing_provenance_and_early_provenance(self):
        doc = golden("acme", "ticket")
        self.assertIn("provenance: required", contracts.validate("ticket", doc))
        self.assertTrue(contracts.validate("ticket", {**doc, "provenance": PROV}, provenance=False))

    def test_provenance_shape(self):
        doc = golden("acme", "ticket")
        bad = {**PROV, "raw_sha256": "xyz"}
        self.assertTrue(any("raw_sha256" in e for e in contracts.validate("ticket", {**doc, "provenance": bad})))
        extra = {**PROV, "source_hint": "fetch it", "id": "ACME-12", "inputs": {"jobs": {"raw_path": "raw/x", "raw_sha256": "c" * 64}}}
        self.assertEqual(contracts.validate("ticket", {**doc, "provenance": extra}), [])
        self.assertTrue(contracts.validate("ticket", {**doc, "provenance": {**PROV, "surprise": 1}}))

    def test_ticket_violations(self):
        base = golden("acme", "ticket")
        cases = {
            "title": lambda d: d.pop("title"),
            "is_bug": lambda d: d.update(is_bug="yes"),
            "unknown field": lambda d: d.update(summary="x"),
            "null required": lambda d: d.update(status=None),
            "attachments": lambda d: d.update(attachments=[{"id": "a", "name": "n"}]),
            "schema": lambda d: d.update(schema="ticket.v2"),
            "unknowns": lambda d: d.update(unknowns=[{"field": "x"}]),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                d = copy.deepcopy(base)
                mutate(d)
                self.assertTrue(contracts.validate("ticket", d, provenance=False), name)

    def test_ticket_key_must_match_profile_ref(self):
        doc = golden("acme", "ticket")
        refs = {"ticket_key": "(?P<key>ZED-\\d+)"}
        self.assertTrue(any("refs.ticket_key" in e for e in contracts.validate("ticket", doc, provenance=False, refs=refs)))
        self.assertEqual(contracts.validate("ticket", doc, provenance=False, refs={"ticket_key": "(?P<key>ACME-\\d+)"}), [])

    def test_change_violations(self):
        base = golden("acme", "change")
        cases = {
            "state": lambda d: d.update(state="DONE"),
            "number": lambda d: d.update(number="42"),
            "approvals": lambda d: d.update(approvals={"known": True}),
            "thread": lambda d: d["threads"][0].pop("resolvable"),
            "note line": lambda d: d["threads"][0]["notes"][0].update(line="10"),
            "pipeline status": lambda d: d["pipelines"][0].update(status="ok"),
            "draft": lambda d: d.pop("draft"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                d = copy.deepcopy(base)
                mutate(d)
                self.assertTrue(contracts.validate("change", d, provenance=False), name)

    def test_change_threads_absent_is_valid_unknown_not_none(self):
        d = golden("acme", "change")
        d.pop("threads")
        self.assertEqual(contracts.validate("change", d, provenance=False), [])

    def test_change_list_items_are_lite(self):
        d = golden("acme", "change_list")
        self.assertEqual(len(d["items"]), 2)
        d["items"][0]["threads"] = []
        self.assertTrue(contracts.validate("change_list", d, provenance=False))

    def test_pipeline_violations(self):
        base = golden("acme", "pipeline")
        for name, mutate in {"status": lambda d: d.update(status="green"), "job": lambda d: d["jobs"][0].pop("allow_failure"),
                             "jobs": lambda d: d.pop("jobs"), "sha": lambda d: d.pop("sha")}.items():
            with self.subTest(name):
                d = copy.deepcopy(base)
                mutate(d)
                self.assertTrue(contracts.validate("pipeline", d, provenance=False), name)

    def test_unknown_kind(self):
        self.assertIn("unknown kind", contracts.validate("nope", {})[0])

    def test_contract_fields_for_mappings(self):
        req, opt = contracts.contract_fields("change")
        self.assertIn("approvals", req)
        self.assertIn("threads", opt)
        self.assertNotIn("provenance", req + opt)


class DesignFrameTests(unittest.TestCase):
    def frame(self, fidelity="exact"):
        return {"schema": "design_frame.v1", "fidelity": fidelity, "frame": {"id": "1:1", "name": "F"},
                "root": {"id": "1:1", "name": "F", "type": "frame", "box": {"x": 0, "y": 0, "w": 100, "h": 50}},
                "unknowns": []}

    def test_exact_frame_valid_and_exact_may_not_carry_estimates(self):
        d = self.frame()
        self.assertEqual(contracts.validate("design_frame", d, provenance=False), [])
        d["root"]["estimated"] = {"box": True}
        self.assertTrue(any("exact" in e for e in contracts.validate("design_frame", d, provenance=False)))

    def test_approx_requires_every_number_and_color_flagged(self):
        d = self.frame("approx")
        errs = contracts.validate("design_frame", d, provenance=False)
        self.assertTrue(errs and all("not flagged" in e for e in errs))
        d["root"]["estimated"] = {"box": True}  # a parent path covers its children
        self.assertEqual(contracts.validate("design_frame", d, provenance=False), [])
        d["root"]["fills"] = [{"type": "solid", "color": "#112233", "opacity": 1}]
        errs = contracts.validate("design_frame", d, provenance=False)
        self.assertEqual(len(errs), 2)  # the color and the opacity
        d["root"]["estimated"].update({"fills.0.color": True, "fills.0.opacity": True})
        self.assertEqual(contracts.validate("design_frame", d, provenance=False), [])

    def test_approx_walks_children_and_text_numbers(self):
        d = self.frame("approx")
        d["root"]["estimated"] = {"box": True}
        d["root"]["children"] = [{"id": "1:2", "name": "T", "type": "text", "box": {"x": 1, "y": 2, "w": 3, "h": 4},
                                  "estimated": {"box": True}, "text": {"characters": "hi", "font_size": 12}}]
        errs = contracts.validate("design_frame", d, provenance=False)
        self.assertEqual(len(errs), 1)
        self.assertIn("text.font_size", errs[0])
        self.assertIn("1:2", errs[0])
        d["root"]["children"][0]["estimated"]["text.font_size"] = True
        self.assertEqual(contracts.validate("design_frame", d, provenance=False), [])

    def test_estimated_values_must_be_true(self):
        d = self.frame("approx")
        d["root"]["estimated"] = {"box": False}
        self.assertTrue(contracts.validate("design_frame", d, provenance=False))

    def test_corner_radii_shape_and_approx_flag(self):
        d = self.frame()
        d["root"]["corner_radii"] = [1, 2, 3, 4]
        self.assertEqual(contracts.validate("design_frame", d, provenance=False), [])
        d["root"]["corner_radii"] = [1, 2, 3]
        self.assertTrue(any("corner_radii" in e for e in contracts.validate("design_frame", d, provenance=False)))
        d["root"]["corner_radii"] = [1, "x", 3, 4]
        self.assertTrue(contracts.validate("design_frame", d, provenance=False))
        a = self.frame("approx")
        a["root"]["estimated"] = {"box": True}
        a["root"]["corner_radii"] = [1, 2, 3, 4]
        errs = contracts.validate("design_frame", a, provenance=False)
        self.assertEqual(len(errs), 4)
        self.assertTrue(all("corner_radii" in e for e in errs))
        a["root"]["estimated"]["corner_radii"] = True
        self.assertEqual(contracts.validate("design_frame", a, provenance=False), [])

    def test_node_rules(self):
        d = self.frame()
        d["root"]["type"] = "widget"
        self.assertTrue(contracts.validate("design_frame", d, provenance=False))
        d = self.frame()
        d["root"]["fills"] = [{"type": "solid", "color": "red"}]
        self.assertTrue(contracts.validate("design_frame", d, provenance=False))
        d = self.frame()
        d["root"]["opacity"] = 2
        self.assertTrue(contracts.validate("design_frame", d, provenance=False))
        d = self.frame()
        del d["root"]["box"]
        self.assertTrue(contracts.validate("design_frame", d, provenance=False))


class PngAndIdsTests(unittest.TestCase):
    def test_png_info(self):
        self.assertEqual(contracts.png_info(png(7, 3)), (7, 3))
        for bad in (b"", b"not a png at all, just text.", contracts.PNG_SIG + b"\x00" * 16,
                    contracts.PNG_SIG + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 0, 5) + b"\x00" * 5):
            with self.subTest(bad=bad[:12]), self.assertRaises(ValueError):
                contracts.png_info(bad)

    def test_design_image_contract(self):
        doc = {"schema": "design_image.v1", "path": "raw/design_image/a.png", "sha256": "x", "width": 4, "height": 3, "unknowns": []}
        self.assertEqual(contracts.validate("design_image", doc, provenance=False), [])
        self.assertTrue(contracts.validate("design_image", {**doc, "width": 0}, provenance=False))

    def test_safe_id(self):
        self.assertEqual(contracts.safe_id("3544:5710"), "3544-5710")
        self.assertEqual(contracts.safe_id("group/proj#42"), "group-proj-42")
        self.assertEqual(contracts.safe_id("ACME-12"), "ACME-12")
        self.assertEqual(contracts.safe_id("a b.c_d"), "a-b.c_d")
        for bad in ("", "   ", ".", ".."):
            with self.subTest(bad=bad), self.assertRaises(FactoryError):
                contracts.safe_id(bad)

    def test_collisions_name_both_ids(self):
        msgs = contracts.id_collisions(["a:b", "c", "a/b", "a:b"])
        self.assertEqual(len(msgs), 1)
        self.assertIn("'a:b'", msgs[0])
        self.assertIn("'a/b'", msgs[0])
        self.assertEqual(contracts.id_collisions(["x", "y"]), [])


class FreshnessTests(ProviderCase):
    def test_stale_reasons_and_load_contract(self):
        self.make_run("r1")
        rd = self.home / "runs" / "r1"
        raw = rd / "raw" / "ticket" / "T-1.json"
        raw.parent.mkdir(parents=True)
        raw.write_text("{}", encoding="utf-8")
        doc = golden("acme", "ticket")
        doc["provenance"] = {**PROV, "raw_path": "raw/ticket/T-1.json", "raw_sha256": contracts.sha256_file(raw)}
        cpath = contracts.contract_path(rd, "ticket", "T-1")
        cpath.parent.mkdir(parents=True)
        cpath.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(contracts.stale_reasons(rd, doc), [])
        got, problems = contracts.load_contract("r1", "ticket", "T-1")
        self.assertEqual((got["key"], problems), ("ACME-12", []))
        raw.write_text('{"changed": true}', encoding="utf-8")
        self.assertTrue(any("changed since ingest" in r for r in contracts.stale_reasons(rd, doc)))
        self.assertTrue(contracts.load_contract("r1", "ticket", "T-1")[1])
        raw.unlink()
        self.assertTrue(any("missing" in r for r in contracts.stale_reasons(rd, doc)))
        self.assertEqual(contracts.load_contract("r1", "ticket", "nope")[0], None)


if __name__ == "__main__":
    unittest.main()
