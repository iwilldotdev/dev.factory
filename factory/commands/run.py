"""`factory run start|status|list|branch|decide|block|unblock|human|replan|rewind` (DESIGN §2.2, §3)."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

from ..collectors.test import copy_main_files, files_from_main_of, prepare_worktree, setup_command
from ..core import (HUMAN_TYPES, SCHEMA_VERSION, hotfix_of, run_targets, FactoryError, GateFailed, claim_is_recent, current_owner, ensure_owner, event,
                    factory_home, flaky_checks,
                    git, git_dirty, git_head, now, register_session, repo_conf, run_dir, target_branch, run_load,
                    run_save, run_update, session_id, session_short, workspace_load, write_json)
from .. import integrity
from . import gate

# kind -> one-line meaning (shown when an invalid kind is given; DESIGN section on blockers lists the same)
BLOCKER_KIND_HELP = {
    "env": "the run's own environment (env up/login/ports/worktree) does not work",
    "access": "a credential or permission is missing (repo, pipeline, ticket, design file)",
    "question": "only the user can answer it",
    "dependency": "another ticket, run or MR must land first",
    "conflict": "a merge or cherry-pick conflict needs resolving",
    "decision": "a product or scope decision the user must make",
    "infra": "a service, machine or network outside the run (CI, registry, VPN, shared database) is down",
    "tooling": "the factory or a tool lacks a capability the run needs",
}
BLOCKER_KINDS = tuple(BLOCKER_KIND_HELP)


def blocker_kind(value: str) -> str:
    """argparse type: an invalid kind lists every valid one with its meaning."""
    if value not in BLOCKER_KIND_HELP:
        lines = "".join(f"\n  {k}: {v}" for k, v in BLOCKER_KIND_HELP.items())
        raise argparse.ArgumentTypeError(f"invalid blocker kind {value!r}; valid kinds:{lines}")
    return value


def _emit(args, payload, text: str) -> None:
    print(json.dumps(payload, indent=2) if getattr(args, "json", False) else text)


def run_short(run: dict) -> str:
    prefix = run["workspace"].lower() + "-"
    return run["id"][len(prefix):] if run["id"].startswith(prefix) else run["id"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def branch_name(run: dict, ws: dict, alias: str, target: str | None, mirror: bool = False) -> str:
    """Branch name from workspace branch_format (feature|bug|hotfix); legacy default when no format applies."""
    first = run["tickets"][0]
    kind = "hotfix" if run.get("hotfix") else ("bug" if first["type"] == "bug" else "feature")
    formats = ws.get("branch_format") or {}
    fmt = formats.get(kind) or (formats.get("bug") if kind == "hotfix" else None)
    if not fmt:
        base = f"{'bugfix' if first['type'] == 'bug' else 'feature'}/{first['key']}-{slug(alias)}"
        return f"{base}-{target}" if mirror and target else base
    name = _format_branch(run, ws, alias, target, fmt, kind, first)
    if mirror and target and run.get("hotfix"):
        base_name = _format_branch(run, ws, alias, run["hotfix"].get("base"), fmt, kind, first)
        if name in (base_name, ((run.get("repos") or {}).get(alias) or {}).get("branch")):
            name = f"{name}-{target}"  # a hotfix format without {target}: the mirror must not collide with the base
    return name


def _format_branch(run: dict, ws: dict, alias: str, target: str | None, fmt: str, kind: str, first: dict) -> str:
    target = target or ""
    values = {"key": first["key"], "slug": slug(first.get("summary") or alias), "target": target,
              "target_upper": target.upper(), "package": (ws.get("ship") or {}).get("package") or ""}
    try:
        return fmt.format(**values)
    except (KeyError, IndexError, ValueError) as err:
        raise FactoryError(f"branch_format.{kind} {fmt!r} is invalid: {err}") from err


# ---------- start ----------

ISSUE_BUG_TYPES = {"bug", "defeito", "erro"}


def issue_is_bug(path: Path) -> bool | None:
    """True/False from the saved Jira issue's issuetype (Bug/Defeito/Erro, any case); None when unreadable or absent."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fields = data.get("fields") if isinstance(data, dict) and isinstance(data.get("fields"), dict) else data
    itype = fields.get("issuetype") if isinstance(fields, dict) else None
    name = itype.get("name") if isinstance(itype, dict) else itype
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().lower() in ISSUE_BUG_TYPES


def cmd_start(args) -> int:
    ws = workspace_load(args.workspace)
    tickets = [t.upper() for t in args.tickets]
    bugs = {b.upper() for b in args.bug or []}
    hotfix = None
    targets = list((ws.get("ship") or {}).get("targets") or [])
    if getattr(args, "hotfix", False):
        from .workspace import hotfix_config, hotfix_config_errors
        cfg = hotfix_config(ws)
        if not cfg:
            raise FactoryError("--hotfix needs ship.hotfix in workspace.json (or a target named homolog)")
        bad = hotfix_config_errors(dict(ws, ship=dict(ws.get("ship") or {}, hotfix=cfg)))
        if bad:
            raise FactoryError("; ".join(bad))
        hotfix = {"base": cfg["base"], "mirror": list(cfg.get("mirror") or [])}
        targets = [hotfix["base"], *hotfix["mirror"]]
        if not args.feature:
            bugs |= set(tickets)
    run_id = f"{ws['name'].lower()}-{(args.epic or tickets[0]).lower()}"
    if (run_dir(run_id) / "run.json").exists():
        raise FactoryError(f"run {run_id} already exists")
    derived = []
    if not args.feature:  # the saved Jira issuetype decides when the issue was fetched into the run dir already
        for t in tickets:
            if issue_is_bug(run_dir(run_id) / "remote" / f"issue-{t}.json"):
                bugs.add(t)
                derived.append(t)
    stamp = now()
    run = {
        "schemaVersion": SCHEMA_VERSION, "id": run_id, "workspace": ws["name"], "kind": "ticket",
        "tickets": [{"key": t, "type": "bug" if t in bugs else "feature",
                     "parent": args.epic.upper() if args.epic else None, "depends_on": []} for t in tickets],
        "repos": {}, "targets": targets,
        "phase": "intake", "risk": args.risk,
        "loops": {"build": 0, "review": 0},
        "decisions": [], "blockers": [], "events": [], "sessions": [],
        "created": stamp, "updated": stamp,
    }
    if hotfix:
        run["hotfix"] = hotfix
    if getattr(args, "login_user", None):
        run["login_user"] = args.login_user
    for sub in ("remote", "figma", "checks", "evidence", "reviews", "baseline"):
        (run_dir(run_id) / sub).mkdir(parents=True, exist_ok=True)
    event(run, "phase", **{"from": None, "to": "intake"})
    register_session(run)
    if derived:
        event(run, "retype", to="bug", tickets=derived, source="jira issuetype")
    run_save(run)
    note = f" (bug derived from the Jira issuetype: {', '.join(derived)})" if derived else ""
    _emit(args, {"run": run_id, "dir": str(run_dir(run_id)), "bug": derived}, f"started {run_id} at {run_dir(run_id)}{note}")
    return 0


# ---------- status / list ----------

