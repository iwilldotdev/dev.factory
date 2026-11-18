"""`factory ship plan|verify`: MR/Jira ship plan from evidence, and readback verification."""

from __future__ import annotations

import json
import string
from pathlib import Path

from .. import fetches, shots
from ..core import (
    FactoryError,
    GateFailed,
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

DRAFT_PREFIX = "Draft: "


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
    for key in run.get("repos", {}):
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
LEGACY_TITLE_FIELDS = TITLE_FIELDS + ("target_upper", "area")
DEFAULT_TITLE = "[{key}] - {summary}"
JIRA_MODES = ("none", "comment", "transition", "comment+transition")
LOCAL_LINK_MARKERS = ("](evidence/", "](C:", "](./", "file://")


def _ticket_summary(run: dict, ticket: dict) -> str:
    """Summary from run.json, else from the Jira readback remote/issue-<KEY>.json."""
    if ticket.get("summary"):
        return str(ticket["summary"])
    p = run_dir(run["id"]) / "remote" / f"issue-{ticket.get('key')}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        return str(fields.get("summary") or data.get("summary") or "")
    return ""


def title_format(ship: dict) -> tuple[str, tuple[str, ...]]:
    """(format, allowed placeholders): `ship.title_format`, else the legacy `mr_title_format`, else the default."""
    if ship.get("title_format"):
        return str(ship["title_format"]), TITLE_FIELDS
    if ship.get("mr_title_format"):
        return str(ship["mr_title_format"]), LEGACY_TITLE_FIELDS
    return DEFAULT_TITLE, TITLE_FIELDS


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
    first = tickets[0]
    key = first.get("key", run["id"])
    summary = _ticket_summary(run, first)
    out = fmt.format(key=key, summary=summary, repo=alias, target=target, target_upper=target.upper(),
                     area=first.get("area") or key).strip()
    if not summary:  # no summary known: never emit a dangling separator
        out = out.rstrip(" -").rstrip()
    return out


def reviewers(ship: dict) -> list[str]:
    """GitLab usernames from `ship.reviewers`; an identity entry (Jira/name) without gitlab_username is an error."""
    out = []
    for i, item in enumerate(ship.get("reviewers") or []):
        if isinstance(item, str):
            out.append(item)
            continue
        user = item.get("gitlab_username") if isinstance(item, dict) else None
        if not user:
            who = "?"
            if isinstance(item, dict):
                who = item.get("jira") or item.get("jira_account") or item.get("name") or item.get("email") or "?"
            raise FactoryError(f"ship.reviewers[{i}] ({who}): missing gitlab_username; set it in workspace.json "
                               "(a Jira identity alone cannot be assigned as an MR reviewer)")
        out.append(str(user))
    return out


def _unwrap_mr(data):
    """gitlab_get_merge_request saves {"merge_request": {...}, notes?, ...}; the MR object is what we compare."""
    if isinstance(data, dict) and isinstance(data.get("merge_request"), dict):
        return data["merge_request"]
    return data


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
    """Local tip of the MR source branch (refs are shared by the run's worktrees)."""
    wt = target_worktree(run, workspace, alias, target)
    if wt is None or not Path(wt).is_dir():
        return None
    return git(wt, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False) or None


def _readback_sha(data: dict) -> str | None:
    refs = data.get("diff_refs") if isinstance(data.get("diff_refs"), dict) else {}
    return data.get("sha") or refs.get("head_sha")


def _merged_mrs(run_id: str) -> list[dict]:
    out = []
    for p in sorted((run_dir(run_id) / "remote").glob("mr-*.json")):
        if p.name.endswith("-discussions.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        data = _unwrap_mr(data)
        if isinstance(data, dict):
            data["_file"] = p.name
            out.append(data)
    return out


def check_dependencies(run: dict, internal: set[str] | None = None) -> list[str]:
    errors = []
    in_run = {t["key"] for t in run.get("tickets", [])} | set(internal or ())
    mrs = _merged_mrs(run["id"])
    for t in run.get("tickets", []):
        for dep in t.get("depends_on") or []:
            if dep in in_run:
                continue
            needle = dep.lower()
            merged = any(
                m.get("state") == "merged" and needle in json.dumps(m, ensure_ascii=False).lower()
                for m in mrs
            )
            if not merged:
                errors.append(f"{t['key']} depends on {dep}: no remote/mr-*.json showing state merged for it")
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
    out = [f"Baseline (merge-base) pre-existing quality failures: {detail}. This MR adds none (prove gate)."]
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


def _followup_lines(run_id: str) -> list[str]:
    """Unresolved minor findings of the latest code and qa reviews, so they have a destination."""
    from . import gate
    items = []
    for kind in ("code", "qa"):
        for f in (gate.latest_review(run_id, kind) or {}).get("findings") or []:
            if f.get("severity") == "minor" and not f.get("resolved"):
                where = f" `{f['file']}`" if f.get("file") else ""
                items.append(f"- {f.get('id')}{where}: {' '.join(str(f.get('text', '')).split())}")
    return ["", "## Follow-ups", ""] + items if items else []


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
    lines += ["", "## Quality", ""] + _baseline_summary(run["id"], alias) + _quality_lines(run, alias, targets or [target])
    lines += _followup_lines(run["id"])
    if cross:
        lines += ["", "## Hotfix mirror", ""] + cross
    if deps:
        lines += ["", "## Dependencies", ""]
        lines += [f"Depends on {{mr_url:{a}:{t}}}" for a, t in deps]
    return "\n".join(lines) + "\n"


def _cross(run: dict, alias: str, target: str, targets: list[str]) -> list[str]:
    hotfix = run.get("hotfix")
    if hotfix and target == hotfix["base"]:
        return [f"Mirrored to {{mr_url:{alias}:{t}}}" for t in targets if t != target]
    if hotfix:
        return [f"Mirror of {{mr_url:{alias}:{hotfix['base']}}}"]
    return []


def local_link_errors(label: str, description: str) -> list[str]:
    return [f"{label}: description links to a local file ({m!r}); GitLab cannot open it" for m in LOCAL_LINK_MARKERS
            if m in (description or "")]


def _adf_comment(mrs: list[dict]) -> dict:
    content = []
    for mr in mrs:
        content.append({"type": "paragraph", "content": [
            {"type": "text", "text": f"{mr['repo']} ({mr['target']}): "},
            {"type": "inlineCard", "attrs": {"url": f"{{mr_url:{mr['repo']}:{mr['target']}}}"}},
        ]})
    return {"version": 1, "type": "doc", "content": content}


def _needs_user(mode: str, kind: str, draft: bool = True) -> bool:
    if mode == "draft-auto":
        return not (kind in ("push", "mr", "readback") and (kind != "mr" or draft))
    return True


def build_plan(run_id: str, allow_behind: bool = False, internal_deps: set[str] | None = None,
               jira_mode: str | None = None) -> dict:
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
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
    jira_mode = jira_mode or ship.get("jira") or "comment+transition"
    if jira_mode not in JIRA_MODES:
        raise FactoryError(f"ship jira mode {jira_mode!r} is not one of: {', '.join(JIRA_MODES)}")

    errors = integrity_errors(run_id, run, workspace) + check_dependencies(run, internal_deps)
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
    hotfix = run.get("hotfix")
    seq = [(a, t) for t in targets for a in repos] if hotfix else [(a, t) for a in repos for t in targets]
    mrs = []
    for alias, target in seq:
        cross = _cross(run, alias, target, targets)
        deps = [] if hotfix else [(a, t) for (a, t) in seq if (a != alias and t == target and repos.index(a) < repos.index(alias))
                or (a == alias and targets.index(t) < targets.index(target))]
        base_title = _title(fmt, run, alias, target)
        src = source_branch(run, alias, target, targets)
        entry = {
            "repo": alias,
            "target": target,
            "source_branch": src,
            "head_sha": branch_head(run, workspace, alias, target, src),
            "target_branch": target_branch(workspace, alias, target),
            "title": (DRAFT_PREFIX + base_title) if draft else base_title,
            "draft": draft,
            "behind": 0,
            "depends_on": [{"repo": a, "target": t} for a, t in deps],
            "description": _description(run, workspace, alias, target, deps, acs, cross, targets),
            "readback": f"remote/mr-{alias}-{target}.json",
        }
        if reviewer_names:
            entry["reviewers"] = reviewer_names
        mrs.append(entry)

    actions = []
    for mr in mrs:
        label = f"{mr['repo']}/{mr['target']}"
        actions.append({"type": "push", "repo": mr["repo"], "target": mr["target"], "branch": mr["source_branch"],
                        "needs_user": _needs_user(mode, "push")})
        mr_needs_user = _needs_user(mode, "mr", mr["draft"])
        actions.append({"type": "mr", "repo": mr["repo"], "target": mr["target"], "draft": mr["draft"],
                        "note": f"create or update MR {label}; draft flag and 'Draft: ' title prefix when draft",
                        "needs_user": mr_needs_user, **({"reviewers": mr["reviewers"]} if mr.get("reviewers") else {})})
        actions.append({"type": "readback", "repo": mr["repo"], "target": mr["target"], "save_to": mr["readback"],
                        "needs_user": mr_needs_user})  # a readback only reads what its MR action wrote

    keys = [t["key"] for t in run.get("tickets", [])]
    epics = {e["key"]: e for e in workspace.get("epics", [])}
    epic = next((epics[t["parent"]] for t in run.get("tickets", []) if t.get("parent") in epics), None)
    jira = None
    if ship.get("jira_comment") == "mr-links" and jira_mode in ("comment", "comment+transition"):
        jira = {"issues": keys, "adf": _adf_comment(mrs), "epic_ship_comment": (epic or {}).get("ship_comment")}
        actions.append({"type": "jira_comment", "issues": keys, "needs_user": _needs_user(mode, "jira_comment")})
    transition = ship.get("jira_transition") or (epic or {}).get("jira_transition")
    if transition and jira_mode in ("transition", "comment+transition"):
        actions.append({"type": "jira_transition", "issues": keys, "name": transition,
                        "needs_user": _needs_user(mode, "jira_transition")})

    plan = {"schemaVersion": 1, "run": run_id, "autonomy": mode, "jira": jira_mode, "mrs": mrs, "jira_comment": jira, "actions": actions,
            "visual_evidence": shots.gallery(run_id, run)}
    write_json(run_dir(run_id) / "ship-plan.json", plan)
    return plan


def _strip_draft(title: str) -> str:
    t = title.strip()
    for p in ("Draft:", "draft:", "[Draft]"):
        if t.startswith(p):
            return t[len(p):].strip()
    return t


def verify(run_id: str) -> list[str]:
    plan_path = run_dir(run_id) / "ship-plan.json"
    if not plan_path.is_file():
        return ["ship-plan.json missing; run `factory ship plan` first"]
    plan = read_json(plan_path)
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
    errors = list(integrity_errors(run_id, run, workspace))
    errors += review_errors(run_id, run, workspace)
    verified = []
    for mr in plan.get("mrs", []):
        label = f"{mr['repo']}/{mr['target']}"
        rb = run_dir(run_id) / mr.get("readback", f"remote/mr-{mr['repo']}-{mr['target']}.json")
        if not rb.is_file():
            errors.append(f"{label}: missing readback {rb.name}")
            continue
        try:
            data = json.loads(rb.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            errors.append(f"{label}: readback is not valid JSON ({err})")
            continue
        data = _unwrap_mr(data)
        if data.get("source_branch") != mr["source_branch"]:
            errors.append(f"{label}: source_branch {data.get('source_branch')!r} != planned {mr['source_branch']!r}")
        if data.get("target_branch") != mr["target_branch"]:
            errors.append(f"{label}: target_branch {data.get('target_branch')!r} != planned {mr['target_branch']!r}")
        errors += local_link_errors(label + " (planned)", mr.get("description", ""))
        errors += local_link_errors(label + " (readback)", str(data.get("description") or ""))
        title = str(data.get("title", ""))
        is_draft = bool(data.get("draft") or data.get("work_in_progress")) or title.lower().startswith("draft:")
        if is_draft != bool(mr["draft"]):
            errors.append(f"{label}: draft is {is_draft}, planned {mr['draft']}")
        if _strip_draft(title) != _strip_draft(mr["title"]):
            errors.append(f"{label}: title {title!r} != planned {mr['title']!r}")
        current = branch_head(run, workspace, mr["repo"], mr["target"], mr["source_branch"])
        remote_sha = _readback_sha(data)
        if not mr.get("head_sha"):
            errors.append(f"{label}: ship plan recorded no head for {mr['source_branch']}; re-run factory ship plan")
        elif current != mr["head_sha"]:
            errors.append(f"{label}: {mr['source_branch']} is at {str(current)[:8]}, ship plan recorded "
                          f"{mr['head_sha'][:8]}; rewind to prove (factory run rewind {run_id} prove \"<reason>\")")
        elif remote_sha != mr["head_sha"]:
            errors.append(f"{label}: MR head sha {str(remote_sha)[:8]} != proven HEAD {mr['head_sha'][:8]} "
                          "(push the proven commit; never push unproven commits)")
        verified.append({"repo": mr["repo"], "target": mr["target"], "url": data.get("web_url"), "iid": data.get("iid")})
    if not errors:
        run = run_load(run_id)
        event(run, "ship", verified=verified)
        run_save(run)
    return list(dict.fromkeys(errors))


def update_description(run_id: str, repo: str | None = None, target: str | None = None) -> dict:
    """Rebuild the MR description(s) from current evidence; actions update the existing MR (iid from the readback)."""
    plan_path = run_dir(run_id) / "ship-plan.json"
    if not plan_path.is_file():
        raise FactoryError("ship-plan.json missing; run `factory ship plan` first")
    plan = read_json(plan_path)
    run = run_load(run_id)
    workspace = workspace_load(run["workspace"])
    errors = integrity_errors(run_id, run, workspace)  # same proof checks as `ship plan`: no description from stale evidence
    if errors:
        raise GateFailed(errors)
    acs = read_json(run_dir(run_id) / "acs.json")
    mode = plan.get("autonomy") or (workspace.get("autonomy") or {}).get("ship", "ask")
    selected = [m for m in plan.get("mrs", []) if (not repo or m["repo"] == repo) and (not target or m["target"] == target)]
    if not selected:
        raise FactoryError(f"no MR in ship-plan.json for repo={repo or '*'} target={target or '*'}")
    targets = list(dict.fromkeys(m["target"] for m in plan["mrs"]))
    missing, iids = [], {}
    for mr in selected:
        rb = run_dir(run_id) / mr.get("readback", f"remote/mr-{mr['repo']}-{mr['target']}.json")
        try:
            data = _unwrap_mr(json.loads(rb.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            data = None
        iid = data.get("iid") if isinstance(data, dict) else None
        if not iid:
            missing.append(mr)
        iids[(mr["repo"], mr["target"])] = iid
    if missing:
        fetch = [fetches.mr_fetch(run_id, workspace, m["repo"], "<iid>", f"mr-{m['repo']}-{m['target']}.json") for m in missing]
        raise GateFailed([f"{m['repo']}/{m['target']}: missing or unreadable MR readback {m.get('readback')} (no iid)"
                          for m in missing], fetch=fetch)
    actions = []
    for mr in selected:
        deps = [(d["repo"], d["target"]) for d in mr.get("depends_on", [])]
        mr["description"] = _description(run, workspace, mr["repo"], mr["target"], deps, acs,
                                         _cross(run, mr["repo"], mr["target"], targets), targets)
        needs = _needs_user(mode, "mr", bool(mr.get("draft", True)))
        actions.append({"type": "mr_update_description", "repo": mr["repo"], "target": mr["target"],
                        "iid": iids[(mr["repo"], mr["target"])], "description": mr["description"], "needs_user": needs})
        actions.append({"type": "readback", "repo": mr["repo"], "target": mr["target"], "save_to": mr["readback"],
                        "needs_user": needs})
    write_json(plan_path, plan)
    return {"schemaVersion": 1, "run": run_id, "actions": actions}


# ---------- CLI ----------

def _cmd_plan(args) -> int:
    plan = build_plan(args.run, args.allow_behind, jira_mode=getattr(args, "jira", None))
    if getattr(args, "json", False):
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0
    print(f"ship-plan.json: {len(plan['mrs'])} MR(s), autonomy {plan['autonomy']}")
    for mr in plan["mrs"]:
        print(f"- {mr['repo']} -> {mr['target']}: {mr['title']} (branch {mr['source_branch']})")
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
        extra = f" iid {a['iid']}" if a.get("iid") else ""
        print(f"{i}. [{who}] {a['type']} {a['repo']} {a['target']}{extra}")
    print("ship-plan.json descriptions rebuilt from current evidence; run with --json for the description text")
    return 0


def _cmd_verify(args) -> int:
    errs = verify(args.run)
    if errs:
        from ..fetches import readback_fetches
        run = run_load(args.run)
        raise GateFailed(errs, fetch=readback_fetches(args.run, workspace_load(run["workspace"])))
    print("OK ship verified")
    return 0


def register(sub) -> None:
    p = sub.add_parser("ship", help="ship plan and verification")
    ssub = p.add_subparsers(dest="ship_cmd", required=True)
    pl = ssub.add_parser("plan")
    pl.add_argument("run")
    pl.add_argument("--allow-behind", action="store_true")
    pl.add_argument("--jira", choices=["none", "comment", "transition"],
                    help="Jira actions to plan (default: workspace ship.jira, else comment+transition)")
    pl.set_defaults(func=_cmd_plan)
    ud = ssub.add_parser("update-description", help="rebuild MR description(s) from current evidence")
    ud.add_argument("run")
    ud.add_argument("--repo")
    ud.add_argument("--target")
    ud.set_defaults(func=_cmd_update_description)
    v = ssub.add_parser("verify")
    v.add_argument("run")
    v.set_defaults(func=_cmd_verify)
