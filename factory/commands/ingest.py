"""`factory ingest`: turn a raw provider payload into a validated contract (docs/design/provider-agnostic.md sections 2, 6).

    factory ingest <run> <kind> <raw> [--id ID] [--repo A] [--profile P] [--raw name=path ...] [--source-hint S]
                   [--format native] [--stdin] [--ctx name=value ...]
    factory ingest <run> --check

The CLI never touches the network: the agent saves the payload (any tool), this command maps it with the profile of
the workspace role and writes `R/contracts/<kind>/<id>.json` atomically, with provenance. A contract is valid only
while the sha256 of its raw file(s) is unchanged (`--check` reports stale ones).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from .. import contracts, mapping, profiles
from ..core import (FactoryError, GateFailed, repo_conf, run_dir, run_load, workspace_load, write_json)

NUMBER_RE = re.compile(r"(?:^|[-#_/])(\d+)$")
# context names the CLI derives itself (from --id, --repo and the workspace): `--ctx` cannot override them
RESERVED_CTX = {"id", "key", "number", "repo", "scm_project"}


def _out(args, payload: dict, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(text)


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _inside(rd: Path, p: Path) -> str | None:
    try:
        return p.resolve().relative_to(rd.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _locate(rd: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute() and not p.is_file() and (rd / p).is_file():
        p = rd / p
    if not p.is_file():
        raise FactoryError(f"raw file not found: {raw}")
    return p


def _place(rd: Path, src: Path, dest: Path) -> str:
    """Run-relative posix path of the raw: where it already is when inside the run dir, else a copy at `dest`."""
    rel = _inside(rd, src)
    if rel is not None and rel.startswith("raw/"):
        return rel
    if src.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_bytes(dest, src.read_bytes())
    return dest.relative_to(rd).as_posix()


def _claim_id(rd: Path, kind: str, safe: str, ident: str) -> None:
    """Two entity ids that share a safe file name in one run are an ERROR naming both (design 14.7)."""
    path = contracts.contract_path(rd, kind, safe)
    if not path.is_file():
        return
    try:
        prev = json.loads(path.read_text(encoding="utf-8")).get("provenance", {}).get("id")
    except (OSError, ValueError, AttributeError):
        return
    if isinstance(prev, str) and prev != ident:
        raise FactoryError(f"id collision in {kind}: {prev!r} and {ident!r} both map to the file name {safe!r}; "
                           "pass a distinct --id for one of them")


def _number_of(ident: str) -> int | None:
    m = NUMBER_RE.search(ident)
    return int(m.group(1)) if m else None


def _frame_image(rd: Path, safe: str, contract: dict) -> None:
    """Fill `frame.image` from the design_image contract of the same id when the mapping left it out."""
    frame = contract.get("frame")
    if not isinstance(frame, dict) or "image" in frame:
        return
    path = contracts.contract_path(rd, "design_image", safe)
    try:
        img = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(img, dict) and all(k in img for k in ("path", "width", "height")):
        frame["image"] = {"path": img["path"], "width": img["width"], "height": img["height"], "scale": 1}


def ingest_raw(run_id: str, kind: str, raw: str | None, *, ident: str | None = None, repo: str | None = None,
               profile: str | None = None, extra: dict[str, str] | None = None, source_hint: str | None = None,
               fmt: str | None = None, ctx_extra: dict[str, str] | None = None, stdin_bytes: bytes | None = None,
               run: dict | None = None) -> dict:
    """Map one raw payload to `R/contracts/<kind>/<safe id>.json`. Returns a summary dict. Raises FactoryError
    (exit 2) with every mapping error listed; nothing is written on error."""
    if kind not in contracts.KINDS:
        raise FactoryError(f"unknown kind {kind!r}; kinds: {', '.join(contracts.KINDS)}")
    if fmt not in (None, "native"):
        raise FactoryError("--format only accepts `native`")
    if fmt == "native" and kind not in ("design_frame", "design_image"):
        raise FactoryError(f"--format native is not allowed for {kind}: a {kind} comes from a provider payload and must be "
                           "mapped by the profile (native is for approx design frames and design images only)")
    for reserved in (ctx_extra or {}):
        if reserved in RESERVED_CTX:
            raise FactoryError(f"--ctx {reserved}=... is reserved (set by the CLI: {', '.join(sorted(RESERVED_CTX))}); "
                               "use --id / --repo, or another name")
    run = run or run_load(run_id)
    rd = run_dir(run_id)
    if raw is None and stdin_bytes is None:
        raise FactoryError("give the raw file path, or --stdin with --id")
    if ident is None:
        if raw is None:
            raise FactoryError("--stdin needs --id")
        ident = Path(raw).stem
    safe = contracts.safe_id(ident)
    _claim_id(rd, kind, safe, ident)

    # profile (not needed for native contracts)
    prof_name, prof, ws = "native", None, None
    wanted_profile = fmt != "native" and kind != "design_image"
    if fmt != "native":
        try:
            ws = workspace_load(run["workspace"]) if run.get("workspace") else {}
            prof_name, prof = profiles.profile_for(ws, kind, profile)
        except FactoryError:
            if wanted_profile:
                raise
            prof_name, prof = "native", None
    ext = profiles.kind_ext(prof, kind) if prof else contracts.KIND_EXT.get(kind, "json")
    if prof is not None and profiles.kind_spec(prof, kind) is None and wanted_profile:
        raise FactoryError(f"profile {prof_name!r} declares no kind {kind!r}; declared: "
                           f"{', '.join(profiles.declared_kinds(prof)) or 'none'}; samples: "
                           f"{profiles.sample_dir(prof_name, kind)}")

    # raw placement
    dest = contracts.raw_path_for(rd, kind, safe, ext)
    if stdin_bytes is not None:
        _atomic_bytes(dest, stdin_bytes)
        src = dest
    else:
        src = _locate(rd, raw or "")
    raw_rel = _place(rd, src, dest)
    raw_file = rd / raw_rel
    raw_sha = contracts.sha256_file(raw_file)
    if raw_sha is None:
        raise FactoryError(f"cannot read raw {raw_file}")
    inputs: dict[str, dict] = {}
    extras: dict[str, Path] = {}
    for name, rawp in (extra or {}).items():
        sp = _locate(rd, rawp)
        rel = _place(rd, sp, contracts.raw_path_for(rd, kind, f"{safe}.{name}", ext))
        extras[name] = rd / rel
        inputs[name] = {"raw_path": rel, "raw_sha256": contracts.sha256_file(rd / rel) or ""}

    unknown_notes: list[dict] = []
    if kind == "design_image":
        try:
            w, h = contracts.png_info(raw_file.read_bytes())
        except ValueError as err:
            raise FactoryError(f"ingest design_image {ident}: {err}") from err
        contract: dict[str, Any] = {"schema": "design_image.v1", "path": raw_rel, "sha256": raw_sha, "width": w,
                                    "height": h, "unknowns": []}
    elif fmt == "native":
        data = profiles.load_raw_json(raw_file)
        if not isinstance(data, dict):
            raise FactoryError(f"native {kind} must be a JSON object")
        contract = {k: v for k, v in data.items() if k != "provenance"}
        contract.setdefault("unknowns", [])
        if kind == "design_frame":
            if contract.get("fidelity") != "approx":
                raise FactoryError(f"ingest design_frame {ident}: --format native is accepted only for fidelity approx "
                                   "(hand-described from a mockup); an exact frame comes from the design provider and "
                                   "must be mapped by the profile")
            _frame_image(rd, safe, contract)
        problems = contracts.validate(kind, contract, provenance=False)
        if problems:
            raise FactoryError(f"ingest {kind} {ident} (native): {len(problems)} violation(s):\n  - " + "\n  - ".join(problems))
    else:
        assert prof is not None
        names = profiles.kind_inputs(prof, kind)
        optional = profiles.kind_optional_inputs(prof, kind)
        missing = [n for n in names if n != "main" and n not in extras and n not in optional]
        stray = [n for n in extras if n not in names]
        if missing:
            raise FactoryError(f"{kind} needs additional raw input(s) {missing}: pass --raw {missing[0]}=<path>")
        if stray:
            raise FactoryError(f"profile {prof_name!r} kind {kind!r} has no input(s) {stray} (inputs: {names})")
        docs: dict[str, Any] = {"main": profiles.load_raw_json(raw_file)}
        for n, p in extras.items():
            docs[n] = profiles.load_raw_json(p)
        ctx: dict[str, Any] = {"id": ident, "key": ident}
        num = _number_of(ident)
        if num is not None:
            ctx["number"] = num
        if repo:
            ctx["repo"] = repo
            try:
                proj = repo_conf(ws or {}, repo).get("scm_project")
            except FactoryError:
                proj = None
            if proj:
                ctx["scm_project"] = proj
        ctx.update(ctx_extra or {})
        try:
            res = profiles.map_docs(prof, kind, docs, ctx, profiles.mapping_timeout(ws))
        except mapping.MappingFailure as err:
            raise FactoryError(f"ingest {kind} {ident}: {len(err.errors)} error(s) (profile {prof_name}):\n  - "
                               + "\n  - ".join(err.errors)) from err
        contract = res["contract"]
        if kind == "design_frame":
            _frame_image(rd, safe, contract)
            problems = contracts.validate(kind, contract, provenance=False)
            if problems:
                raise FactoryError(f"ingest {kind} {ident}: contract violation:\n  - " + "\n  - ".join(problems))
        unknown_notes = res["unknowns"]

    prov: dict[str, Any] = {
        "profile": prof_name if prof is not None else "native",
        "profile_digest": profiles.digest(prof) if prof is not None else "native",
        "raw_path": raw_rel, "raw_sha256": raw_sha, "ingested_at": contracts.utc_stamp(), "id": ident}
    if source_hint:
        prov["source_hint"] = source_hint
    if fmt == "native":
        prov["format"] = "native"
    if inputs:
        prov["inputs"] = inputs
    contract["provenance"] = prov
    contract = contracts.order_contract(kind, contract)
    problems = contracts.validate(kind, contract)
    if problems:
        raise FactoryError(f"ingest {kind} {ident}: contract violation:\n  - " + "\n  - ".join(problems))
    out_path = contracts.contract_path(rd, kind, safe)
    write_json(out_path, contract)
    return {"kind": kind, "id": ident, "safe_id": safe, "contract": out_path.relative_to(rd).as_posix(),
            "raw_path": raw_rel, "raw_sha256": raw_sha, "profile": prov["profile"],
            "unknowns": contract.get("unknowns", []) or unknown_notes}


def check_run(run_id: str) -> dict:
    """{fresh: [...], stale: [{contract, reasons}], invalid: [...], profile_changed: [...]} for every contract of a run."""
    rd = run_dir(run_id)
    res: dict[str, list] = {"fresh": [], "stale": [], "invalid": [], "profile_changed": []}
    base = rd / "contracts"
    for p in sorted(base.glob("*/*.json")) if base.is_dir() else []:
        rel = p.relative_to(rd).as_posix()
        kind = p.parent.name
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            res["invalid"].append({"contract": rel, "reasons": [f"unreadable: {err}"]})
            continue
        bad = contracts.validate(kind, doc) if kind in contracts.KINDS else [f"unknown kind {kind!r}"]
        if bad:
            res["invalid"].append({"contract": rel, "reasons": bad})
            continue
        stale = contracts.stale_reasons(rd, doc)
        if stale:
            res["stale"].append({"contract": rel, "reasons": stale})
            continue
        res["fresh"].append(rel)
        prov = doc["provenance"]
        if prov.get("profile") != "native":
            try:
                now = profiles.digest(profiles.load_approved(prov["profile"]))
            except FactoryError:
                now = None
            if now != prov.get("profile_digest"):
                res["profile_changed"].append({"contract": rel, "profile": prov["profile"],
                                               "reasons": ["profile missing or not approved" if now is None
                                                           else "profile changed since ingest (advisory)"]})
    return res


def cmd_ingest(args) -> int:
    run_load(args.run)  # run must exist
    if args.check:
        res = check_run(args.run)
        lines = [f"stale {s['contract']}: {'; '.join(s['reasons'])}" for s in res["stale"]]
        lines += [f"invalid {s['contract']}: {'; '.join(s['reasons'][:3])}" for s in res["invalid"]]
        notes = [f"note {s['contract']}: {s['reasons'][0]} ({s['profile']})" for s in res["profile_changed"]]
        if lines:
            if getattr(args, "json", False):
                print(json.dumps({"passed": False, **res}, ensure_ascii=False, indent=2))
                return 1
            raise GateFailed(lines + notes + [f"re-ingest the stale ones: factory ingest {args.run} <kind> <raw>"])
        _out(args, {"passed": True, **res}, "\n".join([f"OK {len(res['fresh'])} contract(s) fresh"] + notes))
        return 0
    if not args.kind:
        raise FactoryError("usage: factory ingest <run> <kind> <raw> | factory ingest <run> --check")
    extra: dict[str, str] = {}
    for item in args.extra_raw or []:
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise FactoryError(f"--raw expects name=path, got {item!r}")
        extra[name] = path
    ctx_extra: dict[str, str] = {}
    for item in args.ctx or []:
        name, sep, val = item.partition("=")
        if not sep or not name:
            raise FactoryError(f"--ctx expects name=value, got {item!r}")
        ctx_extra[name] = val
    data = None
    if args.stdin:
        data = sys.stdin.buffer.read()
        if not data:
            raise FactoryError("--stdin: nothing on standard input")
    res = ingest_raw(args.run, args.kind, args.raw, ident=args.id, repo=args.repo, profile=args.profile, extra=extra,
                     source_hint=args.source_hint, fmt=args.format, ctx_extra=ctx_extra, stdin_bytes=data)
    unk = res["unknowns"]
    text = [f"INGEST OK {res['kind']}/{res['id']} -> {res['contract']} (profile {res['profile']}, {len(unk)} unknown(s))"]
    text += [f"  unknown {u['field']}: {u['reason']}" for u in unk]
    _out(args, res, "\n".join(text))
    return 0


def register(sub) -> None:
    p = sub.add_parser("ingest", help="map a raw payload to a validated contract (or --check freshness)")
    p.add_argument("run")
    p.add_argument("kind", nargs="?", help=f"one of {', '.join(contracts.KINDS)}")
    p.add_argument("raw", nargs="?", help="raw file (copied under raw/<kind>/ when outside the run)")
    p.add_argument("--id", help="entity id (default: the raw file stem)")
    p.add_argument("--repo", help="workspace repo alias (ingest context `repo`)")
    p.add_argument("--profile", help="profile name (default: workspace providers.<role of the kind>)")
    p.add_argument("--raw", dest="extra_raw", action="append", metavar="NAME=PATH", help="additional named raw input")
    p.add_argument("--ctx", action="append", metavar="NAME=VALUE", help="extra ingest context value")
    p.add_argument("--source-hint", dest="source_hint", help="what the agent actually ran to fetch the raw")
    p.add_argument("--format", choices=["native"], help="raw already conforms to the contract (no mapping)")
    p.add_argument("--stdin", action="store_true", help="write standard input to raw/<kind>/<id>.<ext> first")
    p.add_argument("--check", action="store_true", help="re-hash every raw and report stale contracts")
    p.set_defaults(func=cmd_ingest)
