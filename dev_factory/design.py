"""Design contracts and what is derived from them: validation, the PNG header, the property table, crop boxes and tolerances
[L-54, L-55, L-121, L-186 to L-188]."""
from __future__ import annotations

import re
import struct

from .util import FactoryError, canonical, sha256_bytes

PNG_SIG = b"\x89PNG\r\n\x1a\n"
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")
NODE_TYPES = ("frame", "group", "text", "shape", "image", "component", "instance", "other")
PAINT, EFFECT = ("solid", "gradient", "image"), ("drop_shadow", "inner_shadow", "layer_blur", "bg_blur")
NODE_KEYS = {"id", "name", "type", "visible", "box", "render_box", "opacity", "fills", "strokes", "effects", "corner_radius", "corner_radii",
             "layout", "text", "children", "estimated"}
TEXT_NUM, TEXT_STR = ("font_size", "font_weight", "line_height_px", "letter_spacing"), ("characters", "font_family", "align")
MASK_SHARE = 0.02  # a mask covers at most 2% of the compared area [DESIGN section 9]


def png_size(raw: bytes) -> tuple[int, int]:
    """(width, height) from the IHDR chunk; anything that is not a PNG is refused [L-121]."""
    if raw[:8] != PNG_SIG or raw[12:16] != b"IHDR" or len(raw) < 33:
        raise FactoryError("the design image is not a PNG file (convert it to PNG at scale 1 first)")
    w, h = struct.unpack(">II", raw[16:24])
    if not (w and h):
        raise FactoryError("the design image has no pixels")
    return w, h


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _box(v, path: str, errs: list[str]) -> None:
    if not (isinstance(v, dict) and set(v) == {"x", "y", "w", "h"} and all(_num(x) for x in v.values()) and v["w"] >= 0 and v["h"] >= 0):
        errs.append(f"{path}: must be {{x, y, w, h}} numbers in frame CSS pixels (w and h not negative)")


def _color(v, path: str, errs: list[str]) -> None:
    if not (isinstance(v, str) and HEX.match(v)):
        errs.append(f"{path}: must be a #rrggbb color")


def _paints(items, path: str, strokes: bool, errs: list[str]) -> None:
    if not isinstance(items, list):
        errs.append(f"{path}: must be a list")
        return
    for i, p in enumerate(items):
        here = f"{path}[{i}]"
        allowed = {"type", "color", "opacity"} | ({"weight", "align"} if strokes else set())
        if not isinstance(p, dict) or set(p) - allowed or p.get("type") not in PAINT:
            errs.append(f"{here}: must be an object with type {'/'.join(PAINT)} and keys {', '.join(sorted(allowed))}")
            continue
        if "color" in p:
            _color(p["color"], f"{here}.color", errs)
        errs += [f"{here}.{k}: must be a number" for k in ("opacity", "weight") if k in p and not _num(p[k])]


