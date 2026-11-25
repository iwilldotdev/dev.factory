"""Local git through `proc`: run and throwaway worktrees, branch points, dirty checks, commit scans [L-06, L-72, L-89, L-90]."""
from __future__ import annotations

import re
import shutil
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config, proc
from .run import Run
from .util import FactoryError, fwd, redact

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def git(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> proc.Result:
    res = proc.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, env=proc.child_env({"GIT_TERMINAL_PROMPT": "0"}),
                   timeout_s=timeout)
    if check and res.exit != 0:
        raise FactoryError(f"git {' '.join(args[:3])} failed in {fwd(repo)}: {(res.err or res.out).strip()[-300:]}")
    return res


_MEMO: dict | None = None


@contextmanager
def memo():
    """Inside a command that only reads git state, `rev`, `dirty` and `ahead` ask git once per question (a gate chain repeats them)."""
    global _MEMO
    outer, _MEMO = _MEMO, _MEMO if _MEMO is not None else {}
    try:
        yield
    finally:
        _MEMO = outer


def _once(key: tuple, compute):
    if _MEMO is None:
        return compute()
    if key not in _MEMO:
        _MEMO[key] = compute()
    return _MEMO[key]


def _gitdirs(path: Path) -> tuple[Path, Path] | None:
    """(git dir of a work tree, the common git dir that holds the refs) from the files, for the usual layouts only."""
    try:
        dot = Path(path) / ".git"
        gitdir = dot
        if dot.is_file():
            text = dot.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = (Path(path) / text[7:].strip()).resolve()
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        return gitdir, common
    except (OSError, ValueError):
        return None


def _loose_or_packed(dirs: tuple[Path, Path], ref: str) -> str | None:
    try:
        for base in dict.fromkeys(dirs):
            if (base / ref).is_file():
                sha = (base / ref).read_text(encoding="utf-8").strip()
                return sha if SHA.match(sha) else None
        packed = dirs[1] / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref) and line[:1] not in "#^":
                    return line.split()[0] if SHA.match(line.split()[0]) else None
    except (OSError, ValueError):
        pass
    return None


def _read_head(path: Path) -> str | None:
    """HEAD of a work tree from its files (no process); anything unusual returns None and git is asked."""
    dirs = _gitdirs(path)
    try:
        head = (dirs[0] / "HEAD").read_text(encoding="utf-8").strip() if dirs else ""
    except OSError:
        return None
    if head.startswith("ref: "):
        return _loose_or_packed(dirs, head[5:])
    return head if SHA.match(head) else None


def _read_branch(path: Path, ref: str) -> str | None:
    """A local or remote-tracking branch from its ref file; a name that a tag could also mean goes to git."""
    dirs = _gitdirs(path)
    if not dirs or not re.fullmatch(r"[A-Za-z0-9._/-]+", ref) or ".." in ref or ref.endswith(".lock") or _loose_or_packed(dirs, f"refs/tags/{ref}"):
        return None
    return _loose_or_packed(dirs, f"refs/heads/{ref}") or _loose_or_packed(dirs, f"refs/remotes/{ref}")


def rev(repo: Path, ref: str) -> str | None:
    """Full SHA of a ref, or None when it does not resolve."""
    if (fast := _read_head(repo) if ref == "HEAD" else _read_branch(repo, ref)):
        return fast

    def ask():
        res = git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return res.out.strip() if res.exit == 0 and res.out.strip() else None
    return _once(("rev", str(repo), ref), ask)


def head(path: Path) -> str:
    sha = rev(path, "HEAD")
    if sha is None:
        raise FactoryError(f"{fwd(path)} has no commit yet")
    return sha


def dirty(path: Path) -> list[str]:
    """Changed and untracked paths of a worktree (untracked files count) [L-07]."""
    out = _once(("dirty", str(path)), lambda: git(path, "status", "--porcelain", "--untracked-files=all").out)
    return [line for line in out.splitlines() if line.strip()]


_HISTORY: dict[tuple, int | bool] = {}  # answers about two commit ids never change, so they are kept for the life of the process


def ahead(path: Path, base: str) -> int:
    head = _read_head(path)
    if head == base:
        return 0
    if head and SHA.match(base):
        key = ("ahead", head, base)
        if key not in _HISTORY:
            _HISTORY[key] = int(git(path, "rev-list", "--count", f"{base}..{head}").out.strip() or 0)
        return _HISTORY[key]
    return int(_once(("ahead", str(path), base), lambda: git(path, "rev-list", "--count", f"{base}..HEAD").out.strip()) or 0)


