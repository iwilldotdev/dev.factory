"""`factory ship plan|verify|update-description`: delivery plan (schemaVersion 2 intents) from evidence, and readback
verification against `change.v1` / `pipeline.v1` contracts."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

from .. import contracts, design, fetches, mirrordiff, shots
from .workspace import TICKET_MODES
from ..core import (
    FactoryError,
    GateFailed,
    abs_path,
    code_repos,
    event,
    load_evidence,
    read_json,
    repo_conf,
    repo_path_for_run,
    target_branch,
    target_worktree,
    run_dir,
    run_load,
    run_save,
    git,
    workspace_load,
    write_json,
)

PLAN_SCHEMA = 2
CHANGE_ACTIONS = ("push", "change_request", "set_draft", "readback", "pipeline_watch")


def _ordered_targets(run: dict, workspace: dict) -> list[str]:
    if run.get("hotfix"):  # base target first, then the mirrors
        return list(run.get("targets") or [])
    ship_targets = (workspace.get("ship") or {}).get("targets") or []
    run_targets = run.get("targets") or []
    if ship_targets and run_targets:
        return [t for t in ship_targets if t in run_targets]
    return list(run_targets or ship_targets)


def _ordered_repos(run: dict, workspace: dict) -> list[str]:
    aliases = []
    for key in code_repos(run):  # env_only repos only serve the env: no push, no change
        try:
            aliases.append(repo_conf(workspace, key))
        except FactoryError:
            aliases.append({"alias": key, "order": 999})
    aliases.sort(key=lambda r: (r.get("order", 999), r["alias"]))
    return [r["alias"] for r in aliases]


def source_branch(run: dict, alias: str, target: str, targets: list[str]) -> str:
    rec = run["repos"][alias]
    per_target = rec.get("branches") or {}
    if target in per_target:
        return per_target[target]
    if target == rec.get("base") or len(targets) <= 1:
        return rec["branch"]
    return f"{rec['branch']}-{target}"


TITLE_FIELDS = ("key", "summary", "repo", "target")
DEFAULT_TITLE = "[{key}] - {summary}"
LOCAL_LINK_MARKERS = ("](evidence/", "](C:", "](./", "file://")


def _ticket_summary(run: dict, ticket: dict) -> str:
    """Summary from run.json, else the title of the ticket contract; empty when neither is available."""
    if ticket.get("summary"):
        return str(ticket["summary"])
    from .. import contracts
    if not ticket.get("key"):
        return ""
    doc, problems = contracts.load_contract(run["id"], "ticket", str(ticket["key"]))
    return str(doc.get("title") or "") if doc is not None and not problems else ""


def title_format(ship: dict) -> tuple[str, tuple[str, ...]]:
    """(format, allowed placeholders): `ship.title_format`, else the default."""
    return str(ship.get("title_format") or DEFAULT_TITLE), TITLE_FIELDS


def validate_title_format(fmt: str, allowed: tuple[str, ...]) -> None:
    try:
        names = [f for _, f, _, _ in string.Formatter().parse(fmt) if f is not None]
    except ValueError as err:
        raise FactoryError(f"ship.title_format {fmt!r} is not a valid format string ({err})") from err
    bad = sorted({n for n in names if n.split(".")[0].split("[")[0] not in allowed})
    if bad:
        raise FactoryError(f"ship.title_format {fmt!r}: unknown placeholder(s) {', '.join('{' + b + '}' for b in bad)}; "
                           f"allowed: {', '.join('{' + a + '}' for a in allowed)}")


def _title(fmt: str, run: dict, alias: str, target: str) -> str:
    tickets = run.get("tickets") or [{}]
    wanted = ((run.get("hotfix") or {}).get("tickets") or {}).get(target)
    first = next((t for t in tickets if wanted and t.get("key") == wanted), tickets[0])
    key = first.get("key", run["id"])
    summary = _ticket_summary(run, first)
    out = fmt.format(key=key, summary=summary, repo=alias, target=target).strip()
    if not summary:  # no summary known: never emit a dangling separator
        out = out.rstrip(" -").rstrip()
    return out


def reviewers(ship: dict) -> list[str]:
    """SCM user ids from `ship.reviewers`; an entry without `scm_user` (a ticket identity alone) is an error."""
    out = []
    for i, item in enumerate(ship.get("reviewers") or []):
        if isinstance(item, str):
            out.append(item)
            continue
        user = item.get("scm_user") if isinstance(item, dict) else None
        if not user:
            who = "?"
            if isinstance(item, dict):
                who = item.get("ticket_user") or item.get("name") or item.get("email") or "?"
            raise FactoryError(f"ship.reviewers[{i}] ({who}): missing scm_user; set it in workspace.json "
                               "(a ticket identity alone cannot be assigned as a change reviewer)")
        out.append(str(user))
    return out


def change_id(repo: str, target: str) -> str:
    """Entity id of the change of a (repo, target): `contracts/change/<id>.json`, `raw/change/<id>.json`."""
    return f"{repo}-{target}"


def _rel(kind: str, ident: str, ext: str = "json") -> str:
    return f"{kind}/{contracts.safe_id(ident)}.{ext}"


def readback_path(repo: str, target: str) -> str:
    """Run-relative path of the change contract a readback is verified from."""
    return "contracts/" + _rel("change", change_id(repo, target))


def raw_readback_path(repo: str, target: str) -> str:
    return "raw/" + _rel("change", change_id(repo, target))


def integrity_errors(run_id: str, run: dict, workspace: dict) -> list[str]:
    """Plan lock + the prove-gate criteria against the current HEADs (a commit after prove fails here)."""
    from . import gate
    return gate.proof_errors(run_id, run, workspace)


def review_errors(run_id: str, run: dict, workspace: dict) -> list[str]:
    """Latest code + qa reviews still APPROVE, digest-intact, bound to the current SHAs and plan lock."""
    if run.get("kind", "ticket") != "ticket":
        return []
    from . import gate
    plain, changes = gate.review_status(run_id, run, workspace)
    return plain + changes


def branch_head(run: dict, workspace: dict, alias: str, target: str, branch: str) -> str | None:
    """Local tip of the change source branch (refs are shared by the run's worktrees)."""
    wt = target_worktree(run, workspace, alias, target)
    if wt is None or not Path(wt).is_dir():
        return None
    return git(wt, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False) or None


def change_contracts(run_id: str) -> tuple[list[dict], list[str]]:
    """Every fresh, valid `contracts/change/*.json` of the run, plus a line per unusable one (stale, invalid)."""
    return contracts.load_all(run_id, "change")


def _names_ticket(doc: dict, key: str) -> bool:
    """The ticket key appears in the change title, description or source branch (whole token, case-insensitive)."""
    rx = re.compile(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", re.IGNORECASE)
    return any(rx.search(str(doc.get(f) or "")) for f in ("title", "description_md", "source_branch"))


def check_dependencies(run: dict, internal: set[str] | None = None) -> list[str]:
    errors = []
    in_run = {t["key"] for t in run.get("tickets", [])} | set(internal or ())
    changes, bad = change_contracts(run["id"])
    merged = [c for c in changes if c.get("state") == "merged"]
    for t in run.get("tickets", []):
        for dep in t.get("depends_on") or []:
            if dep in in_run:
                continue
            if not any(_names_ticket(c, dep) for c in merged):
                note = f" ({len(bad)} unusable change contract(s): re-ingest)" if bad else ""
                errors.append(f"{t['key']} depends on {dep}: no fresh contracts/change/*.json with state merged naming it{note}")
    return errors


def behind_count(repo: Path, target: str) -> int:
    ref = f"origin/{target}"
    if git(repo, "rev-parse", "--verify", "--quiet", ref, check=False) == "":
        raise FactoryError(f"{repo}: {ref} not found; run `git fetch origin` there first")
    return int(git(repo, "rev-list", "--count", f"HEAD..{ref}"))


STATUS_LABEL = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "missing": "MISSING", "human": "HUMAN"}
TAIL_KEYS = ("key_lines", "output_tail", "green_tail", "body_tail", "output")
MAX_LINES, MAX_WIDTH = 8, 200


def _key_lines(ev: dict) -> list[str]:
    """Evidence `details.key_lines`, else the first lines of its recorded output, else the summary. No paths or links."""
    details = ev.get("details") if isinstance(ev.get("details"), dict) else {}
    lines: list[str] = []
    for key in TAIL_KEYS:
        val = details.get(key)
        if isinstance(val, list):
            lines = [str(v) for v in val]
        elif isinstance(val, str):
            lines = val.splitlines()
        lines = [ln.rstrip() for ln in lines if ln.strip()]
        if lines:
            break
    if not lines and ev.get("summary"):
        lines = [str(ev["summary"])]
    return [ln[:MAX_WIDTH].replace("```", "'''") for ln in lines[:MAX_LINES]]


def _ac_table(run_id: str, alias: str, acs: dict, target: str | None = None) -> tuple[str, list[dict], list[str]]:
    """ACs x checks table (PASS/FAIL/HUMAN), the human checks, and one <details> block per check."""
    checks = {c["id"]: c for c in acs.get("checks", [])}
    rows = ["| AC | Check | Status |", "|---|---|---|"]
    humans: list[dict] = []
    blocks: list[str] = []
    seen: set[str] = set()
    for ac in acs.get("acs", []):
        ids = [c for c in ac.get("checks", []) if checks.get(c, {}).get("repo") in (alias, None)]
        if not ids:
            continue
        text = ac.get("text", "").replace("|", "\\|").replace("\n", " ")
        for cid in ids:
            chk = checks[cid]
            if chk.get("type") == "human":
                status = "human"
                if chk not in humans:
                    humans.append(chk)
            else:
                ev = load_evidence(run_id, f"{cid}@{target}" if target else cid)
                status = (ev or {}).get("status", "missing")
                if ev and cid not in seen:
                    seen.add(cid)
                    body = "\n".join(_key_lines(ev)) or "(no output recorded)"
                    blocks.append(f"<details><summary>{cid} ({chk.get('type')}): {STATUS_LABEL.get(status, status)}"
                                  f"</summary>\n\n```\n{body}\n```\n\n</details>")
            rows.append(f"| {ac['id']}: {text} | {cid} | {STATUS_LABEL.get(status, str(status).upper())} |")
    return "\n".join(rows), humans, blocks


def _baseline_summary(run_id: str, alias: str) -> list[str]:
    p = run_dir(run_id) / "baseline" / f"{alias}.json"
    if not p.is_file():
        return ["Baseline: not recorded."]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["Baseline: unreadable."]
    named: dict[str, list] = {}
    if isinstance(data, dict):
        cmds = data.get("commands") if isinstance(data.get("commands"), dict) else None
        if cmds is not None:
            for k, v in cmds.items():
                if isinstance(v, dict) and isinstance(v.get("failures"), list):
                    named[k] = v["failures"]
        else:
            src = data.get("failures") if isinstance(data.get("failures"), dict) else data
            named = {k: v for k, v in src.items() if isinstance(v, list)}
    detail = ", ".join(f"{k}: {len(v)}" for k, v in named.items()) or "recorded"
    out = [f"Baseline (merge-base) pre-existing quality failures: {detail}. This change adds none (prove gate)."]
    for k, v in named.items():
        if v:
            shown = "; ".join(str(x).replace("\n", " ")[:160] for x in v[:10])
            out.append(f"- `{k}` already failing at the merge-base: {shown}" + (f" (+{len(v) - 10} more)" if len(v) > 10 else ""))
    return out


def _quality_lines(run: dict, alias: str, targets: list[str]) -> list[str]:
    """Quality delta evidence: QUALITY-<repo>, or QUALITY-<repo>@<target> per target on a hotfix run."""
    from . import gate
    qid = gate.quality_id(alias)

    def line(ev: dict | None, label: str) -> str:
        if not ev:
            return f"- {label}: not recorded"
        status = STATUS_LABEL.get(ev.get("status"), str(ev.get("status")).upper())
        return f"- {label}: {status} - {ev.get('summary', '')}".rstrip(" -")

    if run.get("hotfix"):
        return [line(load_evidence(run["id"], f"{qid}@{t}") or load_evidence(run["id"], qid), f"{qid}@{t}") for t in targets]
    return [line(load_evidence(run["id"], qid), qid)]


FOLLOWUP_CAP = 20


def _resolved_findings(history: list[dict]) -> set[tuple[int, str]]:
    """`(index in history, finding id)` pairs that a later round of the same kind marks fixed in its `resolved` list.

    Auditors reuse ids (round 1 F1 is not round 2 F1), so an entry must be unambiguous: `"<round>:<id>"` (`"1:F1"`)
    names that round, a bare id names only the round immediately before the one that lists it. A resolved finding also
    closes the same id with the same text in EARLIER rounds (one finding carried from round to round), never a same-id
    finding with other text."""
    fixed: set[tuple[int, str]] = set()
    for j, later in enumerate(history):
        for entry in later.get("resolved") or []:
            if not isinstance(entry, (str, int)):
                continue
            text = str(entry).strip()
            m = re.match(r"^(\d+):(.+)$", text)
            if m:
                rounds = [k for k in range(j) if history[k].get("round") == int(m.group(1))]
                fixed.update((k, m.group(2).strip()) for k in rounds)
            elif j > 0:
                fixed.add((j - 1, text))

    def texts(k: int, fid: str) -> set[str]:
        return {" ".join(str(f.get("text", "")).split()) for f in history[k].get("findings") or [] if str(f.get("id")) == fid}

    for k, fid in list(fixed):
        mine = texts(k, fid)
        fixed.update((e, fid) for e in range(k) if mine & texts(e, fid))
    return fixed


def _followup_lines(run_id: str) -> list[str]:
    """Open minor findings of every code and qa review round, so they have a destination.

    A minor finding stays open until a later round of the same kind names it in its top-level `resolved` list, as
    `"<round>:<id>"` or as a bare id that means the round right before (absent means still open); the same finding
    repeated in later rounds (same file and text) appears once.
    """
    from . import gate
    items: list[str] = []
    for kind in ("code", "qa"):
        history = gate.review_history(run_id, kind)
        fixed = _resolved_findings(history)
        seen: set[tuple[str, str]] = set()
        for i, rec in enumerate(history):
            for f in rec.get("findings") or []:
                if f.get("severity") != "minor" or f.get("resolved") or (i, str(f.get("id"))) in fixed:
                    continue
                text = " ".join(str(f.get("text", "")).split())
                key = (str(f.get("file") or ""), text)
                if key in seen:
                    continue
                seen.add(key)
                where = f" `{f['file']}`" if f.get("file") else ""
                items.append(f"- {f.get('id')}{where}: {text}")
    if not items:
        return []
    shown = items[:FOLLOWUP_CAP]
    if len(items) > FOLLOWUP_CAP:
        shown.append(f"- ... and {len(items) - FOLLOWUP_CAP} more minor finding(s) in the review files of the run")
    return ["", "## Follow-ups", ""] + shown


def _description(run: dict, workspace: dict, alias: str, target: str, deps: list[tuple[str, str]], acs: dict,
                 cross: list[str] | None = None, targets: list[str] | None = None) -> str:
    lines = ["## Tickets", ""]
    for t in run.get("tickets", []):
        summary = _ticket_summary(run, t)
        lines.append(f"- {t['key']}" + (f" - {summary}" if summary else ""))
    table, humans, blocks = _ac_table(run["id"], alias, acs, target if run.get("hotfix") else None)
    lines += ["", "## Acceptance criteria and evidence", "", table]
    if blocks:
        lines += ["", "\n\n".join(blocks)]
    lines += ["", "## Human checks", ""]
    if humans:
        for h in humans:
            lines.append(f"- {h['id']}: {h.get('text', '')}" + (f" ({h['reason']})" if h.get("reason") else ""))
    else:
        lines.append("None.")
    lines += shots.description_lines(run, alias, target if run.get("hotfix") else None)
    lines += design.description_lines(run["id"])
    lines += ["", "## Quality", ""] + _baseline_summary(run["id"], alias) + _quality_lines(run, alias, targets or [target])
    lines += _followup_lines(run["id"])
    if cross:
        lines += ["", "## Hotfix mirror", ""] + cross
    lines += mirrordiff.description_lines(run, alias, target)
    if deps:
        lines += ["", "## Dependencies", ""]
        lines += [f"Depends on {{change_url:{a}:{t}}}" for a, t in deps]
    return "\n".join(lines) + "\n"


def _cross(run: dict, alias: str, target: str, targets: list[str]) -> list[str]:
    hotfix = run.get("hotfix")
    if hotfix and target == hotfix["base"]:
        return [f"Mirrored to {{change_url:{alias}:{t}}}" for t in targets if t != target]
    if hotfix:
        return [f"Mirror of {{change_url:{alias}:{hotfix['base']}}}"]
    return []


def local_link_errors(label: str, description: str) -> list[str]:
    return [f"{label}: description links to a local file ({m!r}); the remote cannot open it" for m in LOCAL_LINK_MARKERS
            if m in (description or "")]


def _needs_user(mode: str, kind: str, draft: bool = True) -> bool:
    """Whether an intent needs the user's yes. `draft-auto` automates only pushes, read-only intents and draft changes."""
    if mode == "draft-auto":
        if kind in ("push", "readback", "pipeline_watch"):
            return False
        return not (kind in ("change_request", "set_draft") and draft)
    return True


def _links_md(changes: list[dict], prefix: str = "") -> str:
    return "\n".join(f"- {c['repo']} ({c['target']}): {{change_url:{prefix}{c['repo']}:{c['target']}}}" for c in changes)


def _change_actions(c: dict, mode: str, existing: dict | None, watch: bool, run_id: str) -> list[dict]:
    """The intents that deliver one change: push, change_request, set_draft (an existing change whose draft state
    differs), readback of the change, then pipeline_watch with its own readback paths."""
    label_user = lambda kind, draft=True: _needs_user(mode, kind, draft)  # noqa: E731
    acts = [{"type": "push", "repo": c["repo"], "target": c["target"], "branch": c["source_branch"],
             "needs_user": label_user("push")}]
    op = "update" if existing and isinstance(existing.get("number"), int) else "create"
    cr = {"type": "change_request", "op": op, "repo": c["repo"], "target": c["target"], "title": c["title"],
          "body_md": c["body_md"], "source": c["source_branch"], "target_branch": c["target_branch"],
          "draft": c["draft"], "needs_user": label_user("change_request", c["draft"])}
    if op == "update":
        cr["number"] = existing["number"]
    if c.get("reviewers"):
        cr["reviewers"] = c["reviewers"]
    acts.append(cr)
    if op == "update" and existing.get("draft") is not None and bool(existing["draft"]) != bool(c["draft"]):
        acts.append({"type": "set_draft", "repo": c["repo"], "target": c["target"], "draft": c["draft"],
                     "needs_user": label_user("set_draft", c["draft"])})
    ident = change_id(c["repo"], c["target"])
    raw = abs_path(run_dir(run_id) / raw_readback_path(c["repo"], c["target"]))
    acts.append({"type": "readback", "kind": "change", "id": ident, "repo": c["repo"], "target": c["target"],
                 "save_to": raw, "ingest": f"factory ingest {run_id} change {raw} --id {ident} --repo {c['repo']}",
                 "needs_user": label_user("readback")})  # a readback only reads what its change_request wrote
    if watch:
        praw = abs_path(run_dir(run_id) / "raw" / _rel("pipeline", ident))
        jobs = praw + ".jobs"
        acts.append({"type": "pipeline_watch", "repo": c["repo"], "target": c["target"], "sha": c["head_sha"],
                     "save_to": praw, "jobs_save_to": jobs,
                     "ingest": f"factory ingest {run_id} pipeline {praw} --id {ident} --repo {c['repo']} --raw jobs={jobs}",
                     "needs_user": label_user("pipeline_watch")})
    return acts


def _existing_change(run_id: str, repo: str, target: str) -> dict | None:
    """The change of a previous plan/readback (number, draft), when its contract file is readable: it makes the
    next change_request an update. Staleness does not matter for the number; verify re-checks everything."""
    p = run_dir(run_id) / readback_path(repo, target)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def build_plan(run_id: str, allow_behind: bool = False, internal_deps: set[str] | None = None,
               ticket_mode: str | None = None, allow_mirror: int | None = None) -> dict:
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
    if allow_mirror is not None:
        mirrordiff.record_allowance(run, workspace, allow_mirror)
        run_save(run)
    acs = read_json(run_dir(run_id) / "acs.json")
    ship = workspace.get("ship") or {}
    mode = (workspace.get("autonomy") or {}).get("ship", "ask")
    targets = _ordered_targets(run, workspace)
    repos = _ordered_repos(run, workspace)
    if not targets or not repos:
        raise FactoryError("run has no repos or targets to ship")
    fmt, allowed = title_format(ship)
    validate_title_format(fmt, allowed)
    reviewer_names = reviewers(ship)
    ticket_mode = ticket_mode or ship.get("ticket_mode") or "comment+transition"
    if ticket_mode not in TICKET_MODES:
        raise FactoryError(f"ship ticket mode {ticket_mode!r} is not one of: {', '.join(TICKET_MODES)}")

    errors = integrity_errors(run_id, run, workspace) + check_dependencies(run, internal_deps)
    errors += mirrordiff.errors(run, workspace)
    for alias in repos:
        for target in targets:
            wt = target_worktree(run, workspace, alias, target)
            if wt is None:
                errors.append(f"{alias}: no worktree for target {target} (factory run mirror {run_id} {alias} --to {target})")
                continue
            try:
                n = behind_count(wt, target_branch(workspace, alias, target))
            except FactoryError as err:
                errors.append(str(err))
                continue
            if n > 0 and not allow_behind:
                errors.append(f"{alias}: HEAD is {n} commit(s) behind origin/{target_branch(workspace, alias, target)}; rebase/merge via a builder, then "
                              f"`factory run rewind {run_id} prove \"rebased on {target}\"` (prove + review again), "
                              "or --allow-behind with the user's explicit yes")
    if errors:
        raise GateFailed(errors)

    draft = bool(ship.get("draft_default", True))
    watch = bool(ship.get("pipeline_watch", False))
    hotfix = run.get("hotfix")
    seq = [(a, t) for t in targets for a in repos] if hotfix else [(a, t) for a in repos for t in targets]
    changes = []
    for alias, target in seq:
        cross = _cross(run, alias, target, targets)
        deps = [] if hotfix else [(a, t) for (a, t) in seq if (a != alias and t == target and repos.index(a) < repos.index(alias))
                or (a == alias and targets.index(t) < targets.index(target))]
        src = source_branch(run, alias, target, targets)
        entry = {
            "repo": alias,
            "target": target,
            "source_branch": src,
            "head_sha": branch_head(run, workspace, alias, target, src),
            "target_branch": target_branch(workspace, alias, target),
            "title": _title(fmt, run, alias, target),  # never carries a draft marker: `draft` is its own field
            "draft": draft,
            "behind": 0,
            "depends_on": [{"repo": a, "target": t} for a, t in deps],
            "body_md": _description(run, workspace, alias, target, deps, acs, cross, targets),
            "readback": readback_path(alias, target),
        }
        if reviewer_names:
            entry["reviewers"] = reviewer_names
        changes.append(entry)

    actions: list[dict] = []
    for c in changes:
        actions += _change_actions(c, mode, _existing_change(run_id, c["repo"], c["target"]), watch, run_id)

    keys = [t["key"] for t in run.get("tickets", [])]
    epics = {e["key"]: e for e in workspace.get("epics", [])}
    epic = next((epics[t["parent"]] for t in run.get("tickets", []) if t.get("parent") in epics), None)
    if ship.get("ticket_comment") == "change-links" and ticket_mode in ("comment", "comment+transition"):
        comment = {"type": "issue_comment", "issues": keys, "body_md": _links_md(changes),
                   "needs_user": _needs_user(mode, "issue_comment")}
        if (epic or {}).get("ship_comment"):
            comment["epic_ship_comment"] = epic["ship_comment"]
        actions.append(comment)
    transition = ship.get("ticket_transition") or (epic or {}).get("ticket_transition")
    if transition and ticket_mode in ("transition", "comment+transition"):
        actions.append({"type": "issue_transition", "issues": keys, "to_status": transition,
                        "needs_user": _needs_user(mode, "issue_transition")})

    plan = {"schemaVersion": PLAN_SCHEMA, "run": run_id, "autonomy": mode, "ticket_mode": ticket_mode,
            "changes": changes, "actions": actions, "visual_evidence": shots.gallery(run_id, run)}
    write_json(run_dir(run_id) / "ship-plan.json", plan)
    return plan


def plan_version_errors(plan: dict) -> list[str]:
    if not isinstance(plan, dict) or plan.get("schemaVersion") != PLAN_SCHEMA:
        got = plan.get("schemaVersion") if isinstance(plan, dict) else None
        return [f"ship-plan.json is schema {got!r}, this factory writes schema {PLAN_SCHEMA}: re-run ship plan"]
    return []


def _already_verified(run: dict, verified: list[dict]) -> bool:
    """`ship verify` and `gate ship` both verify: the second, with the same changes and nothing but gate passes
    recorded since the last `ship` event, must not log the same verification again."""
    events = run.get("events") or []
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("kind") == "ship":
            return (events[i].get("data") or {}).get("verified") == verified \
                and all(e.get("kind") == "gate_pass" for e in events[i + 1:])
    return False


def _contract_errors(label: str, what: str, problems: list[str]) -> list[str]:
    return [f"{label}: {what} unusable: {p}" for p in problems]


def _verify_change(run_id: str, run: dict, workspace: dict, c: dict, body: str, label: str) -> tuple[list[str], dict | None]:
    errors: list[str] = []
    doc, problems = contracts.load_contract(run_id, "change", change_id(c["repo"], c["target"]))
    if doc is None or problems:
        return _contract_errors(label, "change readback", problems), None
    if doc.get("repo") != c["repo"]:
        errors.append(f"{label}: readback is for repo {doc.get('repo')!r}, planned {c['repo']!r}")
    if doc["source_branch"] != c["source_branch"]:
        errors.append(f"{label}: source_branch {doc['source_branch']!r} != planned {c['source_branch']!r}")
    if doc["target_branch"] != c["target_branch"]:
        errors.append(f"{label}: target_branch {doc['target_branch']!r} != planned {c['target_branch']!r}")
    errors += local_link_errors(label + " (planned)", body)
    errors += local_link_errors(label + " (readback)", str(doc.get("description_md") or ""))
    if bool(doc["draft"]) != bool(c["draft"]):
        errors.append(f"{label}: draft is {doc['draft']}, planned {c['draft']}")
    if doc["title"].strip() != str(c["title"]).strip():
        errors.append(f"{label}: title {doc['title']!r} != planned {c['title']!r}")
    planned = c.get("reviewers") or []
    if planned:
        have = doc.get("reviewers")
        if have is None:
            errors.append(f"{label}: readback carries no reviewers (unknown); planned {planned}")
        elif missing := [r for r in planned if r not in have]:
            errors.append(f"{label}: reviewers {have} lack planned {missing}")
    current = branch_head(run, workspace, c["repo"], c["target"], c["source_branch"])
    remote_sha = doc["head_sha"]
    if not c.get("head_sha"):
        errors.append(f"{label}: ship plan recorded no head for {c['source_branch']}; re-run factory ship plan")
    elif current != c["head_sha"]:
        errors.append(f"{label}: {c['source_branch']} is at {str(current)[:8]}, ship plan recorded "
                      f"{c['head_sha'][:8]}; rewind to prove (factory run rewind {run_id} prove \"<reason>\")")
    elif remote_sha != c["head_sha"]:
        errors.append(f"{label}: change head sha {str(remote_sha)[:8]} != proven HEAD {c['head_sha'][:8]} "
                      "(push the proven commit; never push unproven commits)")
    return errors, doc


def _verify_pipeline(run_id: str, act: dict, label: str) -> tuple[list[str], dict | None]:
    """`pipeline_watch` readback: the pipeline contract ran on the watched sha (its status is reported, not judged here)."""
    doc, problems = contracts.load_contract(run_id, "pipeline", change_id(act["repo"], act["target"]))
    if doc is None or problems:
        return _contract_errors(label, "pipeline readback", problems), None
    errors = []
    if doc["sha"] != act["sha"]:
        errors.append(f"{label}: pipeline {doc['id']} ran on {doc['sha'][:8]}, watched sha is {str(act['sha'])[:8]}")
    return errors, doc


def verify(run_id: str) -> list[str]:
    plan_path = run_dir(run_id) / "ship-plan.json"
    if not plan_path.is_file():
        return ["ship-plan.json missing; run `factory ship plan` first"]
    plan = read_json(plan_path)
    stale = plan_version_errors(plan)
    if stale:
        return stale
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
    errors = list(integrity_errors(run_id, run, workspace))
    errors += review_errors(run_id, run, workspace)
    verified = []
    bodies = {(a["repo"], a["target"]): a.get("body_md", "") for a in plan.get("actions", []) if a.get("type") == "change_request"}
    watches = {(a["repo"], a["target"]): a for a in plan.get("actions", []) if a.get("type") == "pipeline_watch"}
    for c in plan.get("changes", []):
        label = f"{c['repo']}/{c['target']}"
        errs, doc = _verify_change(run_id, run, workspace, c, bodies.get((c["repo"], c["target"]), c.get("body_md", "")), label)
        errors += errs
        if doc is None:
            continue
        item = {"repo": c["repo"], "target": c["target"], "url": doc.get("url"), "number": doc.get("number"),
                "head": doc["head_sha"]}
        watch = watches.get((c["repo"], c["target"]))
        if watch:
            perrs, pipe = _verify_pipeline(run_id, watch, label)
            errors += perrs
            if pipe is not None:
                item["pipeline"] = {"id": pipe["id"], "status": pipe["status"]}
        verified.append(item)
    if not errors:
        run = run_load(run_id)
        if not _already_verified(run, verified):
            event(run, "ship", verified=verified)
            run_save(run)
    return list(dict.fromkeys(errors))


def update_description(run_id: str, repo: str | None = None, target: str | None = None) -> dict:
    """Rebuild the change body(ies) from current evidence; the intents update the existing change (number from its readback)."""
    plan_path = run_dir(run_id) / "ship-plan.json"
    if not plan_path.is_file():
        raise FactoryError("ship-plan.json missing; run `factory ship plan` first")
    plan = read_json(plan_path)
    stale = plan_version_errors(plan)
    if stale:
        raise FactoryError(stale[0])
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
    errors = integrity_errors(run_id, run, workspace)  # same proof checks as `ship plan`: no body from stale evidence
    if errors:
        raise GateFailed(errors)
    acs = read_json(run_dir(run_id) / "acs.json")
    mode = plan.get("autonomy") or (workspace.get("autonomy") or {}).get("ship", "ask")
    selected = [c for c in plan.get("changes", []) if (not repo or c["repo"] == repo) and (not target or c["target"] == target)]
    if not selected:
        raise FactoryError(f"no change in ship-plan.json for repo={repo or '*'} target={target or '*'}")
    targets = list(dict.fromkeys(c["target"] for c in plan["changes"]))
    missing = []
    for c in selected:
        doc = _existing_change(run_id, c["repo"], c["target"])
        if not (isinstance(doc, dict) and isinstance(doc.get("number"), int)):
            missing.append(c)
    if missing:
        raise GateFailed([f"{c['repo']}/{c['target']}: missing or unreadable change readback {c.get('readback')} (no number)"
                          for c in missing],
                         fetch=[fetches.change_fetch(run_id, workspace, c["repo"], c["target"]) for c in missing])
    actions = []
    for c in selected:
        deps = [(d["repo"], d["target"]) for d in c.get("depends_on", [])]
        c["body_md"] = _description(run, workspace, c["repo"], c["target"], deps, acs,
                                    _cross(run, c["repo"], c["target"], targets), targets)
        acts = _change_actions(c, mode, _existing_change(run_id, c["repo"], c["target"]), False, run_id)
        actions += [a for a in acts if a["type"] in ("change_request", "readback")]
        for a in plan["actions"]:  # keep the stored intent in step with the rebuilt body
            if a.get("type") == "change_request" and (a["repo"], a["target"]) == (c["repo"], c["target"]):
                a["body_md"] = c["body_md"]
    write_json(plan_path, plan)
    return {"schemaVersion": PLAN_SCHEMA, "run": run_id, "actions": actions}


# ---------- CLI ----------

def _cmd_plan(args) -> int:
    plan = build_plan(args.run, args.allow_behind, ticket_mode=getattr(args, "ticket", None),
                      allow_mirror=getattr(args, "allow_mirror_divergence", None))
    if getattr(args, "json", False):
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    print(f"ship-plan.json: {len(plan['changes'])} change(s), autonomy {plan['autonomy']}")
    for c in plan["changes"]:
        print(f"- {c['repo']} -> {c['target']}: {c['title']} (branch {c['source_branch']}{', draft' if c['draft'] else ''})")
    for i, a in enumerate(plan["actions"], 1):
        who = "ASK" if a["needs_user"] else "auto"
        print(f"{i}. [{who}] {a['type']} {a.get('repo', '')} {a.get('target', '')}".rstrip())
    for item in plan["visual_evidence"]:  # Gate S briefing shows these images (DESIGN section 12)
        print(f"visual {item['check']} {item['repo']}: before {len(item['before'])}, after {len(item['after'])}")
        for f in item["files"]:
            print(f"  {f}")
    return 0


def _cmd_update_description(args) -> int:
    out = update_description(args.run, args.repo, args.target)
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    for i, a in enumerate(out["actions"], 1):
        who = "ASK" if a["needs_user"] else "auto"
        extra = f" #{a['number']}" if a.get("number") else ""
        print(f"{i}. [{who}] {a['type']} {a['repo']} {a['target']}{extra}")
    print("ship-plan.json bodies rebuilt from current evidence; run with --json for the body text")
    return 0


def _cmd_verify(args) -> int:
    errs = verify(args.run)
    if errs:
        run = run_load(args.run)
        raise GateFailed(errs, fetch=fetches.readback_fetches(args.run, workspace_load(run["workspace"])))
    print("OK ship verified")
    return 0


def register(sub) -> None:
    p = sub.add_parser("ship", help="delivery plan and verification")
    ssub = p.add_subparsers(dest="ship_cmd", required=True)
    pl = ssub.add_parser("plan")
    pl.add_argument("run")
    pl.add_argument("--allow-behind", action="store_true")
    pl.add_argument("--allow-mirror-divergence", type=int, metavar="DECISION",
                    help="hotfix: a user decision \"mirror diff: <reason>\" that allows the current mirror divergence")
    pl.add_argument("--ticket", choices=["none", "comment", "transition"],
                    help="ticket intents to plan (default: workspace ship.ticket_mode, else comment+transition)")
    pl.set_defaults(func=_cmd_plan)
    ud = ssub.add_parser("update-description", help="rebuild the change body(ies) from current evidence")
    ud.add_argument("run")
    ud.add_argument("--repo")
    ud.add_argument("--target")
    ud.set_defaults(func=_cmd_update_description)
    v = ssub.add_parser("verify")
    v.add_argument("run")
    v.set_defaults(func=_cmd_verify)
