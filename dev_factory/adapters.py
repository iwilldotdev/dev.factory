"""Files a harness adapter owns: marker blocks, whole generated files and one merged hook entry; every change keeps a backup [L-160, L-161]."""
from __future__ import annotations

import json
from pathlib import Path

from .util import FactoryError, fwd

BEGIN, END = "<!-- factory:begin -->", "<!-- factory:end -->"
OWNED = "<!-- factory:generated"  # a whole file carries this line; a file without it is somebody else's and stays untouched


def _read(path: Path) -> str | None:
    """The file's text with its own line endings (no newline translation), None when absent; not UTF-8 is a clear error."""
    if not path.is_file():
        return None
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FactoryError(f"{fwd(path)} is not UTF-8 text ({exc.reason} at byte {exc.start}); convert it or move it away, nothing was changed") from exc


def _save(path: Path, text: str | bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    except OSError as exc:  # a file another program holds open or a read-only folder: the old content stays [L-161]
        raise FactoryError(f"cannot write {fwd(path)}: {exc}; close the program that holds it, then run init again") from exc


def _backup(path: Path) -> str:
    n = 1
    while path.with_name(f"{path.name}.factory-bak-{n}").exists():
        n += 1
    dest = path.with_name(f"{path.name}.factory-bak-{n}")
    _save(dest, path.read_bytes())  # byte for byte
    return f"; the previous file is kept as {dest.name}"


def put(path: Path, new: str, what: str) -> str:
    """Write `new` over an existing file after a backup; nothing changes when it is the same text."""
    old = _read(path)
    if old and "\r\n" in old:  # keep the file's own line endings
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    if old == new:
        return f"unchanged {fwd(path)}"
    note = _backup(path) if old else ""
    _save(path, new)
    return f"wrote {fwd(path)} ({what}){note}"


def put_block(path: Path, block: str) -> str:
    """The factory section of a host file: everything outside the markers stays as it is."""
    text = _read(path) or ""
    block = block.rstrip("\n")
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        new = head + block + rest.split(END, 1)[1]
    else:
        new = text + ("\n" if text and not text.endswith("\n\n") else "") + block + "\n"
    return put(path, new, "factory section")


def put_owned(path: Path, text: str) -> str:
    if path.is_file() and OWNED not in (_read(path) or ""):
        return f"skipped {fwd(path)}: it is not a generated file, so it was left alone"
    return put(path, text, "generated")


def merge_hook(path: Path, event: str, entry: dict, prefix: str) -> str:
    """Add `entry` to `hooks.<event>` of a settings file; other keys and entries stay. Groups whose commands all start with `prefix` are ours."""
    try:
        doc = json.loads(_read(path) or "{}")
    except ValueError as exc:
        raise FactoryError(f"{fwd(path)} is not valid JSON ({exc}); fix it before adding hooks, nothing was changed") from exc
    hooks = doc.setdefault("hooks", {}) if isinstance(doc, dict) else None
    if not isinstance(hooks, dict) or not isinstance(hooks.setdefault(event, []), list):
        raise FactoryError(f"{fwd(path)}: unexpected shape of the hooks setting; nothing was changed")

    def ours(g) -> bool:
        cmds = [h.get("command", "") for h in g.get("hooks", []) if isinstance(h, dict)] if isinstance(g, dict) else []
        return bool(cmds) and all(c.startswith(prefix) for c in cmds)
    before = json.dumps(doc, sort_keys=True)
    hooks[event] = [g for g in hooks[event] if not ours(g) or g == entry]
    if entry not in hooks[event]:
        hooks[event].append(entry)
    if json.dumps(doc, sort_keys=True) == before and path.is_file():
        return f"unchanged {fwd(path)}"
    return put(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n", "hook entries")