def next_action(run: dict) -> str:
    phase = run["phase"]
    open_blockers = [b for b in run.get("blockers", []) if not b.get("resolved")]
    if phase == "done":
        return "nothing: run is done"
    if phase == "closed":
        return f"nothing: run is closed ({(run.get('closed') or {}).get('reason', 'no reason recorded')})"
    if phase == "blocked":
        return "resolve blockers (factory run unblock <run> <n>) or decide with the user"
    if phase not in gate.EVALUATORS:
        return f"unknown phase {phase}"
    errors = gate.evaluate(run["id"], phase)
    if not errors:
        return f"factory gate {run['id']} {phase}"
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    suffix = f" [{len(open_blockers)} open blocker(s)]" if open_blockers else ""
    return f"{errors[0]}{more}{suffix}"


def missing_state(run: dict) -> list[str]:
    """What the run records that is gone: worktrees and recorded heads (status prints it instead of a traceback)."""
    out = []
    for alias, rec in (run.get("repos") or {}).items():
        if not isinstance(rec, dict):
            continue
        paths = ([("", rec["worktree"])] if rec.get("worktree") else []) + \
            [(f" ({t})", w) for t, w in (rec.get("worktrees") or {}).items()]
        for label, wt in paths:
            if not Path(wt).is_dir():
                out.append(f"{alias}{label}: worktree missing: {wt}")
            elif rec.get("head") and not label:
                proc = subprocess.run(["git", "-C", str(wt), "cat-file", "-e", f"{rec['head']}^{{commit}}"],
                                      capture_output=True, text=True)
                if proc.returncode != 0:
                    out.append(f"{alias}: recorded head {str(rec['head'])[:8]} not found in {wt}")
    return out


def cmd_status(args) -> int:
    run = run_load(args.run)
    open_blockers = [(i, b) for i, b in enumerate(run.get("blockers", [])) if not b.get("resolved")]
    gone = missing_state(run)
    try:
        action = next_action(run)
        errors = gate.evaluate(run["id"], run["phase"]) if run["phase"] in gate.EVALUATORS else []
    except Exception as err:  # noqa: BLE001 - status degrades (never a traceback) when worktrees or heads are gone
        action, errors = f"cannot evaluate: {err}", []
    if getattr(args, "json", False):
        owner = current_owner(run)
        print(json.dumps({"id": run["id"], "phase": run["phase"], "loops": run["loops"],
                          "owner": owner["session"] if owner else None,
                          "blockers": [dict(b, n=i) for i, b in open_blockers],
                          "flaky": flaky_checks(run["id"]),
                          "next": action, "missing": errors, "gone": gone}, indent=2))
        return 0
    print(f"{run['id']}  phase={run['phase']}  risk={run.get('risk')}  "
          f"loops build={run['loops'].get('build', 0)} review={run['loops'].get('review', 0)}")
    owner = current_owner(run)
    if owner:
        print(f"owner: session {session_short(owner['session'])} since {owner.get('claimed_at')}")
    for i, b in open_blockers:
        print(f"blocker #{i} [{b['kind']}] {b['text']}")
    for g in gone:
        print(f"missing: {g}")
    flaky = flaky_checks(run["id"])
    if flaky:
        print(f"flaky: {', '.join(flaky)} (a fail followed by a pass at the same sha and check digest; not a gate failure)")
    print(f"next: {action}")
    for e in errors[1:6]:
        print(f"  - {e}")
    return 0


def cmd_list(args) -> int:
    root = factory_home() / "runs"
    rows = []
    for p in sorted(root.glob("*/run.json")) if root.is_dir() else []:
        try:
            r = run_load(p.parent.name)
        except FactoryError:
            continue
        if r.get("phase") == "closed" and not getattr(args, "all", False):
            continue
        rows.append({"id": r["id"], "phase": r["phase"], "updated": r.get("updated")})
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['id']}\t{r['phase']}\t{r['updated']}")
        if not rows:
            print("no runs")
    return 0


def cmd_close(args) -> int:
    """Terminal phase `closed` for a run that will not continue (abandoned, superseded); keeps every artifact."""
    reason = (args.reason or "").strip()
    if not reason:
        raise FactoryError("run close needs --reason \"<why the run stops>\"")

    def mutate(run: dict) -> None:
        ensure_owner(run)
        if run["phase"] == "closed":
            raise FactoryError(f"run {run['id']} is already closed")
        if run["phase"] == "done":
            raise FactoryError(f"run {run['id']} is done; nothing to close (run delete removes it)")
        prev = run["phase"]
        event(run, "close", reason=reason, **{"from": prev})
        run["closed"] = {"at": now(), "reason": reason, "from": prev}
        gate._set_phase(run, "closed")

    run = run_update(args.run, mutate)
    from .metrics import refresh
    refresh(run["id"])
    _emit(args, {"phase": "closed", "reason": reason}, f"{run['id']} closed ({reason})")
    return 0


def cmd_retype(args) -> int:
    """Switch a run between feature, bug and hotfix keeping every artifact; only before gate build passed."""
    mode = "bug" if args.bug else "feature" if args.feature else "hotfix"

    def mutate(run: dict) -> None:
        ensure_owner(run)
        if run.get("kind", "ticket") != "ticket":
            raise FactoryError(f"run retype is for ticket runs, not {run.get('kind')}")
        if any(e["kind"] == "gate_pass" and e["data"].get("phase") == "build" for e in run.get("events", [])) or \
                run["phase"] in ("prove", "review", "ship", "feedback", "done", "closed"):
            raise FactoryError("gate build already passed (or the run is past it): retype is no longer possible; "
                               "start a new run or replan")
        ws = workspace_load(run["workspace"])
        before = {"bug": gate.is_bug_run(run), "hotfix": bool(run.get("hotfix")), "phase": run["phase"]}
        if mode == "hotfix":
            from .workspace import hotfix_config, hotfix_config_errors
            cfg = hotfix_config(ws)
            if not cfg:
                raise FactoryError("--hotfix needs ship.hotfix in workspace.json (or a target named homolog)")
            bad = hotfix_config_errors(dict(ws, ship=dict(ws.get("ship") or {}, hotfix=cfg)))
            if bad:
                raise FactoryError("; ".join(bad))
            run["hotfix"] = {"base": cfg["base"], "mirror": list(cfg.get("mirror") or [])}
            run["targets"] = [cfg["base"], *run["hotfix"]["mirror"]]
            for t in run["tickets"]:
                t["type"] = "bug"
        else:
            if run.get("hotfix"):
                if any((rec or {}).get("worktrees") for rec in (run.get("repos") or {}).values()):
                    raise FactoryError(f"cannot retype to {mode}: mirror worktrees exist (run mirror); "
                                       "they would be orphaned")
                run.pop("hotfix")
                run["targets"] = list((ws.get("ship") or {}).get("targets") or [])
            for t in run["tickets"]:
                t["type"] = mode
        phases, _ = gate.machine(run)
        passed = {e["data"].get("phase") for e in run.get("events", []) if e["kind"] == "gate_pass"}
        if run["phase"] not in phases:  # a feature has no reproduce phase
            gate._set_phase(run, "build")
        elif run["phase"] == "build" and "reproduce" in phases and "reproduce" not in passed:
            gate._set_phase(run, "reproduce")  # a bug is reproduced before the fix is built
        event(run, "retype", to=mode, bug=gate.is_bug_run(run), hotfix=bool(run.get("hotfix")),
              before=before, phase=run["phase"])

    run = run_update(args.run, mutate)
    _emit(args, {"retype": mode, "phase": run["phase"]}, f"{run['id']} is now a {mode} run (phase {run['phase']})")
    return 0