def is_ancestor(path: Path, old: str, new: str) -> bool:
    key = ("ancestor", old, new)
    if key in _HISTORY:
        return bool(_HISTORY[key])
    res = git(path, "merge-base", "--is-ancestor", old, new, check=False)
    if SHA.match(old) and SHA.match(new) and res.exit in (0, 1):
        _HISTORY[key] = res.exit == 0
    return res.exit == 0


def show_file(path: Path, sha: str, rel: str) -> bytes:
    """A file exactly as git stores it at `sha`; the red side of red-green reads test files this way [L-35]."""
    res = git(path, "show", f"{sha}:{rel}")
    return res.raw


def slug(text: str, limit: int = 40) -> str:
    """Lowercase ASCII words joined by `-` (accents folded: "árvore" -> "arvore"), cut at a word boundary within `limit`."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    out = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if len(out) > limit:
        out = out[:limit + 1].rsplit("-", 1)[0] if "-" in out[:limit + 1] else out[:limit]
    return out.strip("-") or "change"


def branch_name(template: str, kind: str, key: str, title: str) -> str:
    name = template.format(kind=kind, key=key, slug=slug(title))
    name = re.sub(r"[^A-Za-z0-9._/-]", "-", name)
    name = re.sub(r"\.{2,}|/{2,}", "-", name).strip("/.")
    return name[:-5] if name.endswith(".lock") else name


_OVERLAY: dict[str, dict] = {}


@contextmanager
def overlay(trees: dict[str, dict]):
    """Inside the block `current` answers with these worktree events: `verify --mirror` serves and checks the mirror trees [L-99]."""
    _OVERLAY.update(trees)
    try:
        yield
    finally:
        for repo in trees:
            _OVERLAY.pop(repo, None)


def current(st: dict, repo: str) -> dict | None:
    """The run worktree event of a repo (the latest non-throwaway one)."""
    if repo in _OVERLAY:
        return _OVERLAY[repo]
    found = [w for w in st["worktrees"] if w["repo"] == repo and not w.get("throwaway")]
    return found[-1] if found else None


def run_setup(cfg: dict, repo: dict, path: Path) -> dict:
    """The repo's `setup` command in a worktree: its own dependencies, never a link to another checkout [L-72]."""
    if not repo.get("setup"):
        return {"setup": "none"}
    res = proc.run(repo["setup"], cwd=path, env=proc.child_env(), timeout_s=cfg["checks"]["cmd_timeout_s"], shell=True)
    values = config.resolve_secrets(cfg)[0].values()
    tail = redact("\n".join((res.out + res.err).splitlines()[-10:]), values)
    if res.timed_out or res.exit != 0:
        return {"setup": "fail", "setup_exit": res.exit, "setup_tail": tail}
    return {"setup": "pass", "setup_exit": 0}


def _event(repo: dict, path: Path, branch: str | None, bp: str, throwaway: bool, setup: dict) -> dict:
    return {"type": "worktree", "repo": repo["name"], "path": fwd(path), "branch": branch, "branch_point": bp,
            "throwaway": throwaway, **setup}


def add_worktree(project, run: Run, repo: dict, kind: str, title: str, actor: str | None = None, base_ref: str | None = None) -> dict:
    """Create (or finish the setup of) the run worktree of one repo; the branch point is recorded here [L-89].
    `base_ref` starts a stacked branch from another commit (the head of the dependency) [L-91, L-92]."""
    cfg, main = project.cfg, Path(repo["path"])
    st = run.state()
    have = current(st, repo["name"])
    if have and Path(have["path"]).is_dir():
        if have.get("setup") == "pass" or have.get("setup") == "none":
            raise FactoryError(f"the worktree of {repo['name']} already exists: {have['path']}")
        setup = run_setup(cfg, repo, Path(have["path"]))
        return run.append(_event(repo, Path(have["path"]), have["branch"], have["branch_point"], False, setup), actor)
    base = base_ref or rev(main, repo["base"]) or rev(main, f"{repo.get('remote', 'origin')}/{repo['base']}")
    if base is None:
        raise FactoryError(f"base branch {repo['base']!r} of repo {repo['name']} does not exist in {fwd(main)}")
    path = config.worktree_root(cfg) / run.id / repo["name"]
    if path.exists():
        raise FactoryError(f"{fwd(path)} exists but is not a worktree of this run; remove it or set worktree_root in .factory/local.toml")
    branch = branch_name(repo["branch"], kind, st["ticket"], title)
    if rev(main, f"refs/heads/{branch}"):
        git(main, "worktree", "add", str(path), branch)
        bp = git(main, "merge-base", base, branch).out.strip()
    else:
        git(main, "worktree", "add", "-b", branch, str(path), base)
        bp = base
    return run.append(_event(repo, path, branch, bp, False, run_setup(cfg, repo, path)), actor)


