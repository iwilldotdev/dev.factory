"""Gates read evidence and never write it; a pass or fail is recorded as one `gate` event [L-01, L-16]."""
from __future__ import annotations

import re
from pathlib import Path

from . import acquire, designflow, dryrun, env as envmod, evidence, feedback, git, human, localhttp, lock, media, plan as planmod, plancheck, review, ship
from .checks import RUNTIME
from .run import Run
from .util import FactoryError, fwd, sha256_file

# Run kind -> the ticket `type` it requires; epic and review runs do not derive from a single ticket type.
KIND_TYPE = {"feature": "feature", "bug": "bug", "hotfix": "bug"}
NAMES = ("intake", "plan", "reproduce", "verify", "review", "ship", "feedback", "report")
SECTIONS = ("Root cause", "Evidence", "Ruled out")


def effective_kind(run: Run, st: dict) -> str | None:
    """The explicit run kind, else the ticket contract `type` [L-31]."""
    if st["kind"]:
        return st["kind"]
    env, problems = acquire.load(run, "ticket", st["ticket"]) if st["ticket"] else (None, [])
    return env["data"]["type"] if env and not problems else None


def is_bug(run: Run, st: dict) -> bool:
    return effective_kind(run, st) in ("bug", "hotfix")


def intake_reasons(run: Run, cfg: dict, st: dict) -> list[str]:
    """Every reason `gate intake` would fail now (empty when it would pass)."""
    if st["kind"] == "review":
        from . import review_run
        return review_run.intake_reasons(run, cfg, st)
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


def intake_passed(run: Run, cfg: dict, st: dict) -> bool:
    """A recorded pass that no later retype or contract change invalidated, and that still holds now."""
    g = st["gates"].get("intake")
    return bool(g and g["result"] == "pass" and g["at"] > max(st["retype_at"], st["contract_at"])
                and not intake_reasons(run, cfg, st))


def no_reproduction(plan: dict, st: dict) -> list[str]:
    """A bug plan whose ACs are all `reproduce: false` reproduces nothing: it needs the user's recorded decision (D-n or Q-n named in a reason) [L-33]."""
    acs = plan.get("acs") or []
    if plan.get("kind") != "bug" or not acs or any(a.get("reproduce", True) is True for a in acs):
        return []
    said = " ".join(str(a.get("reason") or "") for a in acs)
    if any(d in said for d in st["decisions"]):
        return []
    return ["bug plan: every AC is reproduce false, so nothing is reproduced; reproduce at least one AC, or record the user's decision "
            "(factory decide \"<words>\") and name its id (D-n) in the AC reason [L-33]"]


def plan_reasons(run: Run, project, st: dict, with_lock: bool = True) -> list[str]:
    """Why `gate plan` would fail: the plan, the worktrees with their setup, open questions, raws, and any drift since the lock."""
    cfg = project.cfg
    plan, out = planmod.load_plan(run)
    if plan is None:
        return out
    out = planmod.validate(plan, cfg, run.dir, effective_kind(run, st))
    out += no_reproduction(plan, st) if not out else []
    if not out:
        out += plancheck.note_problems(plan, run.dir) + plancheck.env_problems(plan, cfg) + designflow.plan_problems(run, project, st, plan) + _env_health(project, plan)
    out += [f"question {q['id']} is still open: the user answers before the plan locks" for q in st["questions"].values() if q["answered"] is None]
    for r in plan["repos"] if isinstance(plan.get("repos"), list) else []:
        w = git.current(st, r)
        if w is None or not Path(w["path"]).is_dir():
            out.append(f"repo {r}: no run worktree; run factory run branch")
        elif w.get("setup") == "fail":
            out.append(f"repo {r}: the setup command failed in the worktree (exit {w.get('setup_exit')}); fix it and run factory run branch again [L-06]\n{w.get('setup_tail', '')}".rstrip())
    out += acquire.stale(run)
    if with_lock and st["locks"]:
        out += lock.problems(run, cfg, st)
    return out


def _env_health(project, plan: dict) -> list[str]:
    """Every repo with runtime checks has an env that answers: all problems in one message, never a surprise at verify [L-86]."""
    out = []
    for repo in sorted({c["repo"] for c in plan["checks"] if c.get("type") in RUNTIME}):
        env = next((r.get("env") for r in project.cfg["repo"] if r["name"] == repo), None)
        if env and not localhttp.healthy(envmod.health_url(env), 3):
            out.append(f"{repo}: the env does not answer at {envmod.health_url(env)}; start it with the env command of this CLI before the plan gate [L-86]")
    return out


