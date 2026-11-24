"""Design pillar over contracts (docs/design/provider-agnostic.md sections 3.5, 7, 7.1).

Gates and collectors read `design_frame.v1` and `design_image.v1` contracts only, never a raw payload. Everything
provider-neutral about a frame lives here: loading and freshness, the fixed node-property -> style-name table
(facts for the style-map check, expectations for `design expect`), tolerances of `approx` frames (CIE76 color
distance), the review decision approx frames need, and the ship-description lines.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from . import contracts
from .core import FactoryError, run_dir

APPROX_DEFAULTS = {"px": 4, "color_delta_e": 6, "font_size_px": 2, "max_diff_ratio": 0.10}
LEGACY_FILE_KEY = re.compile(r"^file_?key$", re.I)
UI_TYPES = ("visual", "style")
PX_KEYS_EXACT = ("fontWeight", "opacity")  # never loosened, even for approx frames


def is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ---------- checks ----------

def check_node(chk: dict) -> str | None:
    """The frame id a visual/style check names: `node`, or its alias `frame`."""
    val = chk.get("node") or chk.get("frame")
    return val if isinstance(val, str) and val else None


def check_fidelity(chk: dict) -> str:
    return chk.get("fidelity") or "exact"


def ui_checks(checks: list[dict]) -> list[dict]:
    return [c for c in checks if isinstance(c, dict) and c.get("type") in UI_TYPES and check_node(c)]


def acs_checks(run_id: str) -> list[dict]:
    try:
        data = json.loads((run_dir(run_id) / "acs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in data.get("checks", []) if isinstance(c, dict)] if isinstance(data, dict) else []


def frame_nodes(checks: list[dict]) -> list[str]:
    """Distinct frame ids of the visual/style checks, in order."""
    out: list[str] = []
    for c in ui_checks(checks):
        n = check_node(c)
        if n not in out:
            out.append(n)
    return out


def source_file_of(checks: list[dict], node: str) -> str | None:
    for c in checks:
        if check_node(c) == node and isinstance(c.get("source_file"), str) and c["source_file"]:
            return c["source_file"]
    return None


# ---------- limits ----------

def approx_limits(workspace: dict | None) -> dict:
    """`limits.approx` of the workspace over the defaults (numbers >= 0 only)."""
    out = dict(APPROX_DEFAULTS)
    raw = ((workspace or {}).get("limits") or {}).get("approx")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out and is_num(v) and v >= 0:
                out[k] = v
    return out


# ---------- loading ----------

def read_frame(rd: Path, node: str) -> dict | None:
    """The frame contract on disk (no validation: use `frame_problems` in gates); None when absent or unreadable."""
    try:
        doc = json.loads(contracts.contract_path(rd, "design_frame", contracts.safe_id(node)).read_text(encoding="utf-8"))
    except (OSError, ValueError, FactoryError):
        return None
    return doc if isinstance(doc, dict) else None


def frame_image(rd: Path, doc: dict | None) -> Path | None:
    """Absolute path of the image a frame contract points at, when the file exists."""
    img = ((doc or {}).get("frame") or {}).get("image")
    if isinstance(img, dict) and isinstance(img.get("path"), str):
        p = Path(rd) / img["path"]
        if p.is_file():
            return p
    return None


def frame_fidelity(rd: Path, node: str) -> str | None:
    doc = read_frame(rd, node)
    return doc.get("fidelity") if doc else None


def approx_frames(run_id: str, checks: list[dict] | None = None) -> list[str]:
    """Frame ids referenced by visual/style checks whose contract is approx."""
    rd = run_dir(run_id)
    checks = acs_checks(run_id) if checks is None else checks
    return [n for n in frame_nodes(checks) if frame_fidelity(rd, n) == "approx"]


def _raw_hint(run_id: str, kind: str, node: str, ws: dict | None) -> str:
    from . import fetches
    return fetches.needs(kind, node, {"run": run_id, "ws": ws})["raw_path"]


def frame_problems(run_id: str, chk_nodes: dict[str, list[dict]], ws: dict | None = None) -> list[str]:
    """Why the frames of a run cannot back a proof: missing or stale contracts, image, `source.version` (exact)."""
    rd = run_dir(run_id)
    out: list[str] = []
    for node, node_checks in chk_nodes.items():
        safe = contracts.safe_id(node)
        want = "approx" if any(check_fidelity(c) == "approx" for c in node_checks) else "exact"
        img_doc, img_prob = contracts.load_contract(run_id, "design_image", node)
        fr_doc, fr_prob = contracts.load_contract(run_id, "design_frame", node)
        if img_doc is None:
            out.append(f"{node}: no design_image contract: save the frame image at "
                       f"{_raw_hint(run_id, 'design_image', node, ws)}, then `factory ingest {run_id} design_image "
                       f"raw/design_image/{safe}.png --id {node}`" + (" (approx: `factory design add`)" if want == "approx" else ""))
        else:
            out += img_prob
        if fr_doc is None:
            out.append(f"{node}: no design_frame contract: save the frame description at "
                       f"{_raw_hint(run_id, 'design_frame', node, ws)}, then `factory ingest {run_id} design_frame "
                       f"<raw> --id {node} --ctx file=<source_file>` (after the image)")
            continue
        out += fr_prob
        if fr_prob:
            continue
        if (fr_doc.get("frame") or {}).get("id") != node:
            out.append(f"{node}: contract design_frame/{safe} is for frame {(fr_doc.get('frame') or {}).get('id')!r}")
        fidelity = fr_doc.get("fidelity")
        if fidelity == "exact":
            if not str((fr_doc.get("source") or {}).get("version") or "").strip():
                out.append(f"{node}: exact frame needs source.version (the design revision it was read at): "
                           "re-read it with a source that reports a revision, or use an approx frame (`factory design add`)")
        elif fidelity == "approx" and want == "exact":
            out.append(f"{node}: frame is approx but the check requires exact (default): approx never satisfies an exact "
                       "requirement. Set `fidelity: \"approx\"` on the check to accept approximate tolerances and a user "
                       "design review, or provide an exact frame")
        img = (fr_doc.get("frame") or {}).get("image")
        if not isinstance(img, dict):
            out.append(f"{node}: frame contract has no frame.image: ingest the design_image first, then re-ingest the "
                       f"design_frame (`factory ingest {run_id} design_frame <raw> --id {node}`)")
        elif img_doc is not None and not img_prob and img.get("path") != img_doc.get("path"):
            out.append(f"{node}: frame.image.path {img.get('path')!r} differs from design_image {img_doc.get('path')!r}: "
                       "re-ingest the design_frame")
    return out


def pack_errors(run_id: str, ws: dict | None = None) -> list[str]:
    """`design check` / plan gate: every visual/style frame has a fresh frame contract and image."""
    checks = acs_checks(run_id)
    by_node: dict[str, list[dict]] = {}
    for c in ui_checks(checks):
        by_node.setdefault(check_node(c), []).append(c)
    return frame_problems(run_id, by_node, ws)


# ---------- colors ----------

def norm_hex(value: str) -> str | None:
    h = value.strip().lstrip("#").lower()
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    if len(h) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", h):
        return None
    return "#" + h[:6]


def rgb_hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in (r, g, b))


def expected_color(value: Any) -> str | None:
    """`#rrggbb` of a hex or rgb()/rgba() string (alpha ignored); None when it is not a color."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.startswith("#"):
        return norm_hex(v)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*[, ]\s*(\d+)\s*[, ]\s*(\d+)\s*(?:[,/]\s*[\d.]+%?\s*)?\)", v, re.I)
    if m:
        return rgb_hex(*(int(x) / 255 for x in m.groups()))
    return None


