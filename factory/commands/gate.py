"""`factory gate <run> <phase>`: exit criteria of a phase (DESIGN §3, §2.3)."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

from .. import integrity, media, mirrordiff, shots
from ..collectors.test import weak_pattern_reason
from ..core import (FactoryError, GateFailed, _dep_run_ids, code_repos, ensure_owner, event, evidence_id, git, git_dirty,
                    git_head, hotfix_of, load_evidence, node_file, now,
                    read_json, repo_conf, repo_path_for_run, run_dir, run_load,
                    run_base_branch, run_resync, run_save, run_targets, target_branch, target_worktree, workspace_load)

PHASES = ["intake", "plan", "build", "prove", "review", "ship", "feedback", "done"]
BUG_PHASES = ["intake", "plan", "reproduce", "build", "prove", "review", "ship", "feedback", "done"]
NEXT = {p: PHASES[i + 1] for i, p in enumerate(PHASES[:-1])}
DIAGNOSIS_SECTIONS = ("Root cause", "Evidence", "Ruled out")
REVIEW_PHASES = ["intake", "plan", "prove", "review", "report", "done"]  # kind review: no build, no ship
BATCH_PHASES = ["intake", "report", "done"]  # kind review-batch parent
RUNTIME_TYPES = {"browser", "visual", "style", "api"}
CHECK_TYPES = RUNTIME_TYPES | {"test", "red-green", "human", shots.SHOT_TYPE}
AC_KINDS = {"ui", "api", "logic", "data", "bug"}
LOCAL_REPRO_TYPES = {"test"}  # reproducible in a temp worktree; the only types required on mirror targets
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
DEFAULT_LIMITS = {"max_diff_ratio": 0.03, "max_tolerance_px": 2}


# ---------- shared helpers ----------

def quality_id(alias: str) -> str:
    return f"QUALITY-{alias}"


def touched_repos(run: dict) -> list[str]:
    """Repos with code in the run; env_only repos (only serve the env) are not touched."""
    return list(code_repos(run))


def resolve_base(repo_path: Path, base: str) -> str:
    """origin/<base> when that ref exists, else the local <base>."""
    if git(repo_path, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}", check=False):
        return f"origin/{base}"
    return base


def is_bug_run(run: dict) -> bool:
    return run.get("kind", "ticket") == "ticket" and any(t.get("type") == "bug" for t in run.get("tickets") or [])


def repro_id(check_id: str) -> str:
    return f"REPRO-{check_id}"


def repro_checks(run_id: str) -> list[dict]:
    """Checks referenced by `kind: bug` ACs that can be reproduced on base (red-green proves the fix later)."""
    try:
        data = load_acs(run_id)
    except FactoryError:
        return []
    by_id = {c.get("id"): c for c in data.get("checks") or []}
    out: dict[str, dict] = {}
    for ac in data.get("acs") or []:
        if ac.get("kind") != "bug":
            continue
        for cid in ac.get("checks") or []:
            chk = by_id.get(cid)
            if chk and chk.get("type") not in ("red-green", "human", shots.SHOT_TYPE):
                out[cid] = chk
    return list(out.values())


def merge_base_for(run: dict, ws: dict, alias: str, target: str | None = None) -> str:
    hf = hotfix_of(run)
    if hf and target and target != hf["base"]:
        # mirror target: nothing is cherry-picked yet at reproduce time, so the merge-base is the target tip
        main = Path(repo_conf(ws, alias).get("path") or repo_path_for_run(run, ws, alias))
        return git(main, "rev-parse", resolve_base(main, target_branch(ws, alias, target)) + "^{commit}")
    wt = repo_path_for_run(run, ws, alias)
    base = resolve_base(wt, run_base_branch(run, ws, alias))
    return git(wt, "merge-base", "HEAD", base)


def diagnosis_errors(run_id: str) -> list[str]:
    path = run_dir(run_id) / "diagnosis.md"
    if not path.is_file():
        return ["diagnosis.md missing in the run dir (author: ## Root cause, ## Evidence, ## Ruled out)"]
    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*#*\s*$", line)
        if m:
            current = m.group(1).strip().lower()
            sections.setdefault(current, "")
        elif current is not None:
            sections[current] += line + chr(10)
    errors = []
    for name in DIAGNOSIS_SECTIONS:
        if name.lower() not in sections:
            errors.append(f"diagnosis.md: section '## {name}' missing")
        elif not sections[name.lower()].strip():
            errors.append(f"diagnosis.md: section '## {name}' is empty")
    return errors


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

# expected key (style map, CSS names) -> payload keys (Figma names) whose values may back it
SIZE_KEYS = frozenset({"width", "height", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "padding",
                       "itemSpacing", "counterAxisSpacing", "cornerRadius", "rectangleCornerRadii", "gap",
                       "minWidth", "maxWidth", "minHeight", "maxHeight"})
FAMILIES = {
    "fontSize": frozenset({"fontSize"}),
    "lineHeight": frozenset({"lineHeightPx", "lineHeight"}),
    "fontWeight": frozenset({"fontWeight"}),
    "letterSpacing": frozenset({"letterSpacing"}),
    "opacity": frozenset({"opacity"}),
    "borderWidth": frozenset({"strokeWeight", "borderWidth"}),
    "fontFamily": frozenset({"fontFamily"}),
    "text": frozenset({"characters", "text"}),
}
for _k in ("width", "height", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "gap",
           "borderRadius", "minWidth", "maxWidth", "minHeight", "maxHeight"):
    FAMILIES[_k] = SIZE_KEYS
COLOR_KEYS = frozenset({"color", "backgroundColor", "borderColor"})
PAINT_KEYS = frozenset({"fills", "strokes", "background", "backgrounds", "backgroundColor", "color", "fill", "stroke"})


def _meta_files(fdir: Path) -> list[Path]:
    """figma/meta.json and figma/meta-<fileKey>.json."""
    if not fdir.is_dir():
        return []
    return sorted(p for p in fdir.iterdir() if p.is_file() and re.fullmatch(r"meta(?:-.+)?\.json", p.name))


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
    elif isinstance(node, str):
        for h in HEX_RE.findall(node):
            c = _norm_hex(h)
            if c:
                out.add(c)
        c = _expected_color(node)
        if c:
            out.add(c)


def _run_relative(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    win = PureWindowsPath(value)
    return not win.anchor and not PurePosixPath(value).anchor and ".." not in win.parts


OUTPUT_KEYS = ("out", "out_dir", "output", "output_path", "output_dir", "screenshot", "screenshot_path", "save_to")


def _inside_run_dir(run_id: str, rel: str) -> bool:
    rd = run_dir(run_id).resolve()
    try:
        return (rd / rel).resolve().is_relative_to(rd)
    except OSError:
        return False


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def payload_facts(payload) -> tuple[dict[str, set], set[str]]:
    """({payload key: {numbers and strings found under that key}}, {paint colors under fills/strokes/...})."""
    by_key: dict[str, set] = {}
    colors: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in PAINT_KEYS:
                    _walk_colors(v, colors)
                if _is_num(v):
                    by_key.setdefault(k, set()).add(float(v))
                elif isinstance(v, str):
                    by_key.setdefault(k, set()).add(v)
                    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)(?:px)?\s*", v)
                    if m:
                        by_key[k].add(float(m.group(1)))
                elif isinstance(v, list) and v and all(_is_num(x) for x in v):
                    by_key.setdefault(k, set()).update(float(x) for x in v)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return by_key, colors


def pack_facts(text: str) -> tuple[list[float], set[str]]:
    """Every number and color in a pack text (diagnostics; the style-map check uses payload_facts)."""
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


def style_map_errors(map_data: dict, pack_text: str, label: str, max_tolerance_px: float | None = None) -> list[str]:
    """Every expected value must match a value of the SAME property family in the node payload."""
    errors = []
    try:
        payload = json.loads(pack_text)
    except json.JSONDecodeError:
        return [f"{label}: Figma pack JSON is not valid"]
    by_key, colors = payload_facts(payload)
    entries = map_data.get("entries")
    if not isinstance(entries, list) or not entries:
        return [f"{label}: style map has no entries"]
    for entry in entries:
        eid = entry.get("id", "?")
        if not entry.get("selector"):
            errors.append(f"{label}: entry {eid} has no selector")
        tol = entry.get("tolerance_px")
        if tol is not None and (not _is_num(tol) or tol < 0):
            errors.append(f"{label}: entry {eid} tolerance_px must be a number >= 0")
        elif max_tolerance_px is not None and tol is not None and tol > max_tolerance_px:
            errors.append(f"{label}: entry {eid} tolerance_px {tol} exceeds limits.max_tolerance_px {max_tolerance_px}")
        expect = entry.get("expect")
        if not isinstance(expect, dict) or not expect:
            errors.append(f"{label}: entry {eid} has no expect")
            continue
        for path, value in _flatten("expect", expect):
            prop = path[len("expect."):]
            key = re.split(r"[.\[]", prop, 1)[0]
            name = f"{label}: {eid}.{prop}={value!r}"
            if isinstance(value, bool) or value is None:
                continue
            color = _expected_color(value)
            if key in COLOR_KEYS or color is not None:
                if color is None:
                    errors.append(f"{name} is not a color")
                elif key not in COLOR_KEYS:
                    errors.append(f"{name}: a color for a non-color property")
                elif color not in colors:
                    errors.append(f"{name} color not in the node's fills/strokes")
                continue
            family = FAMILIES.get(key)
            if family is None:
                errors.append(f"{name}: no Figma property family for {key!r} (cannot be backed by the pack)")
                continue
            found = set().union(*(by_key.get(k, set()) for k in family))
            if isinstance(value, str):
                m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)(?:px)?\s*", value)
                if m:
                    value = float(m.group(1))
                elif value.strip().lower() not in {str(f).strip().lower() for f in found if isinstance(f, str)}:
                    errors.append(f"{name} not in the node's {'/'.join(sorted(family))}")
                    continue
                else:
                    continue
            if _is_num(value) and not any(_is_num(n) and abs(n - value) < 1e-6 for n in found):
                errors.append(f"{name} number not in the node's {'/'.join(sorted(family))}")
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
    limits = workspace_limits(workspace)
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
        if ctype == "test" and not chk.get("cmd"):
            errors.append(f"{cid}: cmd is required")
        if ctype == "red-green":
            if chk.get("of"):
                inner = next((c for c in checks if c.get("id") == chk["of"]), None)
                if chk.get("cmd"):
                    errors.append(f"{cid}: red-green takes either cmd or of, not both")
                elif inner is None:
                    errors.append(f"{cid}: of references unknown check {chk['of']}")
                elif inner.get("type") != "test":
                    errors.append(f"{cid}: of must reference a test check (browser/api inner checks are not supported); {chk['of']} is {inner.get('type')}")
                elif inner.get("repo") != chk.get("repo"):
                    errors.append(f"{cid}: of check {chk['of']} must be in the same repo")
            elif not chk.get("cmd"):
                errors.append(f"{cid}: cmd (or of) is required")
        if ctype == "api" and not chk.get("path"):
            errors.append(f"{cid}: path is required")
        if ctype == "browser" and not chk.get("spec"):
            errors.append(f"{cid}: spec is required")
        if ctype in ("visual", "style") and not chk.get("node"):
            errors.append(f"{cid}: node is required")
        if ctype == "style" and not chk.get("map"):
            errors.append(f"{cid}: map is required")
        if chk.get("adapter") is not None:
            adapter = chk["adapter"]
            if ctype not in ("visual", "style"):
                errors.append(f"{cid}: adapter applies only to visual and style checks")
            elif not _run_relative(adapter):
                errors.append(f"{cid}: adapter must be a path relative to the run dir (e.g. checks/adapter/open.json)")
            elif not _inside_run_dir(run_id, adapter):
                errors.append(f"{cid}: adapter {adapter!r} resolves outside the run dir (link or junction): the plan lock "
                              "only covers files inside it")
        for ikey, irel in integrity.check_inputs(run_id, chk):
            if integrity.is_generated(irel):
                errors.append(f"{cid}: {ikey} {irel!r} is inside a generated dir ({', '.join(sorted(integrity.GENERATED_DIRS))}) "
                              "that the plan lock skips: move this input out of it (e.g. checks/specs/)")
        for okey in OUTPUT_KEYS:
            target = chk.get(okey)
            if isinstance(target, str) and re.match(r"^(?:\./)?checks[\/]", target.replace("\\", "/")):
                errors.append(f"{cid}: {okey} {target!r} points into checks/ (a locked plan dir): collectors write "
                              "generated files under evidence/ only")
        if ctype == "visual" and chk.get("max_diff_ratio") is not None:
            ratio = chk["max_diff_ratio"]
            if not _is_num(ratio) or ratio < 0:
                errors.append(f"{cid}: max_diff_ratio must be a number >= 0")
            elif ratio > limits["max_diff_ratio"]:
                errors.append(f"{cid}: max_diff_ratio {ratio} exceeds limits.max_diff_ratio {limits['max_diff_ratio']}")
        if chk.get("tolerance_px") is not None:
            tol = chk["tolerance_px"]
            if not _is_num(tol) or tol < 0 or tol > limits["max_tolerance_px"]:
                errors.append(f"{cid}: tolerance_px {tol!r} must be a number in [0, limits.max_tolerance_px {limits['max_tolerance_px']}]")
        if chk.get("expect_fail_pattern") is not None:
            try:
                re.compile(str(chk["expect_fail_pattern"]))
            except re.error as err:
                errors.append(f"{cid}: expect_fail_pattern is not a valid regex: {err}")
            else:
                weak = weak_pattern_reason(chk["expect_fail_pattern"])
                if weak:
                    errors.append(f"{cid}: {weak}")
        if ctype == "red-green" and not (chk.get("expect_fail_pattern") or (
                next((c for c in checks if c.get("id") == chk.get("of")), None) or {}).get("expect_fail_pattern")):
            errors.append(f"{cid}: red-green needs expect_fail_pattern (on the check or its `of` test check): the red side "
                          "must fail on the assertion, not on setup")
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
        if ac.get("kind") == "bug" and "red-green" not in types and not types & RUNTIME_TYPES:
            errors.append(f"{aid}: bug AC needs a red-green check, or a runtime check "
                          "(browser/api/visual/style) reproduced on base and passing at HEAD")
        if ac.get("kind") == "bug":
            for cid in refs:
                chk = check_ids.get(cid) or {}
                if chk.get("type") == "test" and not chk.get("expect_fail_pattern"):
                    errors.append(f"{aid}: {cid} needs expect_fail_pattern (regex of the failing test name or assertion): "
                                  "a failure without it is a setup error, not a reproduction")
    for cid in check_ids:
        if cid not in referenced:
            errors.append(f"{cid}: not referenced by any AC")
    # figma pack + style maps
    nodes = sorted({c["node"] for c in checks if c.get("type") in ("visual", "style") and c.get("node")})
    for node in nodes:
        errors.extend(pack_errors(run_id, node))
    if nodes:
        mfiles = _meta_files(run_dir(run_id) / "figma")
        if not mfiles:
            errors.append("figma/meta.json missing (file revision)")
        else:
            for meta in mfiles:
                try:
                    m = read_json(meta)
                    files = m.values() if m and all(isinstance(v, dict) for v in m.values()) else [m]
                    if not all(f.get("lastModified") or f.get("version") for f in files):
                        errors.append(f"figma/{meta.name} names no lastModified/version")
                except (FactoryError, AttributeError):
                    errors.append(f"figma/{meta.name} unreadable")
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
            errors.extend(style_map_errors(mdata, jpath.read_text(encoding="utf-8"), f"{chk['id']} ({chk['map']})",
                                           limits["max_tolerance_px"]))
    return errors


def workspace_limits(workspace: dict) -> dict:
    """Ceilings for check thresholds (workspace `limits`); a plan cannot loosen a check past them."""
    out = dict(DEFAULT_LIMITS)
    for k, v in ((workspace or {}).get("limits") or {}).items():
        if k in out and _is_num(v):
            out[k] = v
    return out


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
    errors.extend(media.errors(run_id, run))
    return errors


def env_errors(ws: dict, checks: list[dict]) -> list[str]:
    """Consolidated env gaps for runtime checks (shared by plan and reproduce)."""
    errors = []
    runtime = sorted({c["repo"] for c in checks if c.get("type") in RUNTIME_TYPES | {shots.SHOT_TYPE} and c.get("repo")})
    for alias in runtime:
        env = (repo_conf(ws, alias).get("env") if any(r.get("alias") == alias for r in ws.get("repos", [])) else None)
        if not env:
            errors.append(f"env: {alias} has no env block in workspace (user must provide start/health/base_url)")
            continue
        errors.extend(f"env {alias}: {e}" for e in _call_external("factory.commands.env", "capability", ws, alias))
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
    errors.extend(env_errors(ws, checks))
    errors.extend(shots.plan_errors(run_id, run, ws))
    return list(dict.fromkeys(errors))


def _reproduce(run_id, run, ws) -> list[str]:
    errors = []
    try:
        data = load_acs(run_id)
    except FactoryError as err:
        return [str(err)]
    by_id = {c.get("id"): c for c in data.get("checks") or []}
    bug_acs = [ac for ac in data.get("acs") or [] if ac.get("kind") == "bug"]
    if not bug_acs:
        errors.append("no kind: bug AC in acs.json (a bug run needs at least one)")
    for ac in bug_acs:
        aid = ac.get("id", "?")
        cands = [by_id[c] for c in ac.get("checks") or []
                 if c in by_id and by_id[c].get("type") not in ("red-green", "human", shots.SHOT_TYPE)]
        if not cands:
            errors.append(f"{aid}: bug AC has no reproducible check (test/browser/api/...) besides red-green")
            continue
        hf = hotfix_of(run)
        for target in run_targets(run):
            tcands = cands if not (hf and target != hf["base"]) else [c for c in cands if c.get("type") in LOCAL_REPRO_TYPES]
            if not tcands:
                continue  # runtime-only AC: reproduced on the base target; mirrors share the cause
            errors.extend(_repro_target_errors(run_id, run, ws, aid, tcands, target))
    errors.extend(diagnosis_errors(run_id))
    errors.extend(env_errors(ws, repro_checks(run_id)))
    errors.extend(shots.before_errors(run_id, run, ws))
    errors.extend(integrity.lock_errors(run, ignore=("diagnosis.md",)))
    return list(dict.fromkeys(errors))


def _repro_target_errors(run_id, run, ws, aid, cands, target) -> list[str]:
    errors, good = [], False
    tag = f"@{target}" if hotfix_of(run) else ""
    for chk in cands:
        alias = chk.get("repo")
        name = chk["id"] + tag
        rec = load_evidence_file(run_id, repro_id(evidence_id(run, chk["id"], target)))
        if rec is None:
            errors.append(f"{aid}: {name}: no REPRO evidence (factory prove {run_id} --repro)")
            continue
        bad = integrity.evidence_error(run, repro_id(evidence_id(run, chk["id"], target)))
        if bad:
            errors.append(f"{aid}: {bad}")
            continue
        if rec.get("status") != "reproduced":
            errors.append(f"{aid}: {name}: REPRO status {rec.get('status')} ({rec.get('summary')})")
            continue
        try:
            mb = merge_base_for(run, ws, alias, target)
        except (FactoryError, KeyError):
            errors.append(f"{aid}: {name}: cannot compute merge-base for {alias}")
            continue
        if rec.get("git_sha") != mb:
            errors.append(f"{aid}: {name}: REPRO evidence not bound to merge-base {mb[:8]} (sha {str(rec.get('git_sha'))[:8]})")
            continue
        good = True
    if not good and not errors:
        errors.append(f"{aid}: no reproduced check{tag}")
    return errors


def load_evidence_file(run_id: str, name: str) -> dict | None:
    return load_evidence(run_id, name)


def _remote_branch_refs(path: Path, own: set[str], allow: set[str] | None) -> list[str]:
    """refs/remotes/<remote>/<name> whose <name> is not one of the run's own branches and, when `allow` is
    given, is one of the allowed branch names (targets, stacked bases, dependency and open-MR source branches)."""
    refs = []
    for ref in git(path, "for-each-ref", "--format=%(refname)", "refs/remotes", check=False).splitlines():
        _remote, _, name = ref[len("refs/remotes/"):].partition("/")
        if not name or name == "HEAD" or name in own:
            continue
        if allow is not None and name not in allow:
            continue
        refs.append(ref)
    return refs


def inherited_allow(run: dict, ws: dict, alias: str) -> set[str]:
    """Remote branch names a commit may legitimately be inherited from: the run's target branches, the branch it
    is stacked on, dependency runs' branches and the source branches of open MRs read back into remote/."""
    rec = (run.get("repos") or {}).get(alias) or {}
    names: set[str] = set()
    for t in [*run_targets(run), rec.get("base")]:
        b = target_branch(ws, alias, t) if t else None
        if b:
            names.add(b)
    if rec.get("base_branch"):
        names.add(rec["base_branch"])
    for rid in _dep_run_ids(run, ws):
        try:
            dep = (run_load(rid).get("repos") or {}).get(alias) or {}
        except (FactoryError, OSError, ValueError):
            continue
        for b in [dep.get("branch"), *(dep.get("branches") or {}).values()]:
            if b:
                names.add(b)
    remote = run_dir(run["id"]) / "remote"
    for p in sorted(remote.glob("mr-*.json")) if remote.is_dir() else []:
        if p.name.endswith("-discussions.json"):
            continue
        try:
            data = read_json(p)
        except FactoryError:
            continue
        if isinstance(data, dict) and isinstance(data.get("merge_request"), dict):
            data = data["merge_request"]
        if isinstance(data, dict) and data.get("source_branch") and data.get("state") in (None, "opened"):
            names.add(data["source_branch"])
    return names


