"""`factory setup`: workspace facts for a first `factory.toml` (`--discover`) and a checklist of the current one.
It reads only: it never copies config from elsewhere, never writes a file and never prints a secret value."""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from . import config, git
from .util import FactoryError, fwd

BASE_NAMES = re.compile(r"^(main|master|trunk|develop|development|dev|staging|homolog\w*|release[/-].+|hotfix[/-].+)$")
CHECK_NAMES = re.compile(r"^(test|tests|unit|lint|check|typecheck|type-check|build|format|fmt|verify)([:._-].*)?$", re.I)
PORT_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0|\*|\+):(\d{2,5})")
CI_STEP = re.compile(r"^\s*-?\s*(?:run|script|command)\s*:\s*(\S.*)$")
PROVIDERS = ("ticket", "change", "change_list", "pipeline", "design_frame", "design_image")


def remote_of(url: str) -> dict:
    """Host and path of a remote URL; user names, passwords and tokens are dropped."""
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        return {"host": parts.hostname or "", "path": parts.path.strip("/")}
    if re.match(r"^[^/:]+@?[^/:]*:[^/]", url) and not re.match(r"^[A-Za-z]:[/\\]", url):  # scp-like user@host:group/repo
        host, path = url.split(":", 1)
        return {"host": host.rsplit("@", 1)[-1], "path": path.strip("/")}
    return {"host": "", "path": fwd(url)}  # a local path


def _git(path: Path, *args: str) -> str:
    res = git.git(path, *args, check=False, timeout=30)
    return res.out.strip() if res.exit == 0 else ""


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _stacks(path: Path) -> list[dict]:
    out = []
    pkg = path / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(_read(pkg)).get("scripts") or {}
        except (ValueError, AttributeError):
            scripts = {}
        locks = sorted(p.name for p in path.iterdir() if p.is_file() and re.search(r"lock", p.name, re.I) and p.suffix in (".json", ".yaml", ".lock", ".lockb"))
        out.append({"stack": "node", "manifest": "package.json", "lockfiles": locks, "scripts": scripts,
                    "check_candidates": [k for k in scripts if CHECK_NAMES.match(k)]})
    slns = sorted(p.relative_to(path).as_posix() for p in path.glob("*.sln"))
    projs = sorted(p.relative_to(path).as_posix() for pat in ("*.csproj", "*/*.csproj", "*/*/*.csproj") for p in path.glob(pat))
    if slns or projs:
        tests = [p for p in projs if re.search(r"test", p, re.I) or "Test.Sdk" in _read(path / p, 20_000)]
        out.append({"stack": "csharp", "solutions": slns, "projects": projs[:40], "test_projects": tests[:20]})
    py = path / "pyproject.toml"
    if py.is_file():
        try:
            doc = tomllib.loads(_read(py))
        except tomllib.TOMLDecodeError:
            doc = {}
        out.append({"stack": "python", "manifest": "pyproject.toml", "tools": sorted((doc.get("tool") or {}).keys()),
                    "scripts": (doc.get("project") or {}).get("scripts") or {}})
    make = path / "Makefile"
    if make.is_file():
        targets = sorted(set(re.findall(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)", _read(make), re.M)))
        out.append({"stack": "make", "manifest": "Makefile", "targets": targets[:40],
                    "check_candidates": [f"make {t}" for t in targets if CHECK_NAMES.match(t)]})
    return out


def _ci(path: Path) -> list[dict]:
    files = [p for p in path.glob("*.y*ml") if re.search(r"ci|pipeline", p.name, re.I)]
    files += [p for d in path.glob(".*") if d.is_dir() and d.name != ".git" for p in d.glob("workflows/*.y*ml")]
    out = []
    for p in sorted(files)[:10]:
        steps = [m.group(1).strip() for line in _read(p).splitlines() if (m := CI_STEP.match(line)) and m.group(1).strip() not in ("|", ">")]
        out.append({"file": p.relative_to(path).as_posix(), "steps": steps[:15]})
    return out


def _ports(path: Path) -> list[dict]:
    out = []
    for pat in ("Properties/launchSettings.json", "*/Properties/launchSettings.json", "src/*/Properties/launchSettings.json",
                "vite.config.*", "*/vite.config.*", "package.json"):
        for p in path.glob(pat):
            text = _read(p, 100_000)
            ports = PORT_URL.findall(text) + (re.findall(r"\bport\s*:\s*(\d{2,5})", text) if "vite" in p.name else [])
            ports += re.findall(r"--port[= ](\d{2,5})", text)
            if ports:
                out.append({"file": p.relative_to(path).as_posix(), "ports": sorted(set(ports), key=int)})
    return out[:10]


def facts(path: Path) -> dict:
    """What a person would look up before writing the repo's `[[repo]]` entry."""
    remotes: dict[str, dict] = {}
    for line in _git(path, "remote", "-v").splitlines():
        bits = line.split()
        if len(bits) >= 2:
            remotes.setdefault(bits[0], {"name": bits[0], **remote_of(bits[1])})
    default = _git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1]
    names = _git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").splitlines()
    bases = sorted({n.split("/", 1)[1] if n.startswith(tuple(f"{r}/" for r in remotes)) else n for n in names} - {"HEAD", ""})
    return {"path": fwd(path), "name": path.name, "remotes": list(remotes.values()), "current_branch": _git(path, "branch", "--show-current"),
            "default_branch": default or None, "likely_bases": [b for b in bases if BASE_NAMES.match(b)][:12],
            "stacks": _stacks(path), "ci": _ci(path), "ports": _ports(path),
            "env_files": sorted(p.name for p in path.glob(".env*") if p.is_file()),
            "app_settings": sorted(p.relative_to(path).as_posix() for pat in ("appsettings*.json", "*/appsettings*.json") for p in path.glob(pat))[:20]}


def repos_in(root: Path) -> list[Path]:
    """`root` itself when it is a repo, else its git repos one or two levels down."""
    if (root / ".git").exists():
        return [root]
    found = [p for p in sorted(root.iterdir()) if p.is_dir() and (p / ".git").exists()]
    return found or [q for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".") for q in sorted(p.iterdir())
                     if q.is_dir() and (q / ".git").exists()]