def lab(color: str) -> tuple[float, float, float]:
    """CIE L*a*b* (D65) of `#rrggbb`."""
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    x = (0.4124564 * lin[0] + 0.3575761 * lin[1] + 0.1804375 * lin[2]) / 0.95047
    y = 0.2126729 * lin[0] + 0.7151522 * lin[1] + 0.0721750 * lin[2]
    z = (0.0193339 * lin[0] + 0.1191920 * lin[1] + 0.9503041 * lin[2]) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e76(a: str, b: str) -> float:
    la, lb = lab(a), lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb)) ** 0.5


# ---------- node property table (vendor-neutral) ----------

def iter_nodes(node: Any, depth: int = 0) -> Iterator[dict]:
    """Visible nodes of a frame tree (a hidden node hides its subtree: it is not rendered)."""
    if not isinstance(node, dict) or depth > 200 or node.get("visible") is False:
        return
    yield node
    for kid in node.get("children") or []:
        yield from iter_nodes(kid, depth + 1)


def _paints(node: dict, field: str) -> list[dict]:
    return [p for p in node.get(field) or [] if isinstance(p, dict) and p.get("type") == "solid" and p.get("color")]


def _css_color(paint: dict) -> str:
    color = paint["color"].lower()
    op = paint.get("opacity")
    if is_num(op) and op < 0.995:
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        return f"rgba({r}, {g}, {b}, {round(op, 2):g})"
    return color


