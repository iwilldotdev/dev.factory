"""Shared helpers: errors, canonical JSON, hashing, safe ids, atomic writes, redaction, versions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class FactoryError(Exception):
    """A user-facing failure. `code` is the process exit code (2 = usage, config or lock error)."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def canonical(obj) -> str:
    """Canonical JSON: sorted keys, compact separators, no trailing newline [L-167]."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_id(value: str) -> str:
    """Map an id to a safe file name: characters outside [A-Za-z0-9._-] become '-' [L-157]."""
    out = re.sub(r"[^A-Za-z0-9._-]", "-", value or "")
    if not out or set(out) == {"."}:
        raise FactoryError(f"id {value!r} cannot be mapped to a safe file name")
    return out


def fwd(path) -> str:
    """Absolute, forward-slash path as printed for the harness [L-158]."""
    return Path(os.path.abspath(path)).as_posix()


def write_atomic(path: Path, data) -> None:
    """Write a file through a temp name and replace; retry sharing violations on Windows [L-163]."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        for attempt in range(12):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if tmp.exists():
            tmp.unlink()


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FactoryError(f"cannot read JSON {fwd(path)}: {exc}") from exc


def redact(text: str, secrets) -> str:
    """Replace every secret value in text with ***; values shorter than 3 characters are ignored."""
    for value in sorted((v for v in secrets if v and len(v) >= 3), key=len, reverse=True):
        text = text.replace(value, "***")
    return text


_VER = re.compile(r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?$")


def _vkey(text: str):
    m = _VER.match(text.strip())
    if not m:
        raise FactoryError(f"bad version {text!r}")
    rel = [int(x) for x in m.group(1).split(".")]
    rel += [0] * (4 - len(rel))
    pre = {"a": 0, "b": 1, "rc": 2}.get(m.group(2), 3)
    return (tuple(rel), pre, int(m.group(3) or 0))


def version_ok(spec: str, version: str) -> bool:
    """True when version satisfies a comma-separated specifier such as '>=1.0.0a1,<1.1'."""
    have = _vkey(version)
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b,
           "!=": lambda a, b: a != b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}
    for clause in spec.split(","):
        m = re.match(r"^\s*(>=|<=|==|!=|>|<)\s*(\S+)\s*$", clause)
        if not m:
            raise FactoryError(f"bad version specifier {clause.strip()!r} in {spec!r}")
        if not ops[m.group(1)](have, _vkey(m.group(2))):
            return False
    return True


def build_identity() -> str:
    """Which build runs: VCS commit of an installed or editable checkout, read without spawning git [L-150]."""
    root = Path(__file__).resolve().parent.parent
    git = root / ".git"
    try:
        if git.is_file():  # linked worktree: gitdir pointer
            git = Path(git.read_text().split(":", 1)[1].strip())
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            common = git
            if (git / "commondir").exists():
                common = (git / (git / "commondir").read_text().strip()).resolve()
            for base in (git, common):
                if (base / ref).exists():
                    return "git " + (base / ref).read_text().strip()[:12]
            for line in (common / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return "git " + line.split()[0][:12]
            return "git unknown"
        return "git " + head[:12]
    except (OSError, IndexError):
        pass
    try:
        from importlib import metadata
        direct = metadata.distribution("dev-factory").read_text("direct_url.json")
        if direct:
            commit = json.loads(direct).get("vcs_info", {}).get("commit_id")
            if commit:
                return "git " + commit[:12]
    except Exception:  # noqa: BLE001 - identity is best effort
        pass
    return "release"
