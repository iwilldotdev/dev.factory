"""`factory gate <run> <phase>`: exit criteria of a phase (DESIGN §3, §2.3)."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

from ..core import (FactoryError, GateFailed, event, git, git_dirty, git_head,
                    load_evidence, node_file, now, read_json, repo_conf,
                    repo_path_for_run, run_dir, run_load, run_save,
                    workspace_load)

PHASES = ["intake", "plan", "build", "prove", "review", "ship", "feedback", "done"]
NEXT = {p: PHASES[i + 1] for i, p in enumerate(PHASES[:-1])}
REVIEW_PHASES = ["intake", "plan", "prove", "review", "report", "done"]  # kind review: no build, no ship
BATCH_PHASES = ["intake", "report", "done"]  # kind review-batch parent
RUNTIME_TYPES = {"browser", "visual", "style", "api"}
CHECK_TYPES = RUNTIME_TYPES | {"test", "red-green", "human"}
AC_KINDS = {"ui", "api", "logic", "data", "bug"}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------- shared helpers ----------

def quality_id(alias: str) -> str:
    return f"QUALITY-{alias}"


def touched_repos(run: dict) -> list[str]:
    return list(run.get("repos", {}).keys())


def resolve_base(repo_path: Path, base: str) -> str:
    """origin/<base> when that ref exists, else the local <base>."""
    if git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}", check=False):
        return f"origin/{base}"
    return base


def rule_checks(workspace: dict, run: dict) -> list[dict]:
    """Executable checks from workspace rules[] for each touched repo."""
    touched = touched_repos(run)
    out = []
    for rule in workspace.get("rules", []):
        check = rule.get("check")
        if not isinstance(check, dict):
            continue
        repos = [check["repo"]] if check.get("repo") else touched
        for alias in repos:
            if alias not in touched:
                continue
            item = dict(check)
            item.setdefault("type", "test")
            item.update(id=f"RULE-{rule['id']}-{alias}", repo=alias)
            out.append(item)
    return out


def load_acs(run_id: str) -> dict:
    return read_json(run_dir(run_id) / "acs.json")


def _call_external(module: str, func: str, *args) -> list[str]:
    try:
        fn = getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError):
        return [f"{module}.{func}: not available"]
    try:
        return list(fn(*args) or [])
    except FactoryError as err:
        return [f"{module}.{func}: {err}"]


# ---------- style map ----------

NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _norm_hex(value: str) -> str | None:
    h = value.strip().lstrip("#").lower()
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    if len(h) not in (6, 8) or not re.fullmatch(r"[0-9a-f]+", h):
        return None
    return "#" + h[:6]


def _rgb_hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in (r, g, b))


def _walk_colors(node, out: set) -> None:
    if isinstance(node, dict):
        vals = [node.get(k) for k in ("r", "g", "b")]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in vals):
            out.add(_rgb_hex(*vals))
        for v in node.values():
            _walk_colors(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_colors(v, out)


def pack_facts(text: str) -> tuple[list[float], set[str]]:
    numbers = [float(n) for n in NUM_RE.findall(text)]
    colors = {c for c in (_norm_hex(h) for h in HEX_RE.findall(text)) if c}
    try:
        _walk_colors(json.loads(text), colors)
    except json.JSONDecodeError:
        pass
    return numbers, colors


def _expected_color(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if v.startswith("#"):
        return _norm_hex(v)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*[, ]\s*(\d+)\s*[, ]\s*(\d+)\s*(?:[,/]\s*[\d.]+%?\s*)?\)", v, re.I)
    if m:
        return _rgb_hex(*(int(x) / 255 for x in m.groups()))
    return None


def _flatten(prefix: str, value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(f"{prefix}.{k}", v)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _flatten(f"{prefix}[{i}]", v)
    else:
        yield prefix, value


def style_map_errors(map_data: dict, pack_text: str, label: str) -> list[str]:
    errors = []
    numbers, colors = pack_facts(pack_text)
    lowered = pack_text.lower()
    entries = map_data.get("entries")
    if not isinstance(entries, list) or not entries:
        return [f"{label}: style map has no entries"]
    for entry in entries:
        eid = entry.get("id", "?")
        if not entry.get("selector"):
            errors.append(f"{label}: entry {eid} has no selector")
        expect = entry.get("expect")
        if not isinstance(expect, dict) or not expect:
            errors.append(f"{label}: entry {eid} has no expect")
            continue
        for path, value in _flatten("expect", expect):
            name = f"{label}: {eid}.{path[len('expect.'):]}={value!r}"
            if isinstance(value, bool) or value is None:
                continue
            color = _expected_color(value)
            if color is not None:
                if color not in colors:
                    errors.append(f"{name} color not in Figma pack")
                continue
            if isinstance(value, str):
                m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(px)?", value.strip())
                if m:
                    value = float(m.group(1))
                elif value.strip().lower() not in lowered:
                    errors.append(f"{name} not in Figma pack")
                    continue
                else:
                    continue
            if isinstance(value, (int, float)):
                if not any(abs(n - value) < 1e-6 for n in numbers):
                    errors.append(f"{name} number not in Figma pack")
    return errors


# ---------- plan: acs.json ----------

def pack_errors(run_id: str, node: str) -> list[str]:
    base = run_dir(run_id) / "figma"
    stem = node_file(node)
    errors = []
    jpath, ppath = base / f"{stem}.json", base / f"{stem}.png"
    if not jpath.is_file():
        errors.append(f"figma pack: {jpath.name} missing for node {node}")
    else:
        try:
            json.loads(jpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            errors.append(f"figma pack: {jpath.name} is not valid JSON")
    if not ppath.is_file():
        errors.append(f"figma pack: {ppath.name} missing for node {node}")
    elif ppath.read_bytes()[:8] != PNG_MAGIC:
        errors.append(f"figma pack: {ppath.name} is not a decodable PNG")
    return errors


def validate_acs(run_id: str, run: dict, workspace: dict) -> list[str]:
    path = run_dir(run_id) / "acs.json"
    if not path.is_file():
        return ["acs.json missing"]
    try:
        data = read_json(path)
    except FactoryError as err:
        return [str(err)]
    errors: list[str] = []
    if data.get("schemaVersion") != 1:
        errors.append("acs.json: schemaVersion must be 1")
    acs, checks = data.get("acs") or [], data.get("checks") or []
    if not acs:
        errors.append("acs.json: no acs")
    check_ids: dict[str, dict] = {}
    for chk in checks:
        cid = chk.get("id")
        if not cid:
            errors.append(f"check without id: {chk}")
            continue
        if cid in check_ids:
            errors.append(f"duplicate check id {cid}")
        check_ids[cid] = chk
        ctype = chk.get("type")
        if ctype not in CHECK_TYPES:
            errors.append(f"{cid}: unknown type {ctype!r}")
            continue
        if ctype == "human":
            if not chk.get("text") or not chk.get("reason"):
                errors.append(f"{cid}: human check needs text and reason")
            continue
        alias = chk.get("repo")
        if not alias:
            errors.append(f"{cid}: repo is required")
        else:
            try:
                repo_conf(workspace, alias)
            except FactoryError:
                errors.append(f"{cid}: repo {alias} not in workspace")
            if alias not in run.get("repos", {}):
                errors.append(f"{cid}: repo {alias} has no branch/worktree in the run (factory run branch)")
        if ctype in ("test", "red-green") and not chk.get("cmd"):
            errors.append(f"{cid}: cmd is required")
        if ctype == "api" and not chk.get("path"):
            errors.append(f"{cid}: path is required")
        if ctype == "browser" and not chk.get("spec"):
            errors.append(f"{cid}: spec is required")
        if ctype in ("visual", "style") and not chk.get("node"):
            errors.append(f"{cid}: node is required")
        if ctype == "style" and not chk.get("map"):
            errors.append(f"{cid}: map is required")
    referenced: set[str] = set()
    ac_seen: set[str] = set()
    for ac in acs:
        aid = ac.get("id", "?")
        if aid in ac_seen:
            errors.append(f"duplicate AC id {aid}")
        ac_seen.add(aid)
        if ac.get("kind") not in AC_KINDS:
            errors.append(f"{aid}: kind must be one of {sorted(AC_KINDS)}")
        refs = ac.get("checks") or []
        if not refs:
            errors.append(f"{aid}: has no checks")
        types = set()
        for cid in refs:
            referenced.add(cid)
            if cid not in check_ids:
                errors.append(f"{aid}: references unknown check {cid}")
            else:
                types.add(check_ids[cid].get("type"))
        if ac.get("kind") == "ui":
            for needed in ("visual", "style"):
                if needed not in types:
                    errors.append(f"{aid}: ui AC needs a {needed} check")
        if ac.get("kind") == "bug" and "red-green" not in types:
            errors.append(f"{aid}: bug AC needs a red-green check")
    for cid in check_ids:
        if cid not in referenced:
            errors.append(f"{cid}: not referenced by any AC")
    # figma pack + style maps
    nodes = sorted({c["node"] for c in checks if c.get("type") in ("visual", "style") and c.get("node")})
    for node in nodes:
        errors.extend(pack_errors(run_id, node))
    if nodes:
        meta = run_dir(run_id) / "figma" / "meta.json"
        if not meta.is_file():
            errors.append("figma/meta.json missing (file revision)")
        else:
            try:
                m = read_json(meta)
                files = m.values() if m and all(isinstance(v, dict) for v in m.values()) else [m]
                if not all(f.get("lastModified") or f.get("version") for f in files):
                    errors.append("figma/meta.json names no lastModified/version")
            except (FactoryError, AttributeError):
                errors.append("figma/meta.json unreadable")
    for chk in checks:
        if chk.get("type") != "style" or not chk.get("map") or not chk.get("node"):
            continue
        mpath = run_dir(run_id) / chk["map"]
        if not mpath.is_file():
            errors.append(f"{chk['id']}: style map {chk['map']} missing")
            continue
        try:
            mdata = read_json(mpath)
        except FactoryError as err:
            errors.append(str(err))
            continue
        if mdata.get("node") != chk["node"]:
            errors.append(f"{chk['id']}: style map node {mdata.get('node')!r} != check node {chk['node']!r}")
        jpath = run_dir(run_id) / "figma" / f"{node_file(chk['node'])}.json"
        if jpath.is_file():
            errors.extend(style_map_errors(mdata, jpath.read_text(encoding="utf-8"), f"{chk['id']} ({chk['map']})"))
    return errors


# ---------- phase evaluators ----------

def _intake(run_id, run, ws) -> list[str]:
    errors = []
    if not run.get("tickets"):
        errors.append("no tickets in run")
    for t in run.get("tickets", []):
        if not (run_dir(run_id) / "remote" / f"issue-{t['key']}.json").is_file():
            errors.append(f"remote/issue-{t['key']}.json missing (save the dev.mcp issue response)")
    for i, b in enumerate(run.get("blockers", [])):
        if b.get("kind") == "question" and not b.get("resolved"):
            errors.append(f"open question blocker #{i}: {b.get('text')}")
    return errors


def _plan(run_id, run, ws) -> list[str]:
    errors = validate_acs(run_id, run, ws)
    path = run_dir(run_id) / "acs.json"
    if not path.is_file():
        return errors
    try:
        checks = read_json(path).get("checks") or []
    except FactoryError:
        return errors
    if any(c.get("type") in ("visual", "style") for c in checks):
        errors.extend(_call_external("factory.commands.figma", "check_pack", run_id))
    for alias in sorted({c["repo"] for c in checks if c.get("repo")} | set(run.get("repos", {}))):
        rec = run.get("repos", {}).get(alias)
        if not rec:
            errors.append(f"{alias}: no branch/worktree (factory run branch)")
        elif not Path(rec.get("worktree", "")).is_dir():
            errors.append(f"{alias}: worktree missing on disk: {rec.get('worktree')}")
    runtime = sorted({c["repo"] for c in checks if c.get("type") in RUNTIME_TYPES and c.get("repo")})
    for alias in runtime:
        env = (repo_conf(ws, alias).get("env") if any(r.get("alias") == alias for r in ws.get("repos", [])) else None)
        if not env:
            errors.append(f"env: {alias} has no env block in workspace (user must provide start/health/base_url)")
            continue
        errors.extend(f"env {alias}: {e}" for e in _call_external("factory.commands.env", "capability", ws, alias))
    return list(dict.fromkeys(errors))


def commits_errors(path: Path, base_ref: str, ws: dict, alias: str) -> list[str]:
    commit = ws.get("commit") or {}
    regex = re.compile(commit["header_regex"]) if commit.get("header_regex") else None
    forbidden = [t.lower() for t in commit.get("forbidden_trailers") or []]
    errors = []
    log = git(path, "log", "--format=%H%x1f%B%x1e", f"{base_ref}..HEAD")
    for rec in filter(None, (r.strip() for r in log.split("\x1e"))):
        sha, _, msg = rec.partition("\x1f")
        header = msg.strip().splitlines()[0] if msg.strip() else ""
        if regex and not regex.search(header):
            errors.append(f"{alias} {sha[:8]}: header does not match commit.header_regex: {header!r}")
        low = msg.lower()
        for trailer in forbidden:
            if trailer in low:
                errors.append(f"{alias} {sha[:8]}: forbidden trailer {trailer!r}")
    return errors


def _build(run_id, run, ws) -> list[str]:
    errors = []
    if not run.get("repos"):
        return ["no repos in run (factory run branch)"]
    for alias, rec in run["repos"].items():
        path = repo_path_for_run(run, ws, alias)
        if not path.is_dir():
            errors.append(f"{alias}: worktree missing: {path}")
            continue
        if git_dirty(path):
            errors.append(f"{alias}: worktree has uncommitted changes")
        base = resolve_base(path, rec.get("base", "main"))
        ahead = git(path, "rev-list", "--count", f"{base}..HEAD", check=False)
        if not ahead.isdigit() or int(ahead) == 0:
            errors.append(f"{alias}: no commits ahead of {base}")
        else:
            errors.extend(commits_errors(path, base, ws, alias))
    return errors


def _evidence_errors(run_id, run, ws, check: dict) -> list[str]:
    cid = check["id"]
    ev = load_evidence(run_id, cid)
    if ev is None:
        return [f"{cid}: no evidence (factory prove)"]
    errors = []
    if ev.get("status") != "pass":
        errors.append(f"{cid}: evidence status {ev.get('status')} ({ev.get('summary')})")
    if ev.get("dirty"):
        errors.append(f"{cid}: evidence collected with a dirty worktree")
    alias = check.get("repo")
    if alias:
        try:
            head = git_head(repo_path_for_run(run, ws, alias))
        except (FactoryError, KeyError):
            head = None
        if ev.get("git_sha") != head:
            errors.append(f"{cid}: evidence is stale (sha {str(ev.get('git_sha'))[:8]} != HEAD {str(head)[:8]})")
    return errors


def _prove(run_id, run, ws) -> list[str]:
    errors = []
    try:
        checks = load_acs(run_id).get("checks") or []
    except FactoryError as err:
        return [str(err)]
    for alias in touched_repos(run):
        path = repo_path_for_run(run, ws, alias)
        if path.is_dir() and git_dirty(path):
            errors.append(f"{alias}: worktree is dirty")
    for chk in checks:
        if chk.get("type") != "human":
            errors.extend(_evidence_errors(run_id, run, ws, chk))
    for alias in touched_repos(run):
        if repo_conf(ws, alias).get("quality"):
            errors.extend(_evidence_errors(run_id, run, ws, {"id": quality_id(alias), "repo": alias}))
    for chk in rule_checks(ws, run):
        errors.extend(_evidence_errors(run_id, run, ws, chk))
    return errors


def latest_review(run_id: str, kind: str) -> dict | None:
    best, best_round = None, -1
    for p in (run_dir(run_id) / "reviews").glob(f"{kind}-*.json"):
        try:
            rec = read_json(p)
        except FactoryError:
            continue
        if rec.get("round", 0) > best_round:
            best, best_round = rec, rec.get("round", 0)
    return best


def review_status(run_id, run, ws) -> tuple[list[str], list[str]]:
    """Returns (plain_errors, change_errors). change_errors send the run back to build."""
    plain, changes = [], []
    for kind in ("code", "qa"):
        rec = latest_review(run_id, kind)
        if rec is None:
            plain.append(f"no {kind} review recorded (factory review record)")
            continue
        for alias in touched_repos(run):
            try:
                head = git_head(repo_path_for_run(run, ws, alias))
            except FactoryError:
                head = None
            if (rec.get("repos") or {}).get(alias) != head:
                plain.append(f"{kind} review round {rec.get('round')} is stale for {alias}; re-review HEAD")
        if rec.get("verdict") != "APPROVE":
            changes.append(f"{kind} review round {rec.get('round')} verdict {rec.get('verdict')}")
        for f in rec.get("findings") or []:
            if f.get("severity") in ("blocker", "major") and not f.get("resolved"):
                changes.append(f"{kind} {f.get('id')} [{f['severity']}] {f.get('file', '')} {f.get('text', '')}".strip())
    return plain, changes


def _review(run_id, run, ws) -> list[str]:
    plain, changes = review_status(run_id, run, ws)
    return plain + changes


def _ship(run_id, run, ws) -> list[str]:
    return _call_external("factory.commands.ship", "verify", run_id)


def _feedback(run_id, run, ws) -> list[str]:
    return [f"open blocker #{i}: {b.get('text')}" for i, b in enumerate(run.get("blockers", [])) if not b.get("resolved")]


# ---------- review runs (DESIGN §7) ----------

def _mr_worktree_errors(run: dict, ws: dict, need_head: bool) -> list[str]:
    errors = []
    for mr in run.get("mrs", []):
        alias, rec = mr["repo"], run.get("repos", {}).get(mr["repo"])
        if not rec:
            errors.append(f"{alias}: no worktree for MR !{mr['iid']} (factory run branch {run['id']} {alias} --mr {mr['iid']})")
            continue
        path = Path(rec.get("worktree", ""))
        if not path.is_dir():
            errors.append(f"{alias}: MR worktree missing on disk: {path}")
        elif need_head and git_head(path) != mr["head_sha"]:
            errors.append(f"{alias}: worktree HEAD is not the MR head {mr['head_sha'][:8]}; evidence must bind to the MR head sha")
    return errors


def _review_intake(run_id, run, ws) -> list[str]:
    errors = _intake(run_id, run, ws)
    if not run.get("mrs"):
        errors.append("no MRs in run (factory review start)")
    for mr in run.get("mrs", []):
        if not (run_dir(run_id) / "remote" / f"mr-{mr['repo']}-{mr['iid']}.json").is_file():
            errors.append(f"remote/mr-{mr['repo']}-{mr['iid']}.json missing")
    return errors


def _review_plan(run_id, run, ws) -> list[str]:
    return list(dict.fromkeys(_plan(run_id, run, ws) + _mr_worktree_errors(run, ws, True)))


def _review_prove(run_id, run, ws) -> list[str]:
    return _mr_worktree_errors(run, ws, True) + _prove(run_id, run, ws)


def _review_review(run_id, run, ws) -> list[str]:
    """Both reviews recorded and bound to the MR head. A CHANGES verdict is a result for the report, not a gate failure."""
    return review_status(run_id, run, ws)[0]


def _report(run_id, run, ws) -> list[str]:
    from . import review
    if run.get("kind") == "review-batch":
        if not run.get("children"):
            return ["review batch has no children"]
        return [f"{c}: phase {run_load(c)['phase']}, not done" for c in run["children"] if run_load(c)["phase"] != "done"]
    return review.report_errors(run_id, run, ws)


def _batch_intake(run_id, run, ws) -> list[str]:
    if not run.get("children"):
        return ["review batch has no children"]
    return [f"open question blocker #{i}: {b.get('text')}" for i, b in enumerate(run.get("blockers", []))
            if b.get("kind") == "question" and not b.get("resolved")]


EVALUATORS = {"intake": _intake, "plan": _plan, "build": _build, "prove": _prove,
              "review": _review, "ship": _ship, "feedback": _feedback, "report": _report}
TICKET_EVALUATORS = {k: v for k, v in EVALUATORS.items() if k != "report"}
REVIEW_EVALUATORS = {"intake": _review_intake, "plan": _review_plan, "prove": _review_prove,
                     "review": _review_review, "report": _report}
BATCH_EVALUATORS = {"intake": _batch_intake, "report": _report}


def machine(run: dict) -> tuple[list[str], dict]:
    """(phase order, evaluators) for the run's kind (epic runs use epic.PHASES/EVALUATORS)."""
    kind = run.get("kind")
    if kind == "review":
        return REVIEW_PHASES, REVIEW_EVALUATORS
    if kind == "review-batch":
        return BATCH_PHASES, BATCH_EVALUATORS
    return PHASES, TICKET_EVALUATORS


