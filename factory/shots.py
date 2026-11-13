"""Visual evidence on request (DESIGN §12): the user's yes/no, UI detection, and the gate rules for `screenshot` checks.

A run that touches UI must record the user's answer to "do you want screenshots?" before `gate plan`
(`factory run decide <run> "<answer>" --by user --user-said "<gist>" --visual yes|no`). With `yes`:
every touched UI repo needs a `screenshot` check; bug runs also need `BEFORE-<check>` evidence captured on
the merge-base (`prove --repro --assume-env`); the after image is the check's normal evidence at HEAD.
`ship plan` lists before/after images in ship-plan.json and in each MR description.
"""

from __future__ import annotations

from pathlib import Path

from .core import FactoryError, evidence_id, load_evidence, repo_conf, run_dir, run_targets

SHOT_TYPE = "screenshot"
UI_CHECK_TYPES = {"browser", "visual", "style", SHOT_TYPE}
VISUAL_VALUES = ("yes", "no")


def before_id(check_id: str) -> str:
    return f"BEFORE-{check_id}"


def decision(run: dict) -> str | None:
    """The latest user decision carrying `visual` (yes|no), else None."""
    for rec in reversed(run.get("decisions") or []):
        if rec.get("by") == "user" and rec.get("visual") in VISUAL_VALUES:
            return rec["visual"]
    return None


def is_ui_repo(ws: dict, alias: str) -> bool:
    """`ui: true|false` on the workspace repo wins; unset: a repo whose env has a browser `login` block is UI."""
    try:
        conf = repo_conf(ws, alias)
    except FactoryError:
        return False
    if isinstance(conf.get("ui"), bool):
        return conf["ui"]
    return bool((conf.get("env") or {}).get("login"))


def ui_repos(run: dict, ws: dict) -> list[str]:
    return [a for a in run.get("repos") or {} if is_ui_repo(ws, a)]


def acs_of(run_id: str) -> dict:
    from .commands import gate  # lazy: gate imports this module
    try:
        return gate.load_acs(run_id)
    except FactoryError:
        return {}


def shot_checks(acs: dict) -> list[dict]:
    return [c for c in acs.get("checks") or [] if c.get("type") == SHOT_TYPE]


def ui_reasons(run: dict, ws: dict, acs: dict) -> list[str]:
    """Why the run counts as touching UI (empty = it does not)."""
    out = [f"UI repo {a}" for a in ui_repos(run, ws)]
    out += [f"{ac.get('id')} kind ui" for ac in acs.get("acs") or [] if ac.get("kind") == "ui"]
    types = sorted({c.get("type") for c in acs.get("checks") or [] if c.get("type") in UI_CHECK_TYPES})
    out += [f"{t} checks" for t in types]
    return out


def check_errors(acs: dict) -> list[str]:
    """Shape of `screenshot` checks: `path` (one capture) or `spec` (Playwright steps; screenshots at the end)."""
    errors = []
    for chk in shot_checks(acs):
        cid = chk.get("id", "?")
        if bool(chk.get("path")) == bool(chk.get("spec")):
            errors.append(f"{cid}: screenshot check needs exactly one of path (single capture) or spec (Playwright steps)")
        vp = chk.get("viewport")
        if vp is not None and not (isinstance(vp, list) and len(vp) == 2 and all(isinstance(n, int) and n > 0 for n in vp)):
            errors.append(f"{cid}: viewport must be [width, height]")
    return errors


def _ask_command(run_id: str) -> str:
    return (f"factory run decide {run_id} \"<the user's answer>\" --by user --user-said \"<gist>\" --visual yes|no")


def plan_errors(run_id: str, run: dict, ws: dict) -> list[str]:
    """Plan gate (also re-checked by the prove gate): the question was answered, and a yes has screenshot checks."""
    if run.get("kind", "ticket") != "ticket":
        return []
    acs = acs_of(run_id)
    errors = check_errors(acs)
    reasons = ui_reasons(run, ws, acs)
    answer = decision(run)
    if reasons and answer is None:
        what = "before/after screenshots of the fix" if _is_bug(run) else "screenshots of the implemented screens"
        errors.append(f"visual evidence: the run touches UI ({', '.join(reasons)}); ask the user in the question round "
                      f"whether they want {what}, then record the answer: `{_ask_command(run_id)}`")
    if answer == "yes":
        shots = shot_checks(acs)
        covered = {c.get("repo") for c in shots}
        for alias in ui_repos(run, ws):
            if alias not in covered:
                errors.append(f"visual evidence (user said yes): {alias} has no screenshot check (type screenshot, "
                              "path or spec of the changed screen)")
        if not shots and not ui_repos(run, ws):
            errors.append("visual evidence (user said yes): acs.json has no screenshot check")
    return errors


