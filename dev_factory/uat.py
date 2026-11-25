"""UAT in the browser: after the code and qa reviews, a fresh reviewer uses the screens of a change with a UI as a user would, in
the served env, logged in. Exploration scripts under `<run>/uat/` run in the bundled runner and leave `uat` evidence (screenshots,
output) that the `uat` review cites; they never satisfy a check, and what they find becomes findings [L-22, L-30]."""
from __future__ import annotations

from pathlib import Path

from . import git, plan as planmod
from .util import FactoryError, sha256_file

UI_TYPES = ("browser", "visual", "style", "screenshot")
FOLDER = "uat"


def needed(plan: dict, cfg: dict) -> bool:
    """A change with a UI: a check that drives or captures a screen."""
    return any(c["type"] in UI_TYPES for c in planmod.effective_checks(plan, cfg))


def ui_repos(plan: dict, cfg: dict) -> list[str]:
    return sorted({c["repo"] for c in planmod.effective_checks(plan, cfg) if c["type"] in UI_TYPES})


def check_of(cfg: dict, rel: str, repo: str) -> dict:
    return planmod._norm({"id": f"UAT-{Path(rel).name.split('.')[0]}", "type": "browser", "repo": repo, "spec": rel}, cfg, False)


def run_script(project, run, script: str, repo: str | None, actor: str | None) -> dict:
    """Run one exploration script in the env of `repo` (logged in as the plan's user) and record it as `uat` evidence."""
    from . import verify
    rel = Path(script).as_posix()
    path = (run.dir / rel).resolve()
    if not path.is_relative_to((run.dir / FOLDER).resolve()) or not path.is_file():
        raise FactoryError(f"the exploration script must be a file under {(run.dir / FOLDER).as_posix()}/ (got {script})")
    b = verify.Batch(project, run, verify.Opts(actor=actor))
    repos = ui_repos(b.plan, project.cfg)
    if not repos:
        raise FactoryError("this plan has no screen (no browser, visual, style or screenshot check), so it has no UAT")
    repo = repo or (repos[0] if len(repos) == 1 else None)
    if repo not in repos:
        raise FactoryError(f"pass --repo with one of the repos that have a screen: {', '.join(repos)}")
    c = check_of(project.cfg, path.relative_to(run.dir.resolve()).as_posix(), repo)
    b.spec = lambda _c: sha256_file(path)  # an exploration is not in the plan lock; its own hash binds the record
    tree = b.tree(repo)
    return b.run_one(c, "uat", tree, git.head(tree), not git.dirty(tree))


def records(st: dict, heads: dict[str, str]) -> list[dict]:
    """The `uat` evidence at the current heads."""
    return [e for e in st["evidence"] if e["mode"] == "uat" and e["sha"] == heads.get(e["repo"])]