def merged_mr_heads(run: dict) -> list[str]:
    """Head shas of MRs read back into remote/ as merged: their commits stay inherited after the source branch is
    deleted (squash merges leave the original commits only in the stacked branch)."""
    shas: list[str] = []
    remote = run_dir(run["id"]) / "remote"
    for p in sorted(remote.glob("mr-*.json")) if remote.is_dir() else []:
        if p.name.endswith("-discussions.json"):
            continue
        try:
            data = read_json(p)
        except FactoryError:
            continue
        if isinstance(data, dict) and isinstance(data.get("merge_request"), dict):
            data = data["merge_request"]
        if isinstance(data, dict) and data.get("state") == "merged" and isinstance(data.get("sha"), str):
            shas.append(data["sha"])
    return shas


def _integrates_base(path: Path, parents: list[str], base_ref: str) -> bool:
    """A merge commit whose extra parent is already part of the base branch: it only brings the base in."""
    return len(parents) > 1 and any(
        subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", p, base_ref],
                       capture_output=True, text=True).returncode == 0 for p in parents[1:])


def commits_errors(path: Path, base_ref: str, ws: dict, alias: str, branch: str | None = None, *,
                   own: tuple[str, ...] = (), origin_head: str | None = None,
                   allow: set[str] | None = None, merged_heads: tuple[str, ...] = (),
                   branch_point: str | None = None) -> list[str]:
    """Commit header/trailer rules over the commits the run added.

    `branch_point` (the commit `run branch` created the worktree at; absent in older runs) limits the span to
    `<branch_point>..HEAD` when it is still an ancestor of HEAD, so commits the branch already had when the run
    started (a stacked or reused branch) are not the run's. Merge commits that only bring the base in skip the header
    check; their forbidden trailers still count.

    `origin_head` (a review-fix run: the MR head recorded at conversion) limits the span to
    `<origin_head>..HEAD` without merges: the MR's own earlier commits are not this run's. Otherwise
    `<base>..HEAD`, skipping commits already reachable from another remote branch: every remote branch when
    `allow` is None, else only the `allow` names; the run's own branches (`branch`, `own`) never excuse a commit."""
    commit = ws.get("commit") or {}
    regex = re.compile(commit["header_regex"]) if commit.get("header_regex") else None
    forbidden = [t.lower() for t in commit.get("forbidden_trailers") or []]
    errors = []
    span = [f"{base_ref}..HEAD"]
    if branch_point and not origin_head:
        known = git(path, "rev-parse", "--verify", "--quiet", f"{branch_point}^{{commit}}", check=False)
        if known and subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", known, "HEAD"],
                                    capture_output=True, text=True).returncode == 0:
            span = [f"{known}..HEAD", f"^{base_ref}"]
            before = git(path, "rev-list", "--count", f"{base_ref}..{known}", check=False)
            if before.isdigit() and int(before) > 0:
                print(f"{alias}: {before} commits already on the branch when the run started are not checked")
    if origin_head and git(path, "rev-parse", "--verify", "--quiet", f"{origin_head}^{{commit}}", check=False):
        proc = subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", origin_head, "HEAD"],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            span = ["--no-merges", f"{origin_head}..HEAD"]
        else:
            print(f"{alias}: MR head {origin_head[:8]} is not an ancestor of HEAD (history rewritten); "
                  f"checking {base_ref}..HEAD")
            origin_head = None
    else:
        origin_head = None
    refs = [] if origin_head else _remote_branch_refs(path, {b for b in (branch, *own) if b}, allow)
    if not origin_head:
        refs += [sha for sha in merged_heads
                 if git(path, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)]
    if refs:
        total = git(path, "rev-list", "--count", *span, check=False)
        span += ["--not", *refs]
        kept = git(path, "rev-list", "--count", *span, check=False)
        if total.isdigit() and kept.isdigit() and int(total) > int(kept):
            print(f"{alias}: {int(total) - int(kept)} inherited commits on other remote branches skipped")
    log = git(path, "log", "--format=%H%x1f%P%x1f%B%x1e", *span)
    for rec in filter(None, (r.strip() for r in log.split("\x1e"))):
        sha, _, rest = rec.partition("\x1f")
        parents, _, msg = rest.partition("\x1f")
        # A merge that only brings the base in has no header of its own to check, but its message and trailers do count.
        brings_base = _integrates_base(path, parents.split(), base_ref)
        header = msg.strip().splitlines()[0] if msg.strip() else ""
        if regex and not brings_base and not regex.search(header):
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
    if not code_repos(run):
        return ["every repo of the run is env_only; no code to build (factory run env-only <run> <repo> --off)"]
    for alias, rec in code_repos(run).items():
        path = repo_path_for_run(run, ws, alias)
        if not path.is_dir():
            errors.append(f"{alias}: worktree missing: {path}")
            continue
        if git_dirty(path):
            errors.append(f"{alias}: worktree has uncommitted changes")
        base = resolve_base(path, run_base_branch(run, ws, alias))
        ahead = git(path, "rev-list", "--count", f"{base}..HEAD", check=False)
        if not ahead.isdigit() or int(ahead) == 0:
            errors.append(f"{alias}: no commits ahead of {base}")
        else:
            origin = ((run.get("review_origin") or {}).get("head_sha") or {}).get(alias)
            own = tuple(v for v in (rec.get("branches") or {}).values() if v)
            errors.extend(commits_errors(path, base, ws, alias, rec.get("branch"), own=own, origin_head=origin,
                                         allow=inherited_allow(run, ws, alias),
                                         merged_heads=tuple(merged_mr_heads(run)),
                                         branch_point=rec.get("branch_point")))
    errors.extend(mirrordiff.errors(run, ws))  # hotfix with mirror branches: same changes as the base fix
    return errors


