"""The design pillar: contracts, approx rules, plan rules, crop boxes, tolerances, design reviews and freshness."""
import json
import unittest

from dev_factory import design
from dev_factory.util import FactoryError
from tests.helpers import adapter_scenario, lesson
from tests.m4_support import M4Case, png

IMG = (124, 64)


def node(nid, name, kind, x, y, w, h, **more):
    return {"id": nid, "name": name, "type": kind, "box": {"x": x, "y": y, "w": w, "h": h}, **more}


def flag_all(root: dict) -> dict:
    for _, n in design.walk(root):
        n["estimated"] = {p: True for p in design.numbers_and_colors(n)}
    return root


def frame_data(fid="F-1", fidelity="exact", version="v1", root=None) -> dict:
    """A 124x64 export of a 100x40 frame whose shadow margin is 12 px; two children."""
    root = root or node("root", "Card", "frame", 12, 12, 100, 40, effects=[{"type": "drop_shadow", "color": "#000000", "offset": {"x": 0, "y": 0}, "radius": 12, "spread": 0}],
                        children=[node("n-1", "Fill", "shape", 12, 12, 100, 40, fills=[{"type": "solid", "color": "#c81e1e"}], corner_radius=4),
                                  node("n-2", "Title", "text", 12, 12, 100, 28, text={"characters": "No invoices yet", "font_family": "Arial", "font_size": 20,
                                                                                    "font_weight": 600, "line_height_px": 28, "color": "#1F2933"})])
    return {"id": fid, "name": "Empty state", "fidelity": fidelity, "source": {"file": "design-file", "version": version if fidelity == "exact" else None,
                                                                            "fetched_at": "2026-11-24T10:00:00Z"},
            "image": fid, "unknowns": [], "root": flag_all(root) if fidelity == "approx" else root}


class DesignCase(M4Case):
    def put_png(self, name: str = "F-1.png", size=IMG) -> str:
        path = self.base / name
        path.write_bytes(png(*size))
        return str(path)

    def ingest_design(self, run, data=None, size=IMG, image_id=None):
        data = data or frame_data()
        image = self.put_png(f"{data['id']}.png", size)
        self.ok("ingest", "design_image", "--raw", image, "--stdin", stdin=json.dumps({"id": image_id or data["image"]}))
        raw = self.write(f"raw-{data['id']}.json", json.dumps({"source": "design tool"}), root=self.base)
        return self.ok("ingest", "design_frame", "--raw", str(raw), "--stdin", stdin=json.dumps(data))

    def style_map(self, run, entries=None, frame="F-1"):
        path = run / "checks" / "style" / f"{frame}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"frame": frame, "entries": entries or [{"selector": "[data-test=title]", "node": "n-2", "tolerance_px": 1,
                                                                            "expect": {"fontSize": 20, "color": "#1f2933", "text": "No invoices yet"}}]}), encoding="utf-8")
        return f"checks/style/{frame}.json"

    def design_plan(self, run, visual=None, style=None, frame="F-1", **top):
        visual = {"id": "CHK-1", "type": "visual", "repo": "web", "ac": ["AC-1"], "frame": frame, "node": "n-1", "url": "/index.html", **(visual or {})}
        style = {"id": "CHK-2", "type": "style", "repo": "web", "ac": ["AC-1"], "frame": frame, "url": "/index.html", **(style or {})}
        style.setdefault("map", f"checks/style/{frame}.json" if (run / "checks" / "style" / f"{frame}.json").is_file() else self.style_map(run, frame=frame))
        return {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [{"id": "AC-1", "text": "The empty state matches the design", "source": "ticket:description",
                                                                      "design": [frame]}], "checks": [visual, style], **top}

    def ready(self, **kw):
        run = self.branch()
        self.ingest_design(run, kw.pop("data", None))
        self.up()
        self.write_plan(run, self.design_plan(run, **kw))
        return run


