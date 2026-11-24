"""Ticket media (image/video attachments): the intake gate refuses a pack whose media nobody looked at.

The CLI never downloads. The saved Jira issue (`remote/issue-<KEY>.json`) lists `fields.attachment[]`; the author
downloads each image through dev.mcp (`save_to` under `remote/media/`) and records it in
`remote/media/manifest.json`:

    {"items": [{"name": "<attachment filename>", "id": "<attachment id>", "type": "image/png", "source": "<url or id>",
                "seen": true, "file": "remote/media/<file>"},
               {"name": "<video filename>", "type": "video/mp4", "source": "...", "seen": false,
                "note": "asked the user for the file or a description"}]}

`seen: true` needs the downloaded file; `seen: false` needs a `question` blocker that names the attachment. Packs
without an attachment list (older runs, a filtered `fields`) have nothing to check.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .core import FactoryError, abs_path, fetch_item, read_json, run_dir

MEDIA_DIR = "remote/media"
MANIFEST = f"{MEDIA_DIR}/manifest.json"
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}


def _safe(name: str, mime: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "attachment"
    ext = EXT.get(mime, "")
    return base if not ext or base.lower().endswith(ext) else base + ext


def _save_name(a: dict, name: str, mime: str) -> str:
    """`<id>-<safe name>`: two attachments with the same filename never share a file."""
    safe = _safe(name, mime)
    return f"{a['id']}-{safe}" if a.get("id") else safe


def attachments(run_id: str, run: dict) -> list[dict]:
    """image/* and video/* attachments listed in the saved issue of each ticket."""
    out: list[dict] = []
    for t in run.get("tickets") or []:
        path = run_dir(run_id) / "remote" / f"issue-{t['key']}.json"
        try:
            data = read_json(path)
        except (FactoryError, OSError, ValueError):
            continue
        fields = data.get("fields") if isinstance(data, dict) else None
        for a in (fields.get("attachment") if isinstance(fields, dict) else None) or []:
            if not isinstance(a, dict):
                continue
            mime = str(a.get("mimeType") or "")
            if not mime.startswith(("image/", "video/")):
                continue
            name = str(a.get("filename") or a.get("id") or "")
            out.append({"ticket": t["key"], "name": name, "id": str(a.get("id") or ""), "type": mime,
                        "source": str(a.get("content") or ""), "file": f"{MEDIA_DIR}/{_save_name(a, name, mime)}"})
    return out


def _manifest(run_id: str) -> tuple[list[dict], str | None]:
    path = run_dir(run_id) / MANIFEST
    if not path.is_file():
        return [], None
    try:
        data = read_json(path)
    except (FactoryError, OSError, ValueError) as err:
        return [], f"{MANIFEST} is not valid JSON ({err})"
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], f'{MANIFEST} must be {{"items": [...]}}'
    return [i for i in items if isinstance(i, dict)], None


def _entry(items: list[dict], att: dict, atts: list[dict] | None = None) -> dict | None:
    """The manifest entry of an attachment: by id. By name only for an attachment without id, or for an entry
    without id when no other attachment of the issue has that name (so one entry never covers two attachments)."""
    if att["id"]:
        for i in items:
            if str(i.get("id") or "") == att["id"]:
                return i
    elif att["name"]:
        for i in items:
            if i.get("name") == att["name"]:
                return i
        return None
    if att["name"] and not any(o is not att and o["name"] == att["name"] for o in atts or []):
        for i in items:
            if not i.get("id") and i.get("name") == att["name"]:
                return i
    return None


def _file_ok(run_id: str, rel: Any) -> bool:
    if not isinstance(rel, str) or not rel:
        return False
    root = (run_dir(run_id) / MEDIA_DIR).resolve()
    p = (run_dir(run_id) / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return False
    return p.is_file() and p.stat().st_size > 0


def errors(run_id: str, run: dict) -> list[str]:
    atts = attachments(run_id, run)
    if not atts:
        return []
    items, bad = _manifest(run_id)
    out = [bad] if bad else []
    questions = [str(b.get("text") or "") for b in run.get("blockers") or [] if b.get("kind") == "question"]
    for a in atts:
        what = f"{a['ticket']} attachment {a['name']!r} ({a['type']})"
        e = _entry(items, a, atts)
        if e is None:
            out.append(f"{what} is not in {MANIFEST}: " + (
                f"download it to {a['file']} (FETCH below), look at it, and record it with seen: true"
                if a["type"].startswith("image/") else
                "a video cannot be viewed: `factory run block <run> question` asking the user for the file or a "
                "description naming it, and record it with seen: false"))
        elif e.get("seen") is True:
            if not _file_ok(run_id, e.get("file")):
                out.append(f"{what}: seen: true but its file is missing or empty under {MEDIA_DIR}/ ({e.get('file')!r})")
        elif e.get("seen") is False:
            if not any(a["name"] in q or (a["id"] and a["id"] in q) for q in questions):
                out.append(f"{what}: seen: false needs a `question` blocker naming it (ask the user for the file or a "
                           "description)")
        else:
            out.append(f"{what}: manifest entry needs seen: true or false")
    return out


def fetches(run_id: str, run: dict) -> list[dict]:
    """dev.mcp downloads for the images not yet in the manifest as seen."""
    items, _ = _manifest(run_id)
    out = []
    atts = attachments(run_id, run)
    for a in atts:
        e = _entry(items, a, atts)
        if a["type"].startswith("image/") and a["id"] and not (e and e.get("seen") is True and _file_ok(run_id, e.get("file"))):
            out.append(fetch_item("jira_download_issue_attachment", attachmentId=a["id"],
                                  save_to=abs_path(run_dir(run_id) / a["file"]), overwrite=True))
    return out
