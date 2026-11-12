"""`factory figma check|expect`: Figma pack completeness and style-map skeletons."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any

from ..core import FactoryError, GateFailed, node_file, read_json, run_dir, run_load, write_json

PAINT_TYPES = {"SOLID", "IMAGE", "EMOJI", "VIDEO"}
PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ---------- pack check ----------

def png_size(path: Path) -> tuple[int, int] | None:
    try:
        head = path.read_bytes()[:33]
    except OSError:
        return None
    if len(head) < 24 or not head.startswith(PNG_SIG) or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return (w, h) if w > 0 and h > 0 else None


def _meta_entries(meta: Any) -> list[dict]:
    if isinstance(meta, list):
        return [m for m in meta if isinstance(m, dict)]
    if not isinstance(meta, dict):
        return []
    if "fileKey" in meta:
        return [meta]
    out = []
    for key, val in meta.items():
        if isinstance(val, dict):
            out.append({"fileKey": key, **val})
    return out


def _acs_nodes(run_id: str) -> list[str]:
    acs = read_json(run_dir(run_id) / "acs.json")
    nodes: list[str] = []
    for chk in acs.get("checks", []):
        n = chk.get("node")
        if n and n not in nodes:
            nodes.append(n)
    return nodes


def check_pack(run_id: str) -> list[str]:
    errors: list[str] = []
    try:
        nodes = _acs_nodes(run_id)
    except FactoryError as err:
        return [str(err)]
    if not nodes:
        return errors
    fdir = run_dir(run_id) / "figma"
    file_keys: set[str] = set()
    for node in nodes:
        stem = node_file(node)
        jpath, ppath = fdir / f"{stem}.json", fdir / f"{stem}.png"
        if not jpath.is_file():
            errors.append(f"{node}: missing figma/{stem}.json")
        else:
            try:
                payload = json.loads(jpath.read_text(encoding="utf-8"))
                if isinstance(payload, dict) and payload.get("fileKey"):
                    file_keys.add(str(payload["fileKey"]))
            except (json.JSONDecodeError, OSError) as err:
                errors.append(f"{node}: figma/{stem}.json is not valid JSON ({err})")
        if not ppath.is_file():
            errors.append(f"{node}: missing figma/{stem}.png")
        elif png_size(ppath) is None:
            errors.append(f"{node}: figma/{stem}.png is not a decodable PNG (bad header or zero size)")
    mpath = fdir / "meta.json"
    if not mpath.is_file():
        errors.append("missing figma/meta.json (fileKey + lastModified/version)")
        return errors
    try:
        entries = [e for e in _meta_entries(json.loads(mpath.read_text(encoding="utf-8")))
                   if e.get("fileKey") and (e.get("lastModified") or e.get("version"))]
    except json.JSONDecodeError as err:
        return errors + [f"figma/meta.json is not valid JSON ({err})"]
    if not entries:
        errors.append("figma/meta.json has no entry with fileKey and lastModified/version")
    else:
        have = {str(e["fileKey"]) for e in entries}
        for key in sorted(file_keys - have):
            errors.append(f"figma/meta.json has no revision for fileKey {key}")
    return errors


# ---------- expect ----------

def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _clean(v: float) -> float | int:
    v = round(v, 2)
    return int(v) if v == int(v) else v


def _hex(color: dict, opacity: float = 1.0) -> str | None:
    try:
        r, g, b = (round(float(color[k]) * 255) for k in "rgb")
    except (KeyError, TypeError, ValueError):
        return None
    a = float(color.get("a", 1.0)) * opacity
    if a < 0.995:
        return f"rgba({r}, {g}, {b}, {round(a, 2):g})"
    return f"#{r:02x}{g:02x}{b:02x}"


def _first_solid(paints: Any) -> str | None:
    if not isinstance(paints, list):
        return None
    for p in paints:
        if isinstance(p, dict) and p.get("type") == "SOLID" and p.get("visible", True) and isinstance(p.get("color"), dict):
            return _hex(p["color"], float(p.get("opacity", 1.0)))
    return None


def _is_node(d: dict) -> bool:
    if d.get("type") in PAINT_TYPES or str(d.get("type", "")).startswith("GRADIENT"):
        return False
    return any(k in d for k in ("name", "id", "characters", "absoluteBoundingBox"))


def extract_expect(node: dict) -> dict:
    """Pull CSS-named expectations out of one design-context node (REST or flat style)."""
    style = node.get("style") if isinstance(node.get("style"), dict) else {}

    def pick(key: str) -> Any:
        return node[key] if key in node else style.get(key)

    exp: dict[str, Any] = {}
    is_text = bool(node.get("characters")) or node.get("type") == "TEXT"
    if (v := _num(pick("fontSize"))) is not None:
        exp["fontSize"] = _clean(v)
    if (v := _num(pick("lineHeightPx"))) is not None:
        exp["lineHeight"] = _clean(v)
    elif (v := _num(pick("lineHeight"))) is not None:
        exp["lineHeight"] = _clean(v)
    if (v := _num(pick("fontWeight"))) is not None:
        exp["fontWeight"] = int(v)
    if (v := _num(pick("letterSpacing"))) is not None and v != 0:
        exp["letterSpacing"] = _clean(v)
    if isinstance(pick("fontFamily"), str):
        exp["fontFamily"] = pick("fontFamily")
    fill = _first_solid(node.get("fills"))
    if fill is None and isinstance(node.get("fills"), str):
        fill = node["fills"]
    if fill is None and isinstance(node.get("color"), dict):
        fill = _hex(node["color"])
    if fill is None and isinstance(node.get("color"), str):
        fill = node["color"]
    if fill:
        exp["color" if is_text else "backgroundColor"] = fill
    if (v := _num(node.get("cornerRadius"))) is not None and v != 0:
        exp["borderRadius"] = _clean(v)
    for side in ("Top", "Right", "Bottom", "Left"):
        if (v := _num(node.get(f"padding{side}"))) is not None and v != 0:
            exp[f"padding{side}"] = _clean(v)
    if (v := _num(node.get("itemSpacing"))) is not None and v != 0:
        exp["gap"] = _clean(v)
    box = node.get("absoluteBoundingBox") if isinstance(node.get("absoluteBoundingBox"), dict) else {}
    for dim in ("width", "height"):
        v = _num(box.get(dim) if dim in box else node.get(dim))
        if v is not None:
            exp[dim] = _clean(v)
    stroke = _first_solid(node.get("strokes"))
    if stroke:
        exp["borderColor"] = stroke
        if (w := _num(node.get("strokeWeight"))) is not None and w > 0:
            exp["borderWidth"] = _clean(w)
    if (v := _num(node.get("opacity"))) is not None and v < 1:
        exp["opacity"] = _clean(v)
    if is_text and isinstance(node.get("characters"), str):
        exp["text"] = node["characters"]
    return exp


def _walk(obj: Any, found: list[dict]) -> None:
    if len(found) >= 300:
        return
    if isinstance(obj, dict):
        if _is_node(obj):
            exp = extract_expect(obj)
            if exp:
                found.append({"name": str(obj.get("name") or obj.get("id") or "node"), "id": obj.get("id"), "expect": exp})
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _walk(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, found)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "node"


def build_skeleton(node: str, payload: Any, selector: str | None = None) -> dict:
    found: list[dict] = []
    _walk(payload, found)
    entries = []
    used: set[str] = set()
    for i, f in enumerate(found):
        eid = _slug(f["name"])
        if eid in used:
            eid = f"{eid}-{i}"
        used.add(eid)
        entries.append({
            "id": eid,
            "figma_name": f["name"],
            "selector": selector if (i == 0 and selector) else "TODO",
            "expect": f["expect"],
            "tolerance_px": 1,
        })
    return {"node": node, "entries": entries}


def expect(run_id: str, node: str, selector: str | None = None) -> Path:
    jpath = run_dir(run_id) / "figma" / f"{node_file(node)}.json"
    payload = read_json(jpath)
    skeleton = build_skeleton(node, payload, selector)
    if not skeleton["entries"]:
        raise FactoryError(f"no style values found in {jpath}")
    out = run_dir(run_id) / "checks" / "style" / f"{node_file(node)}.skeleton.json"
    write_json(out, skeleton)
    return out


# ---------- CLI ----------

def _cmd_check(args) -> int:
    run_load(args.run)
    errs = check_pack(args.run)
    if errs:
        raise GateFailed(errs)
    print("OK figma pack complete")
    return 0


def _cmd_expect(args) -> int:
    run_load(args.run)
    out = expect(args.run, args.node, args.selector)
    data = read_json(out)
    print(f"wrote {out} ({len(data['entries'])} entries; replace selector TODO before use)")
    return 0


def register(sub) -> None:
    p = sub.add_parser("figma", help="Figma pack checks and style expectations")
    fsub = p.add_subparsers(dest="figma_cmd", required=True)
    c = fsub.add_parser("check")
    c.add_argument("run")
    c.set_defaults(func=_cmd_check)
    e = fsub.add_parser("expect")
    e.add_argument("run")
    e.add_argument("node")
    e.add_argument("--selector")
    e.set_defaults(func=_cmd_expect)
