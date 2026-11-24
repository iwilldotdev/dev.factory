"""`factory verify` and `factory reproduce`: ordered, reusable, fail-fast runs that append evidence [L-10, L-11, L-14, L-34, L-47].
Both read the plan lock and refuse when the plan or config drifted; neither writes anything a gate could not re-derive."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import checks as chk, config, evidence, git, lock, plan as planmod
from .run import Run
from .util import FactoryError, write_atomic

OK = ("pass", "reproduced")


@dataclass
class Opts:
    check: str | None = None
    repo: str | None = None
    fresh: bool = False
    retry: bool = True
    actor: str | None = None


class Batch:
    """Shared state of one verify or reproduce call: the lock, selected checks, secrets and the fail-fast memory."""

    def __init__(self, project, run: Run, opts: Opts):
        self.project, self.run, self.opts, self.cfg = project, run, opts, project.cfg
        self.st = run.state()
        problems = lock.problems(run, self.cfg, self.st, strict=False)
        if problems:
            raise FactoryError("cannot run checks:\n" + "\n".join(f"  - {p}" for p in problems))
        self.lock, self.plan = lock.current(self.st), planmod.load_plan(run)[0]
        self.all = planmod.effective_checks(self.plan, self.cfg)
        if opts.check and opts.check not in {c["id"] for c in self.all}:
            raise FactoryError(f"no check {opts.check!r} (declared: {', '.join(c['id'] for c in self.all)})")
        self.repos = {r["name"]: r for r in self.cfg["repo"]}
        self.secrets = list(config.resolve_secrets(self.cfg)[0].values())
        self.results: list[dict] = []
        self.prev_sig: tuple | None = None
        self.stopped: str | None = None

    def chosen(self) -> list[dict]:
        return [c for c in self.all if (not self.opts.check or c["id"] == self.opts.check) and (not self.opts.repo or c["repo"] == self.opts.repo)]

    def tree(self, repo: str) -> Path:
        w = git.current(self.st, repo)
        if w is None or not Path(w["path"]).is_dir():
            raise FactoryError(f"repo {repo}: the run worktree is missing; run factory run branch")
        return Path(w["path"])

    def spec(self, c: dict) -> str:
        return self.lock["spec_hashes"][c["id"]]

    def run_one(self, c: dict, mode: str, tree: Path, sha: str, clean: bool, extra: dict | None = None, first: dict | None = None) -> dict:
        """Run one check and append its record. A pass right after the failed `first` attempt is recorded as `flaky` [L-14, L-139]."""
        ctx = chk.Ctx(tree, mode, self.cfg, self.repos[c["repo"]], self.secrets, self.lock["branch_points"].get(c["repo"], ""))
        out = chk.run(c, ctx)
        if first and out["result"] == "pass":
            out.update(result="flaky", kind=None, reason=f"attempt {first['attempt']} failed ({first['reason']}) and attempt "
                       f"{first['attempt'] + 1} passed: flaky never satisfies the gate until a clean run follows [L-14]")
        return evidence.append(self.run, c, self.spec(c), mode, sha, tree, clean, out, self.cfg["checks"]["tail_lines"],
                               {**(extra or {}), **({"retry_of": first["attempt"]} if first else {})}, self.opts.actor)

    def note(self, c: dict, rec: dict | None, reused: bool = False, mode: str | None = None) -> bool:
        """Record one result line; returns False once two identical failures in a row should stop the batch [L-47]."""
        res = {"check": c["id"], "mode": mode or (rec or {}).get("mode", "verify"), "reused": reused,
               "result": rec["result"] if rec else "pass", "kind": rec["kind"] if rec else None,
               "reason": rec["reason"] if rec else "reused: same SHA, same spec, clean worktree [L-10]",
               "attempt": rec["attempt"] if rec else None}
        self.results.append(res)
        if rec is None or rec["result"] in OK:
            self.prev_sig = None
            return True
        sig = evidence.signature(rec)
        stop = sig == self.prev_sig
        self.prev_sig = sig
        if stop:
            self.stopped = f"stopped after two identical failures in a row ({c['id']}: {rec['reason']}); fix the cause before running the rest [L-47]"
        return not stop

    def summary(self, mode: str) -> dict:
        done = {r["check"] for r in self.results}
        rest = [{"check": c["id"], "mode": mode, "result": "not_run", "kind": None, "reused": False, "attempt": None,
                 "reason": self.stopped} for c in self.chosen() if c["id"] not in done and self.stopped]
        results = self.results + rest
        ok = bool(results) and all(r["result"] in OK for r in results)
        return {"mode": mode, "results": results, "stopped": self.stopped, "ok": ok, "exit": 0 if ok else 1}


def order(st: dict, lk: dict, checks: list[dict]) -> list[dict]:
    """Last-failed first, then new or changed, then the cheapest type first [L-11]."""
    def key(c: dict):
        prior = [e for e in st["evidence"] if e["check"] == c["id"] and e["mode"] == "verify"]
        if prior and prior[-1]["result"] != "pass":
            group = 0
        else:
            group = 1 if not any(e["spec_hash"] == lk["spec_hashes"][c["id"]] for e in prior) else 2
        return group, chk.RANK[c["type"]]
    return sorted(checks, key=key)


def verify(project, run: Run, opts: Opts) -> dict:
    b = Batch(project, run, opts)
    heads: dict[str, tuple[Path, str, bool]] = {}
    for c in order(b.st, b.lock, b.chosen()):
        if c["repo"] not in heads:
            path = b.tree(c["repo"])
            heads[c["repo"]] = (path, git.head(path), bool(git.dirty(path)))
        path, sha, dirty = heads[c["repo"]]
        if not opts.fresh and not dirty and evidence.satisfied(b.st, c["id"], "verify", b.spec(c), sha):
            b.note(c, None, reused=True)
            continue
        rec = b.run_one(c, "verify", path, sha, not dirty)
        if opts.retry and c["type"] == "cmd" and rec["result"] == "fail" and rec["kind"] == "assert":
            rec = b.run_one(c, "verify", path, sha, not dirty, first=rec)
        if not b.note(c, rec):
            break
    if b.plan["kind"] == "bug" and not b.stopped:
        _red_green(b, heads)
    out = b.summary("verify")
    out["dirty"] = [r for r, (_, _, d) in heads.items() if d]
    return out


def _red_green(b: Batch, heads: dict) -> None:
    passed = {r["check"] for r in b.results if r["result"] == "pass"}
    for c in b.chosen():
        if not c.get("red_green") or c["id"] not in passed:
            continue
        path, bp = heads[c["repo"]][0], b.lock["branch_points"][c["repo"]]
        fix = git.rev(path, b.plan.get("fix_commit", "HEAD"))
        if fix is None or fix == bp or not git.is_ancestor(path, bp, fix):
            b.results.append({"check": c["id"], "mode": "red", "result": "not_run", "kind": None, "reused": False, "attempt": None,
                              "reason": "fix_commit is not a commit past the branch point: commit the fix first"})
            continue
        if evidence.satisfied(b.st, c["id"], "red", b.spec(c), None, fix) and evidence.satisfied(b.st, c["id"], "green", b.spec(c), None, fix):
            b.note(c, None, reused=True, mode="red-green")
            continue
        repo, parent = b.repos[c["repo"]], git.rev(path, f"{fix}^")
        red = _side(b, c, repo, parent, "red", fix, c["red_green"]["test_files"], path)
        b.note(c, red, mode="red")
        if b.stopped:
            break
        if red["result"] == "reproduced":
            b.note(c, _side(b, c, repo, fix, "green", fix, [], path), mode="green")


def _side(b: Batch, c: dict, repo: dict, sha: str, mode: str, fix: str, test_files: list[str], main: Path) -> dict:
    """One side of red-green in a throwaway worktree. The red side takes its test files from git at the fix commit [L-34, L-35]."""
    with git.throwaway(b.project, b.run, repo, sha, mode, b.opts.actor) as (tree, setup):
        out = None
        if setup["setup"] == "fail":
            out = {"result": "setup", "kind": "setup", "reason": f"worktree setup failed (exit {setup.get('setup_exit')}); never a result [L-06]",
                   "output": setup.get("setup_tail", "")}
        else:
            try:
                for rel in test_files:
                    write_atomic(tree / rel, git.show_file(main, fix, rel))
            except FactoryError as exc:
                out = {"result": "setup", "kind": "setup", "reason": f"test file missing from the fix commit: {exc}", "output": str(exc)}
        if out:
            out |= {"cmd": c["run"], "exit": setup.get("setup_exit"), "duration_s": 0.0}
            return evidence.append(b.run, c, b.spec(c), mode, sha, tree, True, out, b.cfg["checks"]["tail_lines"], {"fix": fix}, b.opts.actor)
        return b.run_one(c, mode, tree, sha, True, {"fix": fix})


def reproduce(project, run: Run, opts: Opts) -> dict:
    """Run every `reproduce` check in a throwaway worktree at the branch point; each must fail with its expected pattern [L-33, L-36]."""
    b = Batch(project, run, opts)
    if b.plan["kind"] != "bug":
        raise FactoryError("reproduce is for bug runs")
    todo: dict[str, list[dict]] = {}
    for c in b.chosen():
        if not c.get("reproduce"):
            continue
        bp = b.lock["branch_points"][c["repo"]]
        if not opts.fresh and evidence.satisfied(b.st, c["id"], "reproduce", b.spec(c), bp):
            b.note(c, None, reused=True, mode="reproduce")
        else:
            todo.setdefault(c["repo"], []).append(c)
    if not todo and not b.results:
        raise FactoryError("no check of this plan has a reproduce pattern")
    for repo, group in todo.items():
        bp = b.lock["branch_points"][repo]
        with git.throwaway(project, run, b.repos[repo], bp, "base", opts.actor) as (tree, setup):
            for c in group:
                if setup["setup"] == "fail":
                    out = {"cmd": c["run"], "exit": setup.get("setup_exit"), "duration_s": 0.0, "output": setup.get("setup_tail", ""),
                           "result": "setup", "kind": "setup", "reason": f"worktree setup failed (exit {setup.get('setup_exit')}); never a reproduction [L-06]"}
                    rec = evidence.append(run, c, b.spec(c), "reproduce", bp, tree, True, out, b.cfg["checks"]["tail_lines"], None, opts.actor)
                else:
                    rec = b.run_one(c, "reproduce", tree, bp, True)
                if not b.note(c, rec, mode="reproduce"):
                    break
        if b.stopped:
            break
    return b.summary("reproduce")


def render(s: dict) -> str:
    lines = []
    for r in s["results"]:
        tag = " (reused)" if r["reused"] else ""
        lines.append(f"{r['check']:<14} {r['mode']:<9} {r['result']}{tag}" + (f": {r['reason']}" if r["reason"] and r["result"] not in OK else ""))
    if s["stopped"]:
        lines.append(s["stopped"])
    for repo in s.get("dirty", []):
        lines.append(f"warning: the worktree of {repo} has uncommitted changes; this evidence will not count until they are committed [L-07]")
    return "\n".join(lines + [f"{s['mode']}: {'ok' if s['ok'] else 'FAILED'}"])