def _node(n, path: str, errs: list[str], seen: set[str]) -> None:
    if not isinstance(n, dict):
        errs.append(f"{path}: must be an object")
        return
    errs += [f"{path}.{k}: unknown key" for k in sorted(set(n) - NODE_KEYS)]
    for k in ("id", "name"):
        if not (isinstance(n.get(k), str) and n[k].strip()):
            errs.append(f"{path}.{k}: must be a non-empty string")
    if n.get("id") in seen:
        errs.append(f"{path}.id: duplicate node id {n['id']!r}")
    seen.add(n.get("id"))
    if n.get("type") not in NODE_TYPES:
        errs.append(f"{path}.type: {n.get('type')!r} is not one of {', '.join(NODE_TYPES)}")
    _box(n.get("box"), f"{path}.box", errs)
    if "render_box" in n:
        _box(n["render_box"], f"{path}.render_box", errs)
    if "visible" in n and not isinstance(n["visible"], bool):
        errs.append(f"{path}.visible: must be true or false")
    if "opacity" in n and not (_num(n["opacity"]) and 0 <= n["opacity"] <= 1):
        errs.append(f"{path}.opacity: must be a number from 0 to 1")
    for k, strokes in (("fills", False), ("strokes", True)):
        if k in n:
            _paints(n[k], f"{path}.{k}", strokes, errs)
    for i, e in enumerate(n.get("effects", [])) if isinstance(n.get("effects", []), list) else []:
        ok = isinstance(e, dict) and e.get("type") in EFFECT and not set(e) - {"type", "color", "offset", "radius", "spread"}
        if not ok or any(k in e and not _num(e[k]) for k in ("radius", "spread")) or ("color" in e and not (isinstance(e["color"], str) and HEX.match(e["color"]))) \
                or ("offset" in e and not (isinstance(e["offset"], dict) and set(e["offset"]) == {"x", "y"} and all(_num(v) for v in e["offset"].values()))):
            errs.append(f"{path}.effects[{i}]: needs a type ({', '.join(EFFECT)}) and numeric radius, spread and offset {{x, y}}, with a #rrggbb color")
    if "corner_radius" in n and not (_num(n["corner_radius"]) and n["corner_radius"] >= 0):
        errs.append(f"{path}.corner_radius: must be a number")
    if "corner_radii" in n and not (isinstance(n["corner_radii"], list) and len(n["corner_radii"]) == 4 and all(_num(v) for v in n["corner_radii"])):
        errs.append(f"{path}.corner_radii: must be [top-left, top-right, bottom-right, bottom-left]")
    lay = n.get("layout")
    if lay is not None:
        pad = lay.get("padding", {}) if isinstance(lay, dict) else None
        if not (isinstance(lay, dict) and lay.get("mode") in ("none", "horizontal", "vertical") and not set(lay) - {"mode", "gap", "padding"}
                and (not lay.get("gap") or _num(lay["gap"])) and isinstance(pad, dict) and set(pad) <= {"t", "r", "b", "l"} and all(_num(v) for v in pad.values())):
            errs.append(f"{path}.layout: must be {{mode: none|horizontal|vertical, gap, padding: {{t, r, b, l}}}}")
    txt = n.get("text")
    if txt is not None:
        ok = isinstance(txt, dict) and not set(txt) - set(TEXT_NUM + TEXT_STR + ("color",)) and all(isinstance(txt[k], str) for k in TEXT_STR if k in txt) \
            and all(_num(txt[k]) for k in TEXT_NUM if k in txt)
        if not ok:
            errs.append(f"{path}.text: keys are {', '.join(TEXT_STR + TEXT_NUM + ('color',))} (text values, numbers, #rrggbb)")
        elif "color" in txt:
            _color(txt["color"], f"{path}.text.color", errs)
    est = n.get("estimated")
    if est is not None and not (isinstance(est, dict) and all(v is True for v in est.values())):
        errs.append(f"{path}.estimated: must map a dotted path to true")
    kids = n.get("children", [])
    if not isinstance(kids, list):
        errs.append(f"{path}.children: must be a list")
        return
    for i, c in enumerate(kids):
        _node(c, f"{path}.children[{i}]", errs, seen)


def numbers_and_colors(n: dict) -> list[str]:
    """Dotted paths of every number and color a node states: an approx frame has to flag each as estimated [L-124]."""
    out = [f"box.{k}" for k in "xywh"] + [f"render_box.{k}" for k in "xywh" if "render_box" in n]
    out += [k for k in ("opacity", "corner_radius") if k in n] + [f"corner_radii.{i}" for i in range(len(n.get("corner_radii", [])))]
    for key in ("fills", "strokes"):
        for i, p in enumerate(n.get(key, [])):
            out += [f"{key}.{i}.{k}" for k in ("color", "opacity", "weight") if k in p]
    for i, e in enumerate(n.get("effects", [])):
        out += [f"effects.{i}.{k}" for k in ("color", "radius", "spread") if k in e] + [f"effects.{i}.offset.{k}" for k in "xy" if "offset" in e]
    lay = n.get("layout") or {}
    out += (["layout.gap"] if "gap" in lay else []) + [f"layout.padding.{k}" for k in lay.get("padding", {})]
    txt = n.get("text") or {}
    return out + [f"text.{k}" for k in TEXT_NUM + ("color",) if k in txt]