def cmd_verify_human(args) -> int:
    """Pass evidence for a `human` check: only what the user stated in chat, bound to the worktree HEAD."""
    said = (args.user_said or "").strip()
    note = (args.note or "").strip()
    if args.by != "user":
        raise FactoryError("verify-human records what the USER confirmed: pass --by user (an agent cannot verify a "
                           "human check)")
    if not said or not note:
        raise FactoryError("verify-human needs --note \"<what the user confirmed>\" and --user-said \"<gist of the "
                           "user's chat message>\"")
    from ..core import evidence_file, file_sha256, read_json, repo_path_for_run, write_evidence

    def mutate(run: dict) -> None:
        ensure_owner(run)
        data = read_json(run_dir(run["id"]) / "acs.json")
        chk = next((c for c in data.get("checks") or [] if c.get("id") == args.check), None)
        if chk is None:
            raise FactoryError(f"{args.check} is not a check of acs.json")
        if chk.get("type") != "human":
            raise FactoryError(f"{args.check} is a {chk.get('type')} check: only `human` checks are verified by the user; "
                               f"run `factory prove {run['id']} --check {args.check}`")
        ws = workspace_load(run["workspace"])
        repos = sorted(run.get("repos") or {})
        alias = args.repo or chk.get("repo") or (repos[0] if repos else None)
        path = repo_path_for_run(run, ws, alias) if alias else None
        if path is not None and not path.is_dir():
            raise FactoryError(f"worktree missing: {path}")
        record = write_evidence(run["id"], dict(chk, repo=alias), {
            "status": "pass", "summary": f"confirmed by the user: {note}",
            "details": {"by": "user", "user_said": said, "note": note, "text": chk.get("text")}}, path, now(), 0.0)
        run.setdefault("events", []).append({"at": now(), "kind": "evidence", "data": {  # `kind` is event()'s own arg
            "check": chk["id"], "status": "pass", "sha": record.get("git_sha"),
            "digest": file_sha256(evidence_file(run["id"], chk["id"])), "kind": "human", "by": "user", "note": note}})
        run["decisions"].append({"at": now(), "phase": run["phase"], "by": "user", "user_said": said,
                                 "text": f"human check {chk['id']} confirmed: {note}"})

    run = run_update(args.run, mutate)
    _emit(args, {"check": args.check, "status": "pass"}, f"{args.check}: human confirmation recorded for {run['id']}")
    return 0


# ---------- branch ----------

def _for_target(args, run: dict, ws: dict, conf: dict, main: Path) -> int:
    """Record a per-target source branch (branches[target]) created from --base in the main repo."""
    alias = conf["alias"]
    rec = run["repos"].get(alias)
    if not rec:
        raise FactoryError(f"repo {alias} has no branch in run {run['id']}; create the main branch first")
    target = args.for_target
    name = args.name or f"{rec['branch']}-{target}"
    base = gate.resolve_base(main, target_branch(ws, alias, args.base))
    if not git(main, "rev-parse", "--verify", "--quiet", base + "^{commit}", check=False):
        raise FactoryError(f"base ref {base} not found in {main}")
    if git(main, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False):
        raise FactoryError(f"branch {name} already exists in {main}")
    git(main, "branch", "--no-track", name, base)
    rec.setdefault("branches", {})[target] = name
    run_save(run)
    _emit(args, {"target": target, "branch": name}, f"{alias}: target {target} ships from branch {name} (from {base})")
    return 0


def cmd_branch(args) -> int:
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    conf = repo_conf(ws, args.repo)
    alias = conf["alias"]
    main = Path(conf.get("path") or "")
    if not conf.get("path") or not (main / ".git").exists():
        raise FactoryError(f"repo {alias}: workspace path is missing or not a git repository: {conf.get('path')!r}")
    if args.mr:
        from . import review
        return review.branch_for_mr(args, run, ws, conf, main)
    if not args.base:
        raise FactoryError("--base is required (or --mr <iid> for a review run)")
    if args.for_target:
        if getattr(args, "env_only", False):
            raise FactoryError("--env-only does not combine with --for-target")
        return _for_target(args, run, ws, conf, main)
    if alias in run["repos"]:
        raise FactoryError(f"repo {alias} already has a branch in run {run['id']}")
    root = ws.get("worktree_root")
    if not root:
        raise FactoryError("workspace worktree_root is not set")
    first = run["tickets"][0]
    branch = args.name or branch_name(run, ws, alias, args.base)
    worktree = Path(root) / f"{run_short(run)}-{alias.lower()}"
    if worktree.exists():
        raise FactoryError(f"worktree path already exists: {worktree}")
    base_branch = None
    if args.from_run:
        parent_rec = (run_load(args.from_run).get("repos") or {}).get(alias)
        if not parent_rec:
            raise FactoryError(f"run {args.from_run} has no branch for repo {alias}")
        base_branch = parent_rec["branch"]
        base = base_branch
    else:
        base = gate.resolve_base(main, target_branch(ws, alias, args.base))
    if not git(main, "rev-parse", "--verify", "--quiet", base + "^{commit}", check=False):
        raise FactoryError(f"base ref {base} not found in {main}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--no-track", "-b", branch, str(worktree), base],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FactoryError(f"git worktree add failed: {proc.stderr.strip()}")
    run["repos"][alias] = {"branch": branch, "base": args.base, "worktree": str(worktree),
                           "head": git_head(worktree), "base_ref": base, "branch_point": git_head(worktree)}
    if base_branch:
        run["repos"][alias]["base_branch"] = base_branch
        run["repos"][alias]["base_run"] = args.from_run
    if getattr(args, "env_only", False):
        run["repos"][alias]["env_only"] = True
        event(run, "env_only", repo=alias, on=True, phase=run.get("phase"), via="branch")
    run_save(run)
    copied: list[str] = []
    warn = _prepare(ws, conf, main, worktree, copied)
    _emit(args, dict(run["repos"][alias], **({"setup_error": warn} if warn else {}), **({"copied_from_main": copied} if copied else {})),
          f"{alias}: branch {branch} from {base} at {worktree}" + (f"\nWARNING: {warn}" if warn else "")
          + (f"\ncopied from main checkout: {', '.join(copied)}" if copied else ""))
    return 1 if warn else 0


def cmd_env_only(args) -> int:
    """Mark (or unmark with --off) an already registered repo as env_only: it only serves the env from its run
    worktree and carries no code, so gate build, prove, review, mirror and ship skip it. acs/checks do not change,
    so no replan is needed; the change is logged as an `env_only` event."""
    on = not args.off

    def mutate(run: dict) -> None:
        ws = workspace_load(run["workspace"])
        alias = repo_conf(ws, args.repo)["alias"]
        rec = (run.get("repos") or {}).get(alias)
        if not rec:
            raise FactoryError(f"repo {alias} has no branch in run {run['id']} (factory run branch {run['id']} {alias} "
                               "--base <b> --env-only)")
        if run.get("kind") == "review" and any(m.get("repo") == alias for m in run.get("mrs") or []):
            raise FactoryError(f"repo {alias} holds an MR under review in run {run['id']}; it cannot be env_only")
        if on and rec.get("worktrees"):
            raise FactoryError(f"repo {alias} already has mirror worktrees ({', '.join(rec['worktrees'])}); "
                               "it carries code and cannot be env_only")
        if bool(rec.get("env_only")) == on:
            return
        if on:
            rec["env_only"] = True
        else:
            rec.pop("env_only", None)
        event(run, "env_only", repo=alias, on=on, phase=run.get("phase"))
        args._alias = alias

    args._alias = None
    run = run_update(args.run, mutate)
    alias = args._alias or repo_conf(workspace_load(run["workspace"]), args.repo)["alias"]
    state = "env_only (serves the env, skipped by build/prove/review/mirror/ship)" if on else "a code repo again"
    _emit(args, {"repo": alias, "env_only": on, "changed": args._alias is not None},
          f"{alias}: {state} in run {run['id']}" + ("" if args._alias else " (unchanged)"))
    return 0


