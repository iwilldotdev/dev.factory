"""Gates read evidence and never write it; a pass or fail is recorded as one `gate` event."""
from __future__ import annotations

from . import acquire, media
from .run import Run
from .util import FactoryError

# Run kind -> the ticket `type` it requires; epic and review runs do not derive from a single ticket type.
KIND_TYPE = {"feature": "feature", "bug": "bug", "hotfix": "bug"}
NAMES = ("intake",)


def effective_kind(run: Run, st: dict) -> str | None:
    """The explicit run kind, else the ticket contract `type` [L-31]."""
    if st["kind"]:
        return st["kind"]
    env, problems = acquire.load(run, "ticket", st["ticket"]) if st["ticket"] else (None, [])
    return env["data"]["type"] if env and not problems else None


def intake_reasons(run: Run, cfg: dict, st: dict) -> list[str]:
    """Every reason `gate intake` would fail now (empty when it would pass)."""
    out: list[str] = []
    env, problems = acquire.load(run, "ticket", st["ticket"])
    out += problems
    if env is not None and not problems:
        kind = st["kind"]
        want = KIND_TYPE.get(kind)
        if want and env["data"]["type"] != want:
            out.append(f"run kind is {kind} but the ticket contract type is {env['data']['type']}: "
                       f"change the kind with factory run retype --kind {want} (it re-runs this gate) [L-31]")
        if env["data"]["id"] != st["ticket"]:
            out.append(f"ticket contract id {env['data']['id']!r} differs from the run ticket {st['ticket']!r}")
        out += media.reasons(run, st)
    if not st["repos"]:
        out.append("no affected repo is declared: start the run with --repos <name>[,<name>]")
    else:
        known = {r["name"] for r in cfg["repo"]}
        out += [f"affected repo {r!r} is not in factory.toml" for r in st["repos"] if r not in known]
    return out


def run_gate(run: Run, cfg: dict, name: str, actor: str | None = None) -> tuple[bool, list[str]]:
    if name not in NAMES:
        raise FactoryError(f"gate {name!r} is not available in this build (available: {', '.join(NAMES)})")
    st = run.state()
    reasons = intake_reasons(run, cfg, st)
    run.append({"type": "gate", "name": name, "result": "fail" if reasons else "pass", "reasons": reasons}, actor)
    return not reasons, reasons


def intake_passed(run: Run, cfg: dict, st: dict) -> bool:
    """A recorded pass that no later retype or contract change invalidated, and that still holds now."""
    g = st["gates"].get("intake")
    return bool(g and g["result"] == "pass" and g["at"] > max(st["retype_at"], st["contract_at"])
                and not intake_reasons(run, cfg, st))