def diagnosis_reasons(run: Run) -> list[str]:
    path = run.dir / "diagnosis.md"
    if not path.is_file():
        return [f"diagnosis.md is missing: write it at {fwd(path)} with ## Root cause, ## Evidence, ## Ruled out and ## Scenario"]
    secs: dict[str, str] = {}
    title = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            title = line[3:].strip().lower()
            secs[title] = ""
        elif title is not None:
            secs[title] += line + "\n"
    out = [f"diagnosis.md: ## {s} is missing or empty [L-38]" for s in SECTIONS if not secs.get(s.lower(), "").strip()]
    scen = secs.get("scenario", "")
    bullets = [ln for ln in scen.splitlines() if re.match(r"^\s*[-*]\s+\S", ln)]
    if "ticket scenario used" not in scen.lower() and not (bullets and all(re.search(r":|because", b) for b in bullets)):
        out.append("diagnosis.md: ## Scenario must say 'ticket scenario used' or list each deviation with its reason ('- what: why') [L-27]")
    return out


def reproduce_reasons(run: Run, project, st: dict, strict: bool = True) -> tuple[list[str], list[str]]:
    """(diagnosis problems, reproduction record problems); the lock must hold first."""
    if not is_bug(run, st):
        return [f"gate reproduce is for bug runs; this run is {effective_kind(run, st)}"], []
    drift = lock.problems(run, project.cfg, st, strict)
    if drift:
        return drift, []
    lk, plan = lock.current(st), planmod.load_plan(run)[0]
    recs = no_reproduction(plan, st)
    for c in planmod.effective_checks(plan, project.cfg):
        bp = lk["branch_points"][c["repo"]]
        heads = {**envmod.heads_of(project, st, c["repo"]), c["repo"]: bp} if c["type"] in RUNTIME and c.get("reproduce") else None
        if c.get("reproduce") and not evidence.satisfied(st, c["id"], "reproduce", lk["spec_hashes"][c["id"]], bp, None, heads):
            recs.append(f"{c['id']}: no reproduced record at the base {lk['branch_points'][c['repo']][:7]}: run factory reproduce [L-03, L-05, L-33]")
    return diagnosis_reasons(run), recs


def verify_reasons(run: Run, project, st: dict, recorded: bool = False) -> list[str]:
    """Every effective check passes at its repo's current HEAD from a clean worktree under the locked spec [L-01, L-07, L-08, L-09].
    With `recorded` (review runs) a check needs a record at HEAD whatever its result: a change that fails its checks is still reviewed."""
    out = lock.problems(run, project.cfg, st)
    if out:
        return out
    if is_bug(run, st):
        out += sum(reproduce_reasons(run, project, st), [])
    lk, plan = lock.current(st), planmod.load_plan(run)[0]
    checks, heads = planmod.effective_checks(plan, project.cfg), {}
    for r in plan["repos"]:
        w = git.current(st, r)
        if w is None or not Path(w["path"]).is_dir():
            out.append(f"repo {r}: the run worktree is missing")
            continue
        path = Path(w["path"])
        heads[r] = (path, git.head(path), w["branch_point"])
        if (d := git.dirty(path)):
            out.append(f"repo {r}: the worktree has {len(d)} uncommitted or untracked path(s); commit them, evidence from a dirty worktree never counts [L-07]")
        if git.ahead(path, w["branch_point"]) == 0:
            out.append(f"repo {r}: no commit past the branch point {w['branch_point'][:7]} [L-87]")
    passing: set[str] = set()
    by_human: set[str] = set()  # ACs backed by a confirmed human check
    asked: set[str] = set()  # ACs waiting for the user: the reason names the check, not the AC
    now = {r: v[1] for r, v in heads.items()}
    for c in checks:
        if c["repo"] not in heads:
            continue
        path, sha, bp = heads[c["repo"]]
        if c["type"] == "human":
            if human.confirmation(st, c, lk["spec_hashes"][c["id"]], sha, now):
                by_human |= {*c["ac"]}
            else:
                asked |= {*c["ac"]}
                out.append(f"{c['id']}: {human.WAITING}: factory confirm {c['id']} --user-said \"<their words>\" ({c['text']}) [L-24]")
            continue
        rheads = envmod.heads_of(project, st, c["repo"]) if c["type"] in RUNTIME else None
        rec = evidence.last(st, c["id"], "verify", lk["spec_hashes"][c["id"]], sha, None, rheads)
        if rec is None and rheads is not None and evidence.last(st, c["id"], "verify", lk["spec_hashes"][c["id"]], sha):
            out.append(f"{c['id']}: the record was made when a served repo had another HEAD ({', '.join(f'{r}@{h[:7]}' for r, h in rheads.items())} now): run factory verify [L-12]")
        elif rec is None:
            out.append(f"{c['id']}: no verify record at {sha[:7]} for the locked spec: run factory verify"
                       + (" (without --scoped: only the full run counts before the review gate)" if c["type"] == "quality" else "") + " [L-08, L-09, L-60]")
        elif rheads is not None and any(v["sha"] != rec["heads"].get(r) for r, v in rec.get("served", {}).items()):
            out.append(f"{c['id']}: the served process was started at another commit than the one it was checked at [L-13, L-85]")
        elif rec["result"] != "pass":
            if not recorded:
                out.append(f"{c['id']}: the latest verify result is {rec['result']} ({rec['reason'] or rec['kind']})")
        else:
            passing |= {*c["ac"]}
        if c.get("red_green") and rec is not None:
            out += _red_green_reasons(st, c, lk, plan, path, bp)
    for a in [] if recorded else plan["acs"]:
        if a["id"] in passing:
            continue
        if a["id"] in by_human and (plan["kind"] == "bug" or a.get("design")):
            out.append(f"{a['id']}: only a human check backs this {'bug' if plan['kind'] == 'bug' else 'design'} AC; add a check a command or the app can fail [L-24]")
        elif a["id"] not in by_human and a["id"] not in asked:
            out.append(f"{a['id']}: no passing check backs this AC [L-01]")
    return out