class ContractTests(DesignCase):
    def test_png_size_reads_the_header_and_refuses_anything_else(self):
        self.assertEqual(design.png_size(png(7, 5)), (7, 5))
        with self.assertRaises(FactoryError):
            design.png_size(b"GIF89a....")

    def test_the_image_contract_takes_its_size_from_the_png(self):
        run = self.branch()
        self.ingest_design(run)
        env = json.loads((run / "contracts" / "design_image" / "F-1.json").read_text(encoding="utf-8"))
        self.assertEqual(env["data"], {"id": "F-1", "width": 124, "height": 64, "scale": 1})
        self.assertEqual(env["source"], "agent")
        out = self.fails("ingest", "design_image", "--raw", self.put_png("x.png"), "--stdin", stdin=json.dumps({"id": "X", "width": 5}))
        self.assertIn("the PNG is 124 pixels", out)

    def test_an_exact_frame_needs_a_source_version_and_every_field_is_validated(self):
        run = self.branch()
        bad = frame_data()
        bad["source"]["version"] = None
        bad["unknowns"] = None
        bad["root"]["children"][0]["fills"][0]["color"] = "red"
        bad["root"]["children"][1]["bogus"] = 1
        raw = self.write("r.json", "{}", root=self.base)
        out = self.fails("ingest", "design_frame", "--raw", str(raw), "--stdin", stdin=json.dumps(bad))
        for part in ("data.source.version", "data.unknowns", "#rrggbb", "unknown key"):
            self.assertIn(part, out)
        self.assertFalse((run / "contracts" / "design_frame").exists())

    @lesson("L-186")
    def test_an_approx_frame_must_flag_every_number_and_color_as_estimated(self):
        """L-186 / decision 17: an approx frame is accepted from the agent only when each number and color is flagged estimated."""
        run = self.branch()
        data = frame_data("A-1", "approx")
        del data["root"]["children"][0]["estimated"]["fills.0.color"]
        raw = self.write("r.json", "{}", root=self.base)
        out = self.fails("ingest", "design_frame", "--raw", str(raw), "--stdin", stdin=json.dumps(data))
        self.assertIn("fills.0.color is a number or color of an approx frame and must be flagged in estimated", out)
        exact = frame_data()
        exact["root"]["estimated"] = {"box.x": True}
        self.assertIn("only an approx frame lists estimated values", self.fails("ingest", "design_frame", "--raw", str(raw), "--stdin", stdin=json.dumps(exact)))
        self.assertTrue(run.exists())

    def test_design_add_stores_an_approx_image_and_a_skeleton_that_ingests_once_completed(self):
        run = self.branch()
        out = self.ok("design", "add", self.put_png("mock.png", (200, 100)), "--name", "Mock", "--id", "mock-1")
        self.assertIn("factory ingest design_frame", out)
        skeleton = json.loads((run / "input" / "design_frame-mock-1.json").read_text(encoding="utf-8"))
        self.assertEqual((skeleton["fidelity"], skeleton["root"]["box"]), ("approx", {"x": 0, "y": 0, "w": 200, "h": 100}))
        self.ok("ingest", "design_frame", "--raw", str(run / "raw" / "design_image" / "mock-1.png"), "--contract", str(run / "input" / "design_frame-mock-1.json"))
        self.assertEqual(self.ok("design", "check").strip().split()[0], "every")

    def test_an_approx_frame_never_replaces_an_exact_frame_with_the_same_id(self):
        run = self.branch()
        self.ingest_design(run)
        raw = self.write("r.json", "{}", root=self.base)
        out = self.fails("ingest", "design_frame", "--raw", str(raw), "--stdin", stdin=json.dumps(frame_data("F-1", "approx")))
        self.assertIn("never replaces an exact one", out)


