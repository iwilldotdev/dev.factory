"""Crop a frame image to the node box, dropping the shadow/blur margin a design export adds."""

from __future__ import annotations

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
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        rad = float(e.get("radius") or 0)
        if kind == "drop_shadow":
            sp = float(e.get("spread") or 0)
            off = e.get("offset") or {}
            ox, oy = float(off.get("x") or 0), float(off.get("y") or 0)
            ext = rad + sp
            l, t = max(l, ext - ox), max(t, ext - oy)
            r, b = max(r, ext + ox), max(b, ext + oy)
        elif kind == "layer_blur":
            l, t, r, b = max(l, rad), max(t, rad), max(r, rad), max(b, rad)
    return l, t, r, b


def node_crop_box(doc: dict, png_w: int, png_h: int):
    """Box (x, y, w, h) in png pixels of the frame's root node inside its image, or None to leave the image alone.
    `doc` is a design_frame contract: `root.box`, `root.render_box` and `root.effects` drive the crop."""
    n = doc.get("root") if isinstance(doc, dict) else None
    if not isinstance(n, dict):
        return None
    box = n.get("box") if isinstance(n.get("box"), dict) else None
    rbox = n.get("render_box") if isinstance(n.get("render_box"), dict) else None
    if not box:
        return None
    if rbox and rbox.get("w") and rbox.get("h"):
        scale = png_w / float(rbox["w"])
        x = (box["x"] - rbox["x"]) * scale
        y = (box["y"] - rbox["y"]) * scale
        w, h = box["w"] * scale, box["h"] * scale
    else:
        nw, nh = box.get("w"), box.get("h")
        if not nw or not nh:
            return None
        if abs(png_w - nw) <= 1 and abs(png_h - nh) <= 1:
            return None
        l, t, r, b = effect_margins(n.get("effects"))
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


def cropped_design(run_dir: Path, doc: dict, out: Path) -> Path | None:
    """Write `out/design-cropped.png` when the frame image carries an effects margin; None keeps the original.
    The image is the one `doc.frame.image.path` names (a design_frame contract)."""
    rel = (((doc or {}).get("frame") or {}).get("image") or {}).get("path")
    if not isinstance(rel, str):
        return None
    try:
        data = (run_dir / rel).read_bytes()
    except OSError:
        return None
    dims = png_dims(data)
    if not dims or not isinstance(doc, dict):
        return None
    box = node_crop_box(doc, *dims)
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
    target = out / "design-cropped.png"
    target.write_bytes(res)
    return target