def cmd_target_ticket(args) -> int:
    """Hotfix with several tickets: the MR of `target` is titled with ticket KEY (default: the first ticket)."""
    def mutate(run: dict) -> None:
        hf = hotfix_of(run)
        if not hf:
            raise FactoryError(f"run {run['id']} is not a hotfix run; target-ticket needs one")
        if args.target not in run_targets(run):
            raise FactoryError(f"{args.target}: not a target of run {run['id']} ({', '.join(run_targets(run))})")
        keys = {str(t.get("key", "")).upper(): t["key"] for t in run.get("tickets") or [] if t.get("key")}
        if args.key.upper() not in keys:
            raise FactoryError(f"{args.key}: not a ticket of run {run['id']} ({', '.join(keys.values())})")
        if run.get("phase") in ("done", "blocked"):
            raise FactoryError(f"run {run['id']} is {run['phase']}; target-ticket is not allowed")
        key = keys[args.key.upper()]
        if (hf.get("tickets") or {}).get(args.target) == key:
            return
        hf.setdefault("tickets", {})[args.target] = key
        event(run, "target_ticket", target=args.target, ticket=key, phase=run.get("phase"))

    run = run_update(args.run, mutate)
    _emit(args, {"target": args.target, "ticket": run["hotfix"]["tickets"][args.target]},
          f"{args.target}: MR titled with {run['hotfix']['tickets'][args.target]} in run {run['id']} (re-run `ship plan`)")
    return 0


def _prepare(ws: dict, conf: dict, main: Path, worktree: Path, copied: list[str] | None = None,
             skipped: list[str] | None = None) -> str | None:
    """Same preparation as the temp worktrees of baseline/repro/red-green (quality.setup or node_modules link), plus
    the repo's `env.files_from_main` (gitignored files copied from the main checkout; their names are appended to
    `copied`; the ones refused, with the reason, to `skipped`)."""
    try:
        prepare_worktree(main, worktree, setup_command(ws, conf["alias"]), (conf.get("env") or {}).get("env_vars"))
    except RuntimeError as err:
        return f"{err}; fix it and rerun the setup command in {worktree}"
    finally:
        if copied is not None:
            copied.extend(copy_main_files(main, worktree, files_from_main_of(ws, conf["alias"]), skipped))
    return None


def cmd_mirror(args) -> int:
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    hf = run.get("hotfix")
    if not hf:
        raise FactoryError("run mirror is for hotfix runs (run start --hotfix)")
    target = args.to
    if target not in (hf.get("mirror") or []):
        raise FactoryError(f"{target!r} is not a mirror target of this hotfix ({hf.get('mirror')})")
    conf = repo_conf(ws, args.repo)
    alias = conf["alias"]
    rec = run["repos"].get(alias)
    if not rec:
        raise FactoryError(f"repo {alias} has no branch in run {run['id']}")
    if rec.get("env_only"):
        raise FactoryError(f"repo {alias} is env_only in run {run['id']}: it carries no code to mirror")
    if target in (rec.get("worktrees") or {}):
        raise FactoryError(f"{alias} already mirrored to {target} ({rec['worktrees'][target]}); "
                           "if a cherry-pick is in progress there, resolve it and `git cherry-pick --continue`")
    base_wt = Path(rec["worktree"])
    if not any(e["kind"] == "gate_pass" and e["data"].get("phase") == "build" for e in run.get("events", [])):
        raise GateFailed([f"gate build has not passed for {hf['base']}; commit the fix and run `factory gate {run['id']} build` first"])
    if git_dirty(base_wt):
        raise GateFailed([f"{alias}: {hf['base']} worktree has uncommitted changes; commit before mirroring"])
    main = Path(conf.get("path") or "")
    base_ref = gate.resolve_base(base_wt, target_branch(ws, alias, rec.get("base", hf["base"])))
    mb = git(base_wt, "merge-base", "HEAD", base_ref)
    shas = git(base_wt, "rev-list", "--reverse", f"{mb}..{rec['branch']}").split()
    if not shas:
        raise GateFailed([f"{alias}: no commits between merge-base and {rec['branch']} to mirror"])
    root = ws.get("worktree_root")
    worktree = Path(root) / f"{run_short(run)}-{alias.lower()}-{target}"
    if worktree.exists():
        raise FactoryError(f"worktree path already exists: {worktree}")
    name = args.name or branch_name(run, ws, alias, target, mirror=True)
    start = gate.resolve_base(main, target_branch(ws, alias, target))
    if not git(main, "rev-parse", "--verify", "--quiet", start + "^{commit}", check=False):
        raise FactoryError(f"base ref {start} not found in {main}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--no-track", "-b", name, str(worktree), start],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise FactoryError(f"git worktree add failed: {proc.stderr.strip()}")
    copied: list[str] = []
    skipped: list[str] = []
    warn = _prepare(ws, conf, main, worktree, copied, skipped)
    if warn:
        print(f"WARNING: {warn}")
    rec.setdefault("branches", {})[target] = name
    rec.setdefault("worktrees", {})[target] = str(worktree)
    pick = subprocess.run(["git", "-C", str(worktree), "cherry-pick", *shas],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if pick.returncode != 0:
        files = [f for f in git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).splitlines() if f]
        text = f"cherry-pick conflict mirroring {alias} to {target} in {worktree}: " + ", ".join(files or ["?"])
        run.setdefault("blockers", []).append({"at": now(), "kind": "conflict", "text": text,
                                               "files": files, "resolved": False})
        event(run, "block", blocker_kind="conflict", text=text, n=len(run["blockers"]) - 1)
        event(run, "mirror", repo=alias, target=target, status="conflict", files=files)
        run_save(run)
        raise GateFailed([text,
                          f"worktree left mid-cherry-pick at {worktree}: a builder resolves the files, `git add` them and runs "
                          "`git cherry-pick --continue`; never --abort or reset; then `factory run unblock`",
                          "resolve by reproducing the base diff: adapt only what the mirror branch structurally requires (moved "
                          "files, import paths); no reformatting, no relocated tests; formatter in check mode only, never write "
                          f"mode on a whole file; then run `factory run mirror-diff {run['id']}` (the gates fail on any divergence)"])
    event(run, "mirror", repo=alias, target=target, status="ok", commits=len(shas))
    run_save(run)
    _emit(args, dict({"target": target, "branch": name, "worktree": str(worktree), "commits": len(shas)},
                     **({"copied_from_main": copied} if copied else {})),
          f"{alias}: mirrored {len(shas)} commit(s) to {target} on {name} at {worktree}"
          + (f"\ncopied from main checkout: {', '.join(copied)}" if copied else ""))
    return 0


def cmd_mirror_diff(args) -> int:
    """Read-only: do the mirror branches carry the same changes as the base fix? Exit 0 equal, 1 divergent."""
    from .. import mirrordiff
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    if not hotfix_of(run):
        raise FactoryError("run mirror-diff is for hotfix runs (run start --hotfix)")
    report = mirrordiff.compute(run, ws)
    if report is None:
        raise FactoryError("no mirror worktree to compare yet (create it with the run mirror command)")
    allowed = (not report["equal"]) and mirrordiff.allowance_valid(run, report)
    if getattr(args, "json", False):
        print(json.dumps({**mirrordiff.public(report), "allowed_by_decision":
                          run["mirror_allow"]["decision"] if allowed else None}, indent=2, ensure_ascii=False))
    elif report["equal"]:
        print("OK mirror diff equal: the mirror branches carry the same changes as the base fix")
        print("\n".join(mirrordiff.render_refs(report)))
    else:
        print("DIVERGENT mirror diff" + (f" (allowed by user decision #{run['mirror_allow']['decision']})" if allowed else ""))
        print("\n".join(mirrordiff.render(report)))
    return 0 if report["equal"] else 1


