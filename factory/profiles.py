"""Provider profiles (docs/design/provider-agnostic.md section 5). Python >= 3.10, stdlib only.

A profile is DATA under `FACTORY_HOME/profiles/<name>/profile.json` with raw `samples/<kind>/<n>.<ext>`, optional
goldens `<n>.expected.json`, optional per-sample ingest context `<n>.ctx.json` and extra named inputs
`<n>.<input>.json` (e.g. `1.jobs.json`). This module loads, digests, lints and checks profiles and renders hints;
the engine is `mapping.py`, the schemas are `contracts.py`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import contracts, mapping
from .core import FactoryError, factory_home

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PROFILE_SCHEMA = "profile.v1"
TOP_KEYS = {"schema", "name", "role", "refs", "kinds", "hints", "cli_write_patterns", "mcp_read_allow", "save_args",
            "notes", "accepted_unknowns"}
KIND_KEYS = {"ext", "map", "optional", "inputs", "fidelity"}
REF_GROUPS = {"ticket_key": {"key"}, "ticket_url": {"key"}, "change_url": {"project", "number"}}
# `file` is used by the spec's own example hint for design kinds
HINT_PLACEHOLDERS = {"key", "id", "number", "scm_project", "repo", "sha", "raw_path", "raw_dir", "file"}
DEFAULT_SAVE_ARGS = ("save_to", "output_path", "output_file", "out", "dest")
ROLE_KINDS = {role: [k for k in contracts.KINDS if contracts.KIND_ROLE[k] == role] for role in contracts.ROLES}


DEFAULT_MAPPING_TIMEOUT_S = 10.0


def profiles_root() -> Path:
    """Approved profiles (read by gates and ingest). Workers never write here."""
    return factory_home() / "profiles"


def drafts_root() -> Path:
    """Profile drafts: the only place a worker writes profile files (`factory profile approve` promotes one)."""
    return factory_home() / "profile-drafts"


def profile_dir(name: str, draft: bool = False) -> Path:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise FactoryError(f"invalid profile name {name!r} (letters, digits, . _ -)")
    return (drafts_root() if draft else profiles_root()) / name


def canonical(doc: Any) -> str:
    return contracts.canonical_json(doc)


def digest(doc: dict) -> str:
    """`sha256:<hex>` of the canonical JSON of profile.json (samples are files next to it and never part of it)."""
    body = {k: v for k, v in doc.items() if k != "samples"}
    return "sha256:" + hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def roles_of(doc: dict) -> list[str]:
    r = doc.get("role")
    return [r] if isinstance(r, str) else [x for x in r if isinstance(x, str)] if isinstance(r, list) else []


def load_profile(name: str, draft: bool = False) -> dict:
    """Read profile.json as is (no approval check): for `profile test|check` and tooling. Gates and ingest go
    through `load_approved` / `profile_for`."""
    path = profile_dir(name, draft) / "profile.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        root = drafts_root() if draft else profiles_root()
        have = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() else []
        raise FactoryError(f"profile {name!r} not found at {path} ({'drafts' if draft else 'profiles'}: "
                           f"{', '.join(have) or 'none'})") from err
    except (OSError, ValueError) as err:
        raise FactoryError(f"profile {name!r}: unreadable profile.json: {err}") from err
    if not isinstance(doc, dict) or doc.get("schema") != PROFILE_SCHEMA:
        raise FactoryError(f"profile {name!r}: schema must be {PROFILE_SCHEMA!r}")
    if doc.get("name") != name:
        raise FactoryError(f"profile {name!r}: `name` is {doc.get('name')!r}; it must equal the folder name")
    return doc


def kind_spec(doc: dict, kind: str) -> dict | None:
    k = (doc.get("kinds") or {}).get(kind)
    return k if isinstance(k, dict) else None


def declared_kinds(doc: dict) -> list[str]:
    return [k for k in contracts.KINDS if isinstance(doc.get("kinds"), dict) and k in doc["kinds"]]


def kind_ext(doc: dict, kind: str) -> str:
    spec = kind_spec(doc, kind) or {}
    ext = spec.get("ext")
    return ext if isinstance(ext, str) and ext else contracts.KIND_EXT.get(kind, "json")


def kind_inputs(doc: dict, kind: str) -> list[str]:
    """Input names of a kind (`main` first); a trailing `?` in the profile (`jobs?`) is stripped here."""
    spec = kind_spec(doc, kind) or {}
    ins = spec.get("inputs")
    return [x.rstrip("?") for x in ins if isinstance(x, str)] if isinstance(ins, list) else ["main"]


def kind_optional_inputs(doc: dict, kind: str) -> set[str]:
    """Inputs declared with a trailing `?`: ingest does not require `--raw <name>=...` for them."""
    spec = kind_spec(doc, kind) or {}
    ins = spec.get("inputs")
    return {x[:-1] for x in ins if isinstance(x, str) and x.endswith("?")} if isinstance(ins, list) else set()


# ---------- approval (drafts -> profiles) ----------

def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def approval_path(name: str) -> Path:
    return profile_dir(name) / "approved.json"


def approval_error(name: str) -> str | None:
    """Why an approved profile cannot be used (None = usable): no approved.json, or profile.json differs from the bytes
    the user approved."""
    cmd = f"factory profile approve {name} --user-said \"<gist>\" (main session, after the user reviewed the draft)"
    try:
        rec = json.loads(approval_path(name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"profile {name!r} is not approved (no approved.json): {cmd}"
    except (OSError, ValueError) as err:
        return f"profile {name!r}: unreadable approved.json: {err}; {cmd}"
    now = file_sha256(profile_dir(name) / "profile.json")
    if not isinstance(rec, dict) or not isinstance(rec.get("file_sha256"), str) or now != rec["file_sha256"]:
        return f"profile {name!r} changed since it was approved (profile.json sha256 differs from approved.json): {cmd}"
    return None


def load_approved(name: str) -> dict:
    """The approved profile; refuses one whose profile.json is not what was approved."""
    doc = load_profile(name)
    err = approval_error(name)
    if err:
        raise FactoryError(err)
    return doc


def seal(pdir: Path, user_said: str) -> dict:
    """Write approved.json for the profile.json in `pdir` (used by `profile approve`; tests seal fixtures with it)."""
    path = Path(pdir) / "profile.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    rec = {"file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "digest": digest(doc),
           "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "user_said": user_said}
    from .core import write_json
    write_json(Path(pdir) / "approved.json", rec)
    return rec


GUARD_FIELDS = ("mcp_read_allow", "cli_write_patterns", "save_args")


def guard_fields(doc: dict) -> dict:
    """The fields the guard hooks read from an approved profile: what the user must see before approving."""
    return {k: (doc.get(k) if isinstance(doc.get(k), list) else []) for k in GUARD_FIELDS}


def _pretty(doc: Any) -> list[str]:
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True).splitlines()


def profile_diff(name: str, draft_doc: dict) -> str:
    """Unified diff of the draft against the current approved profile.json, or `new profile`."""
    try:
        current = json.loads((profile_dir(name) / "profile.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "new profile"
    except (OSError, ValueError):
        return "current approved profile.json is unreadable: treat the draft as a new profile"
    lines = list(difflib.unified_diff(_pretty(current), _pretty(draft_doc), "approved", "draft", lineterm="", n=2))
    return "\n".join(lines) if lines else "no change against the approved profile.json"


def _same_digest(given: str, want: str) -> bool:
    g = (given or "").strip().lower()
    g = g if g.startswith("sha256:") else "sha256:" + g
    return bool(given and given.strip()) and g == want.lower()


def approve(name: str, user_said: str, digest_arg: str = "") -> dict:
    """Promote `profile-drafts/<name>` to `profiles/<name>` (valid drafts only): copy, seal, atomic swap, backup.
    `digest_arg` must equal the draft digest `profile check <name> --draft` printed: the user approves THAT content."""
    said = (user_said or "").strip()
    if not said:
        raise FactoryError("profile approve needs --user-said \"<gist of the user's approval>\"")
    src = profile_dir(name, True)
    if not (src / "profile.json").is_file():
        raise FactoryError(f"no draft {name!r} at {src}")
    draft_doc = load_profile(name, True)
    if not _same_digest(digest_arg, digest(draft_doc)):
        raise FactoryError(f"profile approve needs --digest <sha256> equal to the draft digest that `factory profile check {name} "
                           "--draft` printed (the user approves that content); the draft changed or the digest is missing/wrong: "
                           "re-run check and show the user its output")
    rep = check_profile(name, draft=True)
    if not rep["valid"]:
        raise FactoryError(f"draft {name!r} is invalid; `factory profile check {name} --draft` must pass first:\n  - "
                           + "\n  - ".join(rep["problems"][:10]))
    profile_diff_text = profile_diff(name, draft_doc)  # before the swap: it compares with the CURRENT approved version
    root = profiles_root()
    root.mkdir(parents=True, exist_ok=True)
    stage = root / f".stage-{name}-{uuid.uuid4().hex[:8]}"
    dest = profile_dir(name)
    backup = None
    try:
        shutil.copytree(src, stage)
        (stage / "approved.json").unlink(missing_ok=True)
        rec = seal(stage, said)
        if dest.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            backup = root / ".backups" / f"{name}-{stamp}-{uuid.uuid4().hex[:4]}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, backup)
        os.replace(stage, dest)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"profile": name, "path": str(dest), "backup": str(backup) if backup else None, **rec,
            "guard": guard_fields(draft_doc), "diff": profile_diff_text}


def mapping_timeout(ws: dict | None) -> float:
    v = ((ws or {}).get("limits") or {}).get("mapping_timeout_s")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else DEFAULT_MAPPING_TIMEOUT_S


# ---------- raw / samples ----------

def load_raw_json(path: Path) -> Any:
    try:
        text = Path(path).read_bytes().decode("utf-8-sig")
    except OSError as err:
        raise FactoryError(f"cannot read raw {path}: {err}") from err
    except UnicodeDecodeError as err:
        raise FactoryError(f"raw {path} is not UTF-8 text: {err}") from err
    try:
        return json.loads(text)
    except ValueError as err:
        raise FactoryError(f"raw {path} is not valid JSON: {err}") from err


def sample_dir(name: str, kind: str, draft: bool = False) -> Path:
    return profile_dir(name, draft) / "samples" / kind


def sample_files(name: str, kind: str, ext: str | None = None, draft: bool = False) -> list[Path]:
    """Main sample files of a kind, sorted (`<n>.<ext>`; goldens, contexts and extra inputs have more dots)."""
    base = sample_dir(name, kind, draft)
    if not base.is_dir():
        return []
    ext = ext or contracts.KIND_EXT.get(kind, "json")
    return sorted(p for p in base.iterdir() if p.is_file() and p.suffix == f".{ext}" and p.stem and "." not in p.stem)


def sample_inputs(path: Path, names: list[str], optional: set[str] | None = None) -> dict[str, Path]:
    """Extra input files of a sample; an optional input whose file does not exist is left out."""
    out = {n: path.with_name(f"{path.stem}.{n}{path.suffix}") for n in names if n != "main"}
    return {n: p for n, p in out.items() if not (optional and n in optional and not p.is_file())}


def sample_ctx(path: Path) -> dict:
    ctx = {"repo": "sample", "scm_project": "team/sample", "id": path.stem, "key": path.stem, "number": 1,
           "file": "sample-file"}
    extra = path.with_name(f"{path.stem}.ctx.json")
    if extra.is_file():
        data = load_raw_json(extra)
        if isinstance(data, dict):
            ctx.update(data)
    return ctx


# ---------- mapping a kind ----------

def map_docs(doc: dict, kind: str, docs: dict[str, Any], ctx: dict, timeout_s: float | None = None) -> dict:
    """Run the kind's mapping in a child process with a hard timeout (a regex that backtracks forever cannot hang the
    CLI): the child is killed on timeout and the result is a mapping error. Same result as `map_docs_inline`."""
    limit = timeout_s if timeout_s else DEFAULT_MAPPING_TIMEOUT_S
    payload = json.dumps({"doc": doc, "kind": kind, "docs": docs, "ctx": ctx}, ensure_ascii=False).encode("utf-8")
    root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""), PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run([sys.executable, "-m", "factory.map_worker"], input=payload, capture_output=True,
                              timeout=limit, cwd=root, env=env)
    except subprocess.TimeoutExpired as err:
        raise mapping.MappingFailure([f"mapping of {kind} exceeded the {limit:g}s limit (limits.mapping_timeout_s) and was "
                                      "killed: a regex or expression in the profile is too expensive for this payload"]) from err
    except OSError as err:
        raise mapping.MappingFailure([f"cannot start the mapping worker: {err}"]) from err
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except ValueError as err:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
        raise mapping.MappingFailure([f"mapping worker failed (exit {proc.returncode}): {tail or err}"]) from err
    if out.get("errors"):
        raise mapping.MappingFailure(out["errors"])
    return out["result"]


def map_docs_inline(doc: dict, kind: str, docs: dict[str, Any], ctx: dict) -> dict:
    """Run the kind's mapping in this process. Returns {contract, unknowns, sources}; the contract has no provenance.
    Raises mapping.MappingFailure listing every error (field errors, required nulls, schema violations)."""
    spec = kind_spec(doc, kind)
    if spec is None:
        have = declared_kinds(doc)
        raise mapping.MappingFailure([f"profile {doc.get('name')!r} declares no kind {kind!r} (declared: {', '.join(have) or 'none'})"])
    mp = spec.get("map")
    if not isinstance(mp, dict):
        raise mapping.MappingFailure([f"profile {doc.get('name')!r} kind {kind!r} has no `map` object"])
    ptr = f"/kinds/{kind}/map"
    docs = {**{n: None for n in kind_optional_inputs(doc, kind)}, **docs}  # an absent optional input reads as null (unknown)
    values, rich, errors = mapping.evaluate_map(mp, docs, ctx, ptr)
    req, opt = contracts.contract_fields(kind)
    unknowns: list[dict] = []
    contract: dict[str, Any] = {"schema": f"{kind}.v1"}
    sources = {f: mapping.source_expression(e) for f, e in mp.items()}
    if kind == "design_frame" and "fidelity" not in mp:
        if spec.get("fidelity") in contracts.FIDELITIES:
            contract["fidelity"] = spec["fidelity"]
        else:
            errors.append(f"{ptr}: design_frame needs a `fidelity` mapping or kinds.design_frame.fidelity")
    for f in mp:
        if f not in req and f not in opt:
            errors.append(f"{ptr}/{f}: `{f}` is not a field of {kind}.v1")
    for f in req + opt:
        if f in contract:
            continue
        if f not in mp:
            if f in req:
                errors.append(f"contract field {f} required; not mapped")
            else:
                unknowns.append({"field": f, "reason": "not mapped"})
            continue
        if f not in values:  # its expression errored (already in errors)
            continue
        v = values[f]
        if v is None:
            if f in req:
                errors.append(f"contract field {f} required; expression {sources[f]} gave null (at {ptr}/{f})")
            else:
                unknowns.append({"field": f, "reason": "null"})
            continue
        contract[f] = v
    for r in rich:
        if r not in unknowns:
            unknowns.append(r)
    if errors:
        raise mapping.MappingFailure(errors)
    contract["unknowns"] = unknowns
    contract = contracts.order_contract(kind, contract)
    problems = contracts.validate(kind, contract, provenance=False, refs=doc.get("refs") if isinstance(doc.get("refs"), dict) else None)
    if problems:
        raise mapping.MappingFailure([f"contract violation: {p}" for p in problems])
    return {"contract": contract, "unknowns": unknowns, "sources": sources}


# ---------- lint ----------

def _regex_ok(pat: Any, where: str, errs: list[str], groups: set[str] | None = None) -> None:
    if not isinstance(pat, str):
        errs.append(f"{where}: must be a regex string")
        return
    try:
        rx = mapping.compile_regex(pat, where)
    except mapping.MappingError as err:
        errs.append(f"{where}: {err.message}")
        return
    if groups:
        missing = sorted(groups - set(rx.groupindex))
        if missing:
            errs.append(f"{where}: needs named group(s) {missing}")


def hint_placeholders(text: str) -> list[str]:
    return re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", text)


def lint_profile(doc: Any, name: str | None = None) -> list[str]:
    """Static problems of a profile document (unknown operators, hint placeholders, regex, refs groups, field names)."""
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["profile.json must be an object"]
    if doc.get("schema") != PROFILE_SCHEMA:
        errs.append(f"schema must be {PROFILE_SCHEMA!r}")
    if name is not None and doc.get("name") != name:
        errs.append(f"name {doc.get('name')!r} must equal the folder name {name!r}")
    extra = sorted(set(doc) - TOP_KEYS)
    if extra:
        errs.append(f"unknown top-level keys {extra}")
    roles = roles_of(doc)
    if not roles or any(r not in contracts.ROLES for r in roles):
        errs.append(f"role must be one of {list(contracts.ROLES)} (or a list of them), got {doc.get('role')!r}")
    refs = doc.get("refs", {})
    if not isinstance(refs, dict):
        errs.append("refs must be an object")
        refs = {}
    for k, v in refs.items():
        if k not in REF_GROUPS:
            errs.append(f"refs.{k}: unknown ref (known: {sorted(REF_GROUPS)})")
        else:
            _regex_ok(v, f"refs.{k}", errs, REF_GROUPS[k])
    kinds = doc.get("kinds", {})
    if not isinstance(kinds, dict):
        errs.append("kinds must be an object")
        kinds = {}
    allowed = {k for r in roles for k in ROLE_KINDS.get(r, [])}
    for kind, spec in kinds.items():
        where = f"kinds.{kind}"
        if kind not in contracts.KINDS:
            errs.append(f"{where}: unknown kind (kinds: {', '.join(contracts.KINDS)})")
            continue
        if allowed and kind not in allowed:
            errs.append(f"{where}: kind is outside the profile role {roles}")
        if not isinstance(spec, dict):
            errs.append(f"{where}: must be an object")
            continue
        bad = sorted(set(spec) - KIND_KEYS)
        if bad:
            errs.append(f"{where}: unknown keys {bad}")
        ext = spec.get("ext", contracts.KIND_EXT.get(kind, "json"))
        if kind == "design_image":
            if ext != "png":
                errs.append(f"{where}.ext must be png")
            continue
        if ext != "json":
            errs.append(f"{where}.ext: only json raw payloads can be mapped (got {ext!r})")
        mp = spec.get("map")
        if not isinstance(mp, dict) or not mp:
            errs.append(f"{where}.map must be a non-empty object")
            continue
        req, opt = contracts.contract_fields(kind)
        provided = {"fidelity"} if kind == "design_frame" and spec.get("fidelity") in contracts.FIDELITIES else set()
        for f in mp:
            if f not in req and f not in opt:
                errs.append(f"{where}.map.{f}: not a field of {kind}.v1")
        for f in req:
            if f not in mp and f not in provided:
                errs.append(f"{where}.map: required field {f!r} is not mapped")
        if "fidelity" in spec and spec["fidelity"] not in contracts.FIDELITIES:
            errs.append(f"{where}.fidelity must be one of {list(contracts.FIDELITIES)}")
        o = spec.get("optional", [])
        if not isinstance(o, list) or any(x not in opt for x in o):
            errs.append(f"{where}.optional must list optional fields of {kind}.v1 ({opt})")
        ins = spec.get("inputs")
        if ins is not None and (not isinstance(ins, list) or "main" not in ins or any(not isinstance(x, str) for x in ins)):
            errs.append(f"{where}.inputs must be a list of names including \"main\" (a trailing ? marks an optional "
                        "input, e.g. \"jobs?\")")
        for f, expr in mp.items():
            errs += [f"{where}.map.{f}: {e}" for e in mapping.lint_expr(expr, f"/kinds/{kind}/map/{f}")]
    hints = doc.get("hints", {})
    if not isinstance(hints, dict):
        errs.append("hints must be an object")
        hints = {}
    for k, v in hints.items():
        if k not in contracts.KINDS:
            errs.append(f"hints.{k}: unknown kind")
        elif not isinstance(v, str):
            errs.append(f"hints.{k}: must be a string template")
        else:
            bad = sorted({p for p in hint_placeholders(v) if p not in HINT_PLACEHOLDERS})
            if bad:
                errs.append(f"hints.{k}: unknown placeholder(s) {bad} (known: {sorted(HINT_PLACEHOLDERS)})")
    for key in ("cli_write_patterns",):
        val = doc.get(key, [])
        if not isinstance(val, list):
            errs.append(f"{key} must be a list of regex strings")
        else:
            for i, pat in enumerate(val):
                _regex_ok(pat, f"{key}[{i}]", errs)
    for key in ("mcp_read_allow", "save_args", "accepted_unknowns"):
        val = doc.get(key, [])
        if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
            errs.append(f"{key} must be a list of strings")
    allow = doc.get("mcp_read_allow", [])
    if isinstance(allow, list):
        for i, pat in enumerate(allow):
            if isinstance(pat, str):
                why = _broad_read_allow(pat)
                if why:
                    errs.append(f"mcp_read_allow[{i}] {pat!r}: {why}; list the read tools one by one")
    return errs


# tool names that no read allow-list may match: if a glob matches all of them it certifies write tools as reads
_WRITE_PROBES = ("create_item", "update_item", "delete_item", "merge_item", "post_comment", "transition_issue", "push_files")


def _glob_re(glob: str) -> re.Pattern:
    return re.compile("^" + ".*".join(re.escape(x) for x in glob.split("*")) + "$", re.IGNORECASE)


def _broad_read_allow(pat: str) -> str:
    """Why an `mcp_read_allow` glob is too broad (certifies tools it should not), else ''."""
    p = pat.strip()
    if not p or not p.replace("*", ""):
        return "matches every tool"
    if p.lower().startswith("mcp__"):
        tool = p[5:].split("__", 1)[1] if "__" in p[5:] else ""
        if not tool.replace("*", ""):
            return "is a server-wide glob (every tool of the server)"
    rx = _glob_re(p)
    if all(rx.match(t) or rx.match(f"mcp__srv__{t}") for t in _WRITE_PROBES):
        return "matches every tool, write tools included"
    return ""


# ---------- validity (5.2) ----------

def kind_report(name: str, doc: dict, kind: str, draft: bool = False) -> dict:
    """{samples: n, ok: n, errors: [...]} for one declared kind."""
    out = {"samples": 0, "ok": 0, "errors": []}
    ext = kind_ext(doc, kind)
    files = sample_files(name, kind, ext, draft)
    out["samples"] = len(files)
    if kind == "design_image":
        for p in files:
            try:
                contracts.png_info(p.read_bytes())
                out["ok"] += 1
            except (ValueError, OSError) as err:
                out["errors"].append(f"{kind}/{p.name}: {err}")
    else:
        names = kind_inputs(doc, kind)
        for p in files:
            try:
                docs = {"main": load_raw_json(p)}
                for n, ip in sample_inputs(p, names, kind_optional_inputs(doc, kind)).items():
                    docs[n] = load_raw_json(ip)
                map_docs(doc, kind, docs, sample_ctx(p))
                out["ok"] += 1
            except mapping.MappingFailure as err:
                out["errors"] += [f"{kind}/{p.name}: {e}" for e in err.errors]
            except FactoryError as err:
                out["errors"].append(f"{kind}/{p.name}: {err}")
    if not files:
        out["errors"].append(f"{kind}: no samples in {sample_dir(name, kind, draft)}")
    return out


def check_profile(name: str, draft: bool = False) -> dict:
    """Validity + lint: {valid, lint: [...], kinds: {kind: report}, problems: [...]}. VALID iff there is no lint error
    and every declared kind has at least one sample that maps with zero errors and validates."""
    try:
        doc = load_profile(name, draft)
    except FactoryError as err:
        return {"valid": False, "lint": [], "kinds": {}, "problems": [str(err)], "warnings": [], "digest": None, "guard": {}}
    lint = lint_profile(doc, name)
    warnings: list[str] = []
    if not guard_fields(doc)["cli_write_patterns"] and any(isinstance(v, str) and v.strip() for v in (doc.get("hints") or {}).values()):
        warnings.append("cli_write_patterns is empty although the profile has acquisition hints: a provider CLI write would "
                        "pass the worker guard; list the write commands of the provider CLI as regexes")
    kinds: dict[str, dict] = {}
    problems = list(lint)
    for kind in declared_kinds(doc):
        if lint and any(e.startswith(f"kinds.{kind}") for e in lint):
            continue  # the mapping is not well formed: do not pile sample errors on top
        rep = kind_report(name, doc, kind, draft)
        kinds[kind] = rep
        if rep["ok"] == 0:
            problems += rep["errors"] or [f"{kind}: no sample maps"]
    if not declared_kinds(doc):
        problems.append("profile declares no kinds")
    return {"valid": not problems, "lint": lint, "kinds": kinds, "problems": problems, "warnings": warnings,
            "digest": digest(doc), "guard": guard_fields(doc)}


def provider_errors(ws: dict) -> list[str]:
    """`workspace check`: `providers.<role>` must name existing, valid profiles whose role covers it."""
    prov = ws.get("providers")
    if prov is None:
        return []
    if not isinstance(prov, dict):
        return ["providers must be an object of role -> profile name"]
    out: list[str] = []
    cache: dict[str, dict] = {}
    for role, pname in prov.items():
        if role not in contracts.ROLES:
            out.append(f"providers.{role}: unknown role (roles: {', '.join(contracts.ROLES)})")
            continue
        if not isinstance(pname, str) or not pname:
            out.append(f"providers.{role} must name a profile")
            continue
        if pname not in cache:
            cache[pname] = check_profile(pname)
            err = approval_error(pname) if profile_dir(pname).is_dir() else None
            if err:
                cache[pname] = {"valid": False, "problems": [err]}
        rep = cache[pname]
        if not rep["valid"]:
            out += [f"providers.{role}: profile {pname!r} invalid: {p}" for p in rep["problems"][:5]]
            continue
        doc = load_profile(pname)
        if role not in roles_of(doc):
            out.append(f"providers.{role}: profile {pname!r} has role {doc.get('role')!r}")
    return out


def profile_for(ws: dict, kind: str, override: str | None = None) -> tuple[str, dict]:
    """(name, profile) for a kind: `override`, else `providers.<role of kind>` of the workspace."""
    role = contracts.KIND_ROLE[kind]
    name = override or ((ws.get("providers") or {}).get(role) if isinstance(ws.get("providers"), dict) else None)
    if not name:
        raise FactoryError(f"no profile for kind {kind!r}: set workspace providers.{role} or pass --profile")
    return name, load_approved(name)


# ---------- hints ----------

def render_hint(doc: dict, kind: str, ctx: dict) -> str | None:
    """Acquisition hint of a kind with placeholders filled. A placeholder without a value stays as `<name>`; unknown
    placeholders too. `raw_path`/`raw_dir` are used as given (callers pass absolute paths with forward slashes)."""
    tpl = (doc.get("hints") or {}).get(kind)
    if not isinstance(tpl, str):
        return None

    def sub(m: re.Match) -> str:
        v = ctx.get(m.group(1))
        if v is None or v == "":
            return f"<{m.group(1)}>"
        return str(v).replace("\\", "/") if m.group(1) in ("raw_path", "raw_dir") else str(v)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, tpl)


# ---------- scaffold ----------

def scaffold(name: str, role: str) -> Path:
    """New profile DRAFT under profile-drafts/<name> (workers never write profiles/)."""
    if role not in contracts.ROLES:
        raise FactoryError(f"--role must be one of {', '.join(contracts.ROLES)}")
    pdir = profile_dir(name, True)
    if pdir.exists():
        raise FactoryError(f"profile draft {name!r} already exists: {pdir}")
    from .core import write_json
    write_json(pdir / "profile.json", {"schema": PROFILE_SCHEMA, "name": name, "role": role, "refs": {}, "kinds": {},
                                        "hints": {}, "cli_write_patterns": [], "mcp_read_allow": [], "save_args": []})
    for kind in ROLE_KINDS[role]:
        (pdir / "samples" / kind).mkdir(parents=True, exist_ok=True)
    return pdir
