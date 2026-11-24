"""Plan rules of the runtime check types (`http`, `browser`, `visual`, `style`, `screenshot`): fields, defaults, files [L-52, L-59, L-82, L-86]."""
from __future__ import annotations

import re
from pathlib import Path

from .checks import RUNNER, RUNTIME

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ACTION_KEYS = ("goto", "click", "fill", "wait", "press", "expect_visible", "expect_text", "expect_url")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
NEW_KEYS = {"method", "path", "headers", "body", "expect_status", "expect_json", "session", "spec", "expect_timeout_ms", "frame", "node", "url",
            "viewport", "state_spec", "wait_for", "selector", "max_diff_ratio", "masks", "fit", "settle_ms", "actions", "map", "parse", "scoped", "text", "reason"}
FIELDS = {"quality": ("run", "cwd", "parse", "scoped"), "human": ("text", "reason"),
          "http": ("method", "path", "headers", "body", "expect_status", "expect_json", "session"),
          "browser": ("spec", "expect_timeout_ms"),
          "visual": ("frame", "node", "url", "viewport", "state_spec", "wait_for", "selector", "max_diff_ratio", "masks", "fit", "settle_ms", "actions"),
          "style": ("frame", "url", "viewport", "state_spec", "wait_for", "map", "settle_ms", "actions"),
          "screenshot": ("url", "spec", "viewport", "state_spec", "selector", "wait_for", "settle_ms", "actions")}


def timeout_key(kind: str) -> str:
    return {"cmd": "cmd_timeout_s", "http": "http_timeout_s"}.get(kind, "browser_timeout_s")


def norm(c: dict, cfg: dict) -> dict:
    """Resolved fields of a runtime check as they are hashed into its spec: defaults filled, files declared [L-59]."""
    out = {k: c[k] for k in FIELDS[c["type"]] if k in c}
    if c["type"] == "http":
        out.setdefault("headers", {})
    if c["type"] == "browser":
        out["expect_timeout_ms"] = c.get("expect_timeout_ms") or cfg["checks"]["expect_timeout_ms"]
        out["runner"] = cfg["checks"].get("browser", {}).get("runner")
    if c["type"] == "visual":
        out.setdefault("masks", [])
        out.setdefault("fit", False)
    files = {*c.get("files", []), *(c[k] for k in ("spec", "state_spec", "map") if c.get(k))}
    return out | ({"files": sorted(files)} if files else {})


def _rel(rel, run_dir: Path, under: str, here: str, errs: list[str]) -> None:
    p = Path(str(rel))
    if not isinstance(rel, str) or p.is_absolute() or ".." in p.parts or p.parts[:1] != ("checks",) or not (run_dir / p).is_file() \
            or (under != "checks" and p.parts[:2] != tuple(under.split("/"))):
        errs.append(f"{here}: {rel!r} must be an existing file under {under}/ in the run directory")


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def actions_problems(actions, here: str) -> list[str]:
    ok = isinstance(actions, list) and actions and all(isinstance(a, dict) and len(a) == 1 and next(iter(a)) in ACTION_KEYS for a in actions)
    return [] if ok else [f"{here}: must be a non-empty list of one-key objects with keys from {', '.join(ACTION_KEYS)}"]


