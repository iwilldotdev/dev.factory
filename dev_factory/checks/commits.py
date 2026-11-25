"""The implicit commit-format check of a repo with `commit_pattern` or forbidden trailers [L-89, L-90]."""
from __future__ import annotations

from .. import git


def run_check(check: dict, ctx) -> dict:
    repo = ctx.repo
    problems = git.commit_problems(ctx.path, ctx.branch_point, git.base_ref(ctx.path, repo), repo.get("commit_pattern"),
                                   repo.get("commit_forbidden_trailers", []))
    shown = f"commits {ctx.branch_point[:7]}..HEAD" + (f" match {repo['commit_pattern']}" if repo.get("commit_pattern") else "")
    out = "\n".join(problems)
    return {"cmd": shown, "exit": None, "duration_s": 0.0, "output": out, "result": "fail" if problems else "pass",
            "kind": "assert" if problems else None, "reason": "; ".join(problems[:5])}