def _evidence_errors(run_id, run, ws, check: dict, target: str | None = None) -> list[str]:
    cid = evidence_id(run, check["id"], target)
    ev = load_evidence(run_id, cid)
    if ev is None:
        return [f"{cid}: no evidence (factory prove)"]
    bad = integrity.evidence_error(run, cid)
    if bad:
        return [bad]
    errors = []
    if ev.get("status") != "pass":
        errors.append(f"{cid}: evidence status {ev.get('status')} ({ev.get('summary')})")
    if ev.get("dirty"):
        errors.append(f"{cid}: evidence collected with a dirty worktree")
    if (ev.get("details") or {}).get("mode") == "fast":
        errors.append(f"{cid}: evidence came from `prove --fast` (bail + reuse); run `factory prove {run_id} --final`")
    if (ev.get("details") or {}).get("hot_reload") is True:
        errors.append(f"{cid}: runtime evidence came from a hot-reloaded env (process started at another HEAD); "
                      f"run `factory prove {run_id} --final` (it restarts the env at HEAD)")
    alias = check.get("repo")
    if alias:
        try:
            wt = target_worktree(run, ws, alias, target)
            head = git_head(wt) if wt else None
        except (FactoryError, KeyError):
            head = None
        if ev.get("git_sha") != head:
            errors.append(f"{cid}: evidence is stale (sha {str(ev.get('git_sha'))[:8]} != HEAD {str(head)[:8]})")
    return errors


