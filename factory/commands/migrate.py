"""`factory migrate apply <run|--all> <map.json>`: generic, provider-free in-flight run migration
(docs/design/provider-agnostic.md section 10.1).

The map file (authored outside the repo, e.g. FACTORY_HOME/migrations/<name>.json) is a JSON list of operations, or
{"ops": [...]}. Operations run in order; a failure leaves earlier FILE operations applied (every operation is
idempotent, so re-running after fixing the map is safe) and run.json is only saved when all operations succeeded.

| op | fields | effect |
| rename_key | file, path, to | rename the key at `path` (supports [*]) to `to`; `file: run.json` edits the loaded run |
| move_file | from, to | move files matching `from` (`*` captured, `{1}` in `to`) |
| ingest | kind, raw, [repo, profile, format] | `factory ingest` of every file matching `raw` (id = file stem) |
| replace_text | file, from, to | literal replace in the KEYS of a JSON file (plain text for other files) |
| ws_rename_key | path, to | rename a key in the run's workspace.json |
| ws_replace_text | from, to | literal replace in the keys of the run's workspace.json |

Main session only: `--dry-run` prints every operation and target (shown to the user), then `--digest <map sha256>` plus
`--user-said "<gist>"` apply it (recorded as a user decision with the map sha256). Targets inside
contracts/, evidence/, reviews/, baseline/ and CLI-owned `run.json` keys are refused, and a run whose plan lock already
fails (`lock_errors`) is refused before anything is applied, so the re-seal absorbs only the map's own operations.

After the operations: `run["contracts_schema"]` is set to 2 (the run's own `schemaVersion` is left alone: `run_load`
refuses any other value, so bumping it is a separate, deliberate core change), the plan lock is re-sealed when the
run has one (same sequence number, current workspace digest), and event `migrate {map_sha256, old_lock, new_lock}`
is recorded.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .. import integrity, mapping
from ..core import (FactoryError, ensure_owner, event, factory_home, now, read_json, run_dir, run_load, run_update,
                    workspace_path, write_json)
from . import ingest as ingest_cmd

OPS = {"rename_key": ("file", "path", "to"), "move_file": ("from", "to"), "ingest": ("kind", "raw"),
       "replace_text": ("file", "from", "to"), "ws_rename_key": ("path", "to"), "ws_replace_text": ("from", "to")}
CONTRACTS_SCHEMA = 2


def load_ops(path: Path) -> tuple[list[dict], str]:
    """(operations, sha256 of the map file bytes). Every op is shape-checked up front."""
    try:
        raw = Path(path).read_bytes()
    except OSError as err:
        raise FactoryError(f"cannot read map file {path}: {err}") from err
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError as err:
        raise FactoryError(f"map file {path} is not valid JSON: {err}") from err
    ops = data.get("ops") if isinstance(data, dict) else data
    if not isinstance(ops, list) or not ops:
        raise FactoryError("map file must be a non-empty JSON list of operations (or {\"ops\": [...]})")
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise FactoryError(f"ops[{i}]: `op` must be one of {', '.join(OPS)}")
        miss = [k for k in OPS[op["op"]] if not isinstance(op.get(k), str) or not op.get(k)]
        if miss:
            raise FactoryError(f"ops[{i}] ({op['op']}): missing or non-string field(s) {miss}")
        if op["op"] in ("rename_key", "replace_text"):
            if op["file"] == "run.json":
                _check_run_json_op(op, i)
            else:
                _refuse_protected(op["file"], f"ops[{i}] file")
        elif op["op"] == "move_file":
            _refuse_protected(op["from"], f"ops[{i}] from")
            _refuse_protected(op["to"], f"ops[{i}] to")
        elif op["op"] == "ingest":
            _refuse_protected(op["raw"], f"ops[{i}] raw")
    return ops, hashlib.sha256(raw).hexdigest()


# ---------- JSON helpers ----------

def _containers(data: Any, toks: list[tuple]) -> list[Any]:
    cur = [data]
    for t in toks:
        nxt: list[Any] = []
        for c in cur:
            if t[0] == "key" and isinstance(c, dict) and t[1] in c:
                nxt.append(c[t[1]])
            elif t[0] == "wild" and isinstance(c, list):
                nxt.extend(c)
            elif t[0] == "idx" and isinstance(c, list) and -len(c) <= t[1] < len(c):
                nxt.append(c[t[1]])
            elif t[0] == "filter":
                raise FactoryError("filters are not supported in migration paths")
        cur = nxt
    return cur


def rename_key(data: Any, path: str, to: str) -> int:
    """Rename the final key of `path` in every matching object, keeping key order. Returns the number renamed."""
    scope, toks = mapping.parse_path(path)
    if scope != "main" or not toks or toks[-1][0] != "key":
        raise FactoryError(f"rename_key path {path!r} must end in a key")
    count = 0
    for parent in _containers(data, toks[:-1]):
        old = toks[-1][1]
        if not isinstance(parent, dict) or old not in parent:
            continue
        if to in parent and to != old:
            raise FactoryError(f"rename_key: {to!r} already exists next to {old!r} at {path}")
        items = [(to if k == old else k, v) for k, v in parent.items()]
        parent.clear()
        parent.update(items)
        count += 1
    return count


def replace_keys(data: Any, old: str, new: str) -> int:
    """Literal replace in every JSON object key (recursively). Returns how many keys changed."""
    count = 0
    if isinstance(data, dict):
        items = []
        for k, v in data.items():
            nk = k.replace(old, new) if isinstance(k, str) else k
            count += (nk != k) + replace_keys(v, old, new)
            items.append((nk, v))
        data.clear()
        data.update(items)
    elif isinstance(data, list):
        for v in data:
            count += replace_keys(v, old, new)
    return count


PROTECTED_DIRS = ("contracts", "evidence", "reviews", "baseline")  # written by the CLI only; never a migration target
PROTECTED_RUN_KEYS = {"events", "plan_lock", "decisions", "proved", "evidence", "reviews", "blockers", "sessions", "phase",
                      "contracts_schema", "schemaVersion", "id", "rev", "integrity", "closed"}


def _protected(rel: str) -> str | None:
    """The protected directory a run-relative path (or glob pattern) points into, if any."""
    first = rel.replace("\\", "/").lstrip("./").split("/", 1)[0]
    if first.lower() in PROTECTED_DIRS:
        return first.lower()
    return "a wildcard first segment" if "*" in first and "/" in rel.replace("\\", "/") else None


def _refuse_protected(rel: str, what: str) -> None:
    hit = _protected(rel)
    if hit:
        raise FactoryError(f"refused: {what} {rel!r} targets {hit} (contracts/, evidence/, reviews/ and baseline/ are written "
                           "by the CLI only; re-ingest or re-run the collectors instead)")


def _safe_file(rd: Path, rel: str) -> Path:
    _refuse_protected(rel, "file")
    p = (rd / rel).resolve()
    try:
        p.relative_to(rd.resolve())
    except ValueError as err:
        raise FactoryError(f"file {rel!r} is outside the run directory") from err
    _refuse_protected(p.relative_to(rd.resolve()).as_posix(), "file")
    return p


def _check_run_json_op(op: dict, i: int) -> None:
    if op["op"] == "replace_text":
        raise FactoryError(f"ops[{i}]: replace_text on run.json is refused (it would rewrite keys of events, decisions and the "
                           "plan lock); use rename_key on the specific path")
    scope, toks = mapping.parse_path(op["path"])
    first = toks[0][1] if toks and toks[0][0] == "key" else None
    if first is None or first in PROTECTED_RUN_KEYS or op["to"] in PROTECTED_RUN_KEYS:
        raise FactoryError(f"ops[{i}]: rename_key {op['path']!r} -> {op['to']!r} on run.json touches a CLI-owned field "
                           f"({', '.join(sorted(PROTECTED_RUN_KEYS))})")


def _glob(rd: Path, pattern: str) -> list[tuple[Path, list[str]]]:
    if ".." in Path(pattern).parts or Path(pattern).is_absolute():
        raise FactoryError(f"pattern {pattern!r} must be relative to the run directory")
    _refuse_protected(pattern, "pattern")
    rx = re.compile(re.escape(pattern.replace("\\", "/")).replace(r"\*", "([^/]*)"))
    out = []
    for p in sorted(rd.rglob("*")):
        if p.is_file():
            m = rx.fullmatch(p.relative_to(rd).as_posix())
            if m:
                out.append((p, list(m.groups())))
    return out


# ---------- operations ----------

def _apply_ops(run: dict, ops: list[dict], ws_name: str) -> list[dict]:
    rd = run_dir(run["id"])
    report: list[dict] = []
    for i, op in enumerate(ops):
        kind = op["op"]
        n = 0
        if kind == "rename_key":
            if op["file"] == "run.json":
                n = rename_key(run, op["path"], op["to"])
            else:
                f = _safe_file(rd, op["file"])
                if f.is_file():
                    data = read_json(f)
                    n = rename_key(data, op["path"], op["to"])
                    if n:
                        write_json(f, data)
        elif kind == "replace_text":
            if op["file"] == "run.json":
                n = replace_keys(run, op["from"], op["to"])
            else:
                f = _safe_file(rd, op["file"])
                if f.is_file():
                    if f.suffix == ".json":
                        data = read_json(f)
                        n = replace_keys(data, op["from"], op["to"])
                        if n:
                            write_json(f, data)
                    else:
                        text = f.read_text(encoding="utf-8")
                        n = text.count(op["from"])
                        if n:
                            f.write_text(text.replace(op["from"], op["to"]), encoding="utf-8")
        elif kind == "move_file":
            for src, groups in _glob(rd, op["from"]):
                dest = _safe_file(rd, re.sub(r"\{(\d+)\}", lambda m: groups[int(m.group(1)) - 1], op["to"]))
                if dest.exists():
                    if dest.read_bytes() != src.read_bytes():
                        raise FactoryError(f"ops[{i}] move_file: {dest.relative_to(rd).as_posix()} exists with different content")
                    src.unlink()
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dest))
                n += 1
        elif kind == "ingest":
            for src, _ in _glob(rd, op["raw"]):
                ingest_cmd.ingest_raw(run["id"], op["kind"], str(src), ident=src.stem, repo=op.get("repo"),
                                      profile=op.get("profile"), fmt=op.get("format"),
                                      source_hint="migrate", run=run)
                n += 1
        elif kind.startswith("ws_"):
            f = workspace_path(ws_name)
            data = read_json(f)
            n = rename_key(data, op["path"], op["to"]) if kind == "ws_rename_key" else replace_keys(data, op["from"], op["to"])
            if n:
                write_json(f, data)
        report.append({"op": kind, "changed": n})
    return report


def migrate_run(run_id: str, ops: list[dict], map_sha: str, user_said: str = "") -> dict:
    """Apply `ops` to one run under the run lock; re-seal its plan lock; record the user decision and the `migrate` event.
    Refuses (nothing applied) when the plan lock already shows drift: only the map's own operations may change the plan."""
    result: dict[str, Any] = {}
    ws_name = run_load(run_id).get("workspace") or ""

    def mutate(run: dict) -> None:
        ensure_owner(run)
        lock = run.get("plan_lock") if isinstance(run.get("plan_lock"), dict) else None
        old = lock.get("digest") if lock else None
        if lock:
            drift = integrity.lock_errors(run)
            if drift:
                raise FactoryError("refused: the plan lock of this run already fails before the migration (re-sealing would "
                                   "absorb that change): " + "; ".join(drift) + ". Fix or replan first, then migrate.")
        report = _apply_ops(run, ops, ws_name)
        run["contracts_schema"] = CONTRACTS_SCHEMA
        new = None
        if lock:
            new = integrity.lock_plan(run, "migrate", keep_seq=True)["digest"]
        run.setdefault("decisions", []).append({
            "at": now(), "phase": run.get("phase"), "by": "user", "user_said": user_said,
            "text": f"migrate apply: map sha256 {map_sha}"})
        event(run, "migrate", map_sha256=map_sha, old_lock=old, new_lock=new, user_said=user_said)
        result.update(run=run["id"], ops=report, old_lock=old, new_lock=new)

    run_update(run_id, mutate)
    return result