def node_props(node: dict) -> dict[str, list]:
    """Style-name -> values a node backs: the fixed projection of contract properties.

        text.font_size -> fontSize          text.line_height_px -> lineHeight   text.font_weight -> fontWeight
        text.letter_spacing -> letterSpacing text.font_family -> fontFamily      text.characters -> text
        opacity -> opacity                  box.w/box.h -> width/height         corner_radius -> borderRadius
        corner_radii [tl,tr,br,bl] -> border{TopLeft,TopRight,BottomRight,BottomLeft}Radius (+ borderRadius when all equal)
        layout.gap -> gap                   layout.padding.t/r/b/l -> padding{Top,Right,Bottom,Left}
        strokes[solid].weight -> borderWidth, strokes[solid].color -> borderColor
        fills[solid].color -> backgroundColor (color on a text node), text.color -> color
    """
    out: dict[str, list] = {}

    def put(key: str, val: Any) -> None:
        if val is not None:
            out.setdefault(key, []).append(val)

    text = node.get("text") if isinstance(node.get("text"), dict) else {}
    is_text = node.get("type") == "text" or bool(text)
    for key, src in (("fontSize", "font_size"), ("lineHeight", "line_height_px"), ("fontWeight", "font_weight"),
                     ("letterSpacing", "letter_spacing"), ("fontFamily", "font_family"), ("text", "characters")):
        put(key, text.get(src))
    put("opacity", node.get("opacity"))
    box = node.get("box") if isinstance(node.get("box"), dict) else {}
    put("width", box.get("w"))
    put("height", box.get("h"))
    put("borderRadius", node.get("corner_radius"))
    radii = node.get("corner_radii")
    if isinstance(radii, list) and len(radii) == 4 and all(is_num(x) for x in radii):
        for key, val in zip(("borderTopLeftRadius", "borderTopRightRadius", "borderBottomRightRadius",
                             "borderBottomLeftRadius"), radii):
            put(key, val)
        if len(set(radii)) == 1 and node.get("corner_radius") is None:
            put("borderRadius", radii[0])
    layout = node.get("layout") if isinstance(node.get("layout"), dict) else {}
    put("gap", layout.get("gap"))
    pad = layout.get("padding") if isinstance(layout.get("padding"), dict) else {}
    for key, src in (("paddingTop", "t"), ("paddingRight", "r"), ("paddingBottom", "b"), ("paddingLeft", "l")):
        put(key, pad.get(src))
    for p in _paints(node, "fills"):
        put("color" if is_text else "backgroundColor", _css_color(p))
    if isinstance(text.get("color"), str):
        put("color", text["color"].lower())
    for p in _paints(node, "strokes"):
        put("borderColor", _css_color(p))
        put("borderWidth", p.get("weight"))
    return out