class GeometryTests(unittest.TestCase):
    @lesson("L-55")
    def test_the_crop_uses_the_render_box_and_the_effect_margins(self):
        """L-55: the design PNG starts at the root's render box, so a shadow margin shifts the crop of every node."""
        frame, img = frame_data(), {"width": 124, "height": 64}
        self.assertEqual(design.render_box(frame["root"]), {"x": 0, "y": 0, "w": 124, "h": 64})
        self.assertEqual(design.crop_box(frame, "n-1", img), ([12, 12, 100, 40], []))
        self.assertEqual(design.margins({"effects": [{"type": "drop_shadow", "offset": {"x": 4, "y": 8}, "radius": 10, "spread": 2}]}), (8.0, 4.0, 16.0, 20.0))
        frame["root"]["render_box"] = {"x": 2, "y": 2, "w": 124, "h": 64}
        self.assertEqual(design.crop_box(frame, "n-1", img)[0], [10, 10, 100, 40])
        _, problems = design.crop_box(frame, "n-1", {"width": 90, "height": 64})
        self.assertIn("does not fit", problems[-1] if len(problems) else "")

    def test_masks_are_limited_to_two_percent_each_and_need_a_reason(self):
        crop = [0, 0, 100, 40]
        self.assertEqual(design.mask_problems([{"x": 0, "y": 0, "w": 10, "h": 8, "reason": "date"}], crop, "m"), [])
        self.assertIn("at most 2%", design.mask_problems([{"x": 0, "y": 0, "w": 20, "h": 8, "reason": "date"}], crop, "m")[0])
        self.assertIn("needs x, y, w, h numbers and a reason", design.mask_problems([{"x": 0, "y": 0, "w": 1, "h": 1}], crop, "m")[0])

    def test_the_property_table_gives_the_facts_of_a_node(self):
        title = frame_data()["root"]["children"][1]
        facts = design.facts(title)
        self.assertEqual((facts["fontSize"], facts["color"], facts["text"], facts["lineHeight"], facts["width"]), (20, "#1f2933", "No invoices yet", 28, 100))
        self.assertEqual(design.facts(frame_data()["root"]["children"][0])["backgroundColor"], "#c81e1e")
        self.assertEqual(design.facts({"box": {"x": 0, "y": 0, "w": 1, "h": 1}, "corner_radii": [2, 2, 2, 2]})["borderRadius"], 2)


class PlanRuleTests(DesignCase):
    @lesson("L-186")
    def test_an_ac_with_a_design_frame_needs_a_visual_and_a_style_check_per_frame(self):
        """L-186: gate plan needs one visual and one style check per frame named by an AC, against valid contracts."""
        run = self.branch()
        self.up()
        plan = self.design_plan(run)
        self.write_plan(run, plan)
        self.assertIn("design frame 'F-1' has no contract", self.failing("plan"))
        self.ingest_design(run)
        plan["checks"] = plan["checks"][:1]
        self.write_plan(run, plan)
        self.assertIn("needs at least one style check naming this AC and the frame", self.failing("plan"))
        self.write_plan(run, self.design_plan(run))
        self.ok("gate", "plan")

    @lesson("L-187", "L-54")
    def test_a_style_value_that_is_not_a_fact_of_the_frame_is_refused(self):
        """L-187: expected style values come from the frame contract through the property table, never from the model; L-54: an empty text proves nothing."""
        run = self.ready()
        self.style_map(run, [{"selector": "h1", "node": "n-2", "expect": {"fontSize": 22, "text": "", "borderWidth": 2}}])
        out = self.failing("plan")
        self.assertIn("expect.fontSize: 22 is not a fact of node n-2 in the frame contract (the frame says 20)", out)
        self.assertIn("an empty text proves nothing", out)
        self.assertIn("expect.borderWidth: 2 is not a fact of node n-2", out)
        self.ok("design", "expect", "F-1", "--node", "n-2", "--selector", "h1")
        facts = json.loads((run / "checks" / "style" / "F-1.json").read_text(encoding="utf-8"))["entries"][-1]["expect"]
        self.assertEqual(facts["text"], "No invoices yet")

    @lesson("L-188")
    def test_ratio_and_tolerance_ceilings_follow_the_frame_fidelity_and_sit_in_the_config_hash(self):
        """L-188: max_diff_ratio and tolerance_px above their ceilings fail the plan; the limits are part of the locked config."""
        run = self.ready(visual={"max_diff_ratio": 0.05})
        self.assertIn("max_diff_ratio: 0.05 is above the exact ceiling 0.03", self.failing("plan"))
        self.write_plan(run, self.design_plan(run, style={"map": self.style_map(run, [{"selector": "h1", "node": "n-2", "tolerance_px": 3, "expect": {"fontSize": 20}}])}))
        self.assertIn("tolerance_px: above the ceiling 2 for exact frames", self.failing("plan"))
        self.style_map(run)
        self.write_plan(run, self.design_plan(run))
        self.ok("gate", "plan")
        self.write("factory.toml", (self.proj / "factory.toml").read_text(encoding="utf-8") + "\n[limits]\nmax_diff_ratio = 0.5\n")
        self.assertIn("config changed after plan", "\n".join(self.gate("verify")[1]))

    def test_masks_over_two_percent_and_a_node_outside_the_frame_fail_the_plan(self):
        run = self.ready(visual={"masks": [{"x": 0, "y": 0, "w": 50, "h": 20, "reason": "avatar"}]})
        self.assertIn("covers 25.0% of the area; a mask may cover at most 2%", self.failing("plan"))
        self.write_plan(run, self.design_plan(run, visual={"node": "ghost"}))
        self.assertIn("has no node 'ghost'", self.failing("plan"))
        self.write_plan(run, self.design_plan(run, visual={"max_diff_ratio": 0.02, "masks": [{"x": 0, "y": 0, "w": 10, "h": 8, "reason": "date"}]}))
        self.ok("gate", "plan")

    def test_a_check_frame_must_be_in_the_design_list_of_its_acs(self):
        run = self.ready()
        plan = self.design_plan(run)
        plan["acs"][0]["design"] = ["F-1"]
        plan["checks"][0]["frame"] = "F-9"
        self.write_plan(run, plan)
        self.assertIn("'F-9' is not in the design list of AC-1", self.failing("plan"))

    @lesson("L-190")
    def test_a_ticket_design_link_and_the_screenshot_flag_are_plan_data_the_gate_enforces(self):
        """L-190: a design link needs a frame or an answered question; evidence.screenshots needs a screenshot check; no other session is needed."""
        run = self.ready()
        raw = run / "raw" / "ticket" / "ABC-1.json"
        self.ingest_ticket(run, links=[{"rel": "design", "target": "https://design.example/file?node=9-9"}])
        self.ok("gate", "intake")
        self.write_plan(run, self.design_plan(run))
        self.assertIn("no ingested frame in the plan covers it and no answered question has --ref design:https://design.example/file?node=9-9", self.failing("plan"))
        self.ok("ask", "Is the design link obsolete?", "--ref", "design:https://design.example/file?node=9-9")
        self.ok("answer", "Q-1", "--user-said", "yes, it is the old mock")
        self.ok("gate", "plan")
        self.assertTrue(raw.exists())
        plan = self.design_plan(run, evidence={"screenshots": True})
        self.write_plan(run, plan)
        out = "\n".join(self.cli("plan", "amend", "--reason", "screenshots wanted")[1:])
        self.assertIn("needs a screenshot check", out)


