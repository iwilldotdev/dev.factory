"""User input and blockers as typed events: ask/answer/decide/human/block/unblock [L-126, L-140, L-152, L-177]."""
from __future__ import annotations

from pathlib import Path

from . import evidence
from .run import Run, derive, next_id
from .util import FactoryError

WAITING = "waits for the user's confirmation"

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


def ask(run: Run, text: str, ref: str | None, actor: str | None, save_as: str | None = None) -> dict:
    if not text.strip():
        raise FactoryError("a question needs text")
    extra = {"save_as": save_as} if save_as else {}
    return run.append(lambda evs: {"type": "question", "id": next_id("Q", evs, "question"), "text": text.strip(),
                                   "ref": ref, **extra}, actor)


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


def _contract_ref(run: Run, target: str, what: str) -> tuple[str, str]:
    kind, _, ident = target.partition(":")
    if kind not in ("change", "pipeline") or not ident:
        raise FactoryError(f"{what} takes <kind>:<id> of a change or pipeline contract of this run, e.g. change:42")
    if (kind, ident) not in run.state()["contracts"]:
        raise FactoryError(f"run {run.id} has no {kind} contract {ident}: ingest or fetch it first")
    return kind, ident


def confirm(run: Run, target: str, said: str, heads: dict, actor: str | None) -> dict:
    """The user vouches for an agent-written contract that is used as proof; a later re-ingest asks again [L-21]."""
    said = _said(said)
    kind, ident = _contract_ref(run, target, "confirm")
    return run.append({"type": "confirm", "contract": f"{kind}:{ident}", "user_said": said, "heads": heads}, actor)


def confirmation(st: dict, check: dict, spec: str, sha: str, heads: dict) -> dict | None:
    """The user's confirmation of a human check, valid only while every planned repo still has the commits it was given at [L-24]."""
    rec = evidence.last(st, check["id"], "verify", spec, sha)
    return rec if rec and rec["result"] == "pass" and rec.get("human_heads") == heads else None


def confirm_check(run: Run, project, check_id: str, said: str, actor: str | None) -> dict:
    """`factory confirm CHK-n --user-said`: a `human` evidence record bound to the current HEADs. A new commit asks again [L-24]."""
    from . import git, lock, plan as planmod, review
    said = _said(said)
    st = run.state()
    problems = lock.problems(run, project.cfg, st, strict=False)
    if problems:
        raise FactoryError("cannot confirm: " + "; ".join(problems))
    plan, lk = planmod.load_plan(run)[0], lock.current(st)
    check = next((c for c in planmod.effective_checks(plan, project.cfg) if c["id"] == check_id), None)
    if check is None or check["type"] != "human":
        raise FactoryError(f"{check_id} is not a human check of this plan: confirm takes a human check id (CHK-3) or <kind>:<id> of a change or pipeline contract, e.g. change:42")
    w = git.current(st, check["repo"])
    if w is None or git.dirty(Path(w["path"])):
        raise FactoryError(f"repo {check['repo']}: commit the worktree first; a confirmation is bound to the commit the user saw [L-24]")
    heads = review.heads_of(st, plan["repos"])
    if any(c.get("check") == check_id and c["heads"] != heads and c["user_said"].strip().lower() == said.strip().lower() for c in st["confirms"]):
        raise FactoryError(f"{check_id}: these are the words the user gave at an earlier commit; show them the new commit, ask again and record what they say now [L-24]")
    run.append({"type": "confirm", "check": check_id, "user_said": said, "heads": heads}, actor)
    out = {"cmd": f"human: {check['text']}", "exit": None, "duration_s": 0.0, "output": said, "result": "pass", "kind": None, "reason": ""}
    return evidence.append(run, check, lk["spec_hashes"][check_id], "verify", heads[check["repo"]], Path(w["path"]), True, out,
                           project.cfg["checks"]["tail_lines"], {"human_heads": heads, "user_said": said}, actor)


def reject_check(run: Run, project, check_id: str, said: str, actor: str | None) -> dict:
    """The user rejected a human check at the ship briefing: a finding of the delivery that spends one loop and sends the run back
    to build; a new commit moves the reviews and asks for the confirmation again [L-24, L-135]."""
    from . import lock, plan as planmod, review
    said = _said(said)
    st = run.state()
    plan = planmod.load_plan(run)[0]
    check = next((c for c in planmod.effective_checks(plan, project.cfg) if c["id"] == check_id), None)
    if check is None or check["type"] != "human":
        raise FactoryError(f"{check_id} is not a human check of this plan")
    heads = review.heads_of(st, plan["repos"])
    lock.spend(run, project.cfg, st, "human", None, actor, {"heads": heads, "check": check_id})
    return run.append({"type": "human", "kind": "rejection", "check": check_id, "text": f"{check_id} ({check['text']}): {said}",
                       "user_said": said, "heads": heads}, actor)


def pending(run: Run, project, st: dict) -> list[dict]:
    """Human checks without the user's confirmation at the current commits: asked once, at the ship briefing [L-24]."""
    from . import lock, plan as planmod, review
    lk = lock.current(st)
    if lk is None:
        return []
    plan = planmod.load_plan(run)[0]
    heads = review.heads_of(st, plan["repos"])
    return [c for c in planmod.effective_checks(plan, project.cfg) if c["type"] == "human" and c["repo"] in heads
            and not confirmation(st, c, lk["spec_hashes"][c["id"]], heads[c["repo"]], heads)]


def rejections(st: dict, heads: dict) -> list[dict]:
    """Rejections of human checks given at the current commits: the builder still has to fix them."""
    return [h for h in st["human"] if h.get("kind") == "rejection" and h.get("heads") == heads]


def waive(run: Run, target: str, said: str, actor: str | None) -> dict:
    """A user waiver names one pipeline id; a newer pipeline is not covered [L-20]."""
    said = _said(said)
    kind, ident = _contract_ref(run, target, "waive")
    if kind != "pipeline":
        raise FactoryError("only a pipeline can be waived: waive pipeline:<id>")
    return run.append({"type": "waiver", "pipeline": ident, "user_said": said}, actor)


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