def _bug_runtime_errors(run_id, run, ws) -> list[str]:
    """Bug ACs without red-green: a runtime check must have REPRO `reproduced` at the merge-base (and it passes at HEAD)."""
    try:
        data = load_acs(run_id)
    except FactoryError:
        return []
    by_id = {c.get("id"): c for c in data.get("checks") or []}
    errors = []
    for ac in data.get("acs") or []:
        if ac.get("kind") != "bug":
            continue
        chks = [by_id[c] for c in ac.get("checks") or [] if c in by_id]
        if any(c.get("type") == "red-green" for c in chks):
            continue
        runtime = [c for c in chks if c.get("type") in RUNTIME_TYPES]
        if not runtime:
            continue  # plan gate reports this
        target = run_targets(run)[0]
        errors.extend(e.replace(f"{ac.get('id', '?')}:", f"{ac.get('id', '?')}: runtime bug check:", 1)
                      for e in _repro_target_errors(run_id, run, ws, ac.get("id", "?"), runtime, target))
    return errors


def _prove(run_id, run, ws) -> list[str]:
    """Also re-run by the review gate, ship plan/verify and the ship gate: a commit, a dirty
    worktree or a changed plan file after prove invalidates the proof (rewind to prove)."""
    errors = integrity.lock_errors(run)
    try:
        checks = load_acs(run_id).get("checks") or []
    except FactoryError as err:
        return errors + [str(err)]
    for alias in touched_repos(run):
        for target in run_targets(run):
            path = target_worktree(run, ws, alias, target)
            if path is None:
                errors.append(f"{alias}: no worktree for target {target} (factory run mirror {run_id} {alias} --to {target})")
            elif path.is_dir() and git_dirty(path):
                errors.append(f"{alias}: worktree is dirty" + (f" ({target})" if hotfix_of(run) else ""))
    for chk in checks:
        if chk.get("type") != "human":
            for target in run_targets(run):
                if chk.get("repo") and target_worktree(run, ws, chk["repo"], target) is None:
                    continue  # reported once above
                errors.extend(_evidence_errors(run_id, run, ws, chk, target))
    errors.extend(_bug_runtime_errors(run_id, run, ws))
    errors.extend(shots.prove_errors(run_id, run, ws))
    for alias in touched_repos(run):
        if repo_conf(ws, alias).get("quality"):
            errors.extend(_evidence_errors(run_id, run, ws, {"id": quality_id(alias), "repo": alias}))
    for chk in rule_checks(ws, run):
        errors.extend(_evidence_errors(run_id, run, ws, chk))
    return errors


