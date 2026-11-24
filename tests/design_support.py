"""Design fixtures: real PNGs and design_frame/design_image contracts written into a run (no provider anywhere)."""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory import contracts, core  # noqa: E402
from factory.commands import ingest  # noqa: E402

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def make_png(w: int = 8, h: int = 8, pixel=lambda x, y: (1, 2, 3, 255), ctype: int = 6) -> bytes:
    bpp = {2: 3, 6: 4}[ctype]
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y))[:bpp] for x in range(w)) for y in range(h))
    return b"".join([PNG_SIG, _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)),
                     _chunk(b"IDAT", zlib.compress(rows)), _chunk(b"IEND", b"")])


def default_root(node: str, w: int = 100, h: int = 50) -> dict:
    return {"id": node, "name": "Screen", "type": "frame", "box": {"x": 0, "y": 0, "w": w, "h": h},
            "fills": [{"type": "solid", "color": "#333333", "opacity": 1}],
            "children": [{"id": f"{node}.t", "name": "Title", "type": "text", "box": {"x": 4, "y": 4, "w": 40, "h": 14},
                          "text": {"characters": "Hello", "font_family": "Inter", "font_size": 12, "font_weight": 600}}]}


def write_design(run_id: str, node: str, root: dict | None = None, fidelity: str = "exact", version: str | None = "rev-1",
                 image: bytes | None = None, with_image: bool = True, source_file: str = "file-ref") -> dict:
    """Write raw/design_image + raw/design_frame (native) and ingest both. `root` replaces the default node tree."""
    rd = core.run_dir(run_id)
    safe = contracts.safe_id(node)
    if with_image:
        png = rd / "raw" / "design_image" / f"{safe}.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(image if image is not None else make_png(100, 50))
        ingest.ingest_raw(run_id, "design_image", png.relative_to(rd).as_posix(), ident=node, fmt="native")
    doc = {"schema": "design_frame.v1", "fidelity": fidelity, "frame": {"id": node, "name": "Screen"},
           "root": root if root is not None else default_root(node), "unknowns": []}
    source = {"file": source_file}
    if version:
        source["version"] = version
    doc["source"] = source
    raw = rd / "raw" / "design_frame" / f"{safe}.json"
    core.write_json(raw, doc)
    if fidelity == "approx":
        return ingest.ingest_raw(run_id, "design_frame", raw.relative_to(rd).as_posix(), ident=node, fmt="native")
    # exact frames come from a provider profile; the fixture writes the contract the way a profile ingest would
    ingest._frame_image(rd, safe, doc)
    doc["provenance"] = {"profile": "native", "profile_digest": "native", "id": node,
                         "raw_path": raw.relative_to(rd).as_posix(), "raw_sha256": contracts.sha256_file(raw),
                         "ingested_at": "2026-01-01T00:00:00Z"}
    problems = contracts.validate("design_frame", doc)
    assert not problems, problems
    out = contracts.contract_path(rd, "design_frame", safe)
    core.write_json(out, contracts.order_contract("design_frame", doc))
    return {"kind": "design_frame", "id": node, "contract": out.relative_to(rd).as_posix()}


def write_frame_files(run_dir: Path, node: str, root: dict | None = None, image: bytes | None = None,
                      fidelity: str = "exact") -> Path:
    """Collector fixtures: the frame contract and its image straight into a bare run dir (no run.json, no ingest)."""
    safe = contracts.safe_id(node)
    img_rel = f"raw/design_image/{safe}.png"
    png = Path(run_dir) / img_rel
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(image if image is not None else make_png(8, 6))
    doc = {"schema": "design_frame.v1", "fidelity": fidelity,
           "frame": {"id": node, "name": "Screen", "image": {"path": img_rel, "width": 8, "height": 6, "scale": 1}},
           "root": root if root is not None else default_root(node, 8, 6), "unknowns": []}
    core.write_json(Path(run_dir) / "contracts" / "design_frame" / f"{safe}.json", doc)
    return png


def shadow_root(node: str, w=448, h=768, radius=16, oy=8) -> dict:
    """A root node whose drop shadow widens the exported image (offset down by `oy`)."""
    return {"id": node, "name": "Card", "type": "frame", "box": {"x": 0, "y": 0, "w": w, "h": h},
            "effects": [{"type": "drop_shadow", "radius": radius, "offset": {"x": 0, "y": oy}}]}