def describe_ops(run_id: str, ops: list[dict]) -> list[str]:
    """One line per operation and target for `--dry-run` (nothing is written)."""
    rd = run_dir(run_id)
    ws_name = run_load(run_id).get("workspace") or ""
    lines: list[str] = []
    for i, op in enumerate(ops):
        kind = op["op"]
        head = f"  [{i}] {kind}"
        try:
            if kind in ("rename_key", "replace_text"):
                target = rd / "run.json" if op["file"] == "run.json" else _safe_file(rd, op["file"])
                what = f"{op['path']} -> {op['to']}" if kind == "rename_key" else f"{op['from']!r} -> {op['to']!r}"
                lines.append(f"{head} {target}: {what}")
            elif kind == "move_file":
                hits = _glob(rd, op["from"])
                for src, groups in hits:
                    dest = _safe_file(rd, re.sub(r"\{(\d+)\}", lambda m: groups[int(m.group(1)) - 1], op["to"]))
                    lines.append(f"{head} {src} -> {dest}")
                if not hits:
                    lines.append(f"{head} {rd / op['from']}: no file matches")
            elif kind == "ingest":
                hits = _glob(rd, op["raw"])
                lines += [f"{head} {src} as kind {op['kind']}" + (f" profile {op['profile']}" if op.get("profile") else "")
                          for src, _ in hits]
                if not hits:
                    lines.append(f"{head} {rd / op['raw']}: no file matches")
            else:
                what = f"{op['path']} -> {op['to']}" if kind == "ws_rename_key" else f"{op['from']!r} -> {op['to']!r}"
                lines.append(f"{head} {workspace_path(ws_name)}: {what}")
        except FactoryError as err:
            lines.append(f"{head} REFUSED: {err}")
    return lines


