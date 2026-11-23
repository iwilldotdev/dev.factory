"""Where this CLI runs from: the installed snapshot or a source checkout, and whether the snapshot is stale.

`install.py` copies the runtime into `<profile>/factory-dist/` and writes `provenance.json` there. A tree without
that file is a source checkout (also what `FACTORY_SRC` selects in the shims). Stdlib only; nothing here is fatal.
"""

from __future__ import annotations

import hashlib
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


def runtime_state(repo: Path) -> str | None:
    """Digest of the runtime-relevant state of a source repo: HEAD plus the diff of dirty tracked runtime paths and the
    content of untracked ones. install.py stores it in provenance.json; an install from a dirty tree is only "behind"
    once that state changes again. None when `repo` is not a git work tree."""
    head = _git(repo, "rev-parse", "HEAD")
    if head is None:
        return None
    h = hashlib.sha256(head.encode())
    h.update(b"\0diff\0" + (_git(repo, "diff", "HEAD", "--", *RUNTIME_PATHS) or "").encode("utf-8", "replace"))
    listing = _git(repo, "ls-files", "--others", "--exclude-standard", "--", *RUNTIME_PATHS) or ""
    for rel in sorted(r for r in listing.splitlines() if r):
        h.update(b"\0new\0" + rel.encode("utf-8", "replace") + b"\0")
        try:
            h.update((repo / rel).read_bytes())
        except OSError:
            pass
    return h.hexdigest()


def browser_dist_stale(browser: Path) -> bool:
    """True when a browser/src .ts file is newer than the newest browser/dist .js (the gitignored build is out of date)."""
    def newest(folder: Path, pattern: str) -> float | None:
        times = []
        for f in folder.rglob(pattern):
            try:
                times.append(f.stat().st_mtime)
            except OSError:
                pass
        return max(times) if times else None
    src, built = newest(browser / "src", "*.ts"), newest(browser / "dist", "*.js")
    return src is not None and built is not None and src > built


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
    state = prov.get("state")
    if state:
        moved = head != prov["sha"] or runtime_state(src) != state
    else:  # provenance from an older install.py
        dirty = _git(src, "status", "--porcelain", "--untracked-files=normal", "--", *RUNTIME_PATHS)
        moved = head != prov["sha"] or bool(dirty)
    note = "browser/dist was older than browser/src when installed: run `npm run build` in browser/ first"
    stale = f" ({note})" if prov.get("dist_stale") else ""
    if moved:
        return "installed snapshot is behind source (run install.py)" + stale
    if stale:
        return f"installed snapshot has a stale browser/dist ({note})"
    return None