# ---------- decide / block / unblock / human ----------

def cmd_decide(args) -> int:
    """`--by user` only for a decision the user actually stated in chat; `--user-said` keeps the gist of that message."""
    said = (getattr(args, "user_said", None) or "").strip()
    if args.by == "user" and not said:
        raise FactoryError("--by user needs --user-said \"<gist of the user's chat message>\": record only decisions "
                           "the user actually stated")
    visual = getattr(args, "visual", None)
    if visual and args.by != "user":
        raise FactoryError("--visual records the user's answer to the visual-evidence question: needs --by user")

    def mutate(run: dict) -> None:
        rec = {"at": now(), "phase": run["phase"], "text": args.text, "by": args.by}
        if said:
            rec["user_said"] = said
        if visual:
            rec["visual"] = visual
        run["decisions"].append(rec)

    run = run_update(args.run, mutate)
    _emit(args, {"decisions": len(run["decisions"])}, f"decision #{len(run['decisions']) - 1} recorded")
    return 0


def cmd_block(args) -> int:
    def mutate(run: dict) -> None:
        run["blockers"].append({"at": now(), "kind": args.kind, "text": args.text, "resolved": False})
        event(run, "block", blocker_kind=args.kind, text=args.text, n=len(run["blockers"]) - 1)

    run = run_update(args.run, mutate)
    _emit(args, {"blocker": len(run["blockers"]) - 1}, f"blocker #{len(run['blockers']) - 1} recorded")
    return 0


def cmd_unblock(args) -> int:
    def mutate(run: dict) -> None:
        if not 0 <= args.n < len(run["blockers"]):
            raise FactoryError(f"no blocker #{args.n}")
        run["blockers"][args.n]["resolved"] = True
        run["blockers"][args.n]["resolved_at"] = now()
        if run["phase"] == "blocked" and not any(not b.get("resolved") for b in run["blockers"]):
            prev = next((e["data"].get("resume") or e["data"].get("from") for e in reversed(run["events"])
                         if e["kind"] == "phase" and e["data"].get("to") == "blocked"), None) or "build"
            event(run, "phase", **{"from": "blocked", "to": prev})
            run["phase"] = prev

    run = run_update(args.run, mutate)
    _emit(args, {"phase": run["phase"]}, f"blocker #{args.n} resolved; phase {run['phase']}")
    return 0


def _use_decision(run: dict, n, action: str) -> dict | None:
    """The user decision #n authorising `action` (recorded --by user in the current phase, not used before)."""
    if n is None:
        return None
    decisions = run.get("decisions") or []
    if not 0 <= n < len(decisions):
        raise FactoryError(f"no decision #{n}")
    d = decisions[n]
    if d.get("by") != "user":
        raise FactoryError(f"decision #{n} is not a user decision (--by user)")
    if d.get("phase") != run["phase"]:
        raise FactoryError(f"decision #{n} was recorded in phase {d.get('phase')}, not the current phase {run['phase']}")
    if d.get("used"):
        raise FactoryError(f"decision #{n} already authorised {d['used'].get('action')}")
    d["used"] = {"action": action, "at": now()}
    return d


def _cap_block(run: dict, text: str, resume: str) -> None:
    gate.block(run, text, resume)
    run_save(run)
    raise GateFailed([f"{text}: run blocked. Ask the user; record their decision with "
                      f"`factory run decide {run['id']} \"<decision>\" --by user --user-said \"<gist>\"`, "
                      "then `factory run unblock` and retry with --decision <n>"])


def _plan_loops(loops: dict) -> int:
    """Replans plus check amendments without a user decision: what `autonomy.max_replans` caps."""
    return (loops.get("replan", 0) - loops.get("replan_approved", 0)
            + loops.get("amend", 0) - loops.get("amend_approved", 0))


def cmd_replan(args) -> int:
    """The one legit way to change acs.json/checks/figma/diagnosis after the plan gate: back to plan, lock cleared."""
    run = run_load(args.run)
    if run.get("kind") not in ("ticket", "review", None):
        raise FactoryError(f"run replan is for ticket and review runs, not {run.get('kind')}")
    if run["phase"] in ("intake", "plan", "done"):
        raise FactoryError(f"run {run['id']} is in phase {run['phase']}; nothing to replan")
    loops = run.setdefault("loops", {"build": 0, "review": 0})
    decision = _use_decision(run, getattr(args, "decision", None), "replan")
    if decision is None:
        cap = integrity.locked_autonomy(run).get("max_replans", 2)
        if _plan_loops(loops) + 1 > cap:
            _cap_block(run, f"max_replans ({cap}) reached without a user decision", run["phase"])
    loops["replan"] = loops.get("replan", 0) + 1
    if decision is not None:
        loops["replan_approved"] = loops.get("replan_approved", 0) + 1
    changed = integrity.changed_files(run) if isinstance(run.get("plan_lock"), dict) else []
    integrity.remember_plan(run)  # the next plan lock fills `changed` of this event with what the replan changed
    event(run, "replan", reason=args.reason, **{"from": run["phase"]}, changed=changed, loop=loops["replan"],
          decision=getattr(args, "decision", None))
    event(run, "loop", replan=loops["replan"])
    run.pop("plan_lock", None)
    gate._set_phase(run, "plan")
    run_save(run)
    _emit(args, {"phase": "plan", "replan": loops["replan"]},
          f"phase plan (replan {loops['replan']}); change the plan files, then `factory gate {run['id']} plan`")
    return 0


AMEND_KEYS = {"timing": ("timeout_s", "wait_for", "wait_until", "expect_timeout_ms"),
              "selector": ("timeout_s", "wait_for", "wait_until", "expect_timeout_ms")}