def _digest_ok(given: str | None, sha: str) -> bool:
    g = (given or "").strip().lower()
    return bool(g) and g.removeprefix("sha256:") == sha


def cmd_apply(args) -> int:
    pos = list(args.args)
    if args.all:
        if len(pos) != 1:
            raise FactoryError("usage: factory migrate apply --all <map.json>")
        map_file, runs = pos[0], None
    else:
        if len(pos) != 2:
            raise FactoryError("usage: factory migrate apply <run> <map.json>")
        runs, map_file = [pos[0]], pos[1]
    ops, sha = load_ops(Path(map_file))
    if runs is None:
        root = factory_home() / "runs"
        runs = sorted(p.parent.name for p in root.glob("*/run.json")) if root.is_dir() else []
    if getattr(args, "dry_run", False):
        blocks = {rid: describe_ops(rid, ops) for rid in runs}
        if getattr(args, "json", False):
            print(json.dumps({"map_sha256": sha, "dry_run": True, "runs": blocks}, indent=2, ensure_ascii=False))
        else:
            print(f"DRY RUN (nothing written). map {map_file} sha256 {sha}")
            for rid, lines in blocks.items():
                print(f"run {rid}:")
                print("\n".join(lines))
            print(f"show this to the user, then: factory migrate apply ... --digest {sha} --user-said \"<gist>\"")
        return 0
    said = (getattr(args, "user_said", None) or "").strip()
    if not said:
        raise FactoryError("migrate apply needs --user-said \"<gist of the user's approval of this map>\": the user approves "
                           "the map (its sha256 is recorded) before it touches runs. Main session only.")
    if not _digest_ok(getattr(args, "digest", None), sha):
        raise FactoryError("migrate apply needs --digest <sha256 of the map file> equal to the map's current sha256 (run "
                           "`factory migrate apply ... --dry-run` and show the user every operation and target first); the map "
                           "changed since it was shown, or the digest is missing or wrong")
    done, failed = [], []
    for rid in runs:
        try:
            done.append(migrate_run(rid, ops, sha, said))
        except FactoryError as err:
            failed.append(f"{rid}: {err}")
    lines = [f"migrated {r['run']}: " + ", ".join(f"{o['op']}x{o['changed']}" for o in r["ops"])
             + (f"; plan lock {str(r['old_lock'])[:8]} -> {str(r['new_lock'])[:8]}" if r["new_lock"] else "") for r in done]
    lines += [f"FAILED {f}" for f in failed]
    if getattr(args, "json", False):
        print(json.dumps({"map_sha256": sha, "migrated": done, "failed": failed}, indent=2, ensure_ascii=False))
    else:
        print("\n".join(lines) or "no runs")
    if failed:
        raise FactoryError(f"{len(failed)} run(s) failed to migrate")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("migrate", help="apply a migration map to in-flight runs (generic, provider-free)")
    msub = parser.add_subparsers(dest="migrate_cmd", required=True)
    p = msub.add_parser("apply", help="apply <map.json> to <run> (or --all runs)")
    p.add_argument("args", nargs="+", metavar="[RUN] MAP")
    p.add_argument("--all", action="store_true", help="every run under FACTORY_HOME/runs")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="print every operation and target; write nothing")
    p.add_argument("--digest", help="sha256 of the map file, as printed by --dry-run (required to apply)")
    p.add_argument("--user-said", dest="user_said", help="gist of the user message approving this map (required to apply)")
    p.set_defaults(func=cmd_apply)
