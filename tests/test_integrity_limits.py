"""Major 8: threshold ceilings and style-map property families (over a design_frame contract)."""

from __future__ import annotations

import json
import unittest

from factory import design
from factory.commands import gate
from tests.design_support import write_design
from tests.integrity_support import IntegrityCase, core

ROOT = {"id": "1:2", "name": "Title", "type": "frame", "box": {"x": 7, "y": 9, "w": 320, "h": 48},
        "fills": [{"type": "solid", "color": "#333333", "opacity": 1}],
        "strokes": [{"type": "solid", "color": "#ff0000", "weight": 1}],
        "corner_radius": 4, "layout": {"mode": "horizontal", "gap": 8, "padding": {"t": 0, "r": 0, "b": 0, "l": 12}},
        "text": {"characters": "Title", "font_size": 16, "font_weight": 600, "line_height_px": 24}}
DOC = {"schema": "design_frame.v1", "fidelity": "exact", "frame": {"id": "1:2", "name": "Title"}, "root": ROOT,
       "unknowns": []}


def errs(expect: dict, tol=None, max_tol=2, approx=None, doc=DOC) -> list[str]:
    entry = {"id": "t", "selector": "h1", "expect": expect}
    if tol is not None:
        entry["tolerance_px"] = tol
    return design.style_map_errors({"node": "1:2", "entries": [entry]}, design.frame_facts(doc), "M", max_tol, approx)


class FamilyTests(unittest.TestCase):
    def test_values_from_their_own_family_pass(self):
        self.assertEqual(errs({"fontSize": 16, "fontWeight": 600, "lineHeight": "24px", "width": 320, "height": 48,
                               "paddingLeft": 12, "gap": 8, "borderRadius": 4, "color": "#333333",
                               "borderColor": "rgb(255, 0, 0)"}), [])

    def test_number_from_another_family_fails(self):
        self.assertTrue(errs({"fontSize": 600}))   # a fontWeight value
        self.assertTrue(errs({"fontSize": 99}))    # a number the frame does not have
        self.assertTrue(errs({"width": 16}))       # a fontSize value
        self.assertTrue(errs({"height": 7}))       # box.x is not a size
        self.assertTrue(errs({"fontWeight": 24}))

    def test_color_must_be_a_paint_of_the_node(self):
        self.assertEqual(errs({"backgroundColor": "#333333"}), [])
        self.assertTrue(errs({"color": "#00ff00"}))  # not a paint of the frame
        self.assertTrue(errs({"fontSize": "#333333"}))

    def test_unknown_property_has_no_family(self):
        self.assertIn("no design property", errs({"zIndex": 4})[0])

    def test_tolerance_ceiling(self):
        self.assertEqual(errs({"fontSize": 16}, tol=2), [])
        self.assertIn("exceeds limits.max_tolerance_px 2", errs({"fontSize": 16}, tol=3)[0])
        self.assertTrue(errs({"fontSize": 16}, tol=-1))


class CeilingTests(IntegrityCase):
    NODE = "1:2"

    def ui(self, max_diff_ratio=None, tol=1, check_tol=None, adapter=None):
        write_design(self.rid, self.NODE, ROOT)
        core.write_json(self.rd / "checks" / "s.json", {"node": self.NODE, "entries": [
            {"id": "t", "selector": "h1", "expect": {"fontSize": 16}, "tolerance_px": tol}]})
        visual = {"id": "CHK-1", "type": "visual", "repo": "React", "node": self.NODE, "path": "/x"}
        if max_diff_ratio is not None:
            visual["max_diff_ratio"] = max_diff_ratio
        style = {"id": "CHK-2", "type": "style", "repo": "React", "node": self.NODE, "path": "/x", "map": "checks/s.json"}
        if check_tol is not None:
            style["tolerance_px"] = check_tol
        if adapter is not None:
            visual["adapter"] = style["adapter"] = adapter
        self.acs([visual, style], [{"id": "AC-1", "ticket": "DEV-1", "text": "t", "kind": "ui", "checks": ["CHK-1", "CHK-2"]}])
        return gate.validate_acs(self.rid, core.run_load(self.rid), core.workspace_load("ws"))

    def test_default_ceilings(self):
        self.start()
        self.assertEqual(self.ui(max_diff_ratio=0.03), [])
        self.assertTrue(any("exceeds limits.max_diff_ratio 0.03" in e for e in self.ui(max_diff_ratio=0.2)))
        self.assertTrue(any("exceeds limits.max_tolerance_px 2" in e for e in self.ui(tol=5)))
        self.assertTrue(any("tolerance_px" in e for e in self.ui(check_tol=9)))

    def test_adapter_must_be_run_relative(self):
        self.start()
        self.assertEqual(self.ui(adapter="checks/adapter/open.json"), [])
        for bad in ("../x.json", "/etc/x.json", "C:/x.json", r"C:\x.json", r"checks\..\..\x.json", "", 3):
            errors = self.ui(adapter=bad)
            self.assertTrue(any("adapter must be a path relative" in e for e in errors), (bad, errors))

    def test_workspace_limits_override(self):
        self.ws["limits"] = {"max_diff_ratio": 0.25, "max_tolerance_px": 6}
        self.save_ws()
        self.start()
        self.assertEqual(self.ui(max_diff_ratio=0.2, tol=5), [])
        self.assertTrue(any("max_diff_ratio" in e for e in self.ui(max_diff_ratio=0.3)))


if __name__ == "__main__":
    unittest.main()
