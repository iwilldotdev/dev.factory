"""Shared primitives for the factory CLI. Python >= 3.10, stdlib only.

Every command module and collector imports from here; keep this API stable.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

VERSION = "0.1.0"
SCHEMA_VERSION = 1


class FactoryError(Exception):
    """Usage or configuration error (exit 2). `fetch` lists dev.mcp calls that would fix it."""

    def __init__(self, *args, fetch: list[dict] | None = None):
        super().__init__(*args)
        self.fetch = fetch or []


class GateFailed(Exception):
    """A gate or check failed (exit 1). `reasons` lists what is missing."""

    def __init__(self, reasons: list[str], fetch: list[dict] | None = None):
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.fetch = fetch or []


# ---------- paths ----------

def factory_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".factory").expanduser()


def workspace_path(ws: str) -> Path:
    return factory_home() / "workspaces" / ws / "workspace.json"


def run_dir(run_id: str) -> Path:
    return factory_home() / "runs" / run_id


def secrets_path(ws: str) -> Path:
    return factory_home() / "secrets" / f"{ws}.env"


def fwd(path: Any) -> str:
    return str(path).replace("\\", "/")


def abs_path(path: Path) -> str:
    """Absolute path with forward slashes, for dev.mcp `save_to`."""
    return fwd(Path(path).expanduser().absolute())


def fetch_item(tool: str, **args: Any) -> dict:
    """A ready-to-use dev.mcp call: {tool, args}. save_to is always absolute, forward slashes."""
    return {"tool": tool, "args": args}


def fetch_call(item: dict) -> str:
    """`tool k=v k=v` line for chat/terminal."""
    parts = [item["tool"]]
    for key, val in item["args"].items():
        text = str(val).lower() if isinstance(val, bool) else str(val)
        parts.append(f'{key}="{text}"' if re.search(r"\s", text) else f"{key}={text}")
    return " ".join(parts)


def dedupe_fetch(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for it in items:
        if it not in out:
            out.append(it)
    return out


def node_file(node_id: str) -> str:
    """Figma node id '3544:5710' -> '3544-5710' (pack file stem)."""
    return node_id.replace(":", "-")


# ---------- time / json ----------

def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise FactoryError(f"missing file: {path}") from err
    except json.JSONDecodeError as err:
        raise FactoryError(f"invalid JSON in {path}: {err}") from err


def write_json(path: Path, data: Any) -> None:
    """Atomic write: unique tmp name (safe across processes), retried replace (Windows sharing violations)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------- workspace ----------

def workspace_load(ws: str) -> dict:
    data = read_json(workspace_path(ws))
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise FactoryError(f"workspace {ws}: schemaVersion must be {SCHEMA_VERSION}")
    return data


def repo_conf(workspace: dict, alias: str) -> dict:
    for repo in workspace.get("repos", []):
        if repo.get("alias") == alias or repo.get("name") == alias:
            return repo
    raise FactoryError(f"repo {alias!r} not in workspace {workspace.get('name')}")


# ---------- runs ----------

LOCK_TIMEOUT_S = 10.0
LOCK_STALE_S = 60.0
_SAVED_HERE: dict[str, int] = {}  # run id -> revision this process wrote last
_HELD: set[str] = set()  # run ids whose lock this process already holds (run_update -> run_save)


@contextlib.contextmanager
def _run_lock(run_id: str):
    """Exclusive per-run lock (`run.json.lock`, O_EXCL). Re-entrant inside one process; stale locks are reclaimed."""
    if run_id in _HELD:
        yield
        return
    path = run_dir(run_id) / "run.json.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()}\n".encode())
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_S:
                    path.unlink()
                    continue
            except OSError:
                pass
        except PermissionError:
            pass  # Windows: lock being deleted by its owner
        if time.monotonic() > deadline:
            raise FactoryError(f"run {run_id} is locked by another process ({path}); retry, or delete the lock if stale")
        time.sleep(0.02)
    _HELD.add(run_id)
    try:
        yield
    finally:
        _HELD.discard(run_id)
        try:
            path.unlink()
        except OSError:
            pass


def run_load(run_id: str) -> dict:
    data = read_json(run_dir(run_id) / "run.json")
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise FactoryError(f"run {run_id}: schemaVersion must be {SCHEMA_VERSION}")
    rev = data.get("rev", 0)
    data["_loaded_rev"] = rev if isinstance(rev, int) else 0  # non-persisted: optimistic concurrency token
    return data


