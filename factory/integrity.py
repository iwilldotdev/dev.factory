"""Proof integrity: plan lock, evidence/review digests (DESIGN §2.6).

The plan gate locks what the proof is measured against (acs.json, checks/**, figma/**,
diagnosis.md, and the sha256 of workspace.json: caps, limits, quality commands). Later gates re-hash and refuse changed files: the legit path is
`factory run replan`. Every evidence and review file written by the CLI gets an event
with its sha256; gates refuse files whose bytes do not match their latest event.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from .core import event, evidence_file, file_sha256, git_head, now, run_dir, workspace_path

LOCK_FILES = ("acs.json", "diagnosis.md")
LOCK_DIRS = ("checks", "figma")


# Directories under checks/ where tools drop generated output (screenshots, traces, reports): not plan.
GENERATED_DIRS = frozenset({"out", "output", "screenshots", "artifacts", "test-results", "playwright-report"})


def is_generated(rel: str) -> bool:
    """Run-relative posix path of a generated artifact that must not be part of the plan lock."""
    parts = rel.split("/")
    return parts[0] == "checks" and any(p.lower() in GENERATED_DIRS for p in parts[1:-1])


INPUT_KEYS = ("adapter", "map", "spec", "script")  # check keys naming a file the check reads (run-relative)


def _rel_in_run(run_id: str, value: str, relative_ok: bool = True) -> str | None:
    """Run-relative posix path of `value` when it resolves inside the run dir, else None."""
    rd = run_dir(run_id)
    try:
        raw = value.strip().strip("\"'").replace("<run>", rd.as_posix())
        p = Path(raw)
        if not p.is_absolute():
            if not relative_ok:
                return None
            p = rd / raw
        return p.resolve().relative_to(rd.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def check_inputs(run_id: str, chk: dict) -> list[tuple[str, str]]:
    """[(key, run-relative posix path)] of the input files a check reads from inside the run dir: `adapter`, `map`,
    `spec`, `script`, and paths in `cmd` that resolve inside the run dir (absolute or `<run>/...`)."""
    out: list[tuple[str, str]] = []
    for key in INPUT_KEYS:
        val = chk.get(key)
        if isinstance(val, str) and val.strip():
            rel = _rel_in_run(run_id, val)
            out.append((key, rel if rel is not None else PurePosixPath(val.replace("\\", "/")).as_posix()))
    cmd = chk.get("cmd")
    if isinstance(cmd, str):
        for tok in cmd.split():
            for part in (tok, tok.split("=", 1)[-1]):
                rel = _rel_in_run(run_id, part, relative_ok=False)
                if rel:
                    out.append(("cmd", rel))
    return out


def referenced_files(run_id: str) -> list[str]:
    """Run-relative paths named by acs.json checks (`adapter`, `map`, `spec`, `script`, run-dir paths in `cmd`): they are
    plan too, wherever they live inside the run dir."""
    try:
        data = json.loads((run_dir(run_id) / "acs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[str] = []
    for chk in (data.get("checks") or []) if isinstance(data, dict) else []:
        if isinstance(chk, dict):
            out.extend(rel for _, rel in check_inputs(run_id, chk))
    return out


def plan_files(run_id: str) -> dict[str, str]:
    """{run-relative posix path: sha256} of every locked plan file present now."""
    rd = run_dir(run_id)
    out: dict[str, str] = {}
    for rel in LOCK_FILES:
        digest = file_sha256(rd / rel)
        if digest:
            out[rel] = digest
    for name in LOCK_DIRS:
        base = rd / name
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(rd).as_posix()
                if not is_generated(rel):
                    out[rel] = file_sha256(p) or ""
    root = rd.resolve()
    for rel in referenced_files(run_id):  # adapter/map files wherever they live inside the run dir
        p = rd / rel
        try:
            inside = p.resolve().is_relative_to(root)
        except OSError:
            inside = False
        if inside and p.is_file() and rel not in out:
            out[p.resolve().relative_to(root).as_posix()] = file_sha256(p) or ""
    return out


VOLATILE_WORKSPACE_KEYS: tuple[str, ...] = ()  # none today: no CLI command rewrites workspace.json during a run


def workspace_digest(ws_name: str) -> str | None:
    """sha256 of workspace.json as canonical JSON (key order and whitespace do not count)."""
    path = workspace_path(ws_name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return file_sha256(path)
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in VOLATILE_WORKSPACE_KEYS}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _autonomy(ws_name: str) -> dict:
    try:
        data = json.loads(workspace_path(ws_name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(data.get("autonomy") or {}) if isinstance(data, dict) else {}


def locked_autonomy(run: dict) -> dict:
    """Autonomy caps as locked at the plan gate (a later workspace edit cannot raise them); else the current ones."""
    lock = run.get("plan_lock")
    if isinstance(lock, dict) and isinstance(lock.get("autonomy"), dict):
        return lock["autonomy"]
    return _autonomy(run["workspace"])


def workspace_error(run: dict) -> str | None:
    """workspace.json changed since the plan lock (caps, limits, setup): the plan must be redone."""
    lock = run.get("plan_lock")
    if not isinstance(lock, dict) or "workspace" not in lock:
        return None
    if lock["workspace"] != workspace_digest(run["workspace"]):
        return (f"workspace changed after plan; replan (`factory run replan {run['id']} \"<reason>\"` with a user "
                "decision, then the plan gate locks the new workspace.json)")
    return None


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()


def code_scope(run_id: str, files: dict[str, str] | None = None) -> str:
    """Digest of what the `code` review depends on: AC ids and text, diagnosis, figma pack and the workspace.
    Changing only checks/ or the checks of acs.json (a replan of how things are proven) leaves it equal."""
    rd = run_dir(run_id)
    files = plan_files(run_id) if files is None else files
    try:
        data = json.loads((rd / "acs.json").read_text(encoding="utf-8"))
        acs = [[a.get("id"), a.get("text")] for a in data.get("acs") or [] if isinstance(a, dict)]
    except (OSError, ValueError, AttributeError):
        acs = None
    keep = {k: v for k, v in files.items() if k == "diagnosis.md" or k.startswith("figma/")}
    return hashlib.sha256(json.dumps({"acs": acs, "files": keep}, sort_keys=True).encode("utf-8")).hexdigest()


def acs_snapshot(run_id: str) -> tuple[dict, str | None]:
    """({check id: check entry}, sha256 of acs.json without `checks`) as of now; kept in the plan lock so
    `run amend-check` can tell what changed in a check without re-planning."""
    try:
        data = json.loads((run_dir(run_id) / "acs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    checks = {c["id"]: c for c in data.get("checks") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)}
    rest = {k: v for k, v in data.items() if k != "checks"}
    return checks, hashlib.sha256(json.dumps(rest, sort_keys=True).encode("utf-8")).hexdigest()


def lock_plan(run: dict, reason: str = "plan gate", keep_seq: bool = False, keep_workspace: bool = False) -> dict:
    """Record run["plan_lock"] (caller saves). `seq` = first event index evidence must come after. `keep_workspace`
    (amend-check) carries the previous workspace digest and autonomy caps instead of re-reading workspace.json, so a
    relock never absorbs a workspace edit."""
    files = plan_files(run["id"])
    old = run.get("plan_lock") if isinstance(run.get("plan_lock"), dict) else {}
    seq = old.get("seq") if keep_seq and isinstance(old.get("seq"), int) else len(run.get("events", []))
    checks, rest = acs_snapshot(run["id"])
    if keep_workspace and "workspace" in old:
        ws_digest = old.get("workspace")
        autonomy = old["autonomy"] if isinstance(old.get("autonomy"), dict) else _autonomy(run["workspace"])
    else:
        ws_digest, autonomy = workspace_digest(run["workspace"]), _autonomy(run["workspace"])
    run["plan_lock"] = {"at": now(), "seq": seq, "digest": _digest(files), "files": files,
                        "checks": checks, "acs_rest": rest,
                        "code_scope": code_scope(run["id"], files),
                        "workspace": ws_digest, "autonomy": autonomy}
    event(run, "plan_lock", reason=reason, digest=run["plan_lock"]["digest"], files=len(files))
    return run["plan_lock"]


def changed_files(run: dict, ignore: tuple[str, ...] = ()) -> list[str]:
    lock = run.get("plan_lock") or {}
    old = lock.get("files") or {}
    cur = plan_files(run["id"])
    return [n for n in sorted(set(old) | set(cur)) if n not in ignore and old.get(n) != cur.get(n)]


def lock_errors(run: dict, ignore: tuple[str, ...] = ()) -> list[str]:
    if not isinstance(run.get("plan_lock"), dict):
        return [f"no plan lock: the plan gate has not passed since the last replan (factory gate {run['id']} plan)"]
    errors = [e for e in (workspace_error(run),) if e]
    changed = changed_files(run, ignore)
    if changed:
        return errors + [f"plan files changed after the plan gate: {', '.join(changed)}; to change checks use "
                f"`factory run replan {run['id']} \"<reason>\"` and pass the plan gate again"]
    return errors


def _last_event(run: dict, pred) -> tuple[int, dict] | tuple[None, None]:
    events = run.get("events") or []
    for i in range(len(events) - 1, -1, -1):
        if pred(events[i]):
            return i, events[i]
    return None, None


def check_digest(run_id: str, chk: dict) -> str:
    """sha256 of one check definition plus the bytes of the run-dir files it reads (spec, adapter, map, script, cmd
    paths). Recorded in evidence `details.check_digest`; `prove` reuses a pass only when it still matches (DESIGN §14)."""
    rd = run_dir(run_id)
    inputs: dict[str, str] = {}
    for _, rel in check_inputs(run_id, chk):
        inputs[rel] = file_sha256(rd / rel) or ""
    body = {"check": {k: v for k, v in chk.items() if k != "id"}, "inputs": inputs}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


RUNTIME_EVIDENCE_TYPES = frozenset({"browser", "visual", "style", "api", "screenshot"})


def run_heads(run: dict, target: str | None = None) -> dict[str, str]:
    """{alias: HEAD} of every repo worktree of the run (the hotfix target's worktree when `target` is a mirror).
    Runtime evidence exercises the whole stack, so it records these (`details.run_heads`; DESIGN section 14)."""
    heads: dict[str, str] = {}
    for alias, rec in sorted((run.get("repos") or {}).items()):
        rec = rec or {}
        wt = (rec.get("worktrees") or {}).get(target) if target else None
        wt = wt or rec.get("worktree")
        if not wt:
            continue
        try:
            heads[alias] = git_head(Path(wt))
        except Exception:
            continue
    return heads


def run_heads_error(run: dict, check_id: str, ev: dict) -> str | None:
    """Runtime evidence that recorded `run_heads` is stale once any repo of the run moved. Old evidence without them is unchanged."""
    recorded = (ev.get("details") or {}).get("run_heads")
    if ev.get("type") not in RUNTIME_EVIDENCE_TYPES or not isinstance(recorded, dict):
        return None
    target = check_id.split("@", 1)[1] if "@" in check_id else None
    current = run_heads(run, target)
    moved = sorted(a for a, h in current.items() if recorded.get(a) != h)
    if moved:
        return (f"{check_id}: runtime evidence was collected when {', '.join(moved)} was at another commit "
                f"(run_heads); re-run factory prove")
    return None


def evidence_error(run: dict, check_id: str) -> str | None:
    """None when evidence/<id>.json is the file `prove` wrote after the current plan lock."""
    path = evidence_file(run["id"], check_id)
    i, ev = _last_event(run, lambda e: e.get("kind") == "evidence" and (e.get("data") or {}).get("check") == check_id)
    if ev is None:
        return f"{check_id}: no evidence event in run.json (evidence was not written by factory prove)"
    digest = (ev.get("data") or {}).get("digest")
    if not digest or digest != file_sha256(path):
        return f"{check_id}: evidence file does not match the digest factory prove recorded (edited or hand-written)"
    lock = run.get("plan_lock")
    if isinstance(lock, dict) and isinstance(lock.get("seq"), int) and i < lock["seq"]:
        return f"{check_id}: evidence predates the plan lock; re-run factory prove"
    base = check_id.split("@", 1)[0]
    j, _ = _last_event(run, lambda e: e.get("kind") == "amend_check" and (e.get("data") or {}).get("check") == base)
    if j is not None and i < j:
        return f"{check_id}: check was amended (run amend-check) after this evidence; re-run factory prove"
    try:
        ev_rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(ev_rec, dict):
        return None
    return check_digest_error(run, check_id, ev_rec) or run_heads_error(run, check_id, ev_rec)


def current_check(run_id: str, check_id: str) -> dict | None:
    """The acs.json entry of `check_id` (base id, without `@target`) as it is now, else None (rules, quality, REPRO)."""
    base = check_id.split("@", 1)[0]
    try:
        data = json.loads((run_dir(run_id) / "acs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for chk in (data.get("checks") or []) if isinstance(data, dict) else []:
        if isinstance(chk, dict) and chk.get("id") == base:
            return chk
    return None


def check_digest_error(run: dict, check_id: str, ev: dict) -> str | None:
    """Evidence that recorded `details.check_digest` is stale once the check (its acs.json entry or any run-dir file it
    reads, e.g. a spec shared with another check) no longer hashes the same. Evidence without it is judged as before."""
    recorded = (ev.get("details") or {}).get("check_digest")
    if not isinstance(recorded, str) or ev.get("type") == "quality" or ev.get("kind") in ("repro", "before"):
        return None
    chk = current_check(run["id"], check_id)
    if chk is None:
        return None
    if check_digest(run["id"], chk) != recorded:
        return (f"{check_id}: the check or a file it reads changed since this evidence (check_digest); "
                f"re-run factory prove")
    return None


def review_error(run: dict, path: Path, kind: str, rnd) -> str | None:
    i, ev = _last_event(run, lambda e: e.get("kind") == "review" and (e.get("data") or {}).get("review_kind") == kind
                        and (e.get("data") or {}).get("round") == rnd)
    if ev is None:
        return f"{kind} review round {rnd}: no review event in run.json (not recorded by factory review record)"
    digest = (ev.get("data") or {}).get("digest")
    if not digest or digest != file_sha256(path):
        return f"{kind} review round {rnd}: file does not match the digest factory review record wrote (edited or hand-written)"
    return None


def review_lock_error(run: dict, rec: dict, kind: str) -> str | None:
    """A review counts only for the plan lock it was recorded against (a replan invalidates it)."""
    lock = run.get("plan_lock") if isinstance(run.get("plan_lock"), dict) else {}
    if kind == "code" and rec.get("plan_code") and rec.get("plan_code") == lock.get("code_scope"):
        return None  # only checks changed (replan of the proof): the code review is still about the same ACs
    if rec.get("plan_lock") != lock.get("digest"):
        return (f"{kind} review round {rec.get('round')} was recorded against another plan lock "
                f"({str(rec.get('plan_lock'))[:8]} != {str(lock.get('digest'))[:8]}; replan since): re-review")
    return None
