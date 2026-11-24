"""`factory next`: the one required action, derived from events, with the reasons a pending gate would fail."""
from __future__ import annotations

import os
from pathlib import Path

from . import acquire, gates, git, lock, media
from .project import Project
from .run import Run
from .util import fwd, safe_id

ENV_REPEAT = 2  # identical env failures before the run is reported blocked [L-168]


def _flag(project: Project) -> str:
    """` --project <dir>` when the cwd is outside the project, so every printed command works from anywhere [L-141]."""
    try:
        Path(os.getcwd()).resolve().relative_to(project.dir)
        return ""
    except ValueError:
        return f' --project "{fwd(project.dir)}"'


def phase(run: Run, project: Project, st: dict) -> str:
    if st["closed"]:
        return "closed"
    if not gates.intake_passed(run, project.cfg, st):
        return "intake"
    if not gates.passed(run, project, st, "plan"):
        return "plan"
    if gates.is_bug(run, st) and not gates.passed(run, project, st, "reproduce"):
        return "reproduce"
    return "build" if not gates.passed(run, project, st, "verify") else "review"


def _repeated_env_failure(st: dict) -> dict | None:
    last = st["env_failures"][-ENV_REPEAT:]
    if len(last) == ENV_REPEAT and len({(e.get("kind"), e.get("reason"), e.get("repo")) for e in last}) == 1:
        return last[-1]
    return None


def compute(project: Project, run: Run) -> dict:
    st = run.state()
    flag, tid = _flag(project), st["ticket"]
    ph = phase(run, project, st)
    out: dict = {"run": run.id, "ticket": tid, "kind": gates.effective_kind(run, st), "phase": ph,
                 "action": None, "kind_of_action": None, "command": None, "files": [], "reasons": [], "role": "orchestrator",
                 "decisions": [d["id"] for d in st["decisions"].values() if not d["consumed"]], "refused": []}
    reasons = gates.reasons_for(run, project, st, {"build": "verify"}.get(ph, ph)) if ph in ("intake", "plan", "reproduce", "build") else []

    def set_(action, kind, command=None, files=(), role="orchestrator"):
        out.update(action=action, kind_of_action=kind, command=command, files=[fwd(f) for f in files], role=role)

    if st["closed"]:
        set_(f"the run is closed: {st['closed'].get('reason', '')}".strip(), "none")
    elif any(b["open"] for b in st["blockers"].values()):
        b = next(b for b in st["blockers"].values() if b["open"])
        set_(f"blocked by {b['id']} ({b['kind']}): {b['reason']}. Tell the user, then resume once they answered", "user",
             f"factory unblock {b['id']}{' --decision <Q-n|D-n>' if b['kind'] in ('question', 'decision') else ''}{flag}")
    elif (fail := _repeated_env_failure(st)):
        out["refused"] = ["factory env up"]
        set_(f"blocked: the same environment failure happened {ENV_REPEAT} times ({fail.get('kind')}: {fail.get('reason', 'no reason recorded')}). "
             "Do not retry the same command; record it and tell the user", "user",
             f'factory block --kind infra --reason "<what failed>"{flag}')
    elif (qs := [q for q in st["questions"].values() if q["answered"] is None]):
        q = qs[0]
        set_(f"ask the user {q['id']} (one batched message for all open questions): {q['text']}", "user",
             f'factory answer {q["id"]} --user-said "<their words>"{flag}')
    elif ph == "intake":
        _intake_action(project, run, st, reasons, set_, flag)
    elif ph == "plan":
        _plan_action(project, run, st, reasons, set_, flag)
    elif ph == "reproduce":
        _reproduce_action(project, run, st, reasons, set_, flag)
    elif ph == "build":
        trees = [Path(w["path"]) for r in st["repos"] if (w := git.current(st, r))]
        if reasons:
            set_("build: implement the change in the worktree(s), commit it, then run verify; verify must pass at the HEAD of every "
                 "repo from a clean worktree, and the gate lists what is still missing", "command", f"factory verify{flag}", trees, "builder")
        else:
            set_("every check passes at HEAD; run the verify gate", "command", f"factory gate verify{flag}")
    else:
        set_("verify passed; the review phase is not available in this build", "none")
    out["gate"] = {"build": "verify"}.get(ph, ph)
    out["reasons"] = reasons
    if out["decisions"]:
        out["decisions_note"] = "recorded user decisions not consumed yet: " + ", ".join(out["decisions"])
    return out


