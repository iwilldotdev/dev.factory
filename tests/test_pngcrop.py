import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import pngcrop  # noqa: E402
from tests.design_support import make_png, shadow_root, write_frame_files  # noqa: E402


def frame(root):
    return {"schema": "design_frame.v1", "root": root}


class PngCropTests(unittest.TestCase):
    def test_margins_from_drop_shadow(self):
        self.assertEqual(pngcrop.effect_margins(shadow_root("1:2")["effects"]), (16, 8, 16, 24))

    def test_box_from_effects_matches_reference_case(self):
        self.assertEqual(pngcrop.node_crop_box(frame(shadow_root("1:2")), 480, 800), (16, 8, 448, 768))

    def test_box_from_scaled_effects(self):
        self.assertEqual(pngcrop.node_crop_box(frame(shadow_root("1:2")), 960, 1600), (32, 16, 896, 1536))

    def test_box_from_render_box(self):
        root = {"id": "1:2", "box": {"x": 100, "y": 50, "w": 40, "h": 30}, "render_box": {"x": 90, "y": 45, "w": 60, "h": 50}}
        self.assertEqual(pngcrop.node_crop_box(frame(root), 120, 100), (20, 10, 80, 60))

    def test_unchanged_when_sizes_match_or_bounds_missing_or_inconsistent(self):
        self.assertIsNone(pngcrop.node_crop_box(frame(shadow_root("1:2")), 448, 768))
        self.assertIsNone(pngcrop.node_crop_box(frame({"id": "1:2", "box": {"x": 0, "y": 0, "w": 10, "h": 10}}), 12, 12))
        self.assertIsNone(pngcrop.node_crop_box(frame({"id": "1:2"}), 12, 12))
        self.assertIsNone(pngcrop.node_crop_box({}, 12, 12))
        self.assertIsNone(pngcrop.node_crop_box(frame(shadow_root("1:2")), 480, 900))

    def test_cropped_design_crops_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, out = Path(tmp) / "run", Path(tmp) / "out"

            def inside(x, y):
                return (255, 0, 0, 255) if 2 <= x < 6 and 1 <= y < 4 else (0, 0, 0, 0)

            write_frame_files(run, "1:2", shadow_root("1:2", w=4, h=3, radius=2, oy=1), make_png(8, 6, inside))
            doc = __import__("json").loads((run / "contracts" / "design_frame" / "1-2.json").read_text())
            res = pngcrop.cropped_design(run, doc, out)
            data = res.read_bytes()
            self.assertEqual(pngcrop.png_dims(data), (4, 3))
            img = pngcrop.read_png(data)
            self.assertTrue(all(bytes(r) == bytes([255, 0, 0, 255]) * 4 for r in img["rows"]))

    def test_cropped_design_none_when_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            png = write_frame_files(run, "1:2", shadow_root("1:2", w=4, h=3, radius=2, oy=1), make_png(4, 3))
            doc = __import__("json").loads((run / "contracts" / "design_frame" / "1-2.json").read_text())
            self.assertIsNone(pngcrop.cropped_design(run, doc, Path(tmp) / "out"))
            png.write_bytes(b"png")
            self.assertIsNone(pngcrop.cropped_design(run, doc, Path(tmp) / "out"))
            self.assertIsNone(pngcrop.cropped_design(run, {"frame": {"id": "x"}}, Path(tmp) / "out"))

    def test_filter_decoding(self):
        w, h = 3, 2
        raw = bytes([1, 10, 20, 30, 5, 5, 5, 1, 1, 1]) + bytes([2, 0, 0, 0, 1, 1, 1, 2, 2, 2])
        png = b"".join([pngcrop.PNG_SIG, pngcrop._chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)),
                        pngcrop._chunk(b"IDAT", zlib.compress(raw)), pngcrop._chunk(b"IEND", b"")])
        img = pngcrop.read_png(png)
        self.assertEqual(bytes(img["rows"][0]), bytes([10, 20, 30, 15, 25, 35, 16, 26, 36]))
        self.assertEqual(bytes(img["rows"][1]), bytes([10, 20, 30, 16, 26, 36, 18, 28, 38]))
