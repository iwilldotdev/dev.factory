"""`factory next`: the one required action, derived from events, with the reasons a pending gate would fail."""
from __future__ import annotations

import os
from pathlib import Path

from . import acquire, feedback, gates, git, human, lock, media, plan as planmod, quality, review as rv, ship, shipbody
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
    if st["kind"] == "epic":
        from . import epic
        return epic.phase(project, st)
    if not gates.intake_passed(run, project.cfg, st):
        return "intake"
    if not gates.passed(run, project, st, "plan"):
        return "plan"
    if gates.is_bug(run, st) and not gates.passed(run, project, st, "reproduce"):
        return "reproduce"
    review_run = st["kind"] == "review"  # intake, plan, verify, review, report: it ships nothing
    if review_run and gates.verify_reasons(run, project, st, recorded=True):
        return "verify"
    if not review_run and not gates.passed(run, project, st, "verify"):
        return "build"
    if review_run:
        from . import review_run as rr
        heads = rv.heads_of(st, st["repos"])
        return "review" if not rr.reviewed(st, heads) else "done" if rr.reported(st, heads) else "report"
    if not gates.passed(run, project, st, "review", chain=False):
        return "review"
    if not gates.passed(run, project, st, "ship", chain=False):
        return "ship"
    if gates.passed(run, project, st, "feedback", chain=False):
        return "done"
    heads = rv.heads_of(st, planmod.load_plan(run)[0]["repos"])
    return "build" if feedback.open_round(st, heads) else "feedback"


def _repeated_env_failure(st: dict) -> dict | None:
    last = st["env_failures"][-ENV_REPEAT:]
    if len(last) == ENV_REPEAT and len({(e.get("kind"), e.get("reason"), e.get("repo")) for e in last}) == 1:
        return last[-1]
    return None


def compute(project: Project, run: Run, session: str | None = None) -> dict:
    st = run.state()
    if st["kind"] == "epic" and not st["closed"]:
        from . import epic
        return epic.compute(project, run, session)
    flag, tid = _flag(project), st["ticket"]
    ph = shown = phase(run, project, st)
    ph = "build" if ph == "verify" else ph  # a review run's verify phase is the build step without the building
    out: dict = {"run": run.id, "ticket": tid, "kind": gates.effective_kind(run, st), "phase": shown,
                 "action": None, "kind_of_action": None, "command": None, "files": [], "reasons": [], "role": "orchestrator",
                 "decisions": [d["id"] for d in st["decisions"].values() if not d["consumed"]], "refused": []}
    reasons = gates.reasons_for(run, project, st, {"build": "verify"}.get(ph, ph)) if ph in ("intake", "plan", "reproduce", "build") else []
    stage = None
    if ph == "review":
        reasons = gates.extra_reasons(run, project, st, "review")  # verify holds in this phase
    elif ph == "ship":
        stage, reasons = ship.stage(run, project, st)
    elif ph == "feedback":
        stage, reasons = feedback.stage(run, project, st)

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
        waiting = [r for r in reasons if human.WAITING in r]
        if waiting and len(waiting) == len(reasons):
            first = waiting[0].split(":")[0]
            set_("the commits are verified; only the user's confirmation is missing (ask for every human check in one message, show what each confirms, record their own words): "
                 + "; ".join(r.split(": ", 1)[1].split(" [")[0] for r in waiting), "user", f'factory confirm {first} --user-said "<their words>"{flag}')
        elif reasons:
            set_(("run the checks at the change head: a review run builds nothing, and its worktree is the change, detached" if shown == "verify" else
                  "build: implement the change in the worktree(s), commit it, then run verify; verify must pass at the HEAD of every "
                  "repo from a clean worktree, and the gate lists what is still missing"), "command", f"factory verify{flag}", trees, "builder")
        elif (fb := feedback.open_round(st, rv.heads_of(st, planmod.load_plan(run)[0]["repos"]))):
            set_(f"feedback round {fb['round']} from the change: fix each finding in the worktree(s), commit, then run verify; review and ship "
                 "the update follow (the same changes get an update intent): " + "; ".join(f"{f['source']} {f['text']}" for f in fb["findings"][:6]),
                 "command", f"factory verify{flag}", trees, "builder")
        else:
            set_("every check passes at HEAD; run the verify gate", "command", f"factory gate verify{flag}")
    elif ph == "review":
        _review_action(project, run, st, reasons, set_, flag)
    elif ph == "ship":
        _ship_action(project, run, st, stage, set_, flag)
        if st["ships"] and stage != "plan":
            out["briefing"] = shipbody.briefing(ship.load(run), ship.load(run)["rows"], st)
    elif ph == "feedback":
        _feedback_action(project, run, stage, set_, flag)
    elif ph == "report":
        set_("write the review report: verdict ok, not-ok or incomplete, a summary, the findings and the comment to post; its intents (comment, approve, "
             "ticket comment) are neutral and none of them runs without the user", "command", f'factory review report --file "{fwd(run.dir / "report.json")}"{flag}',
             [run.dir / "report.json"], "reviewer")
    elif st["kind"] == "review":
        set_("the report is recorded (report/plan.json): tell the user the verdict and offer its intents; nothing is posted without them. "
             "To fix the change yourself: factory review fix", "none")
    else:
        set_("the feedback gate passed: no unresolved thread and a green or waived pipeline. Tell the user what was shipped (changes, AC x evidence, "
             "follow-ups, human checks); ready and merge stay with them", "none")
    if ph in ("review", "ship") and (uat := _uat(run, st)):
        out["uat"] = uat
    if ph in ("build", "review"):  # a base that moved is merged before the review, not found at ship [L-108]
        by = {r["name"]: r for r in project.cfg["repo"]}
        late = [f"{r} is {b['count']} commit(s) behind {b['ref']}" for r in st.get("repos", [])
                if r in by and (w := git.current(st, r)) and (b := ship.behind(Path(w["path"]), by[r]["base"], by[r].get("remote", "origin")))["count"]]
        if late:
            out["base_note"] = "; ".join(late) + ": git fetch, merge the base into the run branch and verify again before the review"
    out["gate"] = {"build": "verify"}.get(ph, ph)
    out["reasons"] = reasons
    if out["decisions"]:
        out["decisions_note"] = "recorded user decisions not consumed yet: " + ", ".join(out["decisions"])
    return out