def _intake_action(project: Project, run: Run, st: dict, reasons: list[str], set_, flag: str) -> None:
    tid, safe = st["ticket"], safe_id(st["ticket"])
    env, problems = acquire.load(run, "ticket", tid)
    raw, data = run.dir / "raw" / "ticket" / f"{safe}.json", run.dir / "input" / f"ticket-{safe}.json"
    if env is None or problems:
        if project.cfg["providers"]["ticket"].get("fetch"):
            set_("acquire the ticket contract with the configured adapter", "command", f"factory fetch ticket {tid}{flag}",
                 [], "any")
        else:
            set_("save the ticket payload to the raw path, write the contract data to the input path, then ingest it "
                 "(payloads never go through chat; --stdin can replace --contract)",
                 "command", f'factory ingest ticket --raw "{fwd(raw)}" --contract "{fwd(data)}"{flag}', [raw, data], "any")
        return
    pending = media.reasons(run, st)
    if pending:
        set_("record every ticket media attachment: view each file (save it under the media folder first) or ask the user about it",
             "command", f"factory media seen <attachment-id> --file media/<file>{flag}", [run.dir / "media"], "any")
    elif not st["repos"]:
        set_("declare the affected repos", "command", f"factory run repos <name>[,<name>]{flag}")
    else:
        set_("run the intake gate" if not reasons else "resolve the problems listed under reasons, then run the intake gate",
             "command", f"factory gate intake{flag}")


def _plan_action(project: Project, run: Run, st: dict, reasons: list[str], set_, flag: str) -> None:
    plan_path = run.dir / "plan.json"
    missing = [r for r in st["repos"] if not (w := git.current(st, r)) or not Path(w["path"]).is_dir() or w.get("setup") == "fail"]
    if missing:
        set_(f"create the run worktrees (and their setup) for: {', '.join(missing)}", "command", f"factory run branch{flag}")
    elif st["locks"] and lock.problems(run, project.cfg, st):
        set_("the plan or the config changed after the lock; amend it with a reason (without a recorded user decision it counts "
             "against the loop budget)", "command", f'factory plan amend --reason "<why>" [--decision Q-n]{flag}', [plan_path])
    elif not plan_path.is_file():
        set_("write plan.json: one AC per verifiable behaviour and a check that executes each (factory plan example prints a "
             "minimal plan); ask the user every open question in one round before the gate", "command", f"factory gate plan{flag}",
             [plan_path], "any")
    else:
        set_("run the plan gate (it locks the plan); fix the reasons first if any" if reasons else "run the plan gate; it locks the plan",
             "command", f"factory gate plan{flag}", [plan_path], "any")


def _reproduce_action(project: Project, run: Run, st: dict, reasons: list[str], set_, flag: str) -> None:
    diag, recs = gates.reproduce_reasons(run, project, st)
    if recs:
        set_("reproduce the bug: each reproducing check runs in a throwaway worktree at the branch point and must fail with its "
             "expected pattern", "command", f"factory reproduce{flag}", [], "builder")
    elif diag:
        set_("write diagnosis.md with ## Root cause, ## Evidence, ## Ruled out and ## Scenario, then run the reproduce gate",
             "command", f"factory gate reproduce{flag}", [run.dir / "diagnosis.md"], "any")
    else:
        set_("run the reproduce gate", "command", f"factory gate reproduce{flag}")
