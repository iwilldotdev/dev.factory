import json
import struct
import unittest
import zlib

from ops_helpers import FactoryCase

from factory.commands import figma


def png(w=2, h=3):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PAYLOAD = {
    "fileKey": "FK", "lastModified": "2026-01-01",
    "nodes": [{
        "id": "1:2", "name": "Drawer", "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 480, "height": 900},
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1, "a": 1}}],
        "cornerRadius": 8, "paddingLeft": 16, "paddingTop": 12, "itemSpacing": 8,
        "children": [{
            "id": "1:3", "name": "Title", "type": "TEXT", "characters": "Tarefas",
            "fills": [{"type": "SOLID", "color": {"r": 0.2, "g": 0.4, "b": 0.8}}],
            "style": {"fontSize": 20, "fontWeight": 600, "lineHeightPx": 28, "letterSpacing": 0.5},
        }],
    }],
}


class PackTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.make_run("ws-dev-1")
        self.write("runs/ws-dev-1/acs.json", {"schemaVersion": 1, "acs": [], "checks": [{"id": "C1", "type": "visual", "node": "1:2"}]})

    def test_missing_everything(self):
        errs = figma.check_pack("ws-dev-1")
        self.assertTrue(any("1-2.json" in e for e in errs))
        self.assertTrue(any("1-2.png" in e for e in errs))
        self.assertTrue(any("meta.json" in e for e in errs))

    def test_bad_png_and_json(self):
        self.write("runs/ws-dev-1/figma/1-2.json", "{nope")
        self.write("runs/ws-dev-1/figma/1-2.png", b"not a png")
        errs = figma.check_pack("ws-dev-1")
        self.assertTrue(any("not valid JSON" in e for e in errs))
        self.assertTrue(any("decodable" in e for e in errs))

    def test_ok(self):
        self.write("runs/ws-dev-1/figma/1-2.json", PAYLOAD)
        self.write("runs/ws-dev-1/figma/1-2.png", png())
        self.write("runs/ws-dev-1/figma/meta.json", {"FK": {"lastModified": "2026-01-01", "version": "9"}})
        self.assertEqual(figma.check_pack("ws-dev-1"), [])

    def test_meta_missing_filekey(self):
        self.write("runs/ws-dev-1/figma/1-2.json", PAYLOAD)
        self.write("runs/ws-dev-1/figma/1-2.png", png())
        self.write("runs/ws-dev-1/figma/meta.json", {"OTHER": {"version": "1"}})
        self.assertTrue(any("FK" in e for e in figma.check_pack("ws-dev-1")))

    def test_expect(self):
        self.write("runs/ws-dev-1/figma/1-2.json", PAYLOAD)
        out = figma.expect("ws-dev-1", "1:2", selector="[data-testid=drawer]")
        self.assertEqual(out.name, "1-2.skeleton.json")
        data = json.loads(out.read_text())
        self.assertEqual(data["node"], "1:2")
        by = {e["figma_name"]: e for e in data["entries"]}
        self.assertEqual(by["Drawer"]["selector"], "[data-testid=drawer]")
        self.assertEqual(by["Drawer"]["expect"]["backgroundColor"], "#ffffff")
        self.assertEqual(by["Drawer"]["expect"]["borderRadius"], 8)
        self.assertEqual(by["Drawer"]["expect"]["width"], 480)
        self.assertEqual(by["Drawer"]["expect"]["gap"], 8)
        t = by["Title"]
        self.assertEqual(t["selector"], "TODO")
        self.assertEqual(t["expect"]["fontSize"], 20)
        self.assertEqual(t["expect"]["color"], "#3366cc")
        self.assertEqual(t["expect"]["lineHeight"], 28)
        self.assertEqual(t["expect"]["fontWeight"], 600)
        self.assertEqual(t["tolerance_px"], 1)


if __name__ == "__main__":
    unittest.main()
