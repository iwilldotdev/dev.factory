"""`factory workspace init|import|check` (DESIGN §2.1)."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path, PurePosixPath, PureWindowsPath

from .. import design, profiles as _profiles
from ..core import (SCHEMA_VERSION, url_origin, FactoryError, GateFailed, factory_home, read_json, secrets_path,
                    workspace_load, workspace_path, write_json)

TICKET_MODES = ("none", "comment", "transition", "comment+transition")
SHIP_KEYS = ("targets", "draft_default", "title_format", "ticket_mode", "ticket_comment", "ticket_transition",
             "reviewers", "hotfix", "package", "pipeline_watch")
REVIEWER_KEYS = ("scm_user", "ticket_user", "name", "email")

DEFAULT_AUTONOMY = {"ship": "ask", "max_build_loops": 3, "max_review_rounds": 2, "max_replans": 2}


def _skeleton(name: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "name": name,
        "worktree_root": "",
        "repos": [],
        "ship": {"targets": [], "draft_default": True, "ticket_mode": "comment+transition"},
        "commit": {"header_regex": None, "forbidden_trailers": []},
        "autonomy": dict(DEFAULT_AUTONOMY),
        "limits": {"max_diff_ratio": 0.03, "max_tolerance_px": 2},
        "rules": [],
        "epics": [],
    }


def _out(args, payload: dict, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
    else:
        print(text)


def cmd_init(args) -> int:
    path = workspace_path(args.name)
    if path.exists():
        raise FactoryError(f"workspace {args.name} already exists: {path}")
    write_json(path, _skeleton(args.name))
    _out(args, {"workspace": args.name, "path": str(path)}, f"created {path}")
    return 0


def convert_profile(name: str, profile: dict) -> dict:
    ws = _skeleton(name)
    repos = profile.get("repos") or []
    if isinstance(repos, dict):
        repos = [dict(v, alias=k) for k, v in repos.items()]
    for idx, repo in enumerate(repos, start=1):
        if not isinstance(repo, dict) or not repo.get("alias"):
            raise FactoryError(f"project.json repos[{idx - 1}] needs an alias")
        ws["repos"].append({
            "alias": repo["alias"],
            "name": repo.get("name") or repo["alias"],
            "order": repo.get("order", idx),
            "scm_project": repo.get("scm_project"),
            "path": "",
            "quality": {},
            "env": {},
        })
    if isinstance(profile.get("ship"), dict):
        ws["ship"].update(profile["ship"])
    if isinstance(profile.get("commit"), dict):
        ws["commit"].update(profile["commit"])
    if isinstance(profile.get("epics"), list):
        ws["epics"] = profile["epics"]
    return ws


def cmd_import(args) -> int:
    src = Path(args.src)
    profile = read_json(src)
    if not isinstance(profile, dict):
        raise FactoryError(f"{src}: expected a JSON object")
    path = workspace_path(args.name)
    if path.exists() and not args.force:
        raise FactoryError(f"workspace {args.name} already exists (use --force)")
    ws = convert_profile(args.name, profile)
    write_json(path, ws)
    _out(args, {"workspace": args.name, "path": str(path), "repos": len(ws["repos"])},
         f"imported {len(ws['repos'])} repo(s) into {path}\n"
         f"fill in worktree_root and per-repo path/quality/env, then run: factory workspace check {args.name}")
    return 0


BRANCH_PLACEHOLDERS = {"key", "slug", "target", "target_upper", "package"}


def hotfix_config(ws: dict) -> dict | None:
    """ship.hotfix, or the default (homolog + mirror pack) when a target named homolog exists."""
    ship = ws.get("ship") or {}
    if isinstance(ship.get("hotfix"), dict):
        return ship["hotfix"]
    if "homolog" in (ship.get("targets") or []):
        return {"base": "homolog", "mirror": ["pack"]}
    return None


def _argv_ok(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(a, str) and a for a in value)


def _data_errors(alias: str, env: dict) -> list[str]:
    """env.seed (null or {cmd, cwd, reset}) and env.preflight ([{name, cmd} | {name, request}]): commands are argv arrays."""
    errors: list[str] = []
    seed = env.get("seed")
    if seed is not None:
        if not isinstance(seed, dict):
            errors.append(f"{alias}: env.seed must be null or an object {{cmd, cwd, reset}}")
        else:
            for key in ("cmd", "reset"):
                if key in seed and not _argv_ok(seed[key]):
                    errors.append(f"{alias}: env.seed.{key} must be a non-empty array of strings")
            if "cwd" in seed and not isinstance(seed["cwd"], str):
                errors.append(f"{alias}: env.seed.cwd must be a string")
    pre = env.get("preflight")
    if pre is not None:
        if not isinstance(pre, list):
            errors.append(f"{alias}: env.preflight must be an array of {{name, cmd}} or {{name, request}}")
        else:
            for i, item in enumerate(pre):
                if not (isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]):
                    errors.append(f"{alias}: env.preflight[{i}] needs name (string) and cmd (non-empty array of strings) "
                                  "or request (object)")
                elif ("cmd" in item) == ("request" in item):
                    errors.append(f"{alias}: env.preflight[{i}] needs exactly one of cmd (non-empty array of strings) "
                                  "or request (object)")
                elif "cmd" in item and not _argv_ok(item["cmd"]):
                    errors.append(f"{alias}: env.preflight[{i}].cmd must be a non-empty array of strings")
                elif "request" in item:
                    errors.extend(_request_errors(f"{alias}: env.preflight[{i}].request", item["request"]))
    return errors


REQUEST_KEYS = {"method", "path", "url", "headers", "auth", "user", "bearer_local_storage", "expect_status",
                "expect_body_contains", "expect_json", "timeout_s"}


def _request_errors(where: str, req) -> list[str]:
    """env.preflight[].request: {method, path | url, headers, auth: "storage", user, bearer_local_storage, expect_status,
    expect_body_contains, expect_json, timeout_s} (DESIGN section 14)."""
    if not isinstance(req, dict):
        return [f"{where} must be an object"]
    isint = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
    errors = [f"{where}: unknown key {k!r} ({', '.join(sorted(REQUEST_KEYS))})" for k in req if k not in REQUEST_KEYS]
    if ("path" in req) == ("url" in req):
        errors.append(f"{where} needs exactly one of path (string starting with /) or url (http(s) URL)")
    elif "path" in req and not (isinstance(req["path"], str) and req["path"].startswith("/")):
        errors.append(f"{where}.path must be a string starting with /")
    elif "url" in req and not (isinstance(req["url"], str) and re.match(r"^https?://", req["url"])):
        errors.append(f"{where}.url must be an http(s) URL")
    if "method" in req and req["method"] not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        errors.append(f"{where}.method must be an HTTP method")
    status = req.get("expect_status", 200)
    if not (isint(status) or (isinstance(status, list) and status and all(isint(s) for s in status))):
        errors.append(f"{where}.expect_status must be an integer or a non-empty array of integers")
    if "expect_body_contains" in req and not (isinstance(req["expect_body_contains"], str) and req["expect_body_contains"]):
        errors.append(f"{where}.expect_body_contains must be a non-empty string")
    if "expect_json" in req and not (isinstance(req["expect_json"], dict) and req["expect_json"]
                                     and all(isinstance(k, str) and k for k in req["expect_json"])):
        errors.append(f"{where}.expect_json must be a non-empty object of dotted.path -> expected value")
    if "headers" in req and not (isinstance(req["headers"], dict)
                                 and all(isinstance(k, str) and isinstance(v, (str, int)) for k, v in req["headers"].items())):
        errors.append(f"{where}.headers must be an object of strings")
    if "auth" in req and req["auth"] != "storage":
        errors.append(f"{where}.auth must be 'storage' (the stored login session of `factory env login`)")
    for key in ("user", "bearer_local_storage"):
        if key in req and not (isinstance(req[key], str) and req[key]):
            errors.append(f"{where}.{key} must be a non-empty string")
        if key in req and req.get("auth") != "storage":
            errors.append(f"{where}.{key} needs auth: 'storage'")
    if "timeout_s" in req and not ((isint(req["timeout_s"]) or isinstance(req["timeout_s"], float)) and req["timeout_s"] > 0):
        errors.append(f"{where}.timeout_s must be a positive number")
    return errors


def _files_errors(alias: str, env: dict, main: str | None = None) -> list[str]:
    """env.files_from_main: an array of relative file paths (no absolute path, no `..`) copied from the main checkout
    into every new worktree."""
    from ..collectors.test import safe_rel
    files = env.get("files_from_main")
    if files is None:
        return []
    if not isinstance(files, list):
        return [f"{alias}: env.files_from_main must be an array of relative file paths"]
    errors = [f"{alias}: env.files_from_main[{i}] must be a relative path inside the repo (no absolute path, no `..`), got {f!r}"
              for i, f in enumerate(files) if not (isinstance(f, str) and safe_rel(f))]
    for f in files:  # a directory is a config error, reported once here (not "missing" at every env up)
        if isinstance(f, str) and safe_rel(f) and main and (Path(main) / f).is_dir():
            errors.append(f"{alias}: env.files_from_main entries must be files: {f!r} is a directory in the main checkout")
    return errors


def _warmup_errors(alias: str, env: dict) -> list[str]:
    """env.warmup: an array of paths (starting with /, resolved against the slot's base_url) or http(s) URLs, requested
    once after a cold `env up`."""
    paths = env.get("warmup")
    if paths is None:
        return []
    if not isinstance(paths, list):
        return [f"{alias}: env.warmup must be an array of paths (starting with /) or http(s) URLs"]
    return [f"{alias}: env.warmup[{i}] must be a path starting with / or an http(s) URL, got {p!r}"
            for i, p in enumerate(paths) if not (isinstance(p, str) and (p.startswith("/") or re.match(r"^https?://", p)))]


def _origin_errors(ws: dict) -> list[str]:
    """The CLI never calls remote services: an absolute `env.preflight[].request.url` or `env.warmup` URL must have the origin
    (scheme, host, port) of the base URL of some repo of the workspace env. Judged here when every base URL is known
    (no `{placeholder}`); otherwise `env check`/`env up` judge it at run time."""
    origins, known = set(), True
    for repo in ws.get("repos") or []:
        env = repo.get("env") or {}
        base = env.get("base_url") or env.get("health")
        if not base:
            continue
        if "{" in str(base) or url_origin(base) is None:
            known = False
            continue
        origins.add(url_origin(base))
    if not known:
        return []
    errors = []
    for repo in ws.get("repos") or []:
        env, alias = repo.get("env") or {}, repo.get("alias") or "?"
        urls = [(f"{alias}: env.warmup[{i}]", p) for i, p in enumerate(env.get("warmup") or []
                                                                  if isinstance(env.get("warmup"), list) else [])]
        for i, item in enumerate(env.get("preflight") or [] if isinstance(env.get("preflight"), list) else []):
            req = item.get("request") if isinstance(item, dict) else None
            if isinstance(req, dict) and "url" in req:
                urls.append((f"{alias}: env.preflight[{i}].request.url", req["url"]))
        for where, url in urls:
            if isinstance(url, str) and re.match(r"^https?://", url) and url_origin(url) not in origins:
                errors.append(f"{where} must have the origin of the base URL of a repo of the workspace env (the CLI never "
                              "calls other servers)")
    return errors


def _slot_errors(alias: str, env: dict) -> list[str]:
    """env.slots / port_stride / shareable / login.handoff_rewrite (DESIGN section 11: slots, shared repos, hand-off)."""
    import re
    errors: list[str] = []
    isint = lambda v: isinstance(v, int) and not isinstance(v, bool)  # noqa: E731
    slots = env.get("slots", 1)
    if "slots" in env and not (isint(slots) and slots >= 1):
        errors.append(f"{alias}: env.slots must be an integer >= 1")
        slots = 1
    stride = env.get("port_stride")
    if "port_stride" in env and not (isint(stride) and stride >= 1):
        errors.append(f"{alias}: env.port_stride must be an integer >= 1")
    elif slots > 1 and "port_stride" not in env:
        errors.append(f"{alias}: env.port_stride is required when env.slots > 1")
    if "shareable" in env and not isinstance(env["shareable"], bool):
        errors.append(f"{alias}: env.shareable must be true or false")
    ports = env.get("ports")
    if ports is not None and not (isinstance(ports, list) and all(isint(p) and 0 < p < 65536 for p in ports)):
        errors.append(f"{alias}: env.ports must be an array of port numbers")
        ports = None
    if slots > 1:
        for key in ("health", "base_url"):
            if env.get(key) and not re.match(r"^[a-z][a-z0-9+.\-]*://[^/?#]*:\d+(?:[/?#]|$)", str(env[key]), re.I):
                errors.append(f"{alias}: env.{key} needs an explicit port when env.slots > 1 (it moves per slot)")
        start = env.get("start")
        items = [start] if isinstance(start, str) else [str(a) for a in start] if isinstance(start, list) else []
        has_ph = any(re.search(r"\{(?:port\d*|slot)\}", a) for a in items)
        if items and not has_ph and not any(re.search(rf"(?<!\d){p}(?!\d)", a) for p in ports or [] for a in items):
            errors.append(f"{alias}: env.start carries no port to move per slot: use {{port}} / {{port0}} / {{port1}} "
                          "or the literal numbers of env.ports")
        for m in re.findall(r"\{port(\d*)\}", " ".join(items)):
            if int(m or 0) >= len(ports or []):
                errors.append(f"{alias}: env.start uses {{port{m}}} but env.ports has {len(ports or [])} port(s)")
    login = env.get("login")
    if isinstance(login, dict) and "handoff_rewrite" in login:
        if not isinstance(login["handoff_rewrite"], bool):
            errors.append(f"{alias}: env.login.handoff_rewrite must be true or false")
        elif login["handoff_rewrite"] and not re.search(r"[A-Za-z0-9_.\-]+:\d{2,5}", str(login.get("success_url_contains") or "")):
            errors.append(f"{alias}: env.login.handoff_rewrite needs env.login.success_url_contains with the fixed "
                          "host:port the login redirects to (e.g. localhost:3300/portal/coming)")
    return errors


def _ref_errors(ws: dict) -> list[str]:
    """`{base_url:<ALIAS>}` / `{port:<ALIAS>}` in env.start and env.env_vars name a repo of the workspace (DESIGN section 11)."""
    import re
    ref = re.compile(r"\{(base_url|port):([^{}]*)\}")
    repos = [r for r in ws.get("repos", []) if isinstance(r, dict)]
    known = {r.get("alias") for r in repos}
    errors: list[str] = []
    for repo in repos:
        env = repo.get("env") if isinstance(repo.get("env"), dict) else {}
        start = env.get("start")
        texts = [("env.start", a) for a in ([start] if isinstance(start, str) else start if isinstance(start, list) else [])]
        vars_ = env.get("env_vars") if isinstance(env.get("env_vars"), dict) else {}
        texts += [(f"env.env_vars.{k}", v) for k, v in vars_.items()]
        for where, text in texts:
            for kind, other in ref.findall(str(text)):
                if other not in known:
                    errors.append(f"{repo.get('alias')}: {where} uses {{{kind}:{other}}} but {other!r} is not a repo alias of the workspace")
    return errors


def hotfix_config_errors(ws: dict) -> list[str]:
    import string
    errors = []
    ship = ws.get("ship") or {}
    hf = ship.get("hotfix")
    if hf is not None:
        if not isinstance(hf, dict) or not hf.get("base") or not isinstance(hf.get("mirror", []), list):
            errors.append("ship.hotfix must be {base: <target>, mirror: [<target>, ...]}")
        else:
            targets = ship.get("targets") or []
            for t in [hf["base"], *hf.get("mirror", [])]:
                if t not in targets:
                    errors.append(f"ship.hotfix target {t!r} is not in ship.targets")
            if hf["base"] in hf.get("mirror", []):
                errors.append("ship.hotfix.mirror must not contain the base target")
    fmt = ws.get("branch_format")
    if fmt is not None:
        if not isinstance(fmt, dict):
            errors.append("branch_format must be an object {feature, bug, hotfix}")
        else:
            for key, text in fmt.items():
                if key not in ("feature", "bug", "hotfix"):
                    errors.append(f"branch_format.{key}: unknown key (feature, bug, hotfix)")
                    continue
                try:
                    names = {n for _, n, _, _ in string.Formatter().parse(str(text)) if n}
                except ValueError as err:
                    errors.append(f"branch_format.{key}: {err}")
                    continue
                bad = names - BRANCH_PLACEHOLDERS
                if bad:
                    errors.append(f"branch_format.{key}: unknown placeholder(s) {sorted(bad)}")
    return errors


def ship_config_errors(ws: dict) -> list[str]:
    """Neutral delivery keys: ship.{targets,draft_default,title_format,ticket_mode,ticket_comment,ticket_transition,
    reviewers[{scm_user,ticket_user}],pipeline_watch}, epics[].ticket_transition, repos[].scm_project, scm_user."""
    errors: list[str] = []
    ship = ws.get("ship")
    if ship is not None and not isinstance(ship, dict):
        return ["ship must be an object"]
    ship = ship or {}
    for key in ship:
        if key not in SHIP_KEYS:
            errors.append(f"ship.{key}: unknown key (known: {', '.join(SHIP_KEYS)})")
    if ship.get("ticket_mode") is not None and ship["ticket_mode"] not in TICKET_MODES:
        errors.append(f"ship.ticket_mode must be one of: {', '.join(TICKET_MODES)}")
    if ship.get("ticket_comment") not in (None, "change-links"):
        errors.append("ship.ticket_comment must be \"change-links\" (or absent)")
    if ship.get("ticket_transition") is not None and not isinstance(ship["ticket_transition"], str):
        errors.append("ship.ticket_transition must be a status name (string) or null")
    for flag in ("draft_default", "pipeline_watch"):
        if flag in ship and not isinstance(ship[flag], bool):
            errors.append(f"ship.{flag} must be true or false")
    if ship.get("title_format"):
        from .ship import TITLE_FIELDS, validate_title_format
        try:
            validate_title_format(str(ship["title_format"]), TITLE_FIELDS)
        except FactoryError as err:
            errors.append(str(err))
    reviewers = ship.get("reviewers") or []
    if not isinstance(reviewers, list):
        errors.append("ship.reviewers must be an array of scm user ids or {scm_user, ticket_user}")
    else:
        for i, item in enumerate(reviewers):
            if isinstance(item, str):
                continue
            if not isinstance(item, dict) or not item.get("scm_user"):
                errors.append(f"ship.reviewers[{i}] needs scm_user (a ticket identity alone cannot be a change reviewer)")
            else:
                for k in item:
                    if k not in REVIEWER_KEYS:
                        errors.append(f"ship.reviewers[{i}].{k}: unknown key (known: {', '.join(REVIEWER_KEYS)})")
    for i, epic in enumerate(ws.get("epics") or []):
        if isinstance(epic, dict) and epic.get("ticket_transition") is not None and not isinstance(epic["ticket_transition"], str):
            errors.append(f"epics[{i}].ticket_transition must be a status name (string) or null")
    for repo in ws.get("repos") or []:
        if isinstance(repo, dict) and repo.get("scm_project") is not None and not isinstance(repo["scm_project"], str):
            errors.append(f"{repo.get('alias') or '?'}: scm_project must be a string")
    if ws.get("scm_user") is not None and not isinstance(ws["scm_user"], str):
        errors.append("scm_user must be a string")
    return errors


def known_targets(ws: dict) -> set[str]:
    """Logical targets of the workspace: `ship.targets` plus the hotfix base and mirrors."""
    ship = ws.get("ship") or {}
    hf = ship.get("hotfix") if isinstance(ship.get("hotfix"), dict) else {}
    targets = ship.get("targets")
    mirror = hf.get("mirror")
    known = {t for t in (targets if isinstance(targets, list) else []) if isinstance(t, str)}
    if isinstance(hf.get("base"), str):
        known.add(hf["base"])
    known.update(t for t in (mirror if isinstance(mirror, list) else []) if isinstance(t, str))
    return known


def target_branches_errors(ws: dict) -> list[str]:
    """Optional per-repo `target_branches`: {logical target: git branch}; keys must be known targets."""
    known = known_targets(ws)
    errors = []
    for repo in ws.get("repos", []):
        alias = repo.get("alias") or "?"
        tb = repo.get("target_branches")
        if tb is None:
            continue
        if not isinstance(tb, dict):
            errors.append(f"{alias}: target_branches must be an object {{target: git branch}}")
            continue
        for key, branch in tb.items():
            if key not in known:
                errors.append(f"{alias}: target_branches key {key!r} is not a known target ({sorted(known)})")
            if not isinstance(branch, str) or not branch or any(c.isspace() for c in branch):
                errors.append(f"{alias}: target_branches[{key!r}] must be a non-empty string without whitespace")
    return errors


def target_paths_errors(ws: dict) -> list[str]:
    """Optional per-repo `target_paths`: {logical target: absolute directory}; keys must be known targets (a mirror target,
    or the hotfix base, served only for a repo without a run worktree). No filesystem check, like the other paths here."""
    known = known_targets(ws)
    errors = []
    for repo in ws.get("repos", []):
        alias = repo.get("alias") or "?"
        tp = repo.get("target_paths")
        if tp is None:
            continue
        if not isinstance(tp, dict):
            errors.append(f"{alias}: target_paths must be an object {{target: path}}")
            continue
        for key, path in tp.items():
            if key not in known:
                errors.append(f"{alias}: target_paths key {key!r} is not a known target ({sorted(known)})")
            if not isinstance(path, str) or not path.strip():
                errors.append(f"{alias}: target_paths[{key!r}] must be a non-empty string")
            elif not (Path(path).is_absolute() or PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute()):
                errors.append(f"{alias}: target_paths[{key!r}] must be an absolute path (got {path!r})")
    return errors


def orchestration_config_errors(ws: dict) -> list[str]:
    """Optional `orchestration` (parallel sessions) and workspace-level `env` (lease) blocks."""
    errors: list[str] = []
    orch = ws.get("orchestration")
    if orch is not None:
        if not isinstance(orch, dict):
            errors.append("orchestration must be an object {sessions, per}")
        else:
            unknown = set(orch) - {"sessions", "per", "max_parallel"}
            if unknown:
                errors.append(f"orchestration: unknown key(s) {sorted(unknown)} (sessions, per, max_parallel)")
            if not isinstance(orch.get("sessions", False), bool):
                errors.append("orchestration.sessions must be true or false")
            if orch.get("per", "ticket") not in ("ticket", "run"):
                errors.append("orchestration.per must be ticket or run")
            mp = orch.get("max_parallel")
            if "max_parallel" in orch and (isinstance(mp, bool) or not isinstance(mp, int) or mp < 1):
                errors.append("orchestration.max_parallel must be an integer >= 1")
    env = ws.get("env")
    if env is not None:
        if not isinstance(env, dict):
            errors.append("env must be an object {lease, lease_ttl_s}")
        else:
            unknown = set(env) - {"lease", "lease_ttl_s"}
            if unknown:
                errors.append(f"env: unknown key(s) {sorted(unknown)} (lease, lease_ttl_s)")
            if not isinstance(env.get("lease", False), bool):
                errors.append("env.lease must be true or false")
            ttl = env.get("lease_ttl_s", 900)
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 60:
                errors.append("env.lease_ttl_s must be an integer >= 60")
    return errors


def workspace_warnings(ws: dict) -> list[str]:
    """Non-fatal: a repo with an env block and no explicit boolean `ui` relies on guessing for the visual-evidence question."""
    out = [f"{r.get('alias') or '?'}: has an env block but no explicit `ui: true|false` (visual-evidence question falls "
           "back to UI files in the diff)"
           for r in ws.get("repos", []) if r.get("env") and not isinstance(r.get("ui"), bool)]
    fmt = (ws.get("branch_format") or {}).get("hotfix") if isinstance(ws.get("branch_format"), dict) else None
    hf = hotfix_config(ws)
    if isinstance(fmt, str) and hf and hf.get("mirror"):
        try:
            names = {n for _, n, _, _ in string.Formatter().parse(fmt) if n}
        except ValueError:
            names = set()
        if not names & {"target", "target_upper"}:
            out.append(f"branch_format.hotfix {fmt!r} has no {{target}} or {{target_upper}}: the base and the mirror "
                       "branch names would collide (a `-<target>` suffix is appended to the mirror as a fallback)")
    return out


def check_workspace(ws: dict) -> list[str]:
    missing: list[str] = []
    root = ws.get("worktree_root")
    if not root:
        missing.append("worktree_root is empty")
    elif not Path(root).parent.exists():
        missing.append(f"worktree_root parent does not exist: {root}")
    if not ws.get("repos"):
        missing.append("no repos defined")
    seen = set()
    for repo in ws.get("repos", []):
        alias = repo.get("alias") or "?"
        if alias in seen:
            missing.append(f"duplicate repo alias {alias}")
        seen.add(alias)
        path = repo.get("path")
        if not path:
            missing.append(f"{alias}: path is missing")
        elif not (Path(path) / ".git").exists():
            missing.append(f"{alias}: path is not a git repository: {path}")
        if not repo.get("quality"):
            missing.append(f"{alias}: quality commands are missing")
        if "quality_fast" in repo:
            qf = repo["quality_fast"]
            if not isinstance(qf, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in qf.items()):
                missing.append(f"{alias}: quality_fast must be an object of name -> command (a command may contain {{changed}})")
        if "ui" in repo and not isinstance(repo["ui"], bool):
            missing.append(f"{alias}: ui must be true or false (UI repo: visual-evidence question)")
        env = repo.get("env") or {}
        if not env:
            missing.append(f"{alias}: env block is missing")
        else:
            for key in ("start", "health", "base_url"):
                if not env.get(key):
                    missing.append(f"{alias}: env.{key} is missing")
            if "hot_reload" in env and not isinstance(env["hot_reload"], bool):
                missing.append(f"{alias}: env.hot_reload must be true or false")
            missing.extend(_data_errors(alias, env))
            missing.extend(_files_errors(alias, env, repo.get("path")))
            missing.extend(_warmup_errors(alias, env))
            missing.extend(_slot_errors(alias, env))
    autonomy = ws.get("autonomy") or {}
    if autonomy.get("ship", "ask") not in ("ask", "draft-auto"):
        missing.append("autonomy.ship must be ask or draft-auto")
    if "allow_timing_amends" in autonomy and not isinstance(autonomy["allow_timing_amends"], bool):
        missing.append("autonomy.allow_timing_amends must be true or false")
    missing.extend(_origin_errors(ws))
    missing.extend(_ref_errors(ws))
    if "browser_batch" in (ws.get("limits") or {}) and not isinstance(ws["limits"]["browser_batch"], bool):
        missing.append("limits.browser_batch must be true or false")
    approx = (ws.get("limits") or {}).get("approx")
    if approx is not None:
        if not isinstance(approx, dict):
            missing.append("limits.approx must be an object (px, color_delta_e, font_size_px, max_diff_ratio)")
        else:
            for k, v in approx.items():
                if k not in design.APPROX_DEFAULTS:
                    missing.append(f"limits.approx.{k}: unknown (known: {', '.join(design.APPROX_DEFAULTS)})")
                elif isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    missing.append(f"limits.approx.{k} must be a number >= 0")
    missing.extend(ship_config_errors(ws))
    missing.extend(hotfix_config_errors(ws))
    missing.extend(target_branches_errors(ws))
    missing.extend(target_paths_errors(ws))
    missing.extend(orchestration_config_errors(ws))
    missing.extend(_profiles.provider_errors(ws))
    for rule in ws.get("rules", []):
        if not rule.get("id") or not isinstance(rule.get("check"), dict):
            missing.append(f"rule needs id and check: {rule}")
    return missing


def cmd_check(args) -> int:
    ws = workspace_load(args.name)
    missing = check_workspace(ws)
    if missing:
        raise GateFailed(missing)
    warnings = workspace_warnings(ws)
    text = "\n".join([f"OK workspace {args.name}"] + [f"warning: {w}" for w in warnings])
    _out(args, {"workspace": args.name, "ok": True, "warnings": warnings}, text)
    return 0


SECRET_KEY_RE = re.compile(r"pass|secret|token|credential|api[_-]?key|auth|private", re.IGNORECASE)


def redact(value, key: str = ""):
    """Copy of a workspace.json value with every secret-looking key's value replaced (never printed)."""
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key and SECRET_KEY_RE.search(key) and value not in (None, "", False):
        return "***"
    return value


def _secret_count(name: str) -> int:
    try:
        lines = secrets_path(name).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    return sum(1 for ln in lines if "=" in ln and not ln.lstrip().startswith("#"))


def cmd_list(args) -> int:
    root = factory_home() / "workspaces"
    rows = []
    for p in sorted(root.glob("*/workspace.json")) if root.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows.append({"name": p.parent.name, "error": "unreadable workspace.json"})
            continue
        rows.append({"name": p.parent.name, "repos": [r.get("alias") for r in data.get("repos") or []],
                     "targets": (data.get("ship") or {}).get("targets") or [], "path": str(p)})
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        print(f"{r['name']}	{r.get('error') or ', '.join(r['repos'])}	{', '.join(r.get('targets') or [])}")
    if not rows:
        print("no workspaces")
    return 0


def cmd_show(args) -> int:
    ws = workspace_load(args.name)
    data = redact(ws)
    data["_secrets"] = f"{_secret_count(args.name)} key(s) in secrets/{args.name}.env (values never shown)"
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def register(sub) -> None:
    parser = sub.add_parser("workspace", help="create, import and validate workspace.json")
    ws_sub = parser.add_subparsers(dest="ws_cmd", required=True)
    p = ws_sub.add_parser("init", help="create an empty workspace")
    p.add_argument("name")
    p.set_defaults(func=cmd_init)
    p = ws_sub.add_parser("import", help="convert a dev.harness .dev/project.json")
    p.add_argument("name")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_import)
    p = ws_sub.add_parser("check", help="report missing configuration")
    p.add_argument("name")
    p.set_defaults(func=cmd_check)
    p = ws_sub.add_parser("list", help="list the configured workspaces")
    p.set_defaults(func=cmd_list)
    p = ws_sub.add_parser("show", help="workspace.json with secrets redacted")
    p.add_argument("name")
    p.set_defaults(func=cmd_show)
