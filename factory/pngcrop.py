"""Crop a PNG to the node box, dropping the shadow/blur margin Figma adds to exports."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

PNG_SIG = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def _chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield kind, data[pos + 8:pos + 8 + n]
        pos += 12 + n


def read_png(data: bytes):
    if not data.startswith(PNG_SIG):
        return None
    ihdr, idat, extra = None, [], []
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind in (b"PLTE", b"tRNS"):
            extra.append((kind, body))
    if ihdr is None:
        return None
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", ihdr)
    if depth != 8 or interlace or ctype not in CHANNELS:
        return None
    bpp = CHANNELS[ctype]
    stride = w * bpp
    raw = zlib.decompress(b"".join(idat))
    if len(raw) < h * (stride + 1):
        return None
    rows, prev = [], bytearray(stride)
    pos = 0
    for _ in range(h):
        ft = raw[pos]
        cur = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += stride + 1
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            up = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                cur[i] = (cur[i] + a) & 255
            elif ft == 2:
                cur[i] = (cur[i] + up) & 255
            elif ft == 3:
                cur[i] = (cur[i] + ((a + up) >> 1)) & 255
            elif ft == 4:
                p = a + up - c
                pa, pb, pc = abs(p - a), abs(p - up), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (up if pb <= pc else c)
                cur[i] = (cur[i] + pred) & 255
            elif ft != 0:
                return None
        rows.append(cur)
        prev = cur
    return {"w": w, "h": h, "ctype": ctype, "bpp": bpp, "rows": rows, "extra": extra}


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def write_png(img: dict, box: tuple[int, int, int, int]) -> bytes:
    x, y, w, h = box
    bpp = img["bpp"]
    raw = b"".join(b"\x00" + bytes(img["rows"][r][x * bpp:(x + w) * bpp]) for r in range(y, y + h))
    out = [PNG_SIG, _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, img["ctype"], 0, 0, 0))]
    out += [_chunk(k, b) for k, b in img["extra"]]
    out += [_chunk(b"IDAT", zlib.compress(raw)), _chunk(b"IEND", b"")]
    return b"".join(out)


def png_dims(data: bytes):
    if len(data) < 24 or not data.startswith(PNG_SIG) or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def effect_margins(effects) -> tuple[float, float, float, float]:
    """Left, top, right, bottom extent (design px) that shadows and blurs add around the node."""
    l = t = r = b = 0.0
    for e in effects or []:
        if not isinstance(e, dict) or e.get("visible") is False:
            continue
        kind = e.get("type")
        rad = float(e.get("radius") or 0)
        if kind == "DROP_SHADOW":
            sp = float(e.get("spread") or 0)
            off = e.get("offset") or {}
            ox, oy = float(off.get("x") or 0), float(off.get("y") or 0)
            ext = rad + sp
            l, t = max(l, ext - ox), max(t, ext - oy)
            r, b = max(r, ext + ox), max(b, ext + oy)
        elif kind == "LAYER_BLUR":
            l, t, r, b = max(l, rad), max(t, rad), max(r, rad), max(b, rad)
    return l, t, r, b


def _node_entry(doc: dict, node: str) -> dict | None:
    nodes = doc.get("nodes")
    if isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, dict) and n.get("id") == node:
                return n
        return nodes[0] if nodes and isinstance(nodes[0], dict) else None
    return doc if isinstance(doc, dict) else None


def node_crop_box(doc: dict, node: str, png_w: int, png_h: int):
    """Box (x, y, w, h) in png pixels of the node inside its export, or None to leave the image alone."""
    n = _node_entry(doc, node)
    if not n:
        return None
    bbox = n.get("absoluteBoundingBox")
    rbox = n.get("absoluteRenderBounds")
    if isinstance(bbox, dict) and isinstance(rbox, dict) and rbox.get("width") and rbox.get("height"):
        scale = png_w / float(rbox["width"])
        x = (bbox["x"] - rbox["x"]) * scale
        y = (bbox["y"] - rbox["y"]) * scale
        w, h = bbox["width"] * scale, bbox["height"] * scale
    else:
        size = n.get("size") or {}
        nw, nh = size.get("width"), size.get("height")
        if not nw or not nh:
            return None
        if abs(png_w - nw) <= 1 and abs(png_h - nh) <= 1:
            return None
        l, t, r, b = effect_margins((n.get("visual") or {}).get("effects") or n.get("effects"))
        if not (l or t or r or b):
            return None
        scale = png_w / (nw + l + r)
        if abs(png_h - (nh + t + b) * scale) > 1.5:
            return None
        x, y, w, h = l * scale, t * scale, nw * scale, nh * scale
    x, y, w, h = round(x), round(y), round(w), round(h)
    if (x, y, w, h) == (0, 0, png_w, png_h) or w <= 0 or h <= 0:
        return None
    if x < 0 or y < 0 or x + w > png_w or y + h > png_h:
        return None
    return x, y, w, h


def cropped_figma(run_dir: Path, node: str, stem: str, out: Path) -> Path | None:
    """Write `out/figma-cropped.png` when the export carries an effects margin; None keeps the original."""
    try:
        doc = json.loads((run_dir / "figma" / f"{stem}.json").read_text(encoding="utf-8"))
        data = (run_dir / "figma" / f"{stem}.png").read_bytes()
    except (OSError, ValueError):
        return None
    dims = png_dims(data)
    if not dims or not isinstance(doc, dict):
        return None
    box = node_crop_box(doc, node, *dims)
    if not box:
        return None
    try:
        img = read_png(data)
        if not img:
            return None
        res = write_png(img, box)
    except (zlib.error, struct.error, IndexError):
        return None
    out.mkdir(parents=True, exist_ok=True)
    target = out / "figma-cropped.png"
    target.write_bytes(res)
    return target
