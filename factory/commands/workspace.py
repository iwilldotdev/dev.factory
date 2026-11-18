"""`factory workspace init|import|check` (DESIGN §2.1)."""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

from ..core import (SCHEMA_VERSION, FactoryError, GateFailed, factory_home, read_json, secrets_path,
                    workspace_load, workspace_path, write_json)

DEFAULT_AUTONOMY = {"ship": "ask", "max_build_loops": 3, "max_review_rounds": 2, "max_replans": 2}


def _skeleton(name: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "name": name,
        "worktree_root": "",
        "repos": [],
        "ship": {"targets": [], "draft_default": True},
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
            "gitlab_project": repo.get("gitlab_project"),
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
    """env.seed (null or {cmd, cwd, reset}) and env.preflight ([{name, cmd}]): commands are argv arrays."""
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
            errors.append(f"{alias}: env.preflight must be an array of {{name, cmd}}")
        else:
            for i, item in enumerate(pre):
                if not (isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"] and _argv_ok(item.get("cmd"))):
                    errors.append(f"{alias}: env.preflight[{i}] needs name (string) and cmd (non-empty array of strings)")
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


def target_branches_errors(ws: dict) -> list[str]:
    """Optional per-repo `target_branches`: {logical target: git branch}; keys must be known targets."""
    ship = ws.get("ship") or {}
    hf = ship.get("hotfix") if isinstance(ship.get("hotfix"), dict) else {}
    targets = ship.get("targets")
    mirror = hf.get("mirror")
    known = {t for t in (targets if isinstance(targets, list) else []) if isinstance(t, str)}
    if isinstance(hf.get("base"), str):
        known.add(hf["base"])
    known.update(t for t in (mirror if isinstance(mirror, list) else []) if isinstance(t, str))
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
            missing.extend(_slot_errors(alias, env))
    autonomy = ws.get("autonomy") or {}
    if autonomy.get("ship", "ask") not in ("ask", "draft-auto"):
        missing.append("autonomy.ship must be ask or draft-auto")
    if "allow_timing_amends" in autonomy and not isinstance(autonomy["allow_timing_amends"], bool):
        missing.append("autonomy.allow_timing_amends must be true or false")
    missing.extend(_ref_errors(ws))
    if "browser_batch" in (ws.get("limits") or {}) and not isinstance(ws["limits"]["browser_batch"], bool):
        missing.append("limits.browser_batch must be true or false")
    missing.extend(hotfix_config_errors(ws))
    missing.extend(target_branches_errors(ws))
    missing.extend(orchestration_config_errors(ws))
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