def _uat(run: Run, st: dict) -> dict | None:
    """The early UAT checkpoint of a UI run: the screenshot and visual artifacts to show the user, proposed once verify first
    passed. It never blocks; the user's answer is a typed human event (`factory human --kind answer`) [L-175]."""
    shots = []
    for e in st["evidence"]:
        if e["mode"] == "verify" and e["result"] == "pass" and e["spec_hash"] and any(a.endswith(".png") for a in e.get("artifacts", [])):
            shots += [fwd(run.dir / a) for a in e["artifacts"] if a.endswith(".png")]
    asked = any(h.get("text", "").startswith("uat:") for h in st["human"])
    if not shots or asked:
        return None
    return {"action": "show these images to the user and ask whether the UI is what they expect, at the ship briefing "
                      "(questions belong to intake); record their words with factory human --kind answer \"uat: <their words>\" (never blocks the run)", "images": sorted(set(shots))[-12:]}


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
        set_("the ticket and its media are recorded: declare the repos it affects (a scout reads the ticket and the code when unsure; repos: "
             + ", ".join(r["name"] for r in project.cfg["repo"]) + ")", "command", f"factory run repos <name>[,<name>]{flag}", [], "any")
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
             "minimal plan); questions belong to intake: decide what is still open, write the assumption in notes/design.md and report it", "command", f"factory gate plan{flag}",
             [plan_path], "any")
    elif any("env does not answer" in r or "env cannot serve" in r for r in reasons):
        repo = next((r.split(":")[0] for r in reasons if "env" in r), "<repo>")
        set_("the plan has runtime checks and the env of a repo is not serving the run worktree: start it (it starts what it depends on first), then run the plan gate",
             "command", f"factory env up {repo}{flag}", [], "builder")
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


def _review_action(project: Project, run: Run, st: dict, reasons: list[str], set_, flag: str) -> None:
    plan = planmod.load_plan(run)[0]
    heads = rv.heads_of(st, plan["repos"])
    asked = [k for k in rv.BOUND if (e := rv.latest(st, k)) and e["repos"] == heads and e["verdict"] == "CHANGES"
             and e["bound"].get(rv.BOUND[k]) == lock.current(st)[rv.BOUND[k]]]
    trees = [Path(w["path"]) for r in plan["repos"] if (w := git.current(st, r))]
    if asked:
        set_(f"the {' and '.join(asked)} review asked for changes (see the reasons): fix them in the worktree(s), commit, then run verify; "
             "a fresh review of the new commit follows", "command", f"factory verify{flag}", trees, "builder")
    elif reasons and all(r.startswith("approx frame") for r in reasons):
        frames = [r.split()[2].rstrip(":") for r in reasons]
        set_("an approximate design frame needs the user's own design review: show the user the design image and the capture "
             f"(the visual artifacts), ask whether it is close enough, then record their words for each frame: {', '.join(frames)}",
             "user", f'factory design review {frames[0]} --user-said "<their words>"{flag}')
    elif reasons:
        kind = "code" if any(r.startswith(("no code", "the code")) for r in reasons) else "qa"
        path = run.dir / "notes" / f"review-{kind}.json"  # reviews/ is CLI-owned: the hook refuses a hand write there
        rules = quality.guidelines(project.cfg, plan) if kind == "code" else []
        more = (" The code review also cites every guideline with followed true or false: " + "; ".join(f"{g['id']}: {g['text']}" for g in rules) + "." if rules else "")
        more += (" The risk is high: the code and qa reviews come from two different reviewers, each the strongest the harness has." if plan.get("risk") == "high" else "")
        set_("start a fresh reviewer (never the builder, with the strongest model the harness has) on the diff from branch point to HEAD in "
             f"every worktree: it writes the {kind} review as JSON (factory review example prints the shape) and records it; the code "
             "review lists changed_values and the qa review lists one exercises entry per AC; the same reviewer may record both kinds" + more,
             "command", f'factory review record --kind {kind} --file "{fwd(path)}"{flag}', [path], "reviewer")
    else:
        set_("both reviews are APPROVE at HEAD; run the review gate", "command", f"factory gate review{flag}")


