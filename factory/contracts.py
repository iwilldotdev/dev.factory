"""Contract schemas (docs/design/provider-agnostic.md section 3 and 7). Python >= 3.10, stdlib only.

Pure-python validators, no jsonschema dependency. Each validator returns a list of violation strings (empty = valid).
Objects are strict: an unknown key is a violation, so a mapping typo is caught instead of silently shipped.

Also here: provenance, safe ids with collision detection, PNG header parsing, raw freshness (stale detection) and the
loader other steps use to read a contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Any, Iterable

from .core import FactoryError, run_dir

KINDS = ("ticket", "change", "change_list", "pipeline", "design_frame", "design_image")
KIND_ROLE = {"ticket": "tickets", "change": "scm", "change_list": "scm", "pipeline": "scm",
             "design_frame": "design", "design_image": "design"}
ROLES = ("tickets", "scm", "design")
KIND_EXT = {"design_image": "png"}  # default `ext` per kind; everything else json
PNG_SIG = b"\x89PNG\r\n\x1a\n"
STATES = ("open", "merged", "closed")
CI_STATUSES = ("pending", "running", "success", "failed", "canceled", "skipped")
NODE_TYPES = ("frame", "group", "text", "shape", "image", "component", "instance", "other")
PAINT_TYPES = ("solid", "gradient", "image")
EFFECT_TYPES = ("drop_shadow", "inner_shadow", "layer_blur", "bg_blur")
LAYOUT_MODES = ("none", "horizontal", "vertical")
FIDELITIES = ("exact", "approx")

# ---------- schema engine ----------

STR = ("str",)
INT = ("int",)
BOOL = ("bool",)
NUM = ("num",)
COLOR = ("color",)
ANY = ("any",)


def enum(*values: str) -> tuple:
    return ("enum", values)


def lst(item: tuple) -> tuple:
    return ("list", item)


def obj(req: dict, opt: dict | None = None) -> tuple:
    return ("obj", req, opt or {})


HEX = re.compile(r"#[0-9a-fA-F]{6}")
SHA = re.compile(r"[0-9a-f]{64}")
UNKNOWN = obj({"field": STR, "reason": STR})
THREAD_NOTE = obj({"author": STR, "body_md": STR, "system": BOOL}, {"path": STR, "line": INT})
THREAD = obj({"id": STR, "resolved": BOOL, "resolvable": BOOL, "system": BOOL, "notes": lst(THREAD_NOTE)})
APPROVALS = obj({"known": BOOL, "by": lst(STR)})
BOX = obj({"x": NUM, "y": NUM, "w": NUM, "h": NUM})
PAINT = obj({"type": enum(*PAINT_TYPES)}, {"color": COLOR, "opacity": NUM, "weight": NUM, "align": STR})
EFFECT = obj({"type": enum(*EFFECT_TYPES)}, {"color": COLOR, "offset": obj({"x": NUM, "y": NUM}), "radius": NUM, "spread": NUM})
LAYOUT = obj({"mode": enum(*LAYOUT_MODES)}, {"gap": NUM, "padding": obj({"t": NUM, "r": NUM, "b": NUM, "l": NUM})})
TEXT = obj({"characters": STR}, {"font_family": STR, "font_size": NUM, "font_weight": INT, "line_height_px": NUM,
                                 "letter_spacing": NUM, "align": STR, "color": COLOR})

CHANGE_LITE_REQ = {"repo": STR, "number": INT, "url": STR, "title": STR, "description_md": STR, "source_branch": STR,
                   "target_branch": STR, "head_sha": STR, "state": enum(*STATES), "draft": BOOL, "approvals": APPROVALS}
CHANGE_LITE_OPT = {"project": STR, "author": STR, "reviewers": lst(STR)}

SCHEMAS: dict[str, tuple] = {
    "ticket": obj(
        {"schema": enum("ticket.v1"), "key": STR, "title": STR, "type": STR, "is_bug": BOOL, "status": STR,
         "description_md": STR, "unknowns": lst(UNKNOWN)},
        {"url": STR, "acceptance_criteria": lst(STR), "labels": lst(STR), "parent": obj({"key": STR}, {"title": STR}),
         "assignee": obj({"id": STR}, {"name": STR}),
         "links": lst(obj({"kind": STR, "url": STR}, {"key": STR})),
         "attachments": lst(obj({"id": STR, "name": STR, "mime": STR, "url": STR}, {"size": INT})),
         "comments": lst(obj({"id": STR, "author": STR, "created": STR, "body_md": STR}))}),
    "change": obj(
        {"schema": enum("change.v1"), **CHANGE_LITE_REQ, "unknowns": lst(UNKNOWN)},
        {**CHANGE_LITE_OPT, "threads": lst(THREAD),
         "pipelines": lst(obj({"id": STR, "status": enum(*CI_STATUSES)}, {"sha": STR}))}),
    "change_list": obj({"schema": enum("change_list.v1"), "items": lst(obj(CHANGE_LITE_REQ, CHANGE_LITE_OPT)),
                        "unknowns": lst(UNKNOWN)}),
    "pipeline": obj(
        {"schema": enum("pipeline.v1"), "id": STR, "sha": STR, "status": enum(*CI_STATUSES),
         "jobs": lst(obj({"id": STR, "name": STR, "status": enum(*CI_STATUSES), "allow_failure": BOOL},
                         {"stage": STR, "url": STR})), "unknowns": lst(UNKNOWN)},
        {"url": STR}),
    "design_image": obj({"schema": enum("design_image.v1"), "path": STR, "sha256": STR, "width": INT, "height": INT,
                         "unknowns": lst(UNKNOWN)}),
}
SCHEMA_ORDER = {  # field order of a written contract (stable bytes, readable goldens)
    "ticket": ["schema", "key", "url", "title", "type", "is_bug", "status", "description_md", "acceptance_criteria",
               "labels", "parent", "assignee", "links", "attachments", "comments", "unknowns", "provenance"],
    "change": ["schema", "repo", "number", "url", "project", "title", "description_md", "source_branch",
               "target_branch", "head_sha", "state", "draft", "author", "reviewers", "approvals", "threads",
               "pipelines", "unknowns", "provenance"],
    "change_list": ["schema", "items", "unknowns", "provenance"],
    "pipeline": ["schema", "id", "url", "sha", "status", "jobs", "unknowns", "provenance"],
    "design_frame": ["schema", "fidelity", "frame", "source", "root", "unknowns", "provenance"],
    "design_image": ["schema", "path", "sha256", "width", "height", "unknowns", "provenance"],
}
PROVENANCE = obj({"profile": STR, "profile_digest": STR, "raw_path": STR, "raw_sha256": STR, "ingested_at": STR},
                 {"source_hint": STR, "id": STR, "format": STR, "item": INT,
                  "inputs": ("map", obj({"raw_path": STR, "raw_sha256": STR}))})
FRAME_NODE_FIELDS_PLAIN = {"id", "name", "type", "visible", "children", "estimated"}


def _node_spec() -> tuple:
    return ("node",)


def _check(v: Any, spec: tuple, path: str, errs: list[str]) -> None:
    tag = spec[0]
    if tag == "any":
        return
    if v is None:
        errs.append(f"{path}: null")
        return
    if tag == "str":
        if not isinstance(v, str):
            errs.append(f"{path}: expected string, got {_k(v)}")
    elif tag == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            errs.append(f"{path}: expected integer, got {_k(v)}")
    elif tag == "num":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errs.append(f"{path}: expected number, got {_k(v)}")
    elif tag == "bool":
        if not isinstance(v, bool):
            errs.append(f"{path}: expected boolean, got {_k(v)}")
    elif tag == "color":
        if not isinstance(v, str) or not HEX.fullmatch(v):
            errs.append(f"{path}: expected #rrggbb color, got {str(v)[:20]!r}")
    elif tag == "enum":
        if v not in spec[1]:
            errs.append(f"{path}: {str(v)[:40]!r} is not one of {list(spec[1])}")
    elif tag == "list":
        if not isinstance(v, list):
            errs.append(f"{path}: expected array, got {_k(v)}")
        else:
            for i, el in enumerate(v):
                _check(el, spec[1], f"{path}[{i}]", errs)
    elif tag == "map":
        if not isinstance(v, dict):
            errs.append(f"{path}: expected object, got {_k(v)}")
        else:
            for k, el in v.items():
                _check(el, spec[1], f"{path}.{k}", errs)
    elif tag == "obj":
        if not isinstance(v, dict):
            errs.append(f"{path}: expected object, got {_k(v)}")
            return
        req, opt = spec[1], spec[2]
        for k, s in req.items():
            if k not in v:
                errs.append(f"{path}.{k}: required" if path else f"{k}: required")
            else:
                _check(v[k], s, f"{path}.{k}" if path else k, errs)
        for k, el in v.items():
            if k in req:
                continue
            if k in opt:
                if el is not None:
                    _check(el, opt[k], f"{path}.{k}" if path else k, errs)
            else:
                errs.append(f"{path}.{k}: unknown field" if path else f"{k}: unknown field")
    elif tag == "node":
        _check_node(v, path, errs, 0)


def _k(v: Any) -> str:
    return "null" if v is None else "boolean" if isinstance(v, bool) else type(v).__name__


NODE_REQ = {"id": STR, "name": STR, "type": enum(*NODE_TYPES), "box": BOX}
NODE_OPT = {"visible": BOOL, "render_box": BOX, "opacity": NUM, "fills": lst(PAINT), "strokes": lst(PAINT),
            "effects": lst(EFFECT), "corner_radius": NUM, "corner_radii": lst(NUM), "layout": LAYOUT, "text": TEXT,
            "estimated": ("map", ("true",))}
NODE_CAP = 20000


def _check_node(node: Any, path: str, errs: list[str], depth: int) -> None:
    if depth > 200:
        errs.append(f"{path}: tree deeper than 200")
        return
    if not isinstance(node, dict):
        errs.append(f"{path}: expected object, got {_k(node)}")
        return
    plain = {k: v for k, v in node.items() if k != "children"}
    _check(plain, obj(NODE_REQ, NODE_OPT), path, errs)
    est = node.get("estimated")
    if isinstance(est, dict):
        for k, v in est.items():
            if v is not True:
                errs.append(f"{path}.estimated.{k}: must be true")
    radii = node.get("corner_radii")
    if isinstance(radii, list) and len(radii) != 4:
        errs.append(f"{path}.corner_radii: expected 4 numbers [tl, tr, br, bl], got {len(radii)}")
    op = node.get("opacity")
    if isinstance(op, (int, float)) and not isinstance(op, bool) and not 0 <= op <= 1:
        errs.append(f"{path}.opacity: must be within 0-1")
    kids = node.get("children")
    if kids is not None:
        if not isinstance(kids, list):
            errs.append(f"{path}.children: expected array")
        else:
            for i, k in enumerate(kids):
                _check_node(k, f"{path}.children[{i}]", errs, depth + 1)


_DESIGN_FRAME = obj(
    {"schema": enum("design_frame.v1"), "fidelity": enum(*FIDELITIES),
     "frame": obj({"id": STR, "name": STR}, {"image": obj({"path": STR, "width": INT, "height": INT, "scale": NUM})}),
     "root": _node_spec(), "unknowns": lst(UNKNOWN)},
    {"source": obj({"file": STR}, {"version": STR, "fetched_at": STR})})
SCHEMAS["design_frame"] = _DESIGN_FRAME


# ---------- approx rule ----------

def _leaves(v: Any, path: str):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from _leaves(x, f"{path}.{k}" if path else k)
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from _leaves(x, f"{path}.{i}" if path else str(i))
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        yield path, v
    elif isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6,8}", v):
        yield path, v


def _covered(path: str, est: dict) -> bool:
    parts = path.split(".")
    return any(est.get(".".join(parts[:i])) is True for i in range(1, len(parts) + 1))


def approx_violations(doc: dict) -> list[str]:
    """Every numeric or color value of an approx frame's nodes must be listed (itself or a parent path) in the node's
    `estimated` map; an exact frame lists none."""
    errs: list[str] = []
    fidelity = doc.get("fidelity")

    def walk(node: Any, trail: str, depth: int) -> None:
        if not isinstance(node, dict) or depth > 200:
            return
        label = f"{trail}/{node.get('id', '?')}"
        est = node.get("estimated") if isinstance(node.get("estimated"), dict) else {}
        if fidelity == "exact" and est:
            errs.append(f"node {label}: an exact frame cannot list `estimated` values")
        if fidelity == "approx":
            for k, v in node.items():
                if k in FRAME_NODE_FIELDS_PLAIN:
                    continue
                for path, val in _leaves(v, k):
                    if not _covered(path, est):
                        errs.append(f"node {label}: value {val!r} at {path} is not flagged in `estimated`")
        for kid in node.get("children") or []:
            walk(kid, label, depth + 1)

    root = doc.get("root")
    if isinstance(root, dict):
        walk(root, "", 0)
    return errs


# ---------- public validation ----------

def contract_fields(kind: str) -> tuple[list[str], list[str]]:
    """(required, optional) top-level fields a MAPPING supplies (schema, unknowns and provenance are added by ingest)."""
    spec = SCHEMAS[kind]
    skip = {"schema", "unknowns", "provenance"}
    order = SCHEMA_ORDER[kind]
    req = [k for k in order if k in spec[1] and k not in skip]
    opt = [k for k in order if k in spec[2] and k not in skip]
    return req, opt


def native_allowed(kind: str, doc: Any) -> bool:
    """A contract written by an agent without a profile (`--format native`) is trusted only where no provider
    payload exists: approximate design frames (hand-described from a mockup) and design images."""
    if kind == "design_image":
        return True
    return kind == "design_frame" and isinstance(doc, dict) and doc.get("fidelity") == "approx"


def validate(kind: str, doc: Any, provenance: bool = True, refs: dict | None = None) -> list[str]:
    """Violations of `<kind>.v1` (empty = valid). `provenance=False` for a freshly mapped contract. `refs` (profile
    refs) additionally checks the ticket key against `refs.ticket_key`."""
    if kind not in SCHEMAS:
        return [f"unknown kind {kind!r}; kinds: {', '.join(KINDS)}"]
    if not isinstance(doc, dict):
        return [f"contract must be an object, got {_k(doc)}"]
    spec = SCHEMAS[kind]
    body = {k: v for k, v in doc.items() if k != "provenance"}
    errs: list[str] = []
    _check(body, spec, "", errs)
    errs = [e[1:] if e.startswith(".") else e for e in errs]
    if provenance:
        if "provenance" not in doc:
            errs.append("provenance: required")
        else:
            perr: list[str] = []
            _check(doc["provenance"], PROVENANCE, "provenance", perr)
            errs += perr
            prov = doc["provenance"] if isinstance(doc["provenance"], dict) else {}
            if isinstance(prov.get("raw_sha256"), str) and not SHA.fullmatch(prov["raw_sha256"]):
                errs.append("provenance.raw_sha256: expected 64 hex characters")
            if prov.get("format") == "native" and not native_allowed(kind, doc):
                errs.append("provenance.format: `native` contracts are accepted only for approx design frames and design "
                            f"images; map this {kind} through a profile (`factory ingest ... --profile`)")
    elif "provenance" in doc:
        errs.append("provenance: not allowed before ingest")
    if kind == "design_frame" and not errs:
        errs += approx_violations(doc)
    if kind == "ticket" and refs and isinstance(doc.get("key"), str) and isinstance(refs.get("ticket_key"), str):
        try:
            if not re.fullmatch(refs["ticket_key"], doc["key"]):
                errs.append(f"key: {doc['key']!r} does not match refs.ticket_key")
        except re.error:
            pass
    if kind == "design_image" and not errs:
        for k in ("width", "height"):
            if doc[k] <= 0:
                errs.append(f"{k}: must be positive")
    return errs


def order_contract(kind: str, doc: dict) -> dict:
    order = SCHEMA_ORDER[kind]
    out = {k: doc[k] for k in order if k in doc}
    out.update({k: v for k, v in doc.items() if k not in out})
    return out


# ---------- PNG ----------

def png_info(data: bytes) -> tuple[int, int]:
    """(width, height) of a PNG: signature, IHDR first chunk, non-zero size. ValueError describes the problem."""
    if len(data) < 24 or not data.startswith(PNG_SIG):
        raise ValueError("not a PNG (bad signature)")
    if data[12:16] != b"IHDR":
        raise ValueError("PNG without an IHDR chunk")
    w, h = struct.unpack(">II", data[16:24])
    if w <= 0 or h <= 0:
        raise ValueError(f"PNG with zero size ({w}x{h})")
    return w, h


# ---------- hashing / ids ----------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_id(value: str) -> str:
    """File-name stem of an entity id: characters outside [A-Za-z0-9._-] become `-` ('3544:5710' -> '3544-5710')."""
    if not isinstance(value, str) or not value.strip():
        raise FactoryError("entity id is empty")
    out = _UNSAFE.sub("-", value.strip())
    if not out.strip("."):
        raise FactoryError(f"entity id {value!r} is not usable as a file name")
    return out


def id_collisions(ids: Iterable[str]) -> list[str]:
    """Messages for ids that share a safe name (design 14.7): one line per colliding pair, naming both."""
    seen: dict[str, str] = {}
    out: list[str] = []
    for i in ids:
        s = safe_id(i)
        if s in seen and seen[s] != i:
            out.append(f"ids {seen[s]!r} and {i!r} both map to the file name {s!r}")
        else:
            seen.setdefault(s, i)
    return out


def utc_stamp() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- run layout / freshness ----------

def contract_path(rd: Path, kind: str, safe: str) -> Path:
    return Path(rd) / "contracts" / kind / f"{safe}.json"


def raw_path_for(rd: Path, kind: str, safe: str, ext: str | None = None) -> Path:
    return Path(rd) / "raw" / kind / f"{safe}.{ext or KIND_EXT.get(kind, 'json')}"


def stale_reasons(rd: Path, doc: dict) -> list[str]:
    """Why a contract no longer matches its raw files (empty = fresh): invariant 3."""
    prov = doc.get("provenance") if isinstance(doc, dict) else None
    if not isinstance(prov, dict):
        return ["no provenance"]
    out: list[str] = []
    pairs = [("main", prov.get("raw_path"), prov.get("raw_sha256"))]
    for name, rec in (prov.get("inputs") or {}).items():
        if isinstance(rec, dict):
            pairs.append((name, rec.get("raw_path"), rec.get("raw_sha256")))
    for name, rel, sha in pairs:
        if not isinstance(rel, str):
            out.append(f"raw {name}: no raw_path")
            continue
        p = Path(rd) / rel
        now = sha256_file(p)
        if now is None:
            out.append(f"raw {rel} is missing")
        elif now != sha:
            out.append(f"raw {rel} changed since ingest (sha256 {now[:12]} != {str(sha)[:12]})")
    return out


def load_contract(run_id: str, kind: str, ident: str) -> tuple[dict | None, list[str]]:
    """(contract, problems). `ident` is the entity id (made safe here). A contract with problems is not usable as
    proof: missing, unreadable, schema violations or a stale raw. Gates read contracts through this."""
    rd = run_dir(run_id)
    path = contract_path(rd, kind, safe_id(ident))
    if not path.is_file():
        return None, [f"missing contract {kind}/{safe_id(ident)}"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return None, [f"unreadable contract {path.name}: {err}"]
    problems = [f"{kind}/{safe_id(ident)}: {e}" for e in validate(kind, doc)]
    problems += [f"{kind}/{safe_id(ident)}: {e}" for e in stale_reasons(rd, doc)]
    return doc, problems


def load_all(run_id: str, kind: str) -> tuple[list[dict], list[str]]:
    """Every fresh, valid contract of a kind in the run, plus one line per unusable one (invalid or stale)."""
    out: list[dict] = []
    bad: list[str] = []
    d = run_dir(run_id) / "contracts" / kind
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        doc, problems = load_contract(run_id, kind, p.stem)
        if doc is not None and not problems:
            out.append(doc)
        else:
            bad.extend(problems)
    return out, bad