def validate_frame(data) -> dict:
    """The normalized `design_frame/1` data, or a FactoryError naming every bad field (nothing is stored) [L-156, L-188]."""
    errs: list[str] = []
    if not isinstance(data, dict):
        raise FactoryError("contract design_frame: data must be a JSON object")
    errs += [f"data.{k}: unknown key" for k in sorted(set(data) - {"id", "name", "fidelity", "source", "image", "root", "unknowns"})]
    errs += [f"data.{k}: required, and must be a non-empty string" for k in ("id", "name", "image") if not (isinstance(data.get(k), str) and data[k].strip())]
    fid, src = data.get("fidelity"), data.get("source")
    if fid not in ("exact", "approx"):
        errs.append(f"data.fidelity: {fid!r} is not one of exact, approx")
    if not (isinstance(src, dict) and set(src) == {"file", "version", "fetched_at"} and isinstance(src["file"], str) and src["file"].strip()
            and (src["version"] is None or isinstance(src["version"], str)) and isinstance(src["fetched_at"], str) and TS.match(src["fetched_at"])):
        errs.append("data.source: must be {file, version (a string, null only for approx), fetched_at (UTC timestamp)}")
    elif fid == "exact" and not str(src["version"] or "").strip():
        errs.append("data.source.version: an exact frame needs the source version, so ship can check freshness [L-09]")
    if not (isinstance(data.get("unknowns"), list) and all(isinstance(x, str) for x in data["unknowns"])):
        errs.append("data.unknowns: required; list the values you could not map, [] when none")
    root = data.get("root")
    _node(root, "data.root", errs, set())
    if not errs and isinstance(root, dict):
        for path, n in walk(root):
            flagged = set(n.get("estimated", {}))
            if fid == "approx":
                errs += [f"{path}: {p} is a number or color of an approx frame and must be flagged in estimated" for p in numbers_and_colors(n) if p not in flagged]
                errs += [f"{path}.estimated: {p} is flagged but the node has no such value" for p in sorted(flagged - set(numbers_and_colors(n)))]
            elif flagged:
                errs.append(f"{path}.estimated: only an approx frame lists estimated values")
    if errs:
        raise FactoryError("contract design_frame rejected (nothing written):\n  " + "\n  ".join(errs))
    return data


def validate_image(data, raw: bytes | None) -> dict:
    """`design_image/1`: width and height come from the PNG header and must match what the data states [L-121]."""
    if not isinstance(data, dict):
        raise FactoryError("contract design_image: data must be a JSON object")
    if raw is None:
        raise FactoryError("contract design_image: the raw PNG is required")
    w, h = png_size(raw)
    errs = [f"data.{k}: unknown key" for k in sorted(set(data) - {"id", "width", "height", "scale"})]
    if not (isinstance(data.get("id"), str) and data["id"].strip()):
        errs.append("data.id: required, and must be a non-empty string")
    errs += [f"data.{k}: the PNG is {v} pixels, not {data[k]!r}" for k, v in (("width", w), ("height", h)) if k in data and data[k] != v]
    if data.get("scale", 1) != 1:
        errs.append("data.scale: must be 1 (CSS pixels); export the design at scale 1")
    if errs:
        raise FactoryError("contract design_image rejected (nothing written):\n  " + "\n  ".join(errs))
    return {"id": data["id"], "width": w, "height": h, "scale": 1}


def walk(node: dict, path: str = "root"):
    yield path, node
    for i, c in enumerate(node.get("children", [])):
        yield from walk(c, f"{path}.children[{i}]")


def find(frame: dict, node_id: str | None) -> dict | None:
    root = frame["root"]
    return root if node_id in (None, root["id"]) else next((n for _, n in walk(root) if n["id"] == node_id), None)


def margins(node: dict) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) that shadows and blurs add around a node's box [L-55]."""
    m = [0.0, 0.0, 0.0, 0.0]
    for e in node.get("effects", []):
        r, off = e.get("radius", 0) + e.get("spread", 0), e.get("offset", {"x": 0, "y": 0})
        if e["type"] == "drop_shadow":
            m = [max(m[0], r - off["x"]), max(m[1], r - off["y"]), max(m[2], r + off["x"]), max(m[3], r + off["y"])]
        elif e["type"] in ("layer_blur", "bg_blur"):
            m = [max(m[i], r) for i in range(4)]
    return tuple(max(0.0, v) for v in m)  # type: ignore[return-value]


def render_box(node: dict) -> dict:
    """The box an export of the node covers: the declared `render_box`, else the box grown by its effect margins."""
    if "render_box" in node:
        return node["render_box"]
    b, (ml, mt, mr, mb) = node["box"], margins(node)
    return {"x": b["x"] - ml, "y": b["y"] - mt, "w": b["w"] + ml + mr, "h": b["h"] + mt + mb}