def _is_bug(run: dict) -> bool:
    from .commands import gate
    return gate.is_bug_run(run)


def before_errors(run_id: str, run: dict, ws: dict) -> list[str]:
    """Bug run + yes: every screenshot check has BEFORE evidence `pass`, digest-intact, bound to the merge-base."""
    if decision(run) != "yes" or not _is_bug(run):
        return []
    from . import integrity
    from .commands import gate
    errors = []
    target = run_targets(run)[0]
    for chk in shot_checks(acs_of(run_id)):
        bid = before_id(evidence_id(run, chk["id"], target))
        rec = load_evidence(run_id, bid)
        if rec is None:
            errors.append(f"{bid}: no before screenshot on the base (env up on the clean run worktree, then "
                          f"`factory prove {run_id} --repro --assume-env`, before any fix commit)")
            continue
        bad = integrity.evidence_error(run, bid)
        if bad:
            errors.append(bad)
            continue
        if rec.get("status") != "pass":
            errors.append(f"{bid}: status {rec.get('status')} ({rec.get('summary')})")
            continue
        try:
            mb = gate.merge_base_for(run, ws, chk.get("repo"), target)
        except (FactoryError, KeyError):
            errors.append(f"{bid}: cannot compute merge-base for {chk.get('repo')}")
            continue
        if rec.get("git_sha") != mb:
            errors.append(f"{bid}: not bound to merge-base {mb[:8]} (sha {str(rec.get('git_sha'))[:8]})")
    return errors


def prove_errors(run_id: str, run: dict, ws: dict) -> list[str]:
    """Prove gate (and review/ship through proof_errors): the yes still has its checks and before images."""
    if decision(run) != "yes":
        return []
    errors = [e for e in plan_errors(run_id, run, ws) if e.startswith("visual evidence")]
    if errors:
        errors.append(f"the user asked for visual evidence after the plan: `factory run replan {run_id} \"add screenshot "
                      "checks\" --decision <n>`")
    return errors + before_errors(run_id, run, ws)


def _pngs(rec: dict | None) -> list[str]:
    return [a for a in (rec or {}).get("artifacts") or [] if str(a).lower().endswith(".png")]


def gallery(run_id: str, run: dict, alias: str | None = None, target: str | None = None) -> list[dict]:
    """Before/after images per screenshot check (run-relative paths plus absolute ones for the briefing)."""
    acs = acs_of(run_id)
    target = target if target is not None else run_targets(run)[0]
    base_target = run_targets(run)[0]
    rd = run_dir(run_id)
    out = []
    for chk in shot_checks(acs):
        if alias and chk.get("repo") != alias:
            continue
        after = load_evidence(run_id, evidence_id(run, chk["id"], target))
        before = load_evidence(run_id, before_id(evidence_id(run, chk["id"], base_target)))
        item = {
            "check": chk["id"], "repo": chk.get("repo"),
            "acs": [ac.get("id") for ac in acs.get("acs") or [] if chk["id"] in (ac.get("checks") or [])],
            "screen": chk.get("title") or chk.get("path") or chk.get("spec"),
            "before": _pngs(before), "after": _pngs(after),
            "before_sha": (before or {}).get("git_sha"), "after_sha": (after or {}).get("git_sha"),
        }
        item["files"] = [str((rd / p).resolve()) if not Path(p).is_absolute() else p
                         for p in item["before"] + item["after"]]
        out.append(item)
    return out


def description_lines(run: dict, alias: str, target: str | None) -> list[str]:
    """`## Visual evidence` section of an MR description (empty when the run has no screenshot checks for alias)."""
    items = gallery(run["id"], run, alias, target)
    if not items:
        return []
    lines = ["", "## Visual evidence", "", "| Check | Screen | Before (base) | After (HEAD) |", "|---|---|---|---|"]
    for it in items:
        b = " ".join(Path(p).name for p in it["before"]) or "-"
        a = " ".join(Path(p).name for p in it["after"]) or "-"
        screen = str(it["screen"] or "").replace("|", "\\|")
        lines.append(f"| {it['check']} ({', '.join(it['acs'])}) | {screen} | {b} | {a} |")
    lines += ["", "Screenshots captured by `factory prove` (before: merge-base, after: proven HEAD); files in the run's "
              "`evidence/` folder."]
    return lines