def latest_review_file(run_id: str, kind: str) -> tuple[dict | None, Path | None]:
    best, best_path, best_round = None, None, -1
    for p in (run_dir(run_id) / "reviews").glob(f"{kind}-*.json"):
        try:
            rec = read_json(p)
        except FactoryError:
            continue
        rnd = rec.get("round", 0) if isinstance(rec.get("round", 0), int) else 0
        if rnd > best_round:
            best, best_path, best_round = rec, p, rnd
    return best, best_path


def latest_review(run_id: str, kind: str) -> dict | None:
    return latest_review_file(run_id, kind)[0]


def review_history(run_id: str, kind: str) -> list[dict]:
    """Every recorded review of `kind`, oldest round first (unreadable files are skipped)."""
    recs = []
    for p in (run_dir(run_id) / "reviews").glob(f"{kind}-*.json"):
        try:
            rec = read_json(p)
        except FactoryError:
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return sorted(recs, key=lambda r: r.get("round", 0) if isinstance(r.get("round", 0), int) else 0)


def review_rounds(run_id: str) -> int:
    return max((latest_review(run_id, k) or {}).get("round", 0) or 0 for k in ("code", "qa"))


def human_checks(run_id: str) -> list[dict]:
    try:
        return [c for c in load_acs(run_id).get("checks") or [] if c.get("type") == "human" and c.get("id")]
    except FactoryError:
        return []


