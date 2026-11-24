"""Test helper: a ticket raw payload plus its valid, fresh `ticket.v1` contract in a run dir (no profile needed)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factory import contracts, core  # noqa: E402


def put_ticket(run_id: str, key: str, *, title: str = "Thing", type: str = "Story", is_bug: bool = False,
               status: str = "Open", attachments: list | None = None, unknowns: list | None = None,
               raw_ext: str = "json", **extra) -> Path:
    """Write `raw/ticket/<KEY>.<ext>` and `contracts/ticket/<KEY>.json` (provenance bound to the raw sha). Returns the
    contract path. `attachments`: `[{id, name, mime, url}]`; `extra` overrides or adds contract fields."""
    rd = core.run_dir(run_id)
    safe = contracts.safe_id(key)
    raw = contracts.raw_path_for(rd, "ticket", safe, raw_ext)
    raw.parent.mkdir(parents=True, exist_ok=True)
    body = ('{"id": "%s", "title": "%s"}\n' % (key, title)).encode("utf-8")
    raw.write_bytes(body)
    doc = {"schema": "ticket.v1", "key": key, "title": title, "type": type, "is_bug": is_bug, "status": status,
           "description_md": "", "unknowns": list(unknowns or [])}
    if attachments is not None:
        doc["attachments"] = attachments
    doc.update(extra)
    doc["provenance"] = {"profile": "native", "profile_digest": "native", "id": key,
                         "raw_path": raw.relative_to(rd).as_posix(), "raw_sha256": hashlib.sha256(body).hexdigest(),
                         "ingested_at": "2026-01-01T00:00:00Z"}
    path = contracts.contract_path(rd, "ticket", safe)
    core.write_json(path, contracts.order_contract("ticket", doc))
    assert not contracts.validate("ticket", doc), contracts.validate("ticket", doc)
    return path