class UiCheckTests(DesignCase):
    def locked(self, **kw):
        run = self.ready(**kw)
        self.ok("gate", "plan")
        self.fix()
        return run

    @lesson("L-55", "L-56")
    def test_visual_passes_the_crop_the_tolerances_and_the_masks_to_the_runner_and_keeps_the_images(self):
        """L-55: Python computes the crop box; L-56: the app, design and diff images are the before/after record."""
        self.results({"visual": {"details": {"diff_ratio": 0.004, "max_diff_ratio": 0.03, "crop": [12, 12, 100, 40], "size": [100, 40]}}})
        run = self.locked(visual={"masks": [{"x": 0, "y": 0, "w": 10, "h": 8, "reason": "date"}], "fit": True, "wait_for": "#ready", "settle_ms": 100})
        self.ok("verify")
        call = [c for c in self.runner_calls() if c["cmd"] == "visual"][-1]["argv"]
        self.assertEqual(call[call.index("--crop") + 1], "12,12,100,40")
        self.assertEqual((call[call.index("--max-diff-ratio") + 1], call[call.index("--fidelity") + 1]), ("0.03", "exact"))
        self.assertEqual(call[call.index("--mask") + 1], "0,0,10,8")
        self.assertIn("--fit", call)
        self.assertEqual((call[call.index("--wait-for") + 1], call[call.index("--settle-ms") + 1]), ("#ready", "100"))
        self.assertEqual(call[call.index("--viewport") + 1], "124x64")
        rec = [e for e in self.evidence(run) if e["check"] == "CHK-1"][0]
        self.assertEqual(rec["details"]["masks"], [{"x": 0, "y": 0, "w": 10, "h": 8, "reason": "date"}])
        self.assertEqual(rec["details"]["crop"], [12, 12, 100, 40])
        self.assertTrue(any(a.endswith("app.png") for a in rec["artifacts"]))
        self.assertTrue((run / [a for a in rec["artifacts"] if a.endswith("app.png")][0]).is_file())

    @lesson("L-51")
    def test_a_visual_or_style_check_can_reach_a_state_through_a_state_spec_or_inline_actions(self):
        """L-51: a state_spec file or an actions list is executed by the runner before capture or probe, so drawers and toasts are reachable."""
        run = self.ready()
        spec = run / "checks" / "open.json"
        spec.write_text(json.dumps({"actions": [{"click": "#open"}]}), encoding="utf-8")
        plan = self.design_plan(run, visual={"state_spec": "checks/open.json"}, style={"actions": [{"click": "#more"}, {"wait": 50}]})
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.fix()
        self.ok("verify")
        calls = {c["cmd"]: c["argv"] for c in self.runner_calls() if c["cmd"] in ("visual", "style")}
        self.assertEqual(calls["visual"][calls["visual"].index("--state-spec") + 1], (run / "checks" / "open.json").as_posix())
        self.assertTrue(calls["style"][calls["style"].index("--state-spec") + 1].endswith("actions.json"))
        bad = self.design_plan(run, style={"actions": [{"teleport": "#x"}]})
        self.write_plan(run, bad)
        self.assertIn("actions: must be a non-empty list of one-key objects", " ".join(self.cli("plan", "amend", "--reason", "x")[1:]))

    def test_a_failing_visual_check_is_a_failure_with_the_runner_reason(self):
        self.results({"visual": {"exit": 1, "summary": "diff ratio 0.2100 above 0.03", "details": {"diff_ratio": 0.21}}})
        run = self.locked()
        self.assertEqual(self.cli("verify")[0], 1)
        rec = [e for e in self.evidence(run) if e["check"] == "CHK-1"][0]
        self.assertEqual((rec["result"], rec["kind"], rec["reason"]), ("fail", "assert", "diff ratio 0.2100 above 0.03"))

    @lesson("L-186")
    def test_an_approx_frame_uses_the_approx_tolerances_and_needs_the_users_design_review_at_the_review_gate(self):
        """L-186: tolerances follow the frame's fidelity; gate review needs the user's design review of each approx frame, bound to the frame."""
        data = frame_data("F-1", "approx")
        run = self.locked(data=data, visual={"max_diff_ratio": 0.09})
        self.ok("verify")
        style = [c for c in self.runner_calls() if c["cmd"] == "style"][-1]["argv"]
        self.assertEqual([style[style.index(k) + 1] for k in ("--px", "--delta-e", "--font-px", "--fidelity")], ["4", "6", "2", "approx"])
        self.ok("gate", "verify")
        from dev_factory import gates, project
        proj = project.discover(str(self.proj))
        st = proj.run("ABC-1").state()
        reasons = gates.extra_reasons(proj.run("ABC-1"), proj, st, "review")
        self.assertTrue(any(r.startswith("approx frame F-1: no user design review") for r in reasons), reasons)
        self.ok("design", "review", "F-1", "--user-said", "close enough to the mock")
        st = proj.run("ABC-1").state()
        self.assertFalse([r for r in gates.extra_reasons(proj.run("ABC-1"), proj, st, "review") if r.startswith("approx frame")])
        self.assertEqual(st["design_reviews"][-1]["user_said"], "close enough to the mock")

    def test_a_frame_changed_after_its_review_needs_a_new_review(self):
        from dev_factory import designflow, project
        run = self.ready(data=frame_data("F-1", "approx"))
        self.ok("design", "review", "F-1", "--user-said", "fine")
        proj = project.discover(str(self.proj))
        self.assertEqual(designflow.review_reasons(proj.run("ABC-1"), proj.run("ABC-1").state(), json.loads((run / "plan.json").read_text(encoding="utf-8"))), [])
        data = frame_data("F-1", "approx")
        data["root"]["name"] = "Card v2"
        raw = self.write("r.json", "{}", root=self.base)
        self.ok("ingest", "design_frame", "--raw", str(raw), "--stdin", stdin=json.dumps(data))
        reasons = designflow.review_reasons(proj.run("ABC-1"), proj.run("ABC-1").state(), json.loads((run / "plan.json").read_text(encoding="utf-8")))
        self.assertEqual(len(reasons), 1)

    @lesson("L-56")
    def test_a_screenshot_check_is_an_artifact_that_names_no_ac_and_captures_before_images_at_the_base(self):
        """L-56: screenshot is artifact-only, never satisfies an AC; reproduce captures before images on base, verify after images at HEAD."""
        run = self.branch()
        self.up()
        plan = {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [{"id": "AC-1", "text": "Value is 2", "source": "ticket:description"}],
                "checks": [{"id": "CHK-1", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": f'"{self.py}" check.py 2', "expect_pattern": "checks passed: VALUE=2"},
                           {"id": "CHK-2", "type": "screenshot", "repo": "web", "url": "/index.html", "selector": "#box"}], "evidence": {"screenshots": True}}
        self.write_plan(run, plan)
        plan["checks"][1]["ac"] = ["AC-1"]
        self.write_plan(run, plan)
        self.assertIn("never satisfies an AC", self.failing("plan"))
        del plan["checks"][1]["ac"]
        self.write_plan(run, plan)
        self.ok("gate", "plan")
        self.ok("reproduce")
        before = [e for e in self.evidence(run) if e["mode"] == "reproduce"]
        self.assertEqual((before[0]["check"], before[0]["result"]), ("CHK-2", "reproduced"))
        self.fix()
        self.ok("verify")
        after = [e for e in self.evidence(run) if e["check"] == "CHK-2" and e["mode"] == "verify"][0]
        self.assertEqual(after["result"], "pass")
        self.assertTrue(any(a.endswith("shot.png") for a in after["artifacts"]))
        self.assertNotEqual(before[0]["artifacts"], after["artifacts"])
        self.assertEqual(self.gate("verify")[0], 0)

    @property
    def py(self):
        from tests.helpers import PY
        return PY

    @lesson("L-190")
    def test_the_screenshot_flag_needs_a_screenshot_check_in_every_ui_repo(self):
        """L-190: evidence.screenshots is plan data; the gate demands the check from the plan itself."""
        run = self.branch()
        self.up()
        plan = self.default_plan()
        plan["evidence"] = {"screenshots": True}
        self.write_plan(run, plan)
        self.assertIn("evidence.screenshots is true, so repo web needs a screenshot check", self.failing("plan"))


class FreshnessTests(DesignCase):
    def test_refresh_without_an_adapter_needs_the_users_word_and_ship_reads_it_after_the_last_lock(self):
        from dev_factory import designflow, project
        run = self.ready()
        self.ok("gate", "plan")
        proj = project.discover(str(self.proj))
        plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
        before = designflow.ship_reasons(proj.run("ABC-1"), proj.run("ABC-1").state(), plan)
        self.assertIn("run factory design refresh F-1 after the last plan lock", before[0])
        self.assertIn("no [providers.design_frame] adapter", self.fails("design", "refresh", "F-1"))
        self.ok("design", "refresh", "F-1", "--user-said", "the design file is still at v1")
        self.assertEqual(designflow.ship_reasons(proj.run("ABC-1"), proj.run("ABC-1").state(), plan), [])

    @lesson("L-09", "L-122")
    def test_a_moved_source_version_through_the_adapter_needs_a_plan_amend(self):
        """L-09 / L-122: refresh re-acquires an exact frame; a moved version or raw is reported and needs `plan amend` and a new verify."""
        moved = frame_data(version="v2")
        self.configure(extra=f'\n[providers.design_frame]\nfetch = {json.dumps(adapter_scenario(self, stdout=json.dumps({"contract": moved})))}\n')
        run = self.ready()
        self.ok("gate", "plan")
        out = self.fails("design", "refresh", "F-1", code=1)
        self.assertIn("moved at its source (version v2)", out)
        self.assertTrue(self.events(run)[-1]["changed"] or any(e["type"] == "design_refresh" and e["changed"] for e in self.events(run)))
        self.assertIn("a contract was acquired or replaced after the plan lock", "\n".join(self.gate("verify")[1]))


if __name__ == "__main__":
    unittest.main()