def frame_facts(doc: dict) -> tuple[dict[str, set], set[str]]:
    """({style name: {numbers and strings the frame backs}}, {every solid fill/stroke/text color as #rrggbb})."""
    by_key: dict[str, set] = {}
    colors: set[str] = set()
    for node in iter_nodes(doc.get("root")):
        for key, vals in node_props(node).items():
            for v in vals:
                if key in ("color", "backgroundColor", "borderColor"):
                    c = expected_color(v)
                    if c:
                        colors.add(c)
                elif is_num(v):
                    by_key.setdefault(key, set()).add(float(v))
                elif isinstance(v, str):
                    by_key.setdefault(key, set()).add(v)
    for lo, hi in (("minWidth", "width"), ("maxWidth", "width"), ("minHeight", "height"), ("maxHeight", "height")):
        by_key[lo] = by_key[hi] if hi in by_key else set()
    return by_key, colors


# ---------- style-map check ----------

COLOR_KEYS = frozenset({"color", "backgroundColor", "borderColor"})
KNOWN_KEYS = frozenset({"fontSize", "lineHeight", "fontWeight", "letterSpacing", "fontFamily", "text", "opacity", "width",
                        "height", "borderRadius", "borderTopLeftRadius", "borderTopRightRadius", "borderBottomRightRadius",
                        "borderBottomLeftRadius", "gap", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
                        "borderWidth", "minWidth", "maxWidth", "minHeight", "maxHeight"}) | COLOR_KEYS


def _flatten(prefix: str, value: Any):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(f"{prefix}.{k}", v)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _flatten(f"{prefix}[{i}]", v)
    else:
        yield prefix, value


def _num_tol(key: str, approx: dict | None) -> float:
    if approx is None or key in PX_KEYS_EXACT:
        return 1e-6
    return float(approx["font_size_px"] if key == "fontSize" else approx["px"]) + 1e-6


def style_map_errors(map_data: dict, facts: tuple[dict[str, set], set[str]], label: str,
                     max_tolerance_px: float | None = None, approx: dict | None = None) -> list[str]:
    """Every expected value must match a value of the SAME property in the frame contract. `approx` (the workspace
    approx limits) loosens numbers (px / font size) and colors (CIE76) for approximate frames only."""
    by_key, colors = facts
    errors: list[str] = []
    entries = map_data.get("entries")
    if not isinstance(entries, list) or not entries:
        return [f"{label}: style map has no entries"]
    for entry in entries:
        eid = entry.get("id", "?")
        if not entry.get("selector"):
            errors.append(f"{label}: entry {eid} has no selector")
        tol = entry.get("tolerance_px")
        if tol is not None and (not is_num(tol) or tol < 0):
            errors.append(f"{label}: entry {eid} tolerance_px must be a number >= 0")
        elif max_tolerance_px is not None and tol is not None and tol > max_tolerance_px:
            errors.append(f"{label}: entry {eid} tolerance_px {tol} exceeds limits.max_tolerance_px {max_tolerance_px}")
        expect = entry.get("expect")
        if not isinstance(expect, dict) or not expect:
            errors.append(f"{label}: entry {eid} has no expect")
            continue
        for path, value in _flatten("expect", expect):
            prop = path[len("expect."):]
            key = re.split(r"[.\[]", prop, 1)[0]
            name = f"{label}: {eid}.{prop}={value!r}"
            if isinstance(value, bool) or value is None:
                continue
            color = expected_color(value)
            if key in COLOR_KEYS or color is not None:
                if color is None:
                    errors.append(f"{name} is not a color")
                elif key not in COLOR_KEYS:
                    errors.append(f"{name}: a color for a non-color property")
                elif color in colors:
                    continue
                elif approx is not None and any(delta_e76(color, c) <= approx["color_delta_e"] for c in colors):
                    continue
                else:
                    errors.append(f"{name} color not in the frame's fills/strokes"
                                  + (f" (within deltaE {approx['color_delta_e']})" if approx is not None else ""))
                continue
            if key not in KNOWN_KEYS:
                errors.append(f"{name}: no design property for {key!r} (cannot be backed by the frame contract)")
                continue
            found = by_key.get(key, set())
            if isinstance(value, str):
                m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)(?:px)?\s*", value)
                if m:
                    value = float(m.group(1))
                elif value.strip().lower() not in {str(f).strip().lower() for f in found if isinstance(f, str)}:
                    errors.append(f"{name} not in the frame's {key} values")
                    continue
                else:
                    continue
            if is_num(value) and not any(is_num(n) and abs(n - value) <= _num_tol(key, approx) for n in found):
                errors.append(f"{name} number not in the frame's {key} values"
                              + (f" (approx tolerance {approx['font_size_px'] if key == 'fontSize' else approx['px']})"
                                 if approx is not None and key not in PX_KEYS_EXACT else ""))
    return errors


