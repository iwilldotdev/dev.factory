import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import pngcrop  # noqa: E402


def make_png(w, h, pixel, ctype=6):
    bpp = {2: 3, 6: 4}[ctype]
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y))[:bpp] for x in range(w)) for y in range(h))
    return b"".join([pngcrop.PNG_SIG, pngcrop._chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)),
                     pngcrop._chunk(b"IDAT", zlib.compress(rows)), pngcrop._chunk(b"IEND", b"")])


def shadow_node(w=448, h=768, radius=16, oy=8):
    return {"nodes": [{"id": "1:2", "size": {"width": w, "height": h},
                       "visual": {"effects": [{"type": "DROP_SHADOW", "visible": True, "radius": radius,
                                               "offset": {"x": 0, "y": oy}}]}}]}


class PngCropTests(unittest.TestCase):
    def test_margins_from_drop_shadow(self):
        self.assertEqual(pngcrop.effect_margins(shadow_node()["nodes"][0]["visual"]["effects"]), (16, 8, 16, 24))

    def test_box_from_effects_matches_reference_case(self):
        self.assertEqual(pngcrop.node_crop_box(shadow_node(), "1:2", 480, 800), (16, 8, 448, 768))

    def test_box_from_scaled_effects(self):
        self.assertEqual(pngcrop.node_crop_box(shadow_node(), "1:2", 960, 1600), (32, 16, 896, 1536))

    def test_box_from_absolute_bounds(self):
        doc = {"nodes": [{"id": "1:2", "absoluteBoundingBox": {"x": 100, "y": 50, "width": 40, "height": 30},
                        "absoluteRenderBounds": {"x": 90, "y": 45, "width": 60, "height": 50}}]}
        self.assertEqual(pngcrop.node_crop_box(doc, "1:2", 120, 100), (20, 10, 80, 60))

    def test_unchanged_when_sizes_match_or_bounds_missing_or_inconsistent(self):
        self.assertIsNone(pngcrop.node_crop_box(shadow_node(), "1:2", 448, 768))
        self.assertIsNone(pngcrop.node_crop_box({"nodes": [{"id": "1:2", "size": {"width": 10, "height": 10}}]}, "1:2", 12, 12))
        self.assertIsNone(pngcrop.node_crop_box({"nodes": [{"id": "1:2"}]}, "1:2", 12, 12))
        self.assertIsNone(pngcrop.node_crop_box(shadow_node(), "1:2", 480, 900))

    def test_cropped_figma_crops_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, out = Path(tmp) / "run", Path(tmp) / "out"
            (run / "figma").mkdir(parents=True)
            (run / "figma" / "1-2.json").write_text(json.dumps(shadow_node(w=4, h=3, radius=2, oy=1)))

            def inside(x, y):
                return (255, 0, 0, 255) if 2 <= x < 6 and 1 <= y < 4 else (0, 0, 0, 0)

            (run / "figma" / "1-2.png").write_bytes(make_png(8, 6, inside))
            res = pngcrop.cropped_figma(run, "1:2", "1-2", out)
            data = res.read_bytes()
            self.assertEqual(pngcrop.png_dims(data), (4, 3))
            img = pngcrop.read_png(data)
            self.assertTrue(all(bytes(r) == bytes([255, 0, 0, 255]) * 4 for r in img["rows"]))

    def test_cropped_figma_none_when_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            (run / "figma").mkdir(parents=True)
            (run / "figma" / "1-2.json").write_text(json.dumps(shadow_node(w=4, h=3, radius=2, oy=1)))
            (run / "figma" / "1-2.png").write_bytes(make_png(4, 3, lambda x, y: (1, 2, 3, 255)))
            self.assertIsNone(pngcrop.cropped_figma(run, "1:2", "1-2", Path(tmp) / "out"))
            (run / "figma" / "1-2.png").write_bytes(b"png")
            self.assertIsNone(pngcrop.cropped_figma(run, "1:2", "1-2", Path(tmp) / "out"))

    def test_filter_decoding(self):
        w, h = 3, 2
        raw = bytes([1, 10, 20, 30, 5, 5, 5, 1, 1, 1]) + bytes([2, 0, 0, 0, 1, 1, 1, 2, 2, 2])
        png = b"".join([pngcrop.PNG_SIG, pngcrop._chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)),
                        pngcrop._chunk(b"IDAT", zlib.compress(raw)), pngcrop._chunk(b"IEND", b"")])
        img = pngcrop.read_png(png)
        self.assertEqual(bytes(img["rows"][0]), bytes([10, 20, 30, 15, 25, 35, 16, 26, 36]))
        self.assertEqual(bytes(img["rows"][1]), bytes([10, 20, 30, 16, 26, 36, 18, 28, 38]))