def type_problems(c: dict, here: str, run_dir: Path) -> list[str]:
    kind, errs = c.get("type"), []
    allowed = set(FIELDS.get(kind, ())) | {"files"}
    errs += [f"{here}.{k}: does not belong to a {kind} check" for k in sorted(set(c) & NEW_KEYS - allowed)]
    if kind == "human":
        if not (isinstance(c.get("text"), str) and c["text"].strip()):
            errs.append(f"{here}.text: required, what the user is asked to confirm (it is shown with their answer in the change body)")
        if c.get("reason") is not None and not isinstance(c["reason"], str):
            errs.append(f"{here}.reason: must be a string (why a command cannot check it)")
        if c.get("reproduce") or c.get("red_green"):
            errs.append(f"{here}: a human check cannot reproduce a bug [L-24]")
    elif kind == "http":
        if c.get("method") not in METHODS:
            errs.append(f"{here}.method: must be one of {', '.join(METHODS)}")
        path = c.get("path")
        if not (isinstance(path, str) and path.startswith("/") and not path.startswith("//") and "\\" not in path):
            errs.append(f"{here}.path: must start with a single / (it is resolved against the env base_url only) [L-82]")
        if not (isinstance(c.get("headers", {}), dict) and all(isinstance(v, str) for v in c.get("headers", {}).values())):
            errs.append(f"{here}.headers: must be a table of strings (values may be ${{SECRET_KEY}} references)")
        st = c.get("expect_status")
        if not (isinstance(st, int) and not isinstance(st, bool) and 100 <= st <= 599):
            errs.append(f"{here}.expect_status: required, an HTTP status such as 200")
        if "session" in c and not (isinstance(c["session"], str) and SLUG.match(c["session"])):
            errs.append(f"{here}.session: must be a login user slug")
    elif kind == "browser":
        _rel(c.get("spec"), run_dir, "checks", f"{here}.spec", errs)
        if "expect_timeout_ms" in c and not _pos_int(c["expect_timeout_ms"]):
            errs.append(f"{here}.expect_timeout_ms: must be a positive integer")
    elif kind in ("visual", "style", "screenshot"):
        if kind == "screenshot":
            if bool(c.get("url")) == bool(c.get("spec")):
                errs.append(f"{here}: a screenshot check names exactly one of url and spec")
            if c.get("ac"):
                errs.append(f"{here}.ac: a screenshot is an artifact and never satisfies an AC [L-56]")
            if c.get("spec"):
                _rel(c["spec"], run_dir, "checks", f"{here}.spec", errs)
        elif not (isinstance(c.get("frame"), str) and c["frame"].strip()):
            errs.append(f"{here}.frame: required, a design_frame id")
        if "url" in c and not (isinstance(c["url"], str) and c["url"].startswith("/") and not c["url"].startswith("//")):
            errs.append(f"{here}.url: must be a path starting with a single / (resolved against the env base_url)")
        elif "url" not in c and kind != "screenshot":
            errs.append(f"{here}.url: required")
        vp = c.get("viewport")
        if vp is not None and not (isinstance(vp, list) and len(vp) == 2 and all(_pos_int(v) for v in vp)):
            errs.append(f"{here}.viewport: must be [width, height] in pixels")
        if c.get("state_spec"):
            _rel(c["state_spec"], run_dir, "checks", f"{here}.state_spec", errs)
        if "actions" in c:
            errs += actions_problems(c["actions"], f"{here}.actions")
        if "settle_ms" in c and not (isinstance(c["settle_ms"], int) and not isinstance(c["settle_ms"], bool) and c["settle_ms"] >= 0):
            errs.append(f"{here}.settle_ms: must be an integer of milliseconds")
        if kind == "style":
            _rel(c.get("map"), run_dir, "checks/style", f"{here}.map", errs)
        if kind == "visual":
            r = c.get("max_diff_ratio")
            if r is not None and not (isinstance(r, (int, float)) and not isinstance(r, bool) and 0 <= r <= 1):
                errs.append(f"{here}.max_diff_ratio: must be a number from 0 to 1")
            if "fit" in c and not isinstance(c["fit"], bool):
                errs.append(f"{here}.fit: must be true or false")
    return errs


NOTE_SECTIONS = ("Approach", "Complexity", "Reuse", "Architecture risks")


def note_problems(plan: dict, run_dir: Path) -> list[str]:
    """`risk` medium or high needs notes/design.md with the four sections, written before the plan locks [L-176]."""
    if plan.get("risk", "low") not in ("medium", "high"):
        return []
    path = run_dir / "notes" / "design.md"
    if not path.is_file():
        return [f"risk is {plan['risk']}: write {path.as_posix()} with ## " + ", ## ".join(NOTE_SECTIONS) + " (N+1 queries, loops over lists and the layers touched) [L-176]"]
    secs, title = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            title = line[3:].strip().lower()
            secs[title] = ""
        elif title is not None:
            secs[title] += line
    return [f"notes/design.md: ## {s} is missing or empty [L-176]" for s in NOTE_SECTIONS if not secs.get(s.lower(), "").strip()]


def env_problems(plan: dict, cfg: dict) -> list[str]:
    """Env capability of every runtime check, in one list [L-86]."""
    from .checks import runner
    by_name = {r["name"]: r for r in cfg["repo"]}
    out: list[str] = []
    for c in plan.get("checks", []):
        if not isinstance(c, dict) or c.get("type") not in RUNTIME:
            continue
        repo = by_name.get(c.get("repo"))
        env = (repo or {}).get("env")
        here = f"{c.get('id')} ({c['type']}, repo {c.get('repo')})"
        if repo is None:
            continue
        if not env:
            out.append(f"{here}: the repo has no [repo.env], so a runtime check cannot run; add up, base_url and health to factory.toml")
        elif c["type"] == "http" and c.get("session") and not env.get("login_http"):
            out.append(f"{here}: session needs [repo.env] login_http")
        if c["type"] in RUNNER and not (c["type"] == "browser" and cfg["checks"].get("browser", {}).get("runner")):
            try:
                runner.prefix(cfg)
            except Exception as exc:  # noqa: BLE001 - a missing runner is one reason
                out.append(f"{here}: {exc}")
    return sorted(set(out), key=out.index)