def human_pending(run_id: str, run: dict) -> list[dict]:
    """Human checks without the user's pass evidence (`factory run verify-human`)."""
    out = []
    try:
        ws = workspace_load(run["workspace"])
    except FactoryError:
        ws = None
    for chk in human_checks(run_id):
        ev = load_evidence(run_id, chk["id"])
        if ev is None or ev.get("status") != "pass" or integrity.evidence_error(run, chk["id"])                 or _human_stale(run, ws, ev):
            out.append(chk)
    return out


def _human_stale(run: dict, ws: dict | None, ev: dict) -> bool:
    """The user's confirmation is about a worktree state: a later commit or a dirty tree voids it."""
    if ev.get("dirty"):
        return True
    alias = ev.get("repo")
    if not alias or ws is None:
        return False
    try:
        path = repo_path_for_run(run, ws, alias)
        return not path.is_dir() or ev.get("git_sha") != git_head(path) or git_dirty(path)
    except (FactoryError, KeyError):
        return True


def _human_finding(f: dict, human_ids: set[str]) -> bool:
    return f.get("severity") in ("blocker", "major") and (f.get("check") in human_ids or f.get("human") is True)


def review_status(run_id, run, ws) -> tuple[list[str], list[str]]:
    """Returns (plain_errors, change_errors). change_errors send the run back to build.

    A blocker/major finding always counts: `resolved` flags are ignored (resolution = a later round).
    A qa review that is CHANGES only because of `human` checks (blocker/major findings with `check: <human id>`
    or `human: true`, or no finding at all while a human check is pending) is not a change request: the pending
    human checks go to plain_errors (ask the user; `factory run verify-human`) and no review loop is counted."""
    plain, changes = [], []
    humans = {c["id"] for c in human_checks(run_id)}
    pending = {c["id"]: c for c in human_pending(run_id, run)}
    waiting_on_human = False
    for kind in ("code", "qa"):
        rec, path = latest_review_file(run_id, kind)
        if rec is None:
            plain.append(f"no {kind} review recorded (factory review record)")
            continue
        bad = integrity.review_error(run, path, kind, rec.get("round")) or integrity.review_lock_error(run, rec, kind)
        if bad:
            plain.append(bad)
            continue
        for alias in touched_repos(run):
            try:
                head = git_head(repo_path_for_run(run, ws, alias))
            except FactoryError:
                head = None
            if (rec.get("repos") or {}).get(alias) != head:
                plain.append(f"{kind} review round {rec.get('round')} is stale for {alias}; re-review HEAD")
        blocking = [f for f in rec.get("findings") or [] if f.get("severity") in ("blocker", "major")]
        comp = rec.get("completeness") if isinstance(rec.get("completeness"), dict) else {}
        comp_clean = comp.get("gap") == 0 and comp.get("not_checked") == 0
        human_only = (kind == "qa" and rec.get("verdict") != "APPROVE" and bool(humans) and comp_clean
                      and all(_human_finding(f, humans) for f in blocking)
                      and (bool(pending) or any(_human_finding(f, humans) for f in blocking)))
        if human_only:
            waiting_on_human = waiting_on_human or bool(pending)
        elif rec.get("verdict") != "APPROVE":
            changes.append(f"{kind} review round {rec.get('round')} verdict {rec.get('verdict')}")
        for f in blocking:
            if human_only and _human_finding(f, humans):
                continue
            changes.append(f"{kind} {f.get('id')} [{f['severity']}] {f.get('file', '')} {f.get('text', '')}".strip())
    if waiting_on_human:
        for cid, chk in pending.items():
            plain.append(f"{cid}: human check pending: {chk.get('text')} (ask the user, then `factory run verify-human "
                         f"{run_id} {cid} --note \"<what the user confirmed>\" --by user --user-said \"<gist>\"`)")
    return plain, changes


