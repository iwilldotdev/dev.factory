"""Ticket media manifest: every media attachment is `seen` (one file) or `unseen` (with a question) [L-28, L-29]."""
from __future__ import annotations

from pathlib import Path

from . import acquire, contracts
from .run import Run, derive
from .util import FactoryError, fwd


def attachments(run: Run, st: dict) -> list[dict]:
    """Media attachments of the run's ticket contract (empty when there is no valid contract)."""
    if not st["ticket"]:
        return []
    env, problems = acquire.load(run, "ticket", st["ticket"])
    if env is None or problems:
        return []
    return [a for a in env["data"]["attachments"] if contracts.is_media(a)]


def _known(run: Run, att: str) -> None:
    st = run.state()
    if att not in {a["id"] for a in attachments(run, st)}:
        raise FactoryError(f"{att!r} is not a media attachment of the ticket contract of run {run.id}")


def seen(run: Run, att: str, file: str, actor: str | None) -> dict:
    _known(run, att)
    media_dir = (run.dir / "media").resolve()
    path = Path(file) if Path(file).is_absolute() else run.dir / file
    path = path.resolve()
    if media_dir not in path.parents or not path.is_file():
        raise FactoryError(f"{fwd(path)} must be an existing file under {fwd(media_dir)}")

    def build(evs):
        st = derive(evs)
        rel = path.relative_to(run.dir.resolve()).as_posix()
        for other, ev in st["media"].items():
            if other != att and ev.get("state") == "seen" and ev.get("file") == rel:
                raise FactoryError(f"{rel} already records attachment {other}: one file per entry [L-29]")
        return {"type": "media", "attachment": att, "state": "seen", "file": rel}
    return run.append(build, actor)


def unseen(run: Run, att: str, qid: str, actor: str | None) -> dict:
    _known(run, att)
    q = run.state()["questions"].get(qid)
    if q is None or q["ref"] != f"attachment:{att}":
        raise FactoryError(f"{qid} is not a question about this attachment; ask it first: "
                           f"factory ask \"<question>\" --ref attachment:{att}")
    return run.append({"type": "media", "attachment": att, "state": "unseen", "question": qid}, actor)


def reasons(run: Run, st: dict) -> list[str]:
    """Gate-intake reasons for media: unrecorded, missing file, or an unseen entry without its question [L-28, L-29]."""
    out = []
    for att in attachments(run, st):
        ev = st["media"].get(att["id"])
        if ev is None:
            out.append(f"media {att['id']} ({att['name']}) is not recorded: factory media seen {att['id']} --file media/<file>, "
                       f"or ask and run factory media unseen {att['id']} --question Q-n")
        elif ev["state"] == "seen":
            if not (run.dir / ev["file"]).is_file():
                out.append(f"media {att['id']}: recorded file {ev['file']} is missing")
        else:
            q = st["questions"].get(ev.get("question"))
            if q is None or q["ref"] != f"attachment:{att['id']}":
                out.append(f"media {att['id']}: unseen needs a question with ref attachment:{att['id']}")
    return out