def discover(target: Path | None, project) -> dict:
    if target is None and project is not None:
        paths = [Path(r["path"]) for r in project.cfg["repo"]]
    else:
        root = (target or Path.cwd()).resolve()
        if not root.is_dir():
            raise FactoryError(f"{fwd(root)} is not a directory")
        paths = repos_in(root)
    return {"configured": project is not None, "repos": [facts(p) for p in paths if p.is_dir()]}


def _item(area: str, status: str, what: str, key: str, example: str) -> dict:
    return {"area": area, "status": status, "what": what, "key": key, "example": example}


def checklist(project) -> list[dict]:
    """Each config area as done, missing (required before a run) or optional, with its key and a one-line example."""
    cfg, items = project.cfg, []
    for r in cfg["repo"]:
        path, name = Path(r["path"]), r["name"]
        if not path.is_dir():
            items.append(_item("repos", "missing", f"{name}: {fwd(path)} does not exist", "[[repo]] path (or [repo.<name>] path in .factory/local.toml)", 'path = "../api"'))
            continue
        items.append(_item("repos", "done", f"{name}: {fwd(path)}", "[[repo]] path", 'path = "../api"'))
        base_ok = git.rev(path, r["base"]) or git.rev(path, f"{r.get('remote', 'origin')}/{r['base']}")
        items.append(_item("base branches", "done" if base_ok else "missing", f"{name}: base {r['base']}" + ("" if base_ok else " does not exist"), "[[repo]] base", 'base = "develop"'))
        runnable = [c["id"] for c in r.get("check", []) if c.get("kind") != "guideline"]
        cands = [c for s in _stacks(path) for c in s.get("check_candidates", [])]
        items.append(_item("checks", "done" if runnable else "missing" if cands else "optional",
                           f"{name}: " + (", ".join(runnable) if runnable else "no standing check" + (f" (candidates: {', '.join(cands[:6])})" if cands else "")),
                           "[[repo.check]] id, run", '[[repo.check]] id = "unit"  run = "<the repo\'s test command>"'))
        env = r.get("env")
        serves = bool(_ports(path))
        if env:
            deps = env.get("depends_on", [])
            items.append(_item("envs and dependencies", "done", f"{name}: up, base_url {env['base_url']}" + (f", depends_on {', '.join(deps)}" if deps else ""),
                               "[repo.env] up, base_url, health, ready_timeout_s, depends_on", '[repo.env] up = "<start command>"  base_url = "http://127.0.0.1:5000"  health = "/health"'))
        else:
            items.append(_item("envs and dependencies", "missing" if serves else "optional", f"{name}: no [repo.env]" + (" but the repo declares local ports" if serves else ""),
                               "[repo.env] up, base_url, health", '[repo.env] up = "<start command>"  base_url = "http://127.0.0.1:5000"'))
        spec = (env or {}).get("login_spec")
        if spec:
            unset = config.resolve_secrets(cfg, env.get("login_secrets", []))[1]
            ok = (path / spec).is_file() and not unset
            why = (" does not exist" if not (path / spec).is_file() else "") + (f"; secrets not set: {', '.join(unset)} (environment or "
                                                                                 f"{fwd(config.secrets_file(cfg['project']['name']))})" if unset else "")
            items.append(_item("login", "done" if ok else "missing", f"{name}: login spec {spec}{why}",
                               "[repo.env] login_spec, login_secrets", 'login_spec = "e2e/login.json"  login_secrets = ["TEST_USER", "TEST_PASSWORD"]'))
        elif env:
            items.append(_item("login", "optional", f"{name}: no login spec (UI checks run logged out)", "[repo.env] login_spec", 'login_spec = "e2e/login.json"'))
        if r.get("mirror"):
            items.append(_item("mirrors", "done", f"{name}: " + ", ".join(m["name"] for m in r["mirror"]), "[[repo.mirror]] name, branch", '[[repo.mirror]] name = "release"  branch = "release/2.x"'))
    if not any(r.get("mirror") for r in cfg["repo"]):
        items.append(_item("mirrors", "optional", "no hotfix mirror target", "[[repo.mirror]] name, branch", '[[repo.mirror]] name = "release"  branch = "release/2.x"'))
    _, missing = config.resolve_secrets(cfg)
    sfile = config.secrets_file(cfg["project"]["name"])
    if cfg["secrets"]["keys"]:
        items.append(_item("secrets", "missing" if missing else "done", ("not set: " + ", ".join(missing)) if missing else "every key is set",
                           f"[secrets] keys; values in the environment or {fwd(sfile)} (the user writes them)", 'keys = ["TEST_USER", "TEST_PASSWORD"]'))
    reviewers = cfg["ship"].get("reviewers") or []
    items.append(_item("ship reviewers", "done" if reviewers else "optional", f"{len(reviewers)} reviewer(s)", "[ship] reviewers",
                       'reviewers = [{ scm = "ana.lima", tracker = "5f1c2e" }]'))
    for kind in PROVIDERS:
        have = (cfg["providers"].get(kind) or {}).get("fetch")
        items.append(_item("providers", "done" if have else "optional", f"{kind}: " + ("adapter" if have else "no adapter; the agent writes the contract (source: agent, factory ingest)"),
                           f"[providers.{kind}] fetch", f'fetch = "python ~/adapters/{kind}.py {{id}}"'))
    from .checks import runner
    try:
        runner.prefix(cfg)
        status, what = "done", "installed"
    except FactoryError as exc:
        needs = any((r.get("env") or {}).get("login_spec") for r in cfg["repo"])
        status, what = ("missing" if needs else "optional"), str(exc)
    items.append(_item("browser runner", status, what, "[checks.browser] bundled (in .factory/local.toml)", 'bundled = "<checkout>/browser/src/cli.js"'))
    return items


def cmd_setup(args) -> int:
    from . import project as projmod
    try:
        proj = projmod.discover(args.project or args.project_pre)
    except FactoryError:
        proj = None
    if args.discover is not None:
        data = discover(Path(args.discover) if args.discover else None, proj)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if proj is None:
        raise FactoryError("no factory.toml here: run factory setup --discover [DIR], write factory.toml (factory init creates a skeleton), then factory setup")
    items = checklist(proj)
    missing = [i for i in items if i["status"] == "missing"]
    if args.json:
        print(json.dumps({"items": items, "missing": len(missing)}, indent=2, ensure_ascii=False))
    else:
        print("\n".join(f"[{i['status']:<8}] {i['area']}: {i['what']}" + ("" if i["status"] == "done" else f"\n           key: {i['key']}  e.g. {i['example']}")
                        for i in items))
        print(f"{len(missing)} required item(s) missing" + ("; then run factory check" if not missing else ""))
    return 1 if missing else 0
