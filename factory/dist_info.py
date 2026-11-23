"""Where this CLI runs from: the installed snapshot or a source checkout, and whether the snapshot is stale.

`install.py` copies the runtime into `<profile>/factory-dist/` and writes `provenance.json` there. A tree without
that file is a source checkout (also what `FACTORY_SRC` selects in the shims). Stdlib only; nothing here is fatal.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = "provenance.json"
# what the installer copies and the CLI reads at runtime; a change in these makes the snapshot stale
RUNTIME_PATHS = ("factory", "browser/src", "browser/package.json")


def provenance(root: Path = ROOT) -> dict | None:
    """The snapshot's provenance, or None when `root` is a source checkout."""
    try:
        data = json.loads((root / PROVENANCE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def describe(version: str, root: Path = ROOT) -> str:
    """One line for `--version`: version, sha, snapshot or source, path."""
    prov = provenance(root)
    if prov:
        sha = str(prov.get("sha") or "unknown")[:9]
        dirty = "+dirty" if prov.get("dirty") else ""
        return (f"factory {version} (snapshot {sha}{dirty}, installed {prov.get('installed_at', '?')} "
                f"from {prov.get('source', '?')}) at {root}")
    sha = _git(root, "rev-parse", "--short=9", "HEAD")
    return f"factory {version} (source{' ' + sha if sha else ''}) at {root}"


def behind_source(root: Path = ROOT) -> str | None:
    """The notice text when the snapshot's source repo moved on, else None. Runs git (about 2 calls): only call it
    from `--version` and `run status`, never on every command."""
    prov = provenance(root)
    if not prov or not prov.get("source") or not prov.get("sha"):
        return None
    src = Path(str(prov["source"]))
    if not (src / ".git").exists():
        return None
    head = _git(src, "rev-parse", "HEAD")
    if head is None:
        return None
    dirty = _git(src, "status", "--porcelain", "--untracked-files=normal", "--", *RUNTIME_PATHS)
    if head != prov["sha"] or dirty:
        return "installed snapshot is behind source (run install.py)"
    return None