def run_save(run: dict) -> None:
    """Locked, optimistic save: refuses to overwrite a run.json that changed on disk since `run_load`."""
    path = run_dir(run["id"]) / "run.json"
    with _run_lock(run["id"]):
        disk_rev = 0
        if path.is_file():
            try:
                disk = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                disk = None
            if isinstance(disk, dict):
                disk_rev = disk.get("rev", 0) if isinstance(disk.get("rev", 0), int) else 0
                if "_loaded_rev" in run and disk_rev != run["_loaded_rev"]:
                    raise FactoryError(f"run {run['id']} changed on disk by another process since it was loaded; "
                                       "re-run the command")
        run["updated"] = now()
        run["rev"] = disk_rev + 1
        run["_loaded_rev"] = run["rev"]
        write_json(path, {k: v for k, v in run.items() if k != "_loaded_rev"})
        _SAVED_HERE[run["id"]] = run["rev"]


def run_resync(run: dict) -> None:
    """Adopt a write made by this same process through a nested copy (a helper that loaded and saved the run
    while the caller held its own copy): merge the events it appended and take its revision. Writes by other
    processes are left alone, so they still surface as a conflict at save time."""
    path = run_dir(run["id"]) / "run.json"
    try:
        disk = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    rev = disk.get("rev", 0)
    if rev == run.get("_loaded_rev") or _SAVED_HERE.get(run["id"]) != rev:
        return
    mine = run.setdefault("events", [])
    mine.extend(e for e in disk.get("events") or [] if e not in mine)
    run["_loaded_rev"] = rev


def run_update(run_id: str, fn) -> dict:
    """Load under the lock, `fn(run)`, save, return the run: no conflict with other processes is possible."""
    with _run_lock(run_id):
        run = run_load(run_id)
        fn(run)
        run_save(run)
        return run


# ---------- sessions ----------

# Best-effort hint of which Claude session/agent ran a command (whatever the host exports; omitted if none).
SESSION_HINT_VARS = ("CLAUDE_AGENT_ID", "CLAUDE_CODE_AGENT_ID", "CLAUDE_SUBAGENT_ID", "CLAUDE_SESSION_ID",
                     "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT")
# Ownership uses only session ids (not agent ids: subagents of one session share its runs).
SESSION_ID_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
CLAIM_FRESH_S = 12 * 3600


def session_hint() -> dict:
    return {k: os.environ[k] for k in SESSION_HINT_VARS if os.environ.get(k)}


def session_id() -> str | None:
    for k in SESSION_ID_VARS:
        if os.environ.get(k):
            return os.environ[k]
    return None


def session_short(sid: str | None) -> str:
    return (sid or "")[:8]


def claims(run: dict) -> list[dict]:
    """Claim records in run["sessions"] (that list may also hold plain transcript ids/paths for metrics)."""
    return [s for s in run.get("sessions") or [] if isinstance(s, dict) and s.get("session")]


def current_owner(run: dict) -> dict | None:
    c = claims(run)
    return c[-1] if c else None


def claim_is_recent(claim: dict) -> bool:
    try:
        at = _dt.datetime.fromisoformat(str(claim.get("claimed_at")))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - at).total_seconds() < CLAIM_FRESH_S


def ensure_owner(run: dict) -> None:
    """Refuse when another session with a recent claim owns the run. No-op without a session id or a claim."""
    sid = session_id()
    owner = current_owner(run)
    if not sid or not owner or owner["session"] == sid or not claim_is_recent(owner):
        return
    raise FactoryError(f"run {run['id']} is claimed by session {session_short(owner['session'])} "
                       f"(since {owner.get('claimed_at')}); this is session {session_short(sid)}. "
                       f"Take it over with `factory run claim {run['id']} --takeover` if that is intended")


def event(run: dict, kind: str, **data: Any) -> None:
    """Append an event to a loaded run (caller saves)."""
    run.setdefault("events", []).append({"at": now(), "kind": kind, "data": data})


def is_env_only(run: dict, alias: str) -> bool:
    """A repo registered only to serve the local env from a run worktree (`run branch --env-only`, `run env-only`)."""
    return bool(((run.get("repos") or {}).get(alias) or {}).get("env_only"))


def code_repos(run: dict) -> dict:
    """The run's repos that carry code: every repo except env_only ones. Gates, prove, review, ship and mirror use
    these; env up/check/login use every repo (env_only included) so the env starts from the run worktrees."""
    return {a: r for a, r in (run.get("repos") or {}).items() if not (r or {}).get("env_only")}