def _amend_errors(run: dict, cid: str, kind: str) -> tuple[list[str], list[str]]:
    """(refusals, changed check fields): what differs from the locked plan must stay inside the amend kind."""
    lock = run["plan_lock"]
    old_checks = lock.get("checks")
    if not isinstance(old_checks, dict) or lock.get("acs_rest") is None:
        return [f"the plan lock of {run['id']} predates amend-check support: use `factory run replan`"], []
    if cid not in old_checks:
        return [f"{cid} is not a check of the locked plan"], []
    new_checks, new_rest = integrity.acs_snapshot(run["id"])
    errors: list[str] = []
    if new_rest != lock["acs_rest"]:
        errors.append("acs.json changed outside `checks` (ACs, ids, schema): amend-check only changes one check")
    if set(new_checks) != set(old_checks):
        errors.append(f"checks added or removed: {sorted(set(new_checks) ^ set(old_checks))}")
    for other in sorted(set(new_checks) & set(old_checks) - {cid}):
        if new_checks[other] != old_checks[other]:
            errors.append(f"{other} changed: amend-check changes only {cid}")
    old, new = old_checks[cid], new_checks.get(cid, {})
    fields = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    bad = [f for f in fields if f not in AMEND_KEYS[kind]]
    if kind == "selector":  # the spec file (and its helpers) the check reads may change; it is a plan file under checks/
        owned = {rel for _, rel in integrity.check_inputs(run["id"], old)} | {rel for _, rel in integrity.check_inputs(run["id"], new)}
        stray = [f for f in integrity.changed_files(run) if f != "acs.json" and not (f in owned and f.startswith("checks/"))]
        readers: dict[str, list[str]] = {}  # file -> other checks that read it (old or new definition)
        for other in sorted((set(old_checks) | set(new_checks)) - {cid}):
            for chk in (old_checks.get(other), new_checks.get(other)):
                if isinstance(chk, dict):
                    for _, rel in integrity.check_inputs(run["id"], chk):
                        readers.setdefault(rel, [])
                        if other not in readers[rel]:
                            readers[rel].append(other)
        shared = [f for f in integrity.changed_files(run) if f in owned and f.startswith("checks/") and readers.get(f)]
        if shared:
            errors.append(f"{cid}: changed file(s) also read by other checks: "
                          + "; ".join(f"{f} (read by {', '.join(readers[f])})" for f in shared)
                          + "; their evidence would stay valid against a changed spec: use `factory run replan`")
    else:
        stray = [f for f in integrity.changed_files(run) if f != "acs.json"]
    if bad:
        errors.append(f"{cid}: field(s) {bad} cannot be amended as {kind} (allowed: {', '.join(AMEND_KEYS[kind])}"
                      + (" and its spec files under checks/" if kind == "selector" else "") + "); use `factory run replan`")
    if stray:
        errors.append(f"plan files changed that {kind} amend of {cid} does not cover: {stray}")
    if not fields and not integrity.changed_files(run):
        errors.append(f"{cid}: nothing changed against the locked plan (edit the check first)")
    return errors, fields


def cmd_amend_check(args) -> int:
    """After the plan lock, fix only how a check waits or where it looks (timing/selector) without a replan."""
    said = (getattr(args, "user_said", None) or "").strip()
    reason = (args.reason or "").strip()
    if not reason:
        raise FactoryError("amend-check needs --reason \"<why>\"")
    if args.by == "user" and not said:
        raise FactoryError("--by user needs --user-said \"<gist of the user's chat message>\"")
    out: dict = {}

    def mutate(run: dict) -> None:
        ensure_owner(run)
        if run.get("kind") not in ("ticket", "review", None):
            raise FactoryError(f"run amend-check is for ticket and review runs, not {run.get('kind')}")
        if not isinstance(run.get("plan_lock"), dict):
            raise FactoryError(f"run {run['id']} has no plan lock: edit the check freely, then pass `factory gate {run['id']} plan`")
        if integrity.workspace_error(run):  # a relock would absorb workspace.json edits (quality, autonomy, hot_reload)
            raise FactoryError(f"amend-check refused: workspace changed since plan lock: replan "
                               f"(`factory run replan {run['id']} \"<reason>\"`)")
        errors, fields = _amend_errors(run, args.check, args.kind)
        if errors:
            raise FactoryError("amend-check refused:\n  " + "\n  ".join(errors))
        autonomy = integrity.locked_autonomy(run)
        free = args.kind == "timing" and autonomy.get("allow_timing_amends") is True
        loops = run.setdefault("loops", {"build": 0, "review": 0})
        if not free:
            if args.by != "user":
                cap = autonomy.get("max_replans", 2)
                if _plan_loops(loops) + 1 > cap:
                    raise FactoryError(f"max_replans ({cap}) reached: amend-check counts against it like a replan without "
                                       "a user decision; ask the user and retry with --by user --user-said")
            loops["amend"] = loops.get("amend", 0) + 1  # a check-only amendment is not a replan in the metrics
            if args.by == "user":
                loops["amend_approved"] = loops.get("amend_approved", 0) + 1
            event(run, "loop", amend=loops["amend"])
        event(run, "amend_check", check=args.check, amend=args.kind, reason=reason, fields=fields, by=args.by,
              user_said=said or None, counted=not free, phase=run["phase"])
        integrity.lock_plan(run, f"amend-check {args.check}", keep_seq=True, keep_workspace=True)
        out.update(check=args.check, kind=args.kind, fields=fields, counted=not free, phase=run["phase"])

    run_update(args.run, mutate)
    _emit(args, out, f"{args.check}: {args.kind} amend recorded ({', '.join(out['fields']) or 'files'}); plan relocked, "
                     f"phase {out['phase']}" + ("" if out["counted"] else " (not counted as a replan)")
                     + f"; re-run `factory prove {args.run} --check {args.check}`")
    return 0


REWIND_TO = ("build", "prove")


def cmd_rewind(args) -> int:
    """Back to build or prove after the worktree moved (rebase/merge, fixes) past the proof."""
    run = run_load(args.run)
    phases, _ = gate.machine(run)
    if run.get("kind") == "epic" or args.phase not in phases:
        raise FactoryError(f"{run.get('kind')} runs cannot rewind to {args.phase}")
    current = run["phase"]
    if current == "blocked":
        raise FactoryError("run is blocked: resolve the blockers first (factory run unblock)")
    if current not in phases or phases.index(current) <= phases.index(args.phase):
        raise FactoryError(f"run {run['id']} is in phase {current}; rewind goes back to an earlier phase")
    loops = run.setdefault("loops", {"build": 0, "review": 0})
    decision = _use_decision(run, getattr(args, "decision", None), "rewind")
    key = args.phase  # the loop of the phase the rewind goes back to (build or prove)
    if decision is None:  # blocks past max_build_loops
        limit = integrity.locked_autonomy(run).get("max_build_loops", 3)
        if loops.get(key, 0) - loops.get(key + "_approved", 0) + 1 > limit:
            _cap_block(run, f"max_build_loops ({limit}) reached by rewinds without a user decision", current)
    loops[key] = loops.get(key, 0) + 1
    if decision is not None:  # counted, but not against the cap
        loops[key + "_approved"] = loops.get(key + "_approved", 0) + 1
    event(run, "loop", **{key: loops[key]})
    event(run, "rewind", reason=args.reason, **{"from": current, "to": args.phase}, loop=loops[key],
          decision=getattr(args, "decision", None))
    gate._set_phase(run, args.phase)
    run_save(run)
    _emit(args, {"phase": args.phase, "loops": loops}, f"phase {current} -> {args.phase} ({key} loop {loops[key]})")
    return 0


def cmd_login_user(args) -> int:
    """Per-run login e-mail for the runtime collectors; allowed until `gate prove` has passed."""
    def mutate(run: dict) -> None:
        if any(e["kind"] == "gate_pass" and e["data"].get("phase") == "prove" for e in run.get("events", [])):
            raise FactoryError("gate prove already passed; login-user can no longer change (rewind to prove first)")
        previous = run.get("login_user")
        run["login_user"] = args.email
        event(run, "login_user", user=args.email, previous=previous, phase=run["phase"])

    run = run_update(args.run, mutate)
    _emit(args, {"login_user": args.email},
          f"login user {args.email}; then `factory env login {run['workspace']} <repo> --user {args.email}`")
    return 0


def cmd_human(args) -> int:
    run_update(args.run, lambda run: event(run, "human", note=args.note, phase=run["phase"], type=args.type))
    _emit(args, {"ok": True, "type": args.type}, f"human intervention recorded ({args.type})")
    return 0


# ---------- claim / log ----------