def _red_green_reasons(st: dict, c: dict, lk: dict, plan: dict, path: Path, bp: str) -> list[str]:
    fix = git.rev(path, plan.get("fix_commit", "HEAD"))
    if fix is None or fix == bp or not git.is_ancestor(path, bp, fix):
        return [f"{c['id']}: fix_commit {plan.get('fix_commit', 'HEAD')!r} is not a commit past the branch point"]
    h = lk["spec_hashes"][c["id"]]
    out = []
    if not evidence.satisfied(st, c["id"], "red", h, None, fix):
        out.append(f"{c['id']}: red-green not proven at fix {fix[:7]}: the test must fail before the fix and pass after; run factory verify [L-34]")
    elif not evidence.satisfied(st, c["id"], "green", h, None, fix):
        out.append(f"{c['id']}: the green side at fix {fix[:7]} did not pass [L-34]")
    return out


def extra_reasons(run: Run, project, st: dict, name: str) -> list[str]:
    """What a later gate adds on top of verify; callers know that verify holds."""
    plan, lk = planmod.load_plan(run)[0], lock.current(st)
    out = review.reasons(st, plan, lk) + designflow.review_reasons(run, st, plan)
    if name in ("ship", "feedback") and not out:
        from . import mirror
        out = ship.reasons(run, project, st) + designflow.ship_reasons(run, st, plan) + mirror.reasons(run, project, st)
    return out + feedback.reasons(run, project, st) if name == "feedback" and not out else out


def reasons_for(run: Run, project, st: dict, name: str, strict: bool = True) -> list[str]:
    if name == "intake":
        return intake_reasons(run, project.cfg, st)
    if name == "plan":
        return plan_reasons(run, project, st) if intake_passed(run, project.cfg, st) else ["gate intake has not passed"]
    if name == "reproduce":
        return sum(reproduce_reasons(run, project, st, strict), [])
    if name == "report":
        from . import review_run
        return review_run.report_reasons(run, project, st)
    out = verify_reasons(run, project, st)
    return out if name == "verify" or out else extra_reasons(run, project, st, name)


def passed(run: Run, project, st: dict, name: str, chain: bool = True) -> bool:
    """A recorded pass that still holds now: later commits, edits, reviews or drift invalidate it.
    With `chain` false the caller already knows that verify holds, so only what the gate adds is re-read."""
    g = st["gates"].get(name)
    if not (g and g["result"] == "pass"):
        return False
    if name == "intake":
        return intake_passed(run, project.cfg, st)
    if name in ("review", "ship", "feedback") and g["at"] < max(st["review_at"], st["ship_at"] if name != "review" else -1):
        return False
    return not (reasons_for(run, project, st, name) if chain else extra_reasons(run, project, st, name))


def run_gate(run: Run, project, name: str, actor: str | None = None) -> tuple[bool, list[str]]:
    if name not in NAMES:
        raise FactoryError(f"gate {name!r} is not available in this build (available: {', '.join(NAMES)})")
    st = run.state()
    reasons = reasons_for(run, project, st, name, strict=False)
    extra: dict = {}
    if name == "plan" and not reasons and not st["locks"]:
        plan = planmod.load_plan(run)[0]
        reasons = dryrun.reasons(run, project, st, plan, None)
        if not reasons:
            lock.lock_event(run, lock.compute(run, project.cfg, st, plan), {}, actor)
    if not reasons and name == "reproduce":
        extra["diagnosis"] = sha256_file(run.dir / "diagnosis.md")
    run.append({"type": "gate", "name": name, "result": "fail" if reasons else "pass", "reasons": reasons, **extra}, actor)
    return not reasons, reasons