def repo_path_for_run(run: dict, workspace: dict, alias: str) -> Path:
    """Worktree of the run when recorded, else the repo's main path."""
    rec = run.get("repos", {}).get(alias) or {}
    if rec.get("worktree"):
        return Path(rec["worktree"])
    return Path(repo_conf(workspace, alias)["path"])


def run_ids_for_ticket(workspace_name: str, key: str) -> list[str]:
    """Run ids of `workspace_name` that carry ticket `key`, by run.json content (run ids are not assumed to be
    `{ws}-{key}`: epic children, review-fix conversions and renamed runs differ)."""
    guess = f"{str(workspace_name).lower()}-{key.lower()}"
    out: list[str] = []
    root = factory_home() / "runs"
    if root.is_dir():
        for p in sorted(root.glob("*/run.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or str(data.get("workspace", "")).lower() != str(workspace_name).lower():
                continue
            if any(str(t.get("key", "")).upper() == key.upper() for t in data.get("tickets") or [] if isinstance(t, dict)):
                out.append(p.parent.name)
    out.sort(key=lambda rid: rid != guess)  # the conventional id first
    return out or [guess]


def _dep_run_ids(run: dict, workspace: dict) -> list[str]:
    ws = str(workspace.get("name", ""))
    keys: list[str] = []
    for t in run.get("tickets") or []:
        keys += [str(d) for d in t.get("depends_on") or []]
    parent = run.get("parent")
    if parent:
        try:
            epic = read_json(run_dir(parent) / "epic.json")
        except (FactoryError, OSError, ValueError):
            epic = {}
        mine = {str(t.get("key", "")).upper() for t in run.get("tickets") or []}
        for t in epic.get("tickets") or []:
            if str(t.get("key", "")).upper() in mine:
                keys += [str(d) for d in t.get("depends_on") or []]
    ids: list[str] = []
    for k in dict.fromkeys(keys):
        ids += [rid for rid in run_ids_for_ticket(ws, k) if rid not in ids]
    return ids


def repo_path_for_env(run: dict, workspace: dict, alias: str, warn=None) -> Path:
    """Like repo_path_for_run, but an epic child without a worktree for `alias` uses the nearest dependency run's worktree.

    Falling back to the repo's MAIN checkout (no worktree anywhere) is reported through `warn` (default: stderr):
    an env started there serves unreviewed code."""
    if warn is None:
        warn = lambda msg: print(msg, file=sys.stderr)  # noqa: E731
    rec = (run.get("repos") or {}).get(alias) or {}
    if rec.get("worktree"):
        return repo_path_for_run(run, workspace, alias)
    if run.get("parent"):
        seen = {run.get("id")}
        queue = _dep_run_ids(run, workspace)
        while queue:
            rid = queue.pop(0)
            if rid in seen:
                continue
            seen.add(rid)
            try:
                dep = run_load(rid)
            except (FactoryError, OSError, ValueError):
                continue
            wt = ((dep.get("repos") or {}).get(alias) or {}).get("worktree")
            if wt:
                return Path(wt)
            queue += _dep_run_ids(dep, workspace)
    path = repo_path_for_run(run, workspace, alias)
    warn(f"WARNING: run {run.get('id')} has no worktree for {alias}; using the main checkout {path} "
         "(it may not contain the run's code)")
    return path


def hotfix_of(run: dict) -> dict | None:
    hf = run.get("hotfix")
    return hf if isinstance(hf, dict) and hf.get("base") else None


def run_targets(run: dict) -> list[str | None]:
    """Evidence targets: [base, *mirror] for hotfix runs, else [None] (ids unchanged)."""
    hf = hotfix_of(run)
    return [hf["base"], *(hf.get("mirror") or [])] if hf else [None]


def target_branch(workspace: dict, alias: str, target: str | None) -> str | None:
    """Git branch behind a logical target for one repo (`target_branches` in the repo entry); the target itself when unmapped."""
    if not target:
        return target
    try:
        mapped = (repo_conf(workspace, alias).get("target_branches") or {}).get(target)
    except FactoryError:
        return target
    return mapped if isinstance(mapped, str) and mapped else target


def run_base_branch(run: dict, workspace: dict, alias: str) -> str:
    """Git branch the run's repo was branched from. `base` holds a logical target, except in review runs (a real MR branch)."""
    base = (run.get("repos") or {}).get(alias, {}).get("base", "main")
    return base if run.get("kind", "ticket") == "review" else target_branch(workspace, alias, base)


def evidence_id(run: dict, check_id: str, target: str | None) -> str:
    """`CHK-1@homolog` for hotfix runs, plain `CHK-1` otherwise."""
    return f"{check_id}@{target}" if target and hotfix_of(run) else check_id


def target_worktree(run: dict, workspace: dict, alias: str, target: str | None = None) -> Path | None:
    """Worktree for a target; the run worktree for the base/None; None when a mirror is not created yet."""
    hf = hotfix_of(run)
    if hf and target and target != hf["base"]:
        wt = ((run.get("repos") or {}).get(alias) or {}).get("worktrees", {}).get(target)
        return Path(wt) if wt else None
    return repo_path_for_run(run, workspace, alias)


def target_serve_path(run: dict, workspace: dict, alias: str, target: str | None) -> Path | None:
    """Where a mirror target's runtime is served for `alias`: its mirror worktree, else the repo's `target_paths[target]`;
    None for the base target/None, or when the repo has neither (it keeps the run's own resolution)."""
    hf = hotfix_of(run)
    if not (hf and target and target != hf["base"]):
        return None
    wt = target_worktree(run, workspace, alias, target)
    if wt:
        return wt
    try:
        extra = (repo_conf(workspace, alias).get("target_paths") or {}).get(target)
    except FactoryError:
        return None
    return Path(extra) if isinstance(extra, str) and extra else None


def check_target(run: dict, target: str | None) -> str | None:
    """`--target` value of a command: None for absent or the base target (today's behaviour), the mirror name otherwise;
    FactoryError when it is not one of the run's targets."""
    if not target:
        return None
    targets = run_targets(run)
    if target not in targets:
        raise FactoryError(f"--target {target}: not a target of run {run.get('id')} (targets: {', '.join(t for t in targets if t) or 'none, not a hotfix run'})")
    return None if target == targets[0] else target


# ---------- git ----------

def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise FactoryError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def _is_link(path: Path) -> bool:
    """Symlink or Windows junction (reparse point)."""
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def git_dirty(repo: Path) -> bool:
    """Uncommitted changes, untracked files included (.gitignore respected).

    The `node_modules` link that `link_deps` puts in a worktree is not a change.
    """
    out = git(repo, "status", "--porcelain", "--untracked-files=normal")
    for line in out.splitlines():
        if not line.strip():
            continue
        if line.startswith("??"):
            name = line[2:].strip().strip('"').rstrip("/")
            if name == "node_modules" and _is_link(Path(repo) / "node_modules"):
                continue
        return True
    return False


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


# ---------- evidence / reviews ----------

@dataclass
class Ctx:
    """What a collector receives besides its check."""

    run: dict
    workspace: dict
    run_dir: Path
    repo_path: Path | None = None
    env: dict = field(default_factory=dict)  # repo env block from workspace
    out_dir: Path | None = None  # evidence/<check-id>/ for artifacts


def write_evidence(run_id: str, check: dict, body: dict, repo_path: Path | None, started: str, duration_s: float) -> dict:
    """Only `prove` calls this. Fills common fields and writes evidence/<check-id>.json."""
    record = {
        "schemaVersion": SCHEMA_VERSION,
        "check_id": check["id"],
        "type": check["type"],
        "status": body.get("status", "error"),
        "repo": check.get("repo"),
        "git_sha": git_head(repo_path) if repo_path else None,
        "dirty": git_dirty(repo_path) if repo_path else False,
        "collector": f"factory/{VERSION} {check['type']}",
        "started": started,
        "duration_s": round(duration_s, 2),
        "summary": body.get("summary", ""),
        "artifacts": body.get("artifacts", []),
        "details": body.get("details", {}),
    }
    if record["status"] not in ("pass", "fail", "error"):
        record["status"] = "error"
    if record["status"] != "pass" and isinstance(body.get("failure"), dict):
        record["failure"] = body["failure"]  # {kind, reason[, env_kind]}: why it is not a pass (see prove.failure_of)
    keep_history(run_id, check["id"])
    if record["status"] == "pass" and is_flaky(evidence_history(run_id, check["id"]), record):
        record["flaky"] = True
    write_json(run_dir(run_id) / "evidence" / f"{check['id']}.json", record)
    return record


def url_origin(url) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an http(s) URL (default ports filled in, host lower-cased), else None."""
    try:
        parsed = urlparse(str(url))
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed.scheme, parsed.hostname.lower(), port or (443 if parsed.scheme == "https" else 80)


HISTORY_CAP = 20  # attempts kept per check in evidence/history/<id>.jsonl (the latest stays in evidence/<id>.json)
HISTORY_DETAIL_KEYS = ("check_digest", "mode", "run_heads", "fail_kind", "env_kind", "flaky", "http_status")


def history_file(run_id: str, check_id: str) -> Path:
    return run_dir(run_id) / "evidence" / "history" / f"{check_id}.jsonl"


def _slim_attempt(rec: dict) -> dict:
    """What the history keeps of an evidence record: enough to see how a check went before, never the artifacts."""
    details = rec.get("details") if isinstance(rec.get("details"), dict) else {}
    out = {k: rec.get(k) for k in ("check_id", "status", "git_sha", "dirty", "started", "duration_s") if k in rec}
    out["summary"] = str(rec.get("summary") or "")[:300]
    for k in ("failure", "flaky"):
        if rec.get(k):
            out[k] = rec[k]
    out["details"] = {k: details[k] for k in HISTORY_DETAIL_KEYS if k in details}
    return out


def keep_history(run_id: str, check_id: str) -> None:
    """Before `evidence/<id>.json` is overwritten, append the record it holds to the history (capped, oldest dropped).
    Readers of the latest evidence are unchanged; the history is a separate file nothing but flaky detection reads."""
    path = evidence_file(run_id, check_id)
    if not path.is_file():
        return
    try:
        prev = read_json(path)
    except (FactoryError, OSError, ValueError):
        return
    if not isinstance(prev, dict):
        return
    hist = history_file(run_id, check_id)
    lines = [json.dumps(a, ensure_ascii=False) for a in evidence_history(run_id, check_id)]
    lines.append(json.dumps(_slim_attempt(prev), ensure_ascii=False))
    hist.parent.mkdir(parents=True, exist_ok=True)
    tmp = hist.with_name(f"{hist.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")  # temp file + replace: never a torn history
    try:
        tmp.write_text(chr(10).join(lines[-HISTORY_CAP:]) + chr(10), encoding="utf-8")
        os.replace(tmp, hist)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def evidence_history(run_id: str, check_id: str) -> list[dict]:
    """Previous attempts of a check, oldest first (the latest one is `evidence/<id>.json`)."""
    try:
        text = history_file(run_id, check_id).read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def is_flaky(history: list[dict], record: dict) -> bool:
    """`record` (a pass) is flaky when an earlier attempt of the same check FAILED an assertion (`failure.kind` is `assert`: a
    timeout, env or setup failure followed by a pass is an infrastructure recovery, not a flaky check) with the same check
    digest at the same repo sha (and, for runtime evidence, the same `run_heads`): the check flipped without any change."""
    det = record.get("details") if isinstance(record.get("details"), dict) else {}
    digest, heads = det.get("check_digest"), det.get("run_heads")
    if record.get("status") != "pass" or not digest:
        return False
    for old in history:
        odet = old.get("details") if isinstance(old.get("details"), dict) else {}
        failure = old.get("failure") if isinstance(old.get("failure"), dict) else {}
        if (old.get("status") == "fail" and failure.get("kind") == "assert" and odet.get("check_digest") == digest and old.get("git_sha") == record.get("git_sha")
                and odet.get("run_heads") == heads):
            return True
    return False


def flaky_checks(run_id: str) -> list[str]:
    """Evidence ids of the run whose latest record is marked flaky (a fail followed by a pass at the same sha and digest, or a
    browser assert that passed on the immediate retry)."""
    out = []
    for p in sorted((run_dir(run_id) / "evidence").glob("*.json")):
        try:
            rec = read_json(p)
        except (FactoryError, OSError, ValueError):
            continue
        det = rec.get("details") if isinstance(rec, dict) and isinstance(rec.get("details"), dict) else {}
        if isinstance(rec, dict) and (rec.get("flaky") or det.get("flaky")):
            out.append(p.stem)
    return out


def evidence_file(run_id: str, check_id: str) -> Path:
    return run_dir(run_id) / "evidence" / f"{check_id}.json"


def evidence_event(run: dict, record: dict) -> None:
    """`evidence` event with the sha256 of the written file; gates compare it with the file (DESIGN §2.4)."""
    cid = record["check_id"]
    data = {"check": cid, "status": record.get("status"), "sha": record.get("git_sha"),
            "digest": file_sha256(evidence_file(run["id"], cid))}
    fail = record.get("failure")
    if record.get("status") != "pass" and isinstance(fail, dict):  # why it is not a pass (`kind` is event()'s own arg)
        data.update(reason=fail.get("reason"), kind=fail.get("kind"))
    run.setdefault("events", []).append({"at": now(), "kind": "evidence", "data": data})


def load_evidence(run_id: str, check_id: str) -> dict | None:
    path = run_dir(run_id) / "evidence" / f"{check_id}.json"
    return read_json(path) if path.is_file() else None