def crop_box(frame: dict, node_id: str | None, image: dict) -> tuple[list[int], list[str]]:
    """[x, y, w, h] of the node inside the design PNG, whose origin is the root's render box, plus problems [L-55]."""
    node = find(frame, node_id)
    if node is None:
        return [0, 0, 0, 0], [f"frame {frame['id']} has no node {node_id!r}"]
    origin, b, problems = render_box(frame["root"]), node["box"], []
    box = [round(b["x"] - origin["x"]), round(b["y"] - origin["y"]), round(b["w"]), round(b["h"])]
    if frame["fidelity"] == "exact" and (abs(image["width"] - origin["w"]) > 1 or abs(image["height"] - origin["h"]) > 1):
        problems.append(f"the design image is {image['width']}x{image['height']} but the frame render box is {origin['w']:g}x{origin['h']:g} [L-55]")
    if box[0] < 0 or box[1] < 0 or box[2] <= 0 or box[3] <= 0 or box[0] + box[2] > image["width"] or box[1] + box[3] > image["height"]:
        problems.append(f"node {node['id']} ({box[2]}x{box[3]} at {box[0]},{box[1]}) does not fit inside the {image['width']}x{image['height']} design image")
    return box, problems


def mask_problems(masks, crop: list[int], where: str) -> list[str]:
    errs = []
    if not isinstance(masks, list):
        return [f"{where}.masks: must be a list of {{x, y, w, h, reason}}"]
    for i, m in enumerate(masks):
        here = f"{where}.masks[{i}]"
        if not (isinstance(m, dict) and set(m) == {"x", "y", "w", "h", "reason"} and all(_num(m[k]) for k in "xywh") and str(m.get("reason") or "").strip()):
            errs.append(f"{here}: needs x, y, w, h numbers and a reason")
        elif m["w"] <= 0 or m["h"] <= 0 or m["x"] < 0 or m["y"] < 0 or m["x"] + m["w"] > crop[2] or m["y"] + m["h"] > crop[3]:
            errs.append(f"{here}: must lie inside the {crop[2]}x{crop[3]} node area")
        elif m["w"] * m["h"] > MASK_SHARE * crop[2] * crop[3]:
            errs.append(f"{here}: covers {m['w'] * m['h'] / (crop[2] * crop[3]):.1%} of the area; a mask may cover at most 2%")
    return errs


def facts(node: dict) -> dict:
    """Expected computed-style values of a node through the fixed property table of `contracts.md` [L-187]."""
    f: dict = {}
    f["width"], f["height"] = node["box"]["w"], node["box"]["h"]
    is_text = node.get("type") == "text" or "text" in node
    fill = (node.get("fills") or [{}])[0]
    if fill.get("color"):
        f["color" if is_text else "backgroundColor"] = fill["color"].lower()
    txt = node.get("text") or {}
    if txt.get("color"):
        f["color"] = txt["color"].lower()
    stroke = (node.get("strokes") or [{}])[0]
    if stroke.get("color"):
        f["borderColor"] = stroke["color"].lower()
    if "weight" in stroke:
        f["borderWidth"] = stroke["weight"]
    if "corner_radius" in node:
        f["borderRadius"] = node["corner_radius"]
    if "corner_radii" in node:
        f.update(zip(("borderTopLeftRadius", "borderTopRightRadius", "borderBottomRightRadius", "borderBottomLeftRadius"), node["corner_radii"]))
        if len(set(node["corner_radii"])) == 1:
            f["borderRadius"] = node["corner_radii"][0]
    if "opacity" in node:
        f["opacity"] = node["opacity"]
    lay = node.get("layout") or {}
    if "gap" in lay:
        f["gap"] = lay["gap"]
    f.update({f"padding{n}": lay["padding"][k] for k, n in (("t", "Top"), ("r", "Right"), ("b", "Bottom"), ("l", "Left")) if k in lay.get("padding", {})})
    names = {"font_family": "fontFamily", "font_size": "fontSize", "font_weight": "fontWeight", "line_height_px": "lineHeight",
             "letter_spacing": "letterSpacing", "align": "textAlign", "characters": "text"}
    f.update({names[k]: txt[k] for k in names if k in txt})
    return f


def tolerances(cfg: dict, fidelity: str) -> dict:
    """Ceilings by the frame's fidelity, never by a check field: px, color delta E, font size and diff ratio [L-188]."""
    lim = cfg["limits"]
    if fidelity == "approx":
        a = lim["approx"]
        return {"px": a["px"], "delta_e": a["color_delta_e"], "font_px": a["font_size_px"], "max_diff_ratio": a["max_diff_ratio"]}
    return {"px": lim["tolerance_px"], "delta_e": 0, "font_px": 0, "max_diff_ratio": lim["max_diff_ratio"]}


def frame_hash(env: dict) -> str:
    """What a design review and the lock bind to: the raw hash and the contract data."""
    return sha256_bytes(canonical({"raw": env["raw"]["sha256"], "data": env["data"]}).encode("utf-8"))