def _prove_gate(run_id, run, ws) -> list[str]:
    """Gate prove: the proof criteria plus, for a hotfix whose mirrors exist, the mirror diff (the first gate that can
    see it in the normal flow: `run mirror` needs gate build passed). Rewinds re-check it in gate build; ship plan again."""
    return _prove(run_id, run, ws) + mirrordiff.errors(run, ws)


def proof_errors(run_id, run, ws) -> list[str]:
    """The prove-gate criteria re-checked against the current HEADs (nothing is re-collected)."""
    errors = (_review_prove if run.get("kind") == "review" else _prove)(run_id, run, ws)
    if errors:
        errors = errors + [f"the proof no longer matches the worktrees (commit, dirty file or plan change after prove): "
                           f"`factory run rewind {run_id} prove \"<reason>\"`, then prove again"]
    return errors


def _review(run_id, run, ws) -> list[str]:
    plain, changes = review_status(run_id, run, ws)
    return proof_errors(run_id, run, ws) + plain + changes


def _ship(run_id, run, ws) -> list[str]:
    errors = proof_errors(run_id, run, ws) + _call_external("factory.commands.ship", "verify", run_id)
    run_resync(run)  # ship.verify records its `ship` event through its own copy of the run
    return list(dict.fromkeys(errors))


def _feedback(run_id, run, ws) -> list[str]:
    """Every shipped MR: latest pipeline `success` for its head sha (or a user decision naming the pipeline)
    and zero unresolved human threads; no open blockers."""
    from . import feedback
    errors = [f"open blocker #{i}: {b.get('text')}" for i, b in enumerate(run.get("blockers", [])) if not b.get("resolved")]
    rd = run_dir(run_id)
    plan_path = rd / "ship-plan.json"
    if not plan_path.is_file():
        return errors + ["ship-plan.json missing: nothing shipped to take feedback from"]
    try:
        plan = read_json(plan_path)
    except FactoryError as err:
        return errors + [str(err)]
    waivers = [str(d.get("text", "")) for d in run.get("decisions") or []
               if d.get("by") == "user" and d.get("phase") == "feedback" and "pipeline" in str(d.get("text", "")).lower()]
    for mr in plan.get("mrs") or []:
        tag = f"{mr['repo']}-{mr['target']}"
        label = f"{mr['repo']}/{mr['target']}"
        head = mr.get("head_sha")
        rb = rd / mr.get("readback", f"remote/mr-{tag}.json")
        if rb.is_file():
            try:
                data = read_json(rb)
                data = data.get("merge_request", data) if isinstance(data, dict) else {}
                head = data.get("sha") or (data.get("diff_refs") or {}).get("head_sha") or head
            except FactoryError:
                pass
        disc = rd / "remote" / f"mr-{tag}-discussions.json"
        pipes: list = []
        if not disc.is_file():
            errors.append(f"{label}: remote/mr-{tag}-discussions.json missing (factory feedback ingest prints the fetch)")
        else:
            try:
                ddata = read_json(disc)
            except FactoryError as err:
                errors.append(str(err))
                ddata = {}
            open_threads = feedback.discussion_findings(ddata, mr["repo"])
            if open_threads:
                errors.append(f"{label}: {len(open_threads)} unresolved review thread(s); feedback ingest, fix, ship again")
            if isinstance(ddata, dict) and isinstance(ddata.get("pipelines"), list):
                pipes = [x for x in ddata["pipelines"] if isinstance(x, dict) and x.get("id") is not None]
        ppath = rd / "remote" / f"pipeline-{tag}.json"
        pipe: dict = {}
        if ppath.is_file():
            try:
                pdata = read_json(ppath)
                pipe = pdata.get("pipeline", pdata) if isinstance(pdata, dict) else {}
            except FactoryError as err:
                errors.append(str(err))
                continue
        # A waiver names the pipeline it waives: the latest one GitLab lists, else the fetched one.
        current = (max(pipes, key=lambda x: x["id"] if isinstance(x["id"], int) else -1)["id"] if pipes
                   else pipe.get("id"))
        if current is not None and any(re.search(rf"(?<!\w){re.escape(str(current))}(?!\w)", w) for w in waivers):
            continue  # the user waived this pipeline, naming its id
        if not ppath.is_file():
            errors.append(f"{label}: remote/pipeline-{tag}.json missing (fetch it; a user waiver `factory run decide "
                          f"{run_id} \"pipeline #<id>: <why>\" --by user --user-said \"<gist>\"` must name the pipeline id)")
            continue
        if pipes:
            latest = max(pipes, key=lambda x: x["id"] if isinstance(x["id"], int) else -1)
            if pipe.get("id") != latest.get("id"):
                errors.append(f"{label}: pipeline file is #{pipe.get('id')}, latest pipeline is #{latest.get('id')}")
                continue
        if pipe.get("status") != "success":
            errors.append(f"{label}: pipeline #{pipe.get('id')} status {pipe.get('status')!r}, not success")
        elif head and pipe.get("sha") != head:
            errors.append(f"{label}: pipeline #{pipe.get('id')} ran on {str(pipe.get('sha'))[:8]}, MR head is {head[:8]}")
        elif not head:
            errors.append(f"{label}: MR head sha unknown (readback missing)")
    return errors


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
    return proof_errors(run_id, run, ws) + review_status(run_id, run, ws)[0]


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


