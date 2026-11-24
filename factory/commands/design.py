"""`factory design add|check|expect`: design frame contracts, approximate frames and style-map skeletons.

    factory design add <run> <image> --name N [--fidelity approx] [--id ID] [--source-hint S]
    factory design check <run>
    factory design expect <run> <frame> [--selector S]

Exact frames come from a provider: the agent saves the raw frame and image under `raw/` and runs `factory ingest`.
`design add` is the approximate path (a mockup image with no source of truth): spec 7.1.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from .. import contracts, design, pngcrop
from ..core import FactoryError, GateFailed, read_json, run_dir, run_load, workspace_load, write_json
from . import ingest as ingest_cmd


def check_pack(run_id: str) -> list[str]:
    """Plan gate and `design check`: every visual/style frame has a fresh, valid contract and image."""
    run = run_load(run_id)
    ws = _workspace(run)
    return design.pack_errors(run_id, ws)


def _workspace(run: dict) -> dict:
    try:
        return workspace_load(run["workspace"]) if run.get("workspace") else {}
    except FactoryError:
        return {}


# ---------- add (approx) ----------

def normalize_png(data: bytes) -> tuple[bytes, str | None]:
    """(png bytes, note). Re-encodes a decodable 8-bit PNG without ancillary chunks; an undecodable but valid PNG
    variant (16-bit, interlaced) is kept as is with a note."""
    contracts.png_info(data)  # ValueError for a non-PNG
    try:
        img = pngcrop.read_png(data)
        if img:
            return pngcrop.write_png(img, (0, 0, img["w"], img["h"])), None
    except (zlib.error, struct.error, IndexError):
        pass
    return data, "PNG kept byte-for-byte (not 8-bit non-interlaced, cannot be re-encoded here)"


def skeleton_frame(ident: str, name: str, width: int, height: int) -> dict:
    return {"schema": "design_frame.v1", "fidelity": "approx", "frame": {"id": ident, "name": name},
            "root": {"id": ident, "name": name, "type": "frame", "box": {"x": 0, "y": 0, "w": width, "h": height},
                     "estimated": {"box": True}},
            "unknowns": [{"field": "root.children", "reason": "approximate frame: regions, measures, colors and texts "
                                                              "are written by the author from the image"}]}


def add(run_id: str, image: str, name: str, ident: str | None = None, fidelity: str = "approx",
        source_hint: str | None = None) -> dict:
    if fidelity != "approx":
        raise FactoryError("`design add` creates approximate frames only (--fidelity approx); an exact frame comes from "
                           "a design source: save its raw payload and image under raw/ and run `factory ingest`")
    run = run_load(run_id)
    src = Path(image).expanduser()
    if not src.is_file():
        raise FactoryError(f"image not found: {image}")
    try:
        png, note = normalize_png(src.read_bytes())
    except ValueError as err:
        raise FactoryError(f"{image}: {err}. Only PNG is read here: convert the image to PNG first with any tool and "
                           "record the conversion command in --source-hint") from err
    ident = ident or name
    safe = contracts.safe_id(ident)
    rd = run_dir(run_id)
    dest = contracts.raw_path_for(rd, "design_image", safe, "png")
    ingest_cmd._atomic_bytes(dest, png)
    rel = dest.relative_to(rd).as_posix()
    img = ingest_cmd.ingest_raw(run_id, "design_image", rel, ident=ident, source_hint=source_hint, fmt="native", run=run)
    w, h = contracts.png_info(png)
    skel = skeleton_frame(ident, name, w, h)
    skel_path = contracts.raw_path_for(rd, "design_frame", safe, "json")
    write_json(skel_path, skel)
    fr = ingest_cmd.ingest_raw(run_id, "design_frame", skel_path.relative_to(rd).as_posix(), ident=ident,
                               source_hint=source_hint, fmt="native", run=run)
    return {"id": ident, "image": img["contract"], "frame": fr["contract"], "raw_frame": skel_path.relative_to(rd).as_posix(),
            "width": w, "height": h, "note": note}


# ---------- expect ----------

def expect(run_id: str, node: str, selector: str | None = None) -> Path:
    rd = run_dir(run_id)
    doc, problems = contracts.load_contract(run_id, "design_frame", node)
    if doc is None or problems:
        raise FactoryError(f"design_frame {node} is not usable: " + "; ".join(problems or ["missing contract"]))
    ws = _workspace(run_load(run_id))
    tol = design.approx_limits(ws)["px"] if doc.get("fidelity") == "approx" else 1
    skeleton = design.build_skeleton(node, doc, selector, tol)
    if not skeleton["entries"]:
        raise FactoryError(f"no style values found in contract design_frame/{contracts.safe_id(node)}")
    out = rd / "checks" / "style" / f"{contracts.safe_id(node)}.skeleton.json"
    write_json(out, skeleton)
    return out


# ---------- CLI ----------

def _cmd_add(args) -> int:
    res = add(args.run, args.image, args.name, args.id, args.fidelity, args.source_hint)
    print(f"design add OK {res['id']} ({res['width']}x{res['height']}, fidelity approx)")
    print(f"  image contract: {res['image']}")
    print(f"  frame contract: {res['frame']} (skeleton: one root frame, box estimated)")
    if res["note"]:
        print(f"  note: {res['note']}")
    print(f"  next: the author writes {res['raw_frame']} in native format (regions, measures, colors, texts), EVERY number "
          f"and color listed in the node's `estimated` map, then `factory ingest {args.run} design_frame "
          f"{res['raw_frame']} --id {res['id']} --format native`")
    return 0


def _cmd_check(args) -> int:
    run = run_load(args.run)
    errs = design.pack_errors(args.run, _workspace(run))
    if errs:
        from .. import fetches
        raise GateFailed(errs, fetch=fetches.design_fetches(args.run, _workspace(run)))
    checks = design.acs_checks(args.run)
    nodes = design.frame_nodes(checks)
    approx = design.approx_frames(args.run, checks)
    print(f"OK design pack complete ({len(nodes)} frame(s), {len(approx)} approx)")
    for n in approx:
        print(f"  approx {n}: needs a user design review decision before the review gate")
    return 0


def _cmd_expect(args) -> int:
    run_load(args.run)
    out = expect(args.run, args.frame, args.selector)
    data = read_json(out)
    print(f"wrote {out} ({len(data['entries'])} entries; replace selector TODO before use)")
    return 0


def register(sub) -> None:
    p = sub.add_parser("design", help="design frame contracts: approx frames, pack check, style expectations")
    dsub = p.add_subparsers(dest="design_cmd", required=True)
    a = dsub.add_parser("add", help="approximate frame from a mockup image (PNG): image contract + frame skeleton")
    a.add_argument("run")
    a.add_argument("image")
    a.add_argument("--name", required=True)
    a.add_argument("--id", help="frame id (default: the name)")
    a.add_argument("--fidelity", choices=["approx", "exact"], default="approx")
    a.add_argument("--source-hint", dest="source_hint", help="what the agent ran to produce the PNG (conversion, export)")
    a.set_defaults(func=_cmd_add)
    c = dsub.add_parser("check", help="every visual/style frame has a fresh contract and image")
    c.add_argument("run")
    c.set_defaults(func=_cmd_check)
    e = dsub.add_parser("expect", help="style-map skeleton from a frame contract")
    e.add_argument("run")
    e.add_argument("frame")
    e.add_argument("--selector")
    e.set_defaults(func=_cmd_expect)