def remove_tree(main: Path, path: Path) -> None:
    """Remove a throwaway worktree. Links inside it are unlinked, never followed [L-72]."""
    if git(main, "worktree", "remove", str(path), check=False).exit == 0:
        return
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    git(main, "worktree", "prune", check=False)


def retire(main: Path, tree: dict, base: str) -> list[str]:
    """`run delete`: remove a clean worktree and a branch with no commit beyond its base; keep and name anything else."""
    path, branch, out = Path(tree["path"]), tree.get("branch"), []
    keep_branch = f"; kept branch {branch}" if branch else ""
    if path.is_dir():
        changed = dirty(path)
        if changed:
            return [f"kept worktree {fwd(path)}: {len(changed)} uncommitted path(s){keep_branch}"]
        if git(main, "worktree", "remove", str(path), check=False).exit:
            return [f"kept worktree {fwd(path)}: git could not remove it{keep_branch}"]
        out.append(f"removed worktree {fwd(path)}")
        if path.parent.is_dir() and not any(path.parent.iterdir()):
            path.parent.rmdir()
    if branch and rev(main, f"refs/heads/{branch}"):
        excl = [f"^{r}" for r in (tree.get("branch_point"), rev(main, base)) if r]
        unique = int(git(main, "rev-list", "--count", f"refs/heads/{branch}", *excl).out.strip() or 0)
        if unique:
            out.append(f"kept branch {branch}: {unique} commit(s) beyond its base")
        elif git(main, "branch", "-D", branch, check=False).exit == 0:
            out.append(f"deleted branch {branch}")
        else:
            out.append(f"kept branch {branch}: git could not delete it")
    return out


@contextmanager
def throwaway(project, run: Run, repo: dict, sha: str, label: str, actor: str | None = None):
    """A detached worktree at `sha` with the repo's setup done; yields (path, setup result). Removed afterwards."""
    main = Path(repo["path"])
    path = config.worktree_root(project.cfg) / run.id / "_throwaway" / f"{repo['name']}-{label}-{uuid.uuid4().hex[:6]}"
    git(main, "worktree", "add", "--detach", str(path), sha)
    try:
        setup = run_setup(project.cfg, repo, path)
        run.append(_event(repo, path, None, sha, True, setup), actor)
        yield path, setup
    finally:
        remove_tree(main, path)


def commit_problems(path: Path, bp: str, base: str, pattern: str | None, forbidden: list[str]) -> list[str]:
    """Commits in `branch_point..HEAD` (minus what the base already has) that break the header pattern or carry a
    forbidden trailer. A merge that only integrates the base skips the header check, never the trailer check [L-89, L-90]."""
    args = ["log", "--format=%H%x1f%P%x1f%B%x1e", "HEAD", f"^{bp}"]
    base_sha = rev(path, base)
    if base_sha:
        args.append(f"^{base_sha}")
    problems: list[str] = []
    for rec in git(path, *args).out.split("\x1e"):
        if not rec.strip():
            continue
        sha, parents, body = rec.strip("\n").split("\x1f", 2)
        parents = parents.split()
        subject = body.strip().splitlines()[0] if body.strip() else ""
        integrates = len(parents) > 1 and base_sha and any(is_ancestor(path, p, base_sha) for p in parents[1:])
        if pattern and not integrates and not re.search(pattern, subject):
            problems.append(f"{sha[:7]}: header {subject!r} does not match commit_pattern {pattern!r}")
        for key in forbidden:
            if any(re.match(rf"^\s*{re.escape(key)}\s*:", line, re.I) for line in body.splitlines()):
                problems.append(f"{sha[:7]}: carries the forbidden trailer {key}")
    return problems


def changed_files(path: Path, bp: str) -> list[str]:
    """Files added, copied, modified or renamed in `branch_point..HEAD` that still exist: what a scoped quality run scans [L-60]."""
    out = git(path, "diff", "--name-only", "--diff-filter=ACMR", bp, "HEAD").out
    return sorted(ln for ln in out.splitlines() if ln.strip())
