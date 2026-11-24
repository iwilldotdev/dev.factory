"""Design pillar over contracts: exact frames (acme/zeta mapping), `design add|check|expect`, approx frames
(estimated flags, tolerances, review decision) and the rule that approx never counts as exact."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from design_support import make_png, write_design  # noqa: E402
from provider_support import ProviderCase, cli, core  # noqa: E402

from factory import contracts, design  # noqa: E402
from factory.commands import gate  # noqa: E402

NODE = "3544:5710"


class ExactFlowTests(ProviderCase):
    """Real mapping of the synthetic acme/zeta samples, then the consumers of the contracts."""

    def setUp(self):
        super().setUp()
        self.make_workspace()
        self.rd = self.make_run()
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": NODE, "source_file": "file-ref"}])

    def checks(self, checks):
        core.write_json(self.rd / "acs.json", {"schemaVersion": 1, "acs": [], "checks": checks})

    def ingest_samples(self, profile="acme", node=NODE, ctx="file-ref"):
        rc, out = cli("ingest", "ws-acme-12", "design_image", str(self.fixture(profile, "design_image", "1.png")), "--id", node)
        self.assertEqual(rc, 0, out)
        rc, out = cli("ingest", "ws-acme-12", "design_frame", str(self.fixture(profile, "design_frame", "1.json")),
                      "--id", node, "--profile", profile, "--ctx", f"file={ctx}")
        self.assertEqual(rc, 0, out)

    def test_check_prints_fetch_items_from_profile_hints(self):
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 1)
        base = (self.home / "runs/ws-acme-12").as_posix()
        self.assertIn(f"FETCH design_image {NODE} -> {base}/raw/design_image/3544-5710.png | "
                      f"acme-cli design render file-ref {NODE} --scale 1 --out {base}/raw/design_image/3544-5710.png", out)
        self.assertIn(f"FETCH design_frame {NODE} -> {base}/raw/design_frame/3544-5710.json | "
                      f"acme-cli design node file-ref {NODE} --out {base}/raw/design_frame/3544-5710.json", out)

    def test_fetch_json_shape_and_placeholder_without_source_file(self):
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": NODE}])
        rc, out = cli("--json", "design", "check", "ws-acme-12")
        self.assertEqual(rc, 1)
        first = json.loads(out)["fetch"][0]
        self.assertEqual((first["kind"], first["id"]), ("design_image", NODE))
        self.assertIn("<file>", first["hint"])
        self.assertNotIn("tool", first)

    def test_complete_pack_passes_with_no_fetch(self):
        self.ingest_samples()
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("FETCH", out)
        frame = self.read(self.rd, "contracts/design_frame/3544-5710.json")
        self.assertEqual(frame["frame"]["image"]["path"], "raw/design_image/3544-5710.png")
        self.assertEqual(frame["source"]["version"], "rev-77")

    def test_second_profile_shape_gives_the_same_consumers(self):
        self.ingest_samples("zeta", "9:1", "zeta-file")
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": "9:1"}])
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 0, out)
        doc, problems = contracts.load_contract("ws-acme-12", "design_frame", "9:1")
        self.assertEqual(problems, [])
        by_key, _ = design.frame_facts(doc)
        self.assertIn(20.0, by_key["fontSize"])
        self.assertIn("Welcome", by_key["text"])

    def test_exact_needs_source_version(self):
        self.ingest_samples()
        path = self.rd / "contracts/design_frame/3544-5710.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        del doc["source"]["version"]
        core.write_json(path, doc)
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 1)
        self.assertIn("exact frame needs source.version", out)

    def test_changed_raw_makes_contracts_stale(self):
        self.ingest_samples()
        raw = self.rd / "raw/design_image/3544-5710.png"
        raw.write_bytes(raw.read_bytes() + b"x")
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 1)
        self.assertIn("changed since ingest", out)

    def test_frame_ingested_before_image_has_no_image(self):
        rc, out = cli("ingest", "ws-acme-12", "design_frame", str(self.fixture("acme", "design_frame", "1.json")), "--id", NODE,
                      "--ctx", "file=file-ref")
        self.assertEqual(rc, 0, out)
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 1)
        self.assertIn("no design_image contract", out)
        self.assertIn("no frame.image", out)

    def test_legacy_key_is_refused_with_the_migrate_op(self):
        legacy = "file" + "Key"
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": NODE, legacy: "FK"}])
        errors = gate.validate_acs("ws-acme-12", core.run_load("ws-acme-12"), core.workspace_load("ws"))
        self.assertTrue(any(f"key {legacy!r} was renamed `source_file`" in e and '"op":"replace_text"' in e for e in errors), errors)

    def test_frame_alias_for_node(self):
        self.ingest_samples()
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "frame": NODE}])
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 0, out)

    def test_expect_skeleton_projects_node_properties_and_skips_hidden(self):
        self.ingest_samples()
        rc, out = cli("design", "expect", "ws-acme-12", NODE, "--selector", "[data-x]")
        self.assertEqual(rc, 0, out)
        sk = self.read(self.rd, "checks/style/3544-5710.skeleton.json")
        by = {e["design_name"]: e for e in sk["entries"]}
        self.assertEqual(sk["node"], NODE)
        self.assertEqual(by["Empty list"]["expect"], {"width": 1440, "height": 900, "borderRadius": 8,
                                                      "backgroundColor": "#ffffff"})
        self.assertEqual(by["Empty list"]["selector"], "[data-x]")
        self.assertEqual(by["Title"]["expect"], {"fontSize": 24, "lineHeight": 32, "fontWeight": 600, "fontFamily": "Inter",
                                                 "color": "#111111", "width": 320, "height": 32, "text": "Nothing here yet"})
        self.assertNotIn("Hidden hint", by)

    def test_style_map_checked_over_contract_facts(self):
        self.ingest_samples()
        doc, _ = contracts.load_contract("ws-acme-12", "design_frame", NODE)
        facts = design.frame_facts(doc)
        ok = {"entries": [{"id": "t", "selector": "h1", "expect": {"fontSize": 24, "color": "#111111", "width": 320}}]}
        self.assertEqual(design.style_map_errors(ok, facts, "M"), [])
        bad = {"entries": [{"id": "t", "selector": "h1", "expect": {"fontSize": 25, "color": "#123456", "height": 4}}]}
        errors = design.style_map_errors(bad, facts, "M")
        self.assertEqual(len(errors), 3, errors)  # height 4 belongs to the hidden node: not a fact


class ApproxTests(ProviderCase):
    ID = "mockup"

    def setUp(self):
        super().setUp()
        self.make_workspace()
        self.rd = self.make_run(decisions=[], blockers=[])
        self.png = self.tmp / "mockup.png"
        self.png.write_bytes(make_png(200, 100, lambda x, y: (240, 240, 240, 255)))
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": self.ID, "fidelity": "approx"},
                     {"id": "C2", "type": "style", "repo": "api", "node": self.ID, "fidelity": "approx",
                      "map": "checks/style/m.json"}])

    def checks(self, checks):
        core.write_json(self.rd / "acs.json", {"schemaVersion": 1, "acs": [], "checks": checks})

    def add(self, *extra):
        return cli("design", "add", "ws-acme-12", str(self.png), "--name", self.ID, "--source-hint", "converted by hand", *extra)

    def write_regions(self, estimated_ok=True):
        text = {"id": "t1", "name": "Title", "type": "text", "box": {"x": 10, "y": 10, "w": 80, "h": 20},
                "text": {"characters": "Hello", "font_size": 20, "font_weight": 600, "line_height_px": 28},
                "fills": [{"type": "solid", "color": "#202020"}]}
        if estimated_ok:
            text["estimated"] = {"box": True, "text.font_size": True, "text.font_weight": True, "text.line_height_px": True,
                                 "fills": True}
        doc = self.read(self.rd, "raw/design_frame/mockup.json")
        doc["root"]["children"] = [text]
        core.write_json(self.rd / "raw/design_frame/mockup.json", doc)
        return cli("ingest", "ws-acme-12", "design_frame", "raw/design_frame/mockup.json", "--id", self.ID, "--format", "native")

    def test_add_normalizes_png_and_creates_the_skeleton(self):
        rc, out = self.add()
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.rd / "raw/design_image/mockup.png").is_file())
        img = self.read(self.rd, "contracts/design_image/mockup.json")
        self.assertEqual((img["width"], img["height"]), (200, 100))
        self.assertEqual(img["provenance"]["source_hint"], "converted by hand")
        frame = self.read(self.rd, "contracts/design_frame/mockup.json")
        self.assertEqual(frame["fidelity"], "approx")
        self.assertEqual(frame["frame"]["image"]["path"], "raw/design_image/mockup.png")
        self.assertEqual(frame["root"]["box"], {"x": 0, "y": 0, "w": 200, "h": 100})
        self.assertEqual(frame["root"]["estimated"], {"box": True})
        rc, out = cli("ingest", "ws-acme-12", "--check")
        self.assertEqual(rc, 0, out)

    def test_add_rejects_a_non_png_and_exact_fidelity(self):
        jpg = self.tmp / "mockup.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 40)
        rc, out = cli("design", "add", "ws-acme-12", str(jpg), "--name", "m")
        self.assertEqual(rc, 2)
        self.assertIn("convert the image to PNG", out)
        rc, out = self.add("--fidelity", "exact")
        self.assertEqual(rc, 2)
        self.assertIn("approximate frames only", out)
        self.assertFalse((self.rd / "contracts/design_frame/mockup.json").exists())

    def test_native_frame_rejects_unflagged_numbers_and_accepts_flagged_ones(self):
        self.assertEqual(self.add()[0], 0)
        rc, out = self.write_regions(estimated_ok=False)
        self.assertEqual(rc, 2)
        self.assertIn("is not flagged in `estimated`", out)
        rc, out = self.write_regions(estimated_ok=True)
        self.assertEqual(rc, 0, out)

    def test_exact_frame_cannot_carry_estimated_values(self):
        self.assertEqual(self.add()[0], 0)
        doc = self.read(self.rd, "raw/design_frame/mockup.json")
        doc["fidelity"] = "exact"
        core.write_json(self.rd / "raw/design_frame/mockup.json", doc)
        rc, out = cli("ingest", "ws-acme-12", "design_frame", "raw/design_frame/mockup.json", "--id", self.ID, "--format", "native")
        self.assertEqual(rc, 2)
        self.assertIn("accepted only for fidelity approx", out)  # native never produces an exact frame
        self.assertTrue(any("exact frame cannot list `estimated`" in p for p in contracts.validate("design_frame", doc, provenance=False)))

    def test_approx_never_satisfies_an_exact_requirement(self):
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.write_regions()[0], 0)
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": self.ID}])  # fidelity defaults to exact
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 1)
        self.assertIn("approx never satisfies an exact requirement", out)
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": self.ID, "fidelity": "approx"}])
        rc, out = cli("design", "check", "ws-acme-12")
        self.assertEqual(rc, 0, out)
        self.assertIn("approx mockup", out)

    def style_errors(self, expect, approx):
        doc, problems = contracts.load_contract("ws-acme-12", "design_frame", self.ID)
        self.assertEqual(problems, [])
        lim = design.approx_limits({}) if approx else None
        return design.style_map_errors({"entries": [{"id": "t", "selector": "h", "expect": expect}]},
                                       design.frame_facts(doc), "M", 4, lim)

    def test_tolerances_pass_and_fail(self):
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.write_regions()[0], 0)
        # font size 20 estimated: +-2 ok, 3 fails; width 80: +-4 ok, 6 fails; color #202020: deltaE 6 ok, far fails
        self.assertEqual(self.style_errors({"fontSize": 22, "width": 84, "color": "#262626"}, True), [])
        self.assertEqual(len(self.style_errors({"fontSize": 23}, True)), 1)
        self.assertEqual(len(self.style_errors({"width": 86}, True)), 1)
        self.assertEqual(len(self.style_errors({"color": "#505050"}, True)), 1)
        self.assertEqual(len(self.style_errors({"fontWeight": 601}, True)), 1)  # weight is never loosened
        # the same values against exact tolerances fail
        self.assertEqual(len(self.style_errors({"fontSize": 22, "width": 84, "color": "#262626"}, False)), 3)

    def test_delta_e_cie76(self):
        self.assertAlmostEqual(design.delta_e76("#ffffff", "#ffffff"), 0)
        self.assertAlmostEqual(design.delta_e76("#000000", "#ffffff"), 100, delta=0.5)
        self.assertLess(design.delta_e76("#202020", "#262626"), 6)
        self.assertGreater(design.delta_e76("#202020", "#505050"), 6)

    def test_plan_ceilings_rise_only_for_approx_checks(self):
        ws = core.workspace_load("ws")
        run = core.run_load("ws-acme-12")
        self.assertEqual(self.add()[0], 0)  # the frame contract is approx: that, not the check's claim, lifts the ceilings
        approx = {"id": "C1", "type": "visual", "repo": "api", "node": self.ID, "fidelity": "approx", "max_diff_ratio": 0.08,
                  "tolerance_px": 4}
        self.checks([approx])
        errors = gate.validate_acs("ws-acme-12", run, ws)
        self.assertFalse([e for e in errors if "max_diff_ratio" in e or "tolerance_px" in e], errors)
        self.checks([{k: v for k, v in approx.items() if k != "fidelity"}])
        errors = gate.validate_acs("ws-acme-12", run, ws)
        self.assertTrue(any("exceeds limits.max_diff_ratio 0.03" in e for e in errors), errors)
        self.assertTrue(any("tolerance_px 4" in e for e in errors), errors)
        self.checks([{**approx, "max_diff_ratio": 0.5}])
        self.assertTrue(any("exceeds limits.max_diff_ratio 0.1" in e for e in gate.validate_acs("ws-acme-12", run, ws)))

    def test_approx_check_on_an_exact_frame_is_a_plan_error_with_exact_ceilings(self):
        ws = core.workspace_load("ws")
        run = core.run_load("ws-acme-12")
        write_design("ws-acme-12", "ex:1", fidelity="exact")
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": "ex:1", "fidelity": "approx",
                      "max_diff_ratio": 0.08, "tolerance_px": 4}])
        errors = gate.validate_acs("ws-acme-12", run, ws)
        self.assertTrue(any("exact contract" in e and "approx ceilings apply only to approx frames" in e for e in errors), errors)
        self.assertTrue(any("exceeds limits.max_diff_ratio 0.03" in e for e in errors), errors)  # exact ceilings
        self.assertTrue(any("tolerance_px 4" in e for e in errors), errors)
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": "ex:1", "max_diff_ratio": 0.02, "tolerance_px": 1}])
        self.assertFalse([e for e in gate.validate_acs("ws-acme-12", run, ws) if "approx" in e])

    def test_workspace_limits_approx_override_and_validation(self):
        ws = core.workspace_load("ws")
        ws["limits"] = {"approx": {"px": 6, "font_size_px": 3}}
        self.assertEqual(design.approx_limits(ws), {"px": 6, "color_delta_e": 6, "font_size_px": 3, "max_diff_ratio": 0.1})
        self.assertEqual(gate.workspace_limits(ws)["approx"]["px"], 6)
        self.assertEqual(gate.workspace_limits({})["approx"], design.APPROX_DEFAULTS)

    def test_review_gate_needs_a_user_design_review_per_approx_frame(self):
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.write_regions()[0], 0)
        run = core.run_load("ws-acme-12")
        errors = design.review_errors("ws-acme-12", run)
        self.assertEqual(len(errors), 1)
        self.assertIn("needs a user design review", errors[0])
        rc, out = cli("run", "decide", "ws-acme-12", "mockup: design reviewed", "--design-review", self.ID)
        self.assertEqual(rc, 2)  # by agent
        self.assertIn("--by user", out)
        rc, out = cli("run", "decide", "ws-acme-12", "other: design reviewed", "--by", "user", "--user-said", "ok",
                      "--design-review", "nope")
        self.assertEqual(rc, 2)
        rc, out = cli("run", "decide", "ws-acme-12", "mockup: design reviewed", "--by", "user", "--user-said", "looks right",
                      "--design-review", self.ID)
        self.assertEqual(rc, 0, out)
        self.assertEqual(design.review_errors("ws-acme-12", core.run_load("ws-acme-12")), [])
        # a re-ingested (changed) frame invalidates the earlier review
        doc = self.read(self.rd, "raw/design_frame/mockup.json")
        doc["root"]["children"][0]["text"]["characters"] = "Changed"
        core.write_json(self.rd / "raw/design_frame/mockup.json", doc)
        self.assertEqual(cli("ingest", "ws-acme-12", "design_frame", "raw/design_frame/mockup.json", "--id", self.ID,
                             "--format", "native")[0], 0)
        errors = design.review_errors("ws-acme-12", core.run_load("ws-acme-12"))
        self.assertTrue(errors and "predates a change" in errors[0], errors)

    def test_gate_review_includes_the_design_review_requirement(self):
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.write_regions()[0], 0)
        with mock.patch.object(gate, "proof_errors", return_value=[]),                 mock.patch.object(gate, "review_status", return_value=([], [])):
            errors = gate.EVALUATORS["review"]("ws-acme-12", core.run_load("ws-acme-12"), core.workspace_load("ws"))
            self.assertTrue(any("needs a user design review" in e for e in errors), errors)
            cli("run", "decide", "ws-acme-12", "mockup: design reviewed", "--by", "user", "--user-said", "ok",
                "--design-review", self.ID)
            self.assertEqual(gate.EVALUATORS["review"]("ws-acme-12", core.run_load("ws-acme-12"), core.workspace_load("ws")), [])

    def test_exact_frames_need_no_design_review(self):
        self.checks([{"id": "C1", "type": "visual", "repo": "api", "node": NODE}])
        cli("ingest", "ws-acme-12", "design_image", str(self.fixture("acme", "design_image", "1.png")), "--id", NODE)
        cli("ingest", "ws-acme-12", "design_frame", str(self.fixture("acme", "design_frame", "1.json")), "--id", NODE,
            "--ctx", "file=file-ref")
        self.assertEqual(design.review_errors("ws-acme-12", core.run_load("ws-acme-12")), [])
        self.assertEqual(design.description_lines("ws-acme-12"), [])

    def test_ship_description_lists_approx_frames(self):
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.write_regions()[0], 0)
        lines = design.description_lines("ws-acme-12")
        self.assertIn("## Design fidelity: approximate", lines)
        self.assertTrue(any("mockup" in l for l in lines))

    def test_expect_for_approx_defaults_tolerance_to_the_approx_px(self):
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.write_regions()[0], 0)
        rc, out = cli("design", "expect", "ws-acme-12", self.ID)
        self.assertEqual(rc, 0, out)
        sk = self.read(self.rd, "checks/style/mockup.skeleton.json")
        self.assertTrue(all(e["tolerance_px"] == 4 for e in sk["entries"]))

    def test_workspace_check_rejects_a_bad_limits_approx(self):
        from factory.commands import workspace
        ws = core.workspace_load("ws")
        ws["limits"] = {"approx": {"px": -1, "mystery": 2}}
        core.write_json(core.workspace_path("ws"), ws)
        rc, out = cli("workspace", "check", "ws")
        self.assertNotEqual(rc, 0)
        self.assertIn("limits.approx.px must be a number >= 0", out)
        self.assertIn("limits.approx.mystery: unknown", out)



class CornerRadiiTests(unittest.TestCase):
    def test_radii_project_to_per_corner_keys_and_uniform_border_radius(self):
        node = {"id": "1", "name": "n", "type": "frame", "box": {"x": 0, "y": 0, "w": 1, "h": 1}, "corner_radii": [1, 2, 3, 4]}
        props = design.node_props(node)
        self.assertEqual(props["borderTopLeftRadius"], [1])
        self.assertEqual(props["borderBottomLeftRadius"], [4])
        self.assertNotIn("borderRadius", props)
        node["corner_radii"] = [6, 6, 6, 6]
        props = design.node_props(node)
        self.assertEqual(props["borderRadius"], [6])
        self.assertEqual(props["borderTopRightRadius"], [6])
        by_key, _ = design.frame_facts({"root": node})
        self.assertIn(6.0, by_key["borderBottomRightRadius"])
        node["corner_radii"] = [6, "x", 6, 6]
        self.assertNotIn("borderTopLeftRadius", design.node_props(node))

if __name__ == "__main__":
    unittest.main()
