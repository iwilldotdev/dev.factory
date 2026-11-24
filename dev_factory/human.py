"""User input and blockers as typed events: ask/answer/decide/human/block/unblock [L-126, L-140, L-152, L-177]."""
from __future__ import annotations

from .run import Run, derive, next_id
from .util import FactoryError

BLOCKER_KINDS = {
    "question": "the run needs an answer from the user before it can continue",
    "decision": "the run needs a decision from the user (scope, trade-off, waiver)",
    "infra": "the environment or a tool outside the code failed repeatedly",
    "tooling": "the factory or a configured command cannot do what the run needs",
}
HUMAN_KINDS = {
    "answer": "the user answered a question outside the ask/answer flow",
    "decision": "the user decided something that steers the run",
    "env-hint": "the user gave a hint about the environment (port, data, login)",
    "correction": "the user corrected the agent",
}


def _said(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        raise FactoryError("--user-said needs the user's own words; an empty value is refused [L-126]")
    return text


def ask(run: Run, text: str, ref: str | None, actor: str | None) -> dict:
    if not text.strip():
        raise FactoryError("a question needs text")
    return run.append(lambda evs: {"type": "question", "id": next_id("Q", evs, "question"), "text": text.strip(),
                                   "ref": ref}, actor)


def answer(run: Run, qid: str, said: str, actor: str | None) -> dict:
    said = _said(said)

    def build(evs):
        q = derive(evs)["questions"].get(qid)
        if q is None:
            raise FactoryError(f"no question {qid} in run {run.id}")
        if q["answered"] is not None:
            raise FactoryError(f"{qid} is already answered")
        return {"type": "answer", "question": qid, "user_said": said, "decision": qid}
    return run.append(build, actor)


def decide(run: Run, said: str, actor: str | None) -> dict:
    said = _said(said)
    return run.append(lambda evs: {"type": "decision", "id": next_id("D", evs, "decision"), "user_said": said}, actor)


def human(run: Run, kind: str, text: str, actor: str | None) -> dict:
    if kind not in HUMAN_KINDS:
        raise FactoryError(_kinds_msg("human kind", kind, HUMAN_KINDS))
    if not text.strip():
        raise FactoryError("a human event needs text")
    return run.append({"type": "human", "kind": kind, "text": text.strip()}, actor)


def _kinds_msg(what: str, got: str, kinds: dict) -> str:
    return f"invalid {what} {got!r}; valid kinds:\n" + "\n".join(f"  {k}: {v}" for k, v in kinds.items())


def block(run: Run, kind: str, reason: str, actor: str | None) -> dict:
    """One blocker is exactly one event [L-152]."""
    if kind not in BLOCKER_KINDS:
        raise FactoryError(_kinds_msg("blocker kind", kind, BLOCKER_KINDS))
    if not reason.strip():
        raise FactoryError("a blocker needs a reason")
    return run.append(lambda evs: {"type": "blocker", "id": next_id("B", evs, "blocker"), "kind": kind,
                                   "reason": reason.strip()}, actor)


def unblock(run: Run, bid: str, decision: str | None, actor: str | None) -> dict:
    """Close a blocker. User-input blockers need a recorded decision, consumed once [L-126]."""
    def build(evs):
        st = derive(evs)
        b = st["blockers"].get(bid)
        if b is None or not b["open"]:
            raise FactoryError(f"{bid} is not an open blocker of run {run.id}")
        if decision is None and b["kind"] in ("question", "decision"):
            raise FactoryError(f"{bid} is a {b['kind']} blocker: pass --decision <Q-n|D-n> with the user's recorded answer")
        if decision is not None:
            d = st["decisions"].get(decision)
            if d is None:
                raise FactoryError(f"no recorded decision {decision} (answer a question or run factory decide first)")
            if d["consumed"]:
                raise FactoryError(f"decision {decision} was already consumed; ask the user again [L-126]")
        return {"type": "unblock", "blocker": bid, "decision": decision}
    return run.append(build, actor)
