"""The plan lock and `plan amend`: what `gate plan` froze, what drifted since, and the loop budget [L-131 to L-137]."""
from __future__ import annotations

from . import git, human, plan as planmod
from .contracts import PROOF_GRADE
from .plan import chash
from .run import Run
from .util import FactoryError, sha256_file

BUDGET = "loop budget exhausted"


def current(st: dict) -> dict | None:
    return st["locks"][-1] if st["locks"] else None


def compute(run: Run, cfg: dict, st: dict, plan: dict) -> dict:
    """The lock fields of the plan as it is now [L-132]."""
    checks = planmod.effective_checks(plan, cfg)
    files = sorted({f for c in checks for f in c.get("files", [])})
    bps = {r: w["branch_point"] for r in plan["repos"] if (w := git.current(st, r))}
    return {"plan_hash": chash(plan), "core_hash": chash({k: v for k, v in plan.items() if k != "checks"}),
            "acs": {a["id"]: chash(a) for a in plan["acs"]}, "spec_hashes": {c["id"]: planmod.spec_hash(c, run.dir) for c in checks},
            "files": {f: sha256_file(run.dir / f) for f in files if (run.dir / f).is_file()}, "config_hash": planmod.config_hash(cfg),
            "contracts": {e["path"]: e["raw_sha256"] for e in st["contracts"].values() if e["kind"] not in PROOF_GRADE}, "branch_points": bps}


def changed(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """(changed AC ids, changed check ids) between two locks [L-134]."""
    acs = sorted(i for i in {*old["acs"], *new["acs"]} if old["acs"].get(i) != new["acs"].get(i))
    chk = sorted(i for i in {*old["spec_hashes"], *new["spec_hashes"]} if old["spec_hashes"].get(i) != new["spec_hashes"].get(i))
    return acs, chk


def problems(run: Run, cfg: dict, st: dict, strict: bool = True) -> list[str]:
    """Why every later gate fails after the plan, config, a declared file or a contract moved since the lock [L-131, L-132]."""
    lock = current(st)
    if lock is None:
        return ["the plan is not locked: run factory gate plan"]
    plan, errs = planmod.load_plan(run)
    if plan is None:
        return errs
    amend = "run factory plan amend --reason \"<why>\" [--decision Q-n]"
    out: list[str] = []
    try:
        now = compute(run, cfg, st, plan)
    except (KeyError, TypeError, AttributeError):
        return [f"plan changed after lock and is no longer valid: fix plan.json, then {amend}"]
    if now["plan_hash"] != lock["plan_hash"]:
        acs, chk = changed(lock, now)
        out.append(f"plan changed after lock ({', '.join(acs + chk) or 'plan fields'}): {amend}")
    out += [f"{p} changed after lock: {amend}" for p, h in lock["files"].items() if now["files"].get(p) != h]
    if now["config_hash"] != lock["config_hash"]:
        out.append(f"config changed after plan (a command, cap, limit or setup in factory.toml): {amend}")
    if now["contracts"] != lock["contracts"]:
        out.append(f"a contract was acquired or replaced after the plan lock: {amend}")
    diag = run.dir / "diagnosis.md"
    if strict and st["diagnosis"] and (not diag.is_file() or sha256_file(diag) != st["diagnosis"]):
        out.append("diagnosis.md changed after gate reproduce passed: run gate reproduce again")
    return out


def loops_used(st: dict) -> int:
    return sum(1 for e in st["loops"] if e.get("counts"))


def lock_event(run: Run, new: dict, extra: dict, actor: str | None) -> dict:
    return run.append(lambda evs: {"type": "plan_locked", **new, "seq": 1 + sum(1 for e in evs if e.get("type") == "plan_locked"),
                                   **extra}, actor)


def check_decision(st: dict, decision: str | None) -> None:
    if decision is not None:
        d = st["decisions"].get(decision)
        if d is None or d["consumed"]:
            raise FactoryError(f"decision {decision} is not recorded or was already consumed: ask the user and record it first [L-126]")


def spend(run: Run, cfg: dict, st: dict, kind: str, decision: str | None, actor: str | None, extra: dict | None = None) -> None:
    """Spend one loop of the single budget (`amend` or `review`). Without a recorded user decision a spent budget refuses and
    blocks the run; a decision is consumed once and closes the open budget blocker [L-135, L-136]."""
    check_decision(st, decision)
    cap = cfg["autonomy"]["max_loops"]
    if decision is None and loops_used(st) >= cap:
        msg = f"{BUDGET}: {loops_used(st)} of {cap} loops used (autonomy.max_loops); the user has to decide before another {kind}"
        if not any(b["open"] and b["reason"].startswith(BUDGET) for b in st["blockers"].values()):
            human.block(run, "decision", msg, actor)
        raise FactoryError(msg, 1)
    run.append({"type": "loop", "kind": kind, "decision": decision, "counts": decision is None, **(extra or {})}, actor)
    if decision is not None:
        for b in st["blockers"].values():
            if b["open"] and b["reason"].startswith(BUDGET):
                run.append({"type": "unblock", "blocker": b["id"], "decision": decision}, actor)


def amend(run: Run, cfg: dict, reasons: list[str], reason: str, decision: str | None, actor: str | None) -> dict:
    """Re-lock a changed plan. An amend without a recorded user decision counts against `autonomy.max_loops`;
    past the budget it refuses and blocks the run [L-133, L-134, L-136]."""
    st = run.state()
    old = current(st)
    if old is None:
        raise FactoryError("there is no plan lock yet: run factory gate plan first (amend is only for a locked plan)")
    if not reason.strip():
        raise FactoryError("plan amend needs --reason")
    if reasons:
        raise FactoryError("the amended plan is not valid yet:\n" + "\n".join(f"  - {r}" for r in reasons))
    check_decision(st, decision)
    plan, _ = planmod.load_plan(run)
    new = compute(run, cfg, st, plan)
    if all(new[k] == old.get(k) for k in new if k != "branch_points"):
        raise FactoryError("nothing changed since the lock, so there is nothing to amend")
    acs, chk = changed(old, new)
    from .gates import reproduction_decision
    repro = reproduction_decision(plan, st) if plan.get("acs") and not any(a.get("reproduce", True) for a in plan["acs"]) else None
    spend(run, cfg, st, "amend", decision, actor)
    # the lock consumes the decision it rests on, so `gate reproduce` accepts it and nothing else can reuse it [L-126]
    return lock_event(run, new, {"amend": {"reason": reason.strip(), "decision": decision, "changed_acs": acs, "changed_checks": chk,
                                           "counts_loop": decision is None}, **({"decision": d} if (d := decision or repro) else {})}, actor)