EVALUATORS = {"intake": _intake, "plan": _plan, "reproduce": _reproduce, "build": _build, "prove": _prove_gate,
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
    return (BUG_PHASES if is_bug_run(run) else PHASES), TICKET_EVALUATORS


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


def fetches_for(phase: str, run: dict, ws: dict) -> list[dict]:
    """dev.mcp calls that would supply the remote files a failing gate is missing."""
    from .. import fetches
    out: list[dict] = []
    if phase == "intake":
        out += fetches.review_intake_fetches(run, ws) if run.get("kind") == "review" else fetches.intake_fetches(run)
        out += media.fetches(run["id"], run)
    if phase == "plan":
        out += fetches.figma_fetches(run["id"])
    if phase == "feedback":
        out += fetches.feedback_fetches(run["id"], ws)
    return fetches_dedupe(out)


def fetches_dedupe(items: list[dict]) -> list[dict]:
    from ..core import dedupe_fetch
    return dedupe_fetch(items)


def _set_phase(run: dict, new: str, **extra) -> None:
    event(run, "phase", **{"from": run["phase"], "to": new}, **extra)
    run["phase"] = new


def block(run: dict, text: str, resume: str) -> None:
    """Phase -> blocked with a question blocker; `run unblock` resumes at `resume`."""
    _set_phase(run, "blocked", resume=resume)
    run.setdefault("blockers", []).append({"at": now(), "kind": "question", "text": text, "resolved": False})
    event(run, "block", blocker_kind="question", text=text, n=len(run["blockers"]) - 1)


def cmd_gate(args) -> int:
    run = run_load(args.run)
    ensure_owner(run)
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
    allow_mirror = getattr(args, "allow_mirror_divergence", None)
    if allow_mirror is not None:
        if phase not in ("build", "prove"):
            raise FactoryError("--allow-mirror-divergence applies to gate build and gate prove (and ship plan)")
        mirrordiff.record_allowance(run, ws, allow_mirror)
    ws_changed = integrity.workspace_error(run) if phase not in ("intake", "plan") else None
    if ws_changed:  # before any loop/cap logic: caps read from a changed workspace.json are not trusted
        event(run, "gate_fail", phase=phase, reasons=[ws_changed])
        run_save(run)
        raise GateFailed([ws_changed])
    if phase == "review" and evals is TICKET_EVALUATORS:
        plain, changes = review_status(args.run, run, ws)
        errors = proof_errors(args.run, run, ws) + plain + changes
        if errors:
            event(run, "gate_fail", phase=phase, reasons=errors)
            if changes:
                run.setdefault("loops", {"build": 0, "review": 0})
                run["loops"]["build"] = run["loops"].get("build", 0) + 1
                run["loops"]["review"] = run["loops"].get("review", 0) + 1
                autonomy = ws.get("autonomy") or {}
                limit = autonomy.get("max_build_loops", 3)
                max_rounds = autonomy.get("max_review_rounds", 2)
                rounds = review_rounds(args.run)
                if rounds >= max_rounds:
                    block(run, f"max_review_rounds ({max_rounds}) reached with CHANGES; user decision needed", "build")
                    errors = errors + [f"review round {rounds} >= max_review_rounds {max_rounds}: run blocked"]
                elif run["loops"]["build"] - run["loops"].get("build_approved", 0) > limit:  # approved rewinds do not count
                    block(run, f"max_build_loops ({limit}) exceeded; user decision needed", "build")
                    errors = errors + [f"loops.build {run['loops']['build']} > max_build_loops {limit}: run blocked"]
                else:
                    event(run, "loop", build=run["loops"]["build"])
                    _set_phase(run, "build")
                    errors = errors + [f"phase -> build (loop {run['loops']['build']}/{limit})"]
            run_save(run)
            raise GateFailed(errors, fetch=fetches_for(phase, run, ws))
    else:
        errors = evals[phase](args.run, run, ws)
        if errors:
            event(run, "gate_fail", phase=phase, reasons=errors)
            run_save(run)
            raise GateFailed(errors, fetch=fetches_for(phase, run, ws))
    if phase == "build":
        for alias in run.get("repos", {}):
            run["repos"][alias]["head"] = git_head(repo_path_for_run(run, ws, alias))
    if phase == "plan":
        integrity.lock_plan(run, "plan gate")
    elif phase == "reproduce":
        integrity.lock_plan(run, "reproduce gate (diagnosis.md)", keep_seq=True)
    elif phase == "prove":
        run["proved"] = proved_heads(run, ws)
    event(run, "gate_pass", phase=phase)
    new = phases[phases.index(phase) + 1]
    _set_phase(run, new)
    run_save(run)
    if getattr(args, "json", False):
        print(json.dumps({"run": run["id"], "passed": phase, "phase": new}))
    else:
        print(f"PASS {phase} -> {new}")
    return 0


def proved_heads(run: dict, ws: dict) -> dict:
    """{alias: {target or "HEAD": sha}} at the prove gate."""
    out: dict = {}
    for alias in touched_repos(run):
        for target in run_targets(run):
            wt = target_worktree(run, ws, alias, target)
            if wt is not None and wt.is_dir():
                out.setdefault(alias, {})[target or "HEAD"] = git_head(wt)
    return out


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
    p.add_argument("--allow-mirror-divergence", type=int, metavar="DECISION",
                   help="hotfix, gate build|prove: a user decision \"mirror diff: <reason>\" that allows the current mirror divergence")
    p.set_defaults(func=cmd_gate)
