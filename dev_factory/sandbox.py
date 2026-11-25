"""Reviewer sandboxes: a detached worktree per repo at the run's HEAD, where a reviewer may break code to see a check fail
(mutation tests) without touching the run worktrees that other reviewers read and that evidence is recorded from."""
from __future__ import annotations

import uuid
from pathlib import Path

from . import config, git
from .run import Run
from .util import FactoryError, fwd


def root(project, run: Run) -> Path:
    return config.worktree_root(project.cfg) / run.id / "_sandbox"


def create(project, run: Run, actor: str | None) -> list[dict]:
    """One sandbox folder with a worktree per planned repo, each at the HEAD of the run worktree and with the repo's setup done."""
    st, repos = run.state(), {r["name"]: r for r in project.cfg["repo"]}
    folder = root(project, run) / f"{git.slug(actor or 'reviewer', 20) or 'reviewer'}-{uuid.uuid4().hex[:6]}"
    out = []
    for name in st["repos"]:
        w = git.current(st, name)
        if w is None or not Path(w["path"]).is_dir():
            raise FactoryError(f"repo {name}: the run worktree is missing; there is nothing to copy into a sandbox")
        repo, sha, path = repos[name], git.head(Path(w["path"])), folder / name
        git.git(Path(repo["path"]), "worktree", "add", "--detach", str(path), sha)
        setup = git.run_setup(project.cfg, repo, path)
        run.append({"type": "worktree", "repo": name, "path": fwd(path), "branch": None, "branch_point": sha, "throwaway": True,
                    "sandbox": True, **setup}, actor)
        out.append({"repo": name, "path": fwd(path), "sha": sha, "setup": setup["setup"]})
    return out


def drop(project, run: Run) -> list[str]:
    """Remove every sandbox of the run (whatever was changed in them)."""
    base, out = root(project, run), []
    for repo in project.cfg["repo"]:
        for path in sorted(base.glob(f"*/{repo['name']}")) if base.is_dir() else []:
            git.remove_tree(Path(repo["path"]), path)
            out.append(f"removed sandbox {fwd(path)}")
    for folder in sorted(base.glob("*")) if base.is_dir() else []:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    if base.is_dir() and not any(base.iterdir()):
        base.rmdir()
    return out