def cmd_session(args) -> int:
    """Register a Claude session id for metrics (default: this session's). Idempotent; claims no ownership."""
    sid = args.session or session_id()
    if not sid:
        raise FactoryError("no session id given and none in the environment (CLAUDE_CODE_SESSION_ID); pass <session-id>")
    out: dict = {}

    def mutate(run: dict) -> None:
        out["added"] = register_session(run, sid)

    run_update(args.run, mutate)
    _emit(args, {"run": args.run, "session": sid, "added": out["added"]},
          f"{args.run}: session {session_short(sid)} " + ("registered" if out["added"] else "already registered"))
    return 0


def cmd_claim(args) -> int:
    sid = session_id()
    if not sid:
        raise FactoryError("no session id in the environment (CLAUDE_CODE_SESSION_ID); claim from a Claude session")
    out = []
    for rid in args.runs:
        def mutate(run: dict, rid=rid) -> None:
            owner = current_owner(run)
            if owner and owner["session"] == sid:
                out.append({"run": rid, "owner": sid, "already": True})
                return
            if owner and claim_is_recent(owner) and not args.takeover:
                raise FactoryError(f"run {rid} is claimed by session {session_short(owner['session'])} "
                                   f"(since {owner.get('claimed_at')}); use --takeover to take it over")
            run.setdefault("sessions", []).append({"session": sid, "claimed_at": now(), "pid": os.getpid()})
            event(run, "claim", session=sid, takeover=bool(args.takeover and owner),
                  previous=owner["session"] if owner else None)
            out.append({"run": rid, "owner": sid, "takeover": bool(args.takeover and owner)})
        run_update(rid, mutate)
    lines = []
    for o in out:
        what = ("already owned by this session" if o.get("already")
                else f"claimed by {session_short(sid)}" + (" (takeover)" if o.get("takeover") else ""))
        lines.append(f"{o['run']}: {what}")
    _emit(args, out, "\n".join(lines))
    return 0


def _local_time(at) -> str:
    try:
        d = _dt.datetime.fromisoformat(str(at))
    except ValueError:
        return str(at)[:8].ljust(8)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone().strftime("%H:%M:%S")


def _kv(data: dict) -> str:
    parts = []
    for k, v in data.items():
        if v is None or v == [] or v == "":
            continue
        text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        parts.append(f"{k}={text if len(text) <= 80 else text[:77] + '...'}")
    return " ".join(parts)


def event_summary(e: dict) -> str:
    kind, d = e.get("kind"), e.get("data") or {}
    if kind == "gate_pass":
        return f"gate {d.get('phase') or d.get('gate')} passed"
    if kind == "gate_fail":
        reasons = d.get("reasons") or []
        more = f" (+{len(reasons) - 1} more)" if len(reasons) > 1 else ""
        return f"gate {d.get('phase') or d.get('gate')} failed: {reasons[0] if reasons else '?'}{more}"
    if kind == "evidence":
        return f"{d.get('check')} {d.get('status')}"
    if kind == "review":
        return f"{d.get('review_kind')} review round {d.get('round')}: {d.get('verdict')} ({d.get('findings')} findings)"
    if kind == "human":
        return f"human intervention [{d.get('type') or 'other'}]: {d.get('note')}"
    if kind == "block":
        return f"blocker #{d.get('n')} [{d.get('blocker_kind')}] {d.get('text')}"
    if kind == "phase":
        return f"phase {d.get('from')} -> {d.get('to')}"
    if kind == "replan":
        return f"replan from {d.get('from')}: {d.get('reason')}"
    if kind == "rewind":
        return f"rewind {d.get('from')} -> {d.get('to')}: {d.get('reason')}"
    if kind == "close":
        return f"closed from {d.get('from')}: {d.get('reason')}"
    if kind == "env_only":
        return f"{d.get('repo')}: env_only {'on' if d.get('on') else 'off'} (phase {d.get('phase')})"
    if kind == "target_ticket":
        return f"{d.get('target')}: MR ticket {d.get('ticket')} (phase {d.get('phase')})"
    if kind == "retype":
        return f"retyped to {d.get('to')} (phase {d.get('phase')})"
    if kind == "claim":
        return f"claimed by {session_short(d.get('session'))}" + (" (takeover)" if d.get("takeover") else "")
    return _kv(d)


DELETABLE_PHASES = ("done", "report", "blocked", "closed")


def _archive_run(rid: str) -> Path:
    """Zip the run folder into FACTORY_HOME/archive/runs/ and verify the archive before anything is removed."""
    src = run_dir(rid)
    dest_dir = factory_home() / "archive" / "runs"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = dest_dir / f"{rid}-{stamp}.zip"
    files = [p for p in src.rglob("*") if p.is_file()]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, Path(rid) / p.relative_to(src))
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        count = len(zf.namelist())
    if bad or count != len(files):
        raise FactoryError(f"archive of {rid} failed verification ({bad or f'{count}/{len(files)} files'}); nothing removed")
    return zip_path


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # read-only files on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def cmd_delete(args) -> int:
    if not args.yes:
        raise FactoryError("run delete is permanent (an archive zip is kept); pass --yes to confirm")
    ids = list(dict.fromkeys(args.runs))
    runs = {rid: run_load(rid) for rid in ids}
    problems = []
    for rid, run in runs.items():
        ensure_owner(run)
        if run.get("phase") not in DELETABLE_PHASES and not args.force:
            problems.append(f"{rid}: phase {run.get('phase')} is not finished ({', '.join(DELETABLE_PHASES)}); "
                            f"add --force to delete it anyway")
        left = [c for c in run.get("children") or [] if c not in runs and run_dir(c).is_dir()]
        if left:
            problems.append(f"{rid}: children would be orphaned: {', '.join(left)}; delete them in the same call")
    if problems:
        raise FactoryError("refused, nothing deleted:\n  " + "\n  ".join(problems))
    out = []
    for rid, run in runs.items():
        zip_path = _archive_run(rid)
        _rmtree(run_dir(rid))
        worktrees = sorted({str(r.get("worktree")) for r in (run.get("repos") or {}).values()
                            if isinstance(r, dict) and r.get("worktree")})
        out.append({"run": rid, "archive": str(zip_path), "worktrees_left": worktrees})
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
        return 0
    for o in out:
        print(f"deleted {o['run']} (archive: {o['archive']})")
        for wt in o["worktrees_left"]:
            print(f"  worktree kept: {wt} (remove with `git worktree remove` when no longer needed)")
    return 0