# ---------- expect skeleton ----------

def _clean(v: float) -> float | int:
    v = round(float(v), 2)
    return int(v) if v == int(v) else v


def node_expect(node: dict) -> dict:
    """Style-name expectations of one node (first value of each property)."""
    exp: dict[str, Any] = {}
    for key, vals in node_props(node).items():
        v = vals[0]
        if is_num(v):
            if key in ("letterSpacing", "borderRadius", "borderTopLeftRadius", "borderTopRightRadius",
                       "borderBottomRightRadius", "borderBottomLeftRadius", "gap", "paddingTop", "paddingRight", "paddingBottom",
                       "paddingLeft", "borderWidth") and v == 0:
                continue
            if key == "opacity" and v >= 1:
                continue
            exp[key] = int(v) if key == "fontWeight" else _clean(v)
        else:
            exp[key] = v
    if isinstance(exp.get("text"), str) is False:
        exp.pop("text", None)
    return exp


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "node"


def build_skeleton(node_id: str, doc: dict, selector: str | None = None, tolerance_px: float = 1) -> dict:
    entries: list[dict] = []
    used: set[str] = set()
    for node in iter_nodes(doc.get("root")):
        if len(entries) >= 300:
            break
        exp = node_expect(node)
        if not exp:
            continue
        eid = _slug(str(node.get("name") or node.get("id") or "node"))
        if eid in used:
            eid = f"{eid}-{len(entries)}"
        used.add(eid)
        entries.append({"id": eid, "design_name": str(node.get("name") or node.get("id") or "node"),
                        "selector": selector if (not entries and selector) else "TODO", "expect": exp,
                        "tolerance_px": tolerance_px})
    return {"node": node_id, "entries": entries}


# ---------- review decision (approx frames) ----------

def frame_digest(run_id: str, node: str) -> str | None:
    """sha256 over the raw hashes a frame contract and its image rest on (binds a design review to what was reviewed)."""
    doc = read_frame(run_dir(run_id), node)
    if not doc:
        return None
    prov = doc.get("provenance") or {}
    img = ((doc.get("frame") or {}).get("image") or {}).get("path")
    img_sha = contracts.sha256_file(run_dir(run_id) / img) if isinstance(img, str) else None
    return contracts.sha256_bytes(f"{prov.get('raw_sha256')}|{img_sha}".encode("utf-8"))


def review_errors(run_id: str, run: dict) -> list[str]:
    """The review gate needs, per approx frame a check references, a user design-review decision recorded against
    the frame as it is now (`factory run decide <run> ... --by user --user-said ... --design-review <frame>`)."""
    out: list[str] = []
    for node in approx_frames(run_id):
        cur = frame_digest(run_id, node)
        recs = [d for d in run.get("decisions") or []
                if d.get("by") == "user" and d.get("design_review") == node]
        if not recs:
            out.append(f"{node}: approx design frame needs a user design review: ask the user to review the frame "
                       f"against the mockup, then `factory run decide {run_id} \"{node}: design reviewed\" --by user "
                       f"--user-said \"<gist>\" --design-review {node}`")
        elif not any(d.get("design_sha") == cur for d in recs):
            out.append(f"{node}: the design review predates a change to the frame or its image: review it again and "
                       f"record `--design-review {node}`")
    return out


def description_lines(run_id: str) -> list[str]:
    """Ship-description section listing approx frames (empty when every frame is exact)."""
    frames = approx_frames(run_id)
    if not frames:
        return []
    return ["", "## Design fidelity: approximate", ""] + [
        f"- {n}: approximate design (estimated measures, user design review recorded); not a source-of-truth match"
        for n in frames]