def evaluate(run_id: str, phase: str) -> list[str]:
    """Non-mutating: errors that keep `phase` from being left."""
    if phase not in EVALUATORS:
        raise FactoryError(f"no gate for phase {phase!r}; phases: {', '.join(EVALUATORS)}")
    run = run_load(run_id)
    ws = workspace_load(run["workspace"])
    if run.get("kind") == "epic":
        return _epic_errors(run, ws, phase)
    _phases, evals = machine(run)
    if phase not in evals:
        raise FactoryError(f"{run.get('kind')} runs have no gate for phase {phase!r}; phases: {', '.join(evals)}")
    return evals[phase](run_id, run, ws)


def _epic_errors(run: dict, ws: dict, phase: str) -> list[str]:
    from . import epic
    if phase not in epic.EVALUATORS:
        raise FactoryError(f"epic gates: {', '.join(epic.EVALUATORS)}; not {phase!r}")
    return epic.EVALUATORS[phase](run, ws)


def _set_phase(run: dict, new: str) -> None:
    event(run, "phase", **{"from": run["phase"], "to": new})
    run["phase"] = new


def cmd_gate(args) -> int:
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    phase = args.phase
    if phase not in EVALUATORS:
        raise FactoryError(f"no gate for phase {phase!r}; phases: {', '.join(EVALUATORS)}")
    if run["phase"] != phase:
        raise FactoryError(f"run {run['id']} is in phase {run['phase']}, not {phase}")
    if run.get("kind") == "epic":
        return _gate_epic(args, run, ws, phase)
    phases, evals = machine(run)
    if phase not in evals:
        raise FactoryError(f"{run.get('kind')} runs have no gate for phase {phase!r}; phases: {', '.join(evals)}")
    if phase == "review" and evals is TICKET_EVALUATORS:
        plain, changes = review_status(args.run, run, ws)
        errors = plain + changes
        if errors:
            event(run, "gate_fail", phase=phase, reasons=errors)
            if changes:
                run.setdefault("loops", {"build": 0, "review": 0})
                run["loops"]["build"] = run["loops"].get("build", 0) + 1
                run["loops"]["review"] = run["loops"].get("review", 0) + 1
                limit = (ws.get("autonomy") or {}).get("max_build_loops", 3)
                if run["loops"]["build"] > limit:
                    _set_phase(run, "blocked")
                    run.setdefault("blockers", []).append({
                        "at": now(), "kind": "question",
                        "text": f"max_build_loops ({limit}) exceeded; user decision needed", "resolved": False})
                    errors = errors + [f"loops.build {run['loops']['build']} > max_build_loops {limit}: run blocked"]
                else:
                    event(run, "loop", build=run["loops"]["build"])
                    _set_phase(run, "build")
                    errors = errors + [f"phase -> build (loop {run['loops']['build']}/{limit})"]
            run_save(run)
            raise GateFailed(errors)
    else:
        errors = evals[phase](args.run, run, ws)
        if errors:
            event(run, "gate_fail", phase=phase, reasons=errors)
            run_save(run)
            raise GateFailed(errors)
    if phase == "build":
        for alias in run.get("repos", {}):
            run["repos"][alias]["head"] = git_head(repo_path_for_run(run, ws, alias))
    event(run, "gate_pass", phase=phase)
    new = phases[phases.index(phase) + 1]
    _set_phase(run, new)
    run_save(run)
    if getattr(args, "json", False):
        print(json.dumps({"run": run["id"], "passed": phase, "phase": new}))
    else:
        print(f"PASS {phase} -> {new}")
    return 0


def _gate_epic(args, run: dict, ws: dict, phase: str) -> int:
    from . import epic
    errors = _epic_errors(run, ws, phase)
    if errors:
        event(run, "gate_fail", phase=phase, reasons=errors)
        run_save(run)
        raise GateFailed(errors)
    event(run, "gate_pass", phase=phase)
    new = epic.PHASES[epic.PHASES.index(phase) + 1]
    _set_phase(run, new)
    run_save(run)
    if getattr(args, "json", False):
        print(json.dumps({"run": run["id"], "passed": phase, "phase": new}))
    else:
        print(f"PASS {phase} -> {new}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("gate", help="check exit criteria of a phase and advance")
    p.add_argument("run")
    p.add_argument("phase", choices=list(EVALUATORS))
    p.set_defaults(func=cmd_gate)