def cmd_log(args) -> int:
    run = run_load(args.run)
    events = list(run.get("events") or [])
    if args.since:
        try:
            since = _dt.datetime.fromisoformat(args.since)
        except ValueError as err:
            raise FactoryError(f"--since must be an ISO date/time: {err}") from err
        if since.tzinfo is None:
            since = since.astimezone()  # naive = local time

        def after(e) -> bool:
            try:
                at = _dt.datetime.fromisoformat(str(e.get("at")))
            except ValueError:
                return True
            return (at if at.tzinfo else at.replace(tzinfo=_dt.timezone.utc)) >= since
        events = [e for e in events if after(e)]
    if args.tail:
        events = events[-args.tail:]
    if getattr(args, "json", False):
        print(json.dumps(events, indent=2, ensure_ascii=False))
        return 0
    for e in events:
        print(f"{_local_time(e.get('at'))}  {e.get('kind')}  {event_summary(e)}".rstrip())
    if not events:
        print("no events")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("run", help="manage runs")
    rs = parser.add_subparsers(dest="run_cmd", required=True)
    p = rs.add_parser("start")
    p.add_argument("workspace")
    p.add_argument("tickets", nargs="+")
    p.add_argument("--epic")
    p.add_argument("--risk", choices=["normal", "high"], default="normal")
    p.add_argument("--bug", action="append", help="ticket key that is a bug (repeatable)")
    p.add_argument("--hotfix", action="store_true", help="hotfix run: targets = [ship.hotfix.base, *mirror]; tickets are bugs")
    p.add_argument("--feature", action="store_true", help="with --hotfix: tickets stay features")
    p.add_argument("--login-user", help="e-mail the runtime checks log in as (default: workspace test user)")
    p.set_defaults(func=cmd_start)
    p = rs.add_parser("status")
    p.add_argument("run")
    p.set_defaults(func=cmd_status)
    p = rs.add_parser("list")
    p.add_argument("--all", action="store_true", help="include closed runs")
    p.set_defaults(func=cmd_list)
    p = rs.add_parser("close", help="stop a run for good (phase closed); keeps the artifacts, deletable without --force")
    p.add_argument("run")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_close)
    p = rs.add_parser("retype", help="switch a run to bug, feature or hotfix keeping its artifacts (before gate build)")
    p.add_argument("run")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bug", action="store_true")
    g.add_argument("--feature", action="store_true")
    g.add_argument("--hotfix", action="store_true")
    p.set_defaults(func=cmd_retype)
    p = rs.add_parser("verify-human", help="record the user's confirmation of a human check as pass evidence")
    p.add_argument("run")
    p.add_argument("check")
    p.add_argument("--note", required=True, help="what the user confirmed")
    p.add_argument("--by", choices=["agent", "user"], default="agent", help="must be user")
    p.add_argument("--user-said", help="gist of the user's chat message")
    p.add_argument("--repo", help="repo alias whose worktree HEAD the evidence is bound to")
    p.set_defaults(func=cmd_verify_human)
    p = rs.add_parser("branch")
    p.add_argument("run")
    p.add_argument("repo")
    p.add_argument("--base")
    p.add_argument("--mr", type=int, help="review run: detached worktree at the saved head of this MR iid")
    p.add_argument("--name")
    p.add_argument("--from-run", help="child run id whose branch (same repo) this branch stacks on")
    p.add_argument("--for-target", help="record a per-target source branch (branches[target]) instead of the main branch")
    p.add_argument("--env-only", action="store_true",
                   help="the worktree only serves the env (no code): build/prove/review/mirror/ship skip this repo")
    p.set_defaults(func=cmd_branch)
    p = rs.add_parser("env-only", help="mark a registered repo as env_only (serves the env, carries no code); --off undoes")
    p.add_argument("run")
    p.add_argument("repo")
    p.add_argument("--off", action="store_true", help="make the repo a code repo again")
    p.set_defaults(func=cmd_env_only)
    p = rs.add_parser("target-ticket", help="hotfix: title the MR of a target with this ticket (default: the first)")
    p.add_argument("run")
    p.add_argument("target")
    p.add_argument("key")
    p.set_defaults(func=cmd_target_ticket)
    p = rs.add_parser("mirror", help="hotfix: cherry-pick the base-target commits into a mirror target worktree")
    p.add_argument("run")
    p.add_argument("repo")
    p.add_argument("--to", required=True)
    p.add_argument("--name")
    p.set_defaults(func=cmd_mirror)
    p = rs.add_parser("mirror-diff", help="hotfix, read-only: compare the net change of the base branch with each mirror branch "
                                          "(exit 0 equal, 1 divergent)")
    p.add_argument("run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_mirror_diff)
    p = rs.add_parser("decide")
    p.add_argument("run")
    p.add_argument("text")
    p.add_argument("--by", choices=["agent", "user"], default="agent",
                   help="user: only for a decision the user actually stated in chat (needs --user-said)")
    p.add_argument("--user-said", help="gist of the user's chat message behind a --by user decision")
    p.add_argument("--visual", choices=["yes", "no"],
                   help="the user's answer to the visual-evidence question (screenshot checks; DESIGN section 12)")
    p.set_defaults(func=cmd_decide)
    p = rs.add_parser("block")
    p.add_argument("run")
    p.add_argument("kind", type=blocker_kind, metavar="KIND", help="one of: " + ", ".join(BLOCKER_KINDS))
    p.add_argument("text")
    p.set_defaults(func=cmd_block)
    p = rs.add_parser("unblock")
    p.add_argument("run")
    p.add_argument("n", type=int)
    p.set_defaults(func=cmd_unblock)
    p = rs.add_parser("human", help="record a user message that steers the run")
    p.add_argument("run")
    p.add_argument("note")
    p.add_argument("--type", choices=HUMAN_TYPES, default="other",
                   help="decision (a product/scope call), approval (go-ahead, Gate S), env-note (data, user, env hint), "
                        "correction (the user fixed the agent's course), other (default)")
    p.set_defaults(func=cmd_human)
    p = rs.add_parser("login-user", help="set the e-mail the runtime checks log in as (before gate prove passes)")
    p.add_argument("run")
    p.add_argument("email")
    p.set_defaults(func=cmd_login_user)
    p = rs.add_parser("session", help="register a Claude session id on the run so metrics find its transcript (idempotent, no ownership)")
    p.add_argument("run")
    p.add_argument("session", nargs="?", help="session id (default: CLAUDE_CODE_SESSION_ID)")
    p.set_defaults(func=cmd_session)

    p = rs.add_parser("claim", help="record this Claude session as the owner of run(s) (parallel sessions)")
    p.add_argument("runs", nargs="+", metavar="run")
    p.add_argument("--takeover", action="store_true", help="take over a run claimed by another recent session")
    p.set_defaults(func=cmd_claim)
    p = rs.add_parser("delete", help="archive run(s) to FACTORY_HOME/archive/runs as zip, then remove the run folder")
    p.add_argument("runs", nargs="+", metavar="run")
    p.add_argument("--yes", action="store_true", help="confirm the deletion")
    p.add_argument("--force", action="store_true", help="also delete runs that are not done/report/blocked")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_delete)
    p = rs.add_parser("log", help="readable timeline of run events")
    p.add_argument("run")
    p.add_argument("--tail", type=int, help="only the last N events")
    p.add_argument("--since", help="ISO date/time (naive = local)")
    p.set_defaults(func=cmd_log)
    p = rs.add_parser("replan", help="back to plan to change acs.json/checks/figma/diagnosis (clears the plan lock)")
    p.add_argument("run")
    p.add_argument("reason")
    p.add_argument("--decision", type=int, help="user decision # (run decide --by user, this phase); else counts "
                   "against autonomy.max_replans")
    p.set_defaults(func=cmd_replan)
    p = rs.add_parser("amend-check", help="after the plan lock: change only timing or selectors of one check, relock the plan")
    p.add_argument("run")
    p.add_argument("check")
    p.add_argument("--kind", required=True, choices=["timing", "selector"])
    p.add_argument("--reason", required=True)
    p.add_argument("--by", choices=["agent", "user"], default="agent")
    p.add_argument("--user-said", help="gist of the user's chat message behind a --by user amend")
    p.set_defaults(func=cmd_amend_check)
    p = rs.add_parser("rewind", help="back to build or prove after the worktree moved past the proof")
    p.add_argument("run")
    p.add_argument("phase", choices=REWIND_TO)
    p.add_argument("reason")
    p.add_argument("--decision", type=int, help="user decision # (run decide --by user, this phase); else counts "
                   "into the loop of the phase it goes back to (loops.build or loops.prove) and blocks past autonomy.max_build_loops")
    p.set_defaults(func=cmd_rewind)
