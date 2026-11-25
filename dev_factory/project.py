"""Project discovery, `init`, `check` and the config summary."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config, proc, schema, tomlw
from .run import Run
from .util import FactoryError, fwd, version_ok

GITIGNORE_LINE = ".factory/"
FETCH_PLACEHOLDERS = {"id", "repo", "repo_path", "branch", "sha", "run_dir", "project_dir", "out"}
TEMPLATE_PLACEHOLDERS = {"providers.*.fetch": FETCH_PLACEHOLDERS, "metrics.tokens": {"run", "session"},
                         "checks.browser.runner": {"spec", "base_url", "out", "timeout_ms", "storage_state"}}


@dataclass
class Project:
    dir: Path
    cfg: dict
    slot: int = 0  # the env slot of the run that is being worked on (slots.apply)
    pristine: dict | None = None

    @property
    def factory_dir(self) -> Path:
        from .home import state_dir
        return state_dir(self.dir)  # a home workspace keeps runs/ and env/ in its own folder

    @property
    def runs_dir(self) -> Path:
        return self.factory_dir / "runs"

    def run(self, rid: str) -> Run:
        return Run(self.runs_dir, rid)

    def run_ids(self) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        return sorted(p.name for p in self.runs_dir.iterdir() if (p / "events.jsonl").is_file())


def find_root(start: Path | None, explicit: str | None, workspace: str | None = None, ask=None) -> tuple[Path, str | None]:
    """`--project`, then `--workspace`, then climb from the cwd to the first factory.toml [L-141], then the home
    workspaces that list this repository by path or remote. Returns the root and the repo matched by remote only."""
    from . import home
    if explicit:
        root = Path(explicit).resolve()
        if not (root / "factory.toml").is_file():
            raise FactoryError(f"no factory.toml in {fwd(root)}")
        return root, None
    if workspace:
        root = home.factory_home() / workspace
        if not (root / "factory.toml").is_file():
            raise FactoryError(f"no workspace {workspace!r} in {fwd(home.factory_home())} (workspaces: {', '.join(home.names()) or 'none'})")
        return root, None
    here = Path(start or os.getcwd()).resolve()
    for cand in (here, *here.parents):
        if (cand / "factory.toml").is_file():
            return cand, None
    found = home.matches(*home.here(here))
    if found:
        name, by_remote = home.choose(found, ask)
        return home.factory_home() / name, by_remote
    raise home.NoWorkspace(f"no workspace for {fwd(here)}: no factory.toml upward and no workspace in {fwd(home.factory_home())} "
                       "lists this repository; run factory (the setup interview) or pass --workspace NAME")


def discover(explicit: str | None = None, start: Path | None = None, workspace: str | None = None, ask=None) -> Project:
    root, by_remote = find_root(start, explicit, workspace, ask)
    cfg = config.load(root)
    if by_remote:  # another clone of a listed repo: work in this clone when the recorded path is not on this machine
        from .home import here
        top = here(Path(start or os.getcwd()))[0]
        for r in cfg["repo"]:
            if r["name"] == by_remote and top and not Path(r["path"]).is_dir():
                r["path"] = str(top)
    return Project(root, cfg)


def _slug(text: str, lower: bool = False) -> str:
    text = text.lower() if lower else text
    keep = "-" if lower else "._-"
    out = "".join(c if (c.isascii() and c.isalnum()) or c in keep else "-" for c in text).strip("-")
    return (out[:63] if lower else out) or "project"


def skeleton(root: Path, name: str, repos: list[str], base: str) -> dict:
    doc: dict = {"schema": 1, "project": {"name": name}, "repo": []}
    for p in repos:
        rname = name if p == "." else _slug((root / p).resolve().name)
        doc["repo"].append({"path": p, "name": rname, "base": base})
    return doc


def _ensure_gitignore(root: Path) -> bool:
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if GITIGNORE_LINE in text.splitlines() or ".factory" in text.splitlines():
        return False
    path.write_text(text + ("" if not text or text.endswith("\n") else "\n") + GITIGNORE_LINE + "\n", encoding="utf-8")
    return True


def init(root: Path, name: str | None, repos: list[str], base: str) -> list[str]:
    """Write factory.toml and .factory/, and ignore .factory/. Never overwrites an existing factory.toml."""
    target = root / "factory.toml"
    if target.exists():
        raise FactoryError(f"{fwd(target)} already exists; init never overwrites it")
    pname = _slug(name or root.name, lower=True)
    doc = skeleton(root, pname, repos or ["."], base)
    errs: list[str] = []
    schema.validate(doc, schema.SPEC, "", errs)
    if errs:
        raise FactoryError("generated config is invalid:\n  " + "\n  ".join(errs))
    root.mkdir(parents=True, exist_ok=True)
    target.write_text(tomlw.dumps(doc), encoding="utf-8")
    (root / ".factory").mkdir(exist_ok=True)
    done = [f"wrote {fwd(target)}"]
    if _ensure_gitignore(root):
        done.append(f"added {GITIGNORE_LINE} to {fwd(root / '.gitignore')}")
    return done


def check(project: Project) -> tuple[list[str], list[str]]:
    """(errors, warnings) of the effective config: CLI pin, repo paths, template placeholders, secrets by name."""
    cfg, errs, warns = project.cfg, [], []
    spec = cfg["project"].get("cli")
    if spec and not version_ok(spec, __version__):
        errs.append(f"project.cli requires {spec} but this CLI is {__version__} [L-149]")
    for r in cfg["repo"]:
        if not Path(r["path"]).is_dir():
            errs.append(f"repo {r['name']}: path {fwd(r['path'])} does not exist (set it in .factory/local.toml)")
    templates = [(f"providers.{k}.fetch", v["fetch"], FETCH_PLACEHOLDERS) for k, v in cfg["providers"].items() if v.get("fetch")]
    if cfg.get("metrics", {}).get("tokens"):
        templates.append(("metrics.tokens", cfg["metrics"]["tokens"], TEMPLATE_PLACEHOLDERS["metrics.tokens"]))
    if cfg["checks"].get("browser", {}).get("runner"):
        templates.append(("checks.browser.runner", cfg["checks"]["browser"]["runner"], TEMPLATE_PLACEHOLDERS["checks.browser.runner"]))
    for path, tpl, allowed in templates:
        bad = proc.placeholders(tpl) - allowed
        if bad:
            names = lambda items: ", ".join("{" + x + "}" for x in sorted(items))  # noqa: E731
            errs.append(f"{path}: unknown placeholder {names(bad)} (allowed: {names(allowed)})")
        try:
            exe = proc.split_template(tpl)[0]
        except FactoryError as exc:
            errs.append(f"{path}: {exc}")
            continue
        if not shutil.which(exe):
            warns.append(f"{path}: command {exe!r} was not found on PATH")
    warns += runner_needs(project)
    _, missing = config.resolve_secrets(cfg)
    if missing:
        warns.append("secrets not set (environment or " + fwd(config.secrets_path(cfg)) + "): "
                     + ", ".join(missing))
    return errs, warns


def runner_needs(project: Project) -> list[str]:
    """A plan with UI checks needs the bundled browser runner: say how to install it when it is missing (a warning: only verify needs it)."""
    from .checks import RUNNER, runner
    ids = [i for i in project.run_ids() if not project.run(i).state()["closed"]]
    need = []
    for rid in ids:
        try:
            plan = json.loads((project.run(rid).dir / "plan.json").read_text(encoding="utf-8"))
            need += [c.get("id") for c in plan.get("checks", []) if c.get("type") in RUNNER and not (c.get("type") == "browser" and project.cfg["checks"].get("browser", {}).get("runner"))]
        except (OSError, ValueError, AttributeError):
            continue
    if not need:
        return []
    try:
        runner.prefix(project.cfg)
    except FactoryError as exc:
        return [f"plans of open runs need the bundled browser runner for {', '.join(sorted(set(map(str, need))))}: {exc}"]
    return []


def summary(project: Project) -> dict:
    """Effective config for `status --config`: secrets by name and state, never values [L-129]."""
    cfg = project.cfg
    values, missing = config.resolve_secrets(cfg)
    return {"project": cfg["project"]["name"], "dir": fwd(project.dir), "mode": cfg["_meta"]["mode"],
            "local_toml": cfg["_meta"]["local"], "cli_pin": cfg["project"].get("cli"), "cli": __version__,
            "worktree_root": fwd(config.worktree_root(cfg)),
            "repos": [{"name": r["name"], "path": fwd(r["path"]), "base": r["base"], "env": bool(r.get("env")),
                       "checks": [c["id"] for c in r.get("check", [])]} for r in cfg["repo"]],
            "providers": sorted(k for k, v in cfg["providers"].items() if v.get("fetch")),
            "secrets": {k: ("set" if k in values else "missing") for k in cfg["secrets"]["keys"]},
            "autonomy": cfg["autonomy"], "runs": len(project.run_ids())}