def _feedback_action(project: Project, run: Run, stage: str, set_, flag: str) -> None:
    sp, adapter = ship.load(run), bool(project.cfg["providers"].get("change", {}).get("fetch"))
    if stage == "gate":
        set_("no unresolved thread and the latest pipeline is green or waived: run the feedback gate", "command", f"factory gate feedback{flag}")
    elif stage == "refresh" and adapter:
        set_("the changes are shipped: once the reviewers and the CI had their turn, read each change and its pipeline back through the adapters; "
             "unresolved threads and failed jobs become findings and the run returns to build", "command", f"factory feedback{flag}")
    elif stage == "refresh":
        repo = next(iter(sp["heads"]))
        raw, data = feedback.action_paths(run, repo)
        set_("the changes are shipped: once the reviewers and the CI had their turn, save each change as the provider reports it (with threads and "
             "pipelines newest first) and each pipeline, load them with factory ingest --refresh, then run the feedback command with --no-fetch",
             "command", f'factory ingest change --raw "{raw}" --contract "{data}" --refresh{flag}', [Path(raw), Path(data)], "orchestrator")
    elif stage == "ingest":
        set_("the change has unresolved threads or failed jobs that are not recorded yet: record them as findings (the run returns to build)",
             "command", f"factory feedback --no-fetch{flag}")
    else:
        set_("the feedback is not settled (see the reasons): wait for the pipeline, resolve the threads, or ask the user; only the user may waive a "
             "red pipeline (factory waive pipeline:<id> --user-said ...). Refresh when something changed", "user", f"factory feedback{flag}")


def _ship_action(project: Project, run: Run, st: dict, stage: str, set_, flag: str) -> None:
    from . import mirror
    if stage == "plan" and (step := mirror.next_step(project, run, st, flag)):
        set_(f"hotfix: {step[0]}", "command", step[1], [], "builder")
    elif stage == "plan":
        update = " --update" if any(ship._existing(run, st, r) for r in st["repos"]) else ""
        set_("write the ship plan: neutral intents (push, change request with its AC x check x status body, readback, ticket writes) for "
             "the current commits; --ticket none|comment|transition|both picks the ticket writes, --pipeline adds a pipeline readback, "
             "--allow-behind --decision Q-n accepts a target that moved", "command", f"factory ship plan{update}{flag}", [], "orchestrator")
    elif stage == "execute":
        sp = ship.load(run)
        todo = [i for i in sp["intents"] if i["intent"] == "readback" and not ship.changes(run, st, i["repo"], st["ships"][-1]["at"])]
        first = todo[0]
        ask = any(i["needs_user"] for i in sp["intents"])
        how = f"factory ship verify{flag}" if first["adapter"] else f"{first['ingest']}{flag}"
        set_("execute the intents of ship/plan.json in order with your own tools" + (", asking the user before each intent that needs_user "
             "(push, creating or updating the change, ticket writes)" if ask else " (draft-auto lets you push and open a draft without asking; "
             "ticket writes still ask)") + ". Create or update the change with the title, branches and body file named there, then read "
             "it back: save the provider payload and the change contract data (id, url, repo, title, description_md, source_branch, target_branch, "
             "head_sha as the full SHA, state, draft, reviewers, pipelines newest first) to the paths given, ingest them"
             + (" (or let the configured adapter do it in ship verify)" if first["adapter"] else "") + ", then run ship verify",
             "user" if ask else "command", how, [Path(first["raw"]), Path(first["contract_file"]), run.dir / "ship" / "body.md"], "orchestrator")
    elif stage == "fix":
        set_("the change read back differs from the ship plan (see the reasons): fix the change with your own tools, or re-plan, read it "
             "back again and run ship verify", "command", f"factory ship verify{flag}", [], "orchestrator")
    elif stage == "verify":
        set_("the readback matches the ship plan; record it", "command", f"factory ship verify{flag}")
    else:
        set_("the readback is verified; run the ship gate", "command", f"factory gate ship{flag}")
