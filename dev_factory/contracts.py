"""Contract validation (`ticket`, `change`, `pipeline`) in stdlib code: fixed schemas, no mapping layer [L-156]."""
from __future__ import annotations

import mimetypes
import re

from .util import FactoryError

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$")
SCHEMAS = {"ticket": "ticket/1", "change": "change/1", "change_list": "change_list/1", "pipeline": "pipeline/1", "design_frame": "design_frame/1", "design_image": "design_image/1"}
PROOF_GRADE = ("change", "pipeline")  # must come from an adapter or a user confirmation [L-21]
MEDIA_TYPES = ("image/", "video/", "audio/")
MEDIA_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
             ".bmp": "image/bmp", ".svg": "image/svg+xml", ".mp4": "video/mp4", ".mov": "video/quicktime",
             ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v"}

# field -> (type, required). Types: s non-empty text, t text that may be empty, b bool, ts, sha, ss list of
# strings, ("e", ...) enum, ("L", spec) list of objects with a nested spec.
TICKET = {"id": ("s", 1), "url": ("s", 0), "title": ("s", 1), "type": (("e", "feature", "bug"), 1), "status": ("s", 1),
          "description_md": ("t", 1), "acceptance": ("ss", 0), "labels": ("ss", 0),
          "attachments": (("L", {"id": ("s", 1), "name": ("s", 1), "mime": ("t", 0), "size": ("i", 0), "url": ("s", 0)}), 1),
          "comments": (("L", {"id": ("s", 1), "author": ("s", 1), "created_at": ("ts", 1), "body_md": ("t", 1)}), 0),
          "links": (("L", {"rel": ("s", 1), "target": ("s", 1)}), 0)}
CHANGE = {"id": ("s", 1), "url": ("s", 1), "repo": ("s", 1), "title": ("s", 1), "description_md": ("t", 1),
          "source_branch": ("s", 1), "target_branch": ("s", 1), "head_sha": ("sha", 1),
          "state": (("e", "open", "merged", "closed"), 1), "draft": ("b", 1), "reviewers": ("ss", 0), "approvals": ("ss", 0),
          "threads": (("L", {"id": ("s", 1), "author": ("s", 1), "resolved": ("b", 1), "body_md": ("t", 1),
                             "file": ("s", 0), "line": ("i", 0)}), 0), "pipelines": ("ss", 0)}
PIPELINE = {"id": ("s", 1), "sha": ("sha", 1), "created_at": ("ts", 1), "url": ("s", 0),
            "status": (("e", "pending", "running", "success", "failed", "canceled", "skipped"), 1),
            "jobs": (("L", {"name": ("s", 1), "status": ("s", 1), "allow_failure": ("b", 1)}), 0)}
CHANGE_LIST = {"items": (("L", {"id": ("s", 1), "url": ("s", 1), "repo": ("s", 1), "title": ("s", 1), "author": ("s", 1), "draft": ("b", 1),
                                "ticket": ("s", 0), "own": ("b", 0), "approved": ("b", 0)}), 1)}
SPECS = {"ticket": TICKET, "change": CHANGE, "pipeline": PIPELINE, "change_list": CHANGE_LIST}


def _check(value, typ, path: str, errs: list[str]) -> None:
    if isinstance(typ, tuple) and typ[0] == "e":
        if value not in typ[1:] or not isinstance(value, str):
            errs.append(f"{path}: {value!r} is not one of {', '.join(typ[1:])}"
                        + ("; the adapter or agent must map provider values" if path.endswith("type") else ""))
    elif isinstance(typ, tuple):
        if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
            errs.append(f"{path}: must be a list of objects")
            return
        for i, item in enumerate(value):
            _fields(item, typ[1], f"{path}[{i}]", errs)
        ids = [x.get("id") for x in value if isinstance(x.get("id"), str)]
        if len(ids) != len(set(ids)):
            errs.append(f"{path}: duplicate id")
    elif typ == "s" and not (isinstance(value, str) and value.strip()):
        errs.append(f"{path}: must be a non-empty string")
    elif typ == "t" and not isinstance(value, str):
        errs.append(f"{path}: must be a string (it may be empty)")
    elif typ == "b" and not isinstance(value, bool):
        errs.append(f"{path}: must be true or false")
    elif typ == "i" and not (isinstance(value, int) and not isinstance(value, bool)):
        errs.append(f"{path}: must be an integer")
    elif typ == "ts" and not (isinstance(value, str) and TS.match(value)):
        errs.append(f"{path}: must be a UTC timestamp such as 2026-11-24T14:03:11Z")
    elif typ == "sha" and not (isinstance(value, str) and SHA.match(value)):
        errs.append(f"{path}: must be a full lowercase SHA (40 or 64 hex characters)")
    elif typ == "ss" and not (isinstance(value, list) and all(isinstance(x, str) for x in value)):
        errs.append(f"{path}: must be a list of strings")


def _fields(obj: dict, spec: dict, path: str, errs: list[str]) -> None:
    for key in obj:
        if key not in spec:
            errs.append(f"{path}.{key}: unknown key")
    for key, (typ, required) in spec.items():
        value = obj.get(key)
        if value is None:
            if required:
                errs.append(f"{path}.{key}: required, and null is not accepted [L-156]")
            continue
        _check(value, typ, f"{path}.{key}", errs)


def normalize_mime(name: str, mime: str | None) -> str:
    """Lower-case the mime without parameters; use the extension when it is missing or generic [L-29]."""
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in ("", "application/octet-stream", "binary/octet-stream"):
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return MEDIA_EXT.get(ext) or mimetypes.guess_type(name)[0] or mime
    return mime


def is_media(attachment: dict) -> bool:
    return attachment.get("mime", "").startswith(MEDIA_TYPES)


def validate(kind: str, data, repos: list[str] | None = None, raw: bytes | None = None) -> dict:
    """Return the normalized contract data, or raise FactoryError (exit 2) naming every bad field."""
    if kind in ("design_frame", "design_image"):
        from . import design
        return design.validate_frame(data) if kind == "design_frame" else design.validate_image(data, raw)
    if kind not in SPECS:
        raise FactoryError(f"contract kind {kind!r} is not supported in this build (supported: {', '.join(SCHEMAS)})")
    if not isinstance(data, dict):
        raise FactoryError(f"contract {kind}: data must be a JSON object")
    errs: list[str] = []
    _fields(data, SPECS[kind], "data", errs)
    if kind == "change" and repos is not None and isinstance(data.get("repo"), str) and data["repo"] not in repos:
        errs.append(f"data.repo: {data['repo']!r} is not a repo of factory.toml ({', '.join(repos)})")
    if errs:
        raise FactoryError(f"contract {kind} rejected (nothing written):\n  " + "\n  ".join(errs))
    out = {k: v for k, v in data.items() if v is not None}
    for att in out.get("attachments", []):
        att["mime"] = normalize_mime(att["name"], att.get("mime"))
    return out
