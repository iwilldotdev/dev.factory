"""`factory verify` and `factory reproduce`: ordered, reusable, fail-fast runs that append evidence [L-10, L-11, L-14, L-34, L-47].
Both read the plan lock and refuse when the plan or config drifted; neither writes anything a gate could not re-derive."""
from __future__ import annotations

import json
import shutil
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from . import checks as chk, config, env as envmod, envdata, evidence, git, human, lock, plan as planmod, quality, review
from .checks import runner, ui
from .run import Run
from .util import FactoryError, fwd, safe_id, write_atomic

OK = ("pass", "reproduced")
RETRY = ("cmd", "http", "browser", "visual", "style")  # an assertion failure is retried once; only a later pass makes it `flaky` [L-14]


@dataclass
class Opts:
    check: str | None = None
    repo: str | None = None
    fresh: bool = False
    retry: bool = True
    actor: str | None = None
    scoped: bool = False  # quality checks run their scoped template (cheap feedback, never satisfies a gate) [L-60]


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
        self.bound: dict[tuple, dict] = {}  # (repo, tree, mode) -> the runtime binding of this batch
        self.env_failed: dict[str, str] = {}  # repo -> why its env failed: its remaining runtime checks are skipped [L-46]
        self.reset_done: set[str] = set()
        self.sessions: dict[tuple, dict] = {}  # login headers, in memory only [L-52]
        self.pre: dict[str, dict] = {}  # outcomes of checks that ran together in one browser [L-57]
        self.env_problem: str | None = None

    def chosen(self) -> list[dict]:
        return [c for c in self.all if (not self.opts.check or c["id"] == self.opts.check) and (not self.opts.repo or c["repo"] == self.opts.repo)]

    def tree(self, repo: str) -> Path:
        w = git.current(self.st, repo)
        if w is None or not Path(w["path"]).is_dir():
            raise FactoryError(f"repo {repo}: the run worktree is missing; run factory run branch")
        return Path(w["path"])

    def spec(self, c: dict) -> str:
        return self.lock["spec_hashes"][c["id"]]

    def ctx(self, c: dict, mode: str, tree: Path, bound: dict | None) -> chk.Ctx:
        ctx = build_ctx(self.project, self.run, self.plan, c, mode, tree, bound, self.lock["branch_points"].get(c["repo"], ""), self.secrets, self.sessions)
        ctx.scoped = self.opts.scoped and mode == "verify"
        return ctx

    def bind(self, c: dict, tree: Path, mode: str) -> tuple[dict | None, dict | None]:
        """(binding, failure outcome) of a runtime check: the env serves this tree at its HEAD, else one restart, else an error
        [L-13, L-69]. After a failure the repo's remaining runtime checks are `skipped: env` [L-46]."""
        repo, rc = c["repo"], self.repos[c["repo"]]
        if repo in self.env_failed:
            why = self.env_failed[repo]
            return None, {"cmd": ui.shown_of(c), "exit": None, "duration_s": 0.0, "output": why, "result": "skipped", "kind": "env",
                          "reason": f"skipped: the env of {repo} failed earlier in this batch ({why[:200]}) [L-46]"}
        key = (repo, str(tree), mode)
        if key in self.bound:
            return self.bound[key], None
        user = self.plan.get("login_user")
        try:
            if mode == "verify" and repo not in self.reset_done and (rc.get("env") or {}).get("reset"):
                self.reset_done.add(repo)  # one reset before the runtime batch [L-50]
                envdata.seed_or_reset(self.project, rc, "reset", tree, self.run.id, rc["env"]["base_url"])
            info = envmod.ensure(self.project, self.run, repo, tree if mode == "reproduce" else None, user, self.opts.actor)
            if user and rc["env"].get("login_spec") and not envdata.state_file(self.project, repo, user).is_file():
                if (why := envdata.login(self.project, self.run, rc, tree, rc["env"]["base_url"], user)):
                    raise envmod.EnvError("login", why)
        except FactoryError as exc:
            kind = getattr(exc, "kind", "env")
            self.env_failed[repo] = str(exc)
            res = ("error", "stale") if kind == "stale" else ("env", "env")
            return None, {"cmd": ui.shown_of(c), "exit": None, "duration_s": 0.0, "output": str(exc), "result": res[0], "kind": res[1],
                          "reason": f"{kind}: {exc}"[:500]}
        self.bound[key] = info
        return info, None

    def run_one(self, c: dict, mode: str, tree: Path, sha: str, clean: bool, extra: dict | None = None, first: dict | None = None) -> dict:
        """Run one check and append its record. A pass right after the failed `first` attempt is recorded as `flaky` [L-14, L-139]."""
        bound, out = self.bind(c, tree, mode) if c["type"] in chk.RUNTIME else (None, None)
        base = None
        if c["type"] == "quality":
            base, out = quality.baseline_for(self, c)  # taken once at the branch point and recorded [L-61]
        meta = {}
        if bound is not None:
            meta = {"heads": bound["heads"], "served": bound["served"], **({"warnings": bound["warnings"]} if bound["warnings"] else {})}
        if out is None:
            ctx = self.ctx(c, mode, tree, bound)
            ctx.baseline = base
            out = self.pre.pop(c["id"], None) or chk.run(c, ctx)
            if out["result"] == "env":
                self.env_failed[c["repo"]] = out["reason"]
        if first and out["result"] == "pass":
            out.update(result="flaky", kind=None, reason=f"attempt {first['attempt']} failed ({first['reason']}) and attempt "
                       f"{first['attempt'] + 1} passed: flaky never satisfies the gate until a clean run follows [L-14]")
        return evidence.append(self.run, c, self.spec(c), mode, sha, tree, clean, out, self.cfg["checks"]["tail_lines"],
                               {**meta, **(extra or {}), **({"retry_of": first["attempt"]} if first else {})}, self.opts.actor)

    def prefetch(self, todo: list[dict], heads: dict) -> None:
        """Run a repo's browser, visual, style and screenshot checks together in one runner process and one browser [L-57]."""
        if not self.cfg["checks"].get("browser", {}).get("batch", True):
            return
        by_repo: dict[str, list[dict]] = {}
        for c in todo:
            if c["type"] in chk.RUNNER and not (c["type"] == "browser" and self.cfg["checks"].get("browser", {}).get("runner")):
                by_repo.setdefault(c["repo"], []).append(c)
        for repo, cs in by_repo.items():
            if len(cs) < 2 or repo in self.env_failed:
                continue
            bound, fail = self.bind(cs[0], heads[repo][0], "verify")
            if fail:
                continue
            entries, ctxs = [], {}
            for c in cs:
                ctx = self.ctx(c, "verify", heads[repo][0], bound)
                try:
                    argv = chk.build_args(c, ctx)
                except FactoryError:
                    continue  # the single run of that check reports it
                ctxs[c["id"]] = ctx
                entries.append({"id": c["id"], "argv": argv, "out": fwd(ctx.tmp), "timeout_ms": c["timeout_s"] * 1000,
                                "env": {"FACTORY_EXPECT_TIMEOUT_MS": str(c.get("expect_timeout_ms", ""))}})
            if len(entries) > 1:
                self._batch(cs, entries, ctxs, repo)

    def _batch(self, cs: list[dict], entries: list[dict], ctxs: dict, repo: str) -> None:
        scratch = self.run.dir / "artifacts" / "_tmp" / f"batch-{uuid.uuid4().hex[:6]}"
        scratch.mkdir(parents=True, exist_ok=True)
        manifest = scratch / "manifest.json"
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        try:
            o = runner.invoke(self.cfg, ["batch", "--manifest", fwd(manifest)], scratch / "out", sum(e["timeout_ms"] for e in entries) / 1000 + 30,
                              envdata.child_env(self.project, self.repos[repo], self.run.id, self.repos[repo]["env"]["base_url"], self.plan.get("login_user")))
            done = {e["id"]: e for e in ((o.result or {}).get("details") or {}).get("entries", [])}
            for c in cs:
                e = done.get(c["id"])
                if e is None or c["id"] not in ctxs:
                    continue
                try:
                    res = json.loads((ctxs[c["id"]].tmp / runner.RESULT).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                self.pre[c["id"]] = ui.finish(c, ctxs[c["id"]], runner.Outcome(e.get("exit"), res, "", float(e.get("secs", 0)), bool(e.get("timed_out"))),
                                              ui.shown_of(c), c["type"] != "screenshot")
        except FactoryError:
            pass  # a runner that cannot start is reported by each single run
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def note(self, c: dict, rec: dict | None, reused: bool = False, mode: str | None = None) -> bool:
        """Record one result line; returns False once two identical failures in a row should stop the batch [L-47]."""
        res = {"check": c["id"], "mode": mode or (rec or {}).get("mode", "verify"), "reused": reused,
               "result": rec["result"] if rec else "pass", "kind": rec["kind"] if rec else None,
               "reason": rec["reason"] if rec else "reused: same SHA, same spec, clean worktree [L-10]",
               "attempt": rec["attempt"] if rec else None}
        self.results.append(res)
        if rec is None or rec["result"] in OK or rec["result"] == "skipped":
            self.prev_sig = None
            return True
        sig = evidence.signature(rec)
        stop = sig == self.prev_sig
        self.prev_sig = sig
        if stop:
            self.stopped = f"stopped after two identical failures in a row ({c['id']}: {rec['reason']}); fix the cause before running the rest [L-47]"
        return not stop

    def note_human(self, c: dict, sha: str) -> None:
        """A human check is never run: it passes while the user's confirmation covers the current commits, else it is pending [L-24]."""
        now = review.heads_of(self.st, self.plan["repos"])
        done = human.confirmation(self.st, c, self.spec(c), sha, now)
        self.results.append({"check": c["id"], "mode": "verify", "reused": bool(done), "result": "pass" if done else "pending", "kind": None, "attempt": None,
                             "reason": "confirmed by the user [L-24]" if done else f"{human.WAITING}: factory confirm {c['id']} --user-said \"<their words>\" ({c['text']})"})

    def summary(self, mode: str) -> dict:
        done = {r["check"] for r in self.results}
        rest = [{"check": c["id"], "mode": mode, "result": "not_run", "kind": None, "reused": False, "attempt": None,
                 "reason": self.stopped} for c in self.chosen() if c["id"] not in done and self.stopped]
        results = self.results + rest
        ok = bool(results) and all(r["result"] in OK or r["result"] == "pending" for r in results) and not self.env_problem
        env = bool(self.env_problem) or any(r["result"] in ("env", "skipped") or (r["result"] == "error" and r["kind"] == "stale") for r in results)
        return {"mode": mode, "results": results, "stopped": self.stopped, "ok": ok, "exit": 0 if ok else 3 if env else 1,
                **({"env_problem": self.env_problem} if self.env_problem else {})}


def build_ctx(project, run: Run, plan: dict, c: dict, mode: str, tree: Path, bound: dict | None, bp: str, secrets: list[str], sessions: dict) -> chk.Ctx:
    """The context of one check: its worktree and, for a runtime check, the env origin, login state and session headers [L-52]."""
    rc = next(r for r in project.cfg["repo"] if r["name"] == c["repo"])
    user = plan.get("login_user")
    ctx = chk.Ctx(tree, mode, project.cfg, rc, secrets, bp, project, run, user=user)
    if bound is not None:
        state = envdata.state_file(project, c["repo"], user)
        ctx.base_url = rc["env"]["base_url"]
        ctx.state_file = state if rc["env"].get("login_spec") and state.is_file() else None
        ctx.tmp = run.dir / "artifacts" / "_tmp" / f"{safe_id(c['id'])}-{uuid.uuid4().hex[:6]}"
        if c["type"] == "http" and c.get("session"):
            key = (c["repo"], c["session"])
            if key not in sessions:
                sessions[key] = envdata.login_headers(project, rc, tree, run.id, ctx.base_url, c["session"])
            ctx.session = sessions[key]
    return ctx


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


def current_heads(project, st: dict, c: dict) -> dict | None:
    """HEAD of every repo a runtime check serves; reuse needs them all unchanged [L-12]."""
    if c["type"] not in chk.RUNTIME:
        return None
    try:
        return envmod.heads_of(project, st, c["repo"])
    except FactoryError:
        return {}


def verify(project, run: Run, opts: Opts) -> dict:
    b = Batch(project, run, opts)
    heads: dict[str, tuple[Path, str, bool]] = {}
    ordered, reuse = order(b.st, b.lock, b.chosen()), {}
    for c in ordered:
        if c["repo"] not in heads:
            path = b.tree(c["repo"])
            heads[c["repo"]] = (path, git.head(path), bool(git.dirty(path)))
        path, sha, dirty = heads[c["repo"]]
        reuse[c["id"]] = c["type"] != "human" and not opts.fresh and not dirty and evidence.satisfied(b.st, c["id"], "verify", b.spec(c), sha, None, current_heads(project, b.st, c))
    b.prefetch([c for c in ordered if not reuse[c["id"]] and c["type"] != "human"], heads)
    for c in ordered:
        path, sha, dirty = heads[c["repo"]]
        if c["type"] == "human":
            b.note_human(c, sha)
            continue
        if reuse[c["id"]]:
            b.note(c, None, reused=True)
            continue
        rec = b.run_one(c, "verify", path, sha, not dirty)
        if opts.retry and c["type"] in RETRY and rec["result"] == "fail" and rec["kind"] == "assert":
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
    """Run every `reproduce` check in a throwaway worktree at the branch point; each must fail with its expected pattern [L-33, L-36].
    A runtime check is served from that worktree, and the run's env is restored afterwards (a failed restore exits 3) [L-37].
    Screenshot checks capture their before images here, in feature runs too [L-56]."""
    b = Batch(project, run, opts)
    bug = b.plan["kind"] == "bug"
    if not bug and not any(c["type"] == "screenshot" for c in b.chosen()):
        raise FactoryError("reproduce is for bug runs (a feature run uses it only to capture the before screenshots of its screenshot checks)")
    todo: dict[str, list[dict]] = {}
    for c in b.chosen():
        if not (c.get("reproduce") or c["type"] == "screenshot") or (not bug and c["type"] != "screenshot"):
            continue
        bp = b.lock["branch_points"][c["repo"]]
        heads = {**(current_heads(project, b.st, c) or {}), c["repo"]: bp} if c["type"] in chk.RUNTIME else None
        if not opts.fresh and evidence.satisfied(b.st, c["id"], "reproduce", b.spec(c), bp, None, heads):
            b.note(c, None, reused=True, mode="reproduce")
        else:
            todo.setdefault(c["repo"], []).append(c)
    if not todo and not b.results:
        raise FactoryError("no check of this plan has a reproduce pattern" + ("" if bug else " or is a screenshot"))
    for repo, group in todo.items():
        bp = b.lock["branch_points"][repo]
        with git.throwaway(project, run, b.repos[repo], bp, "base", opts.actor) as (tree, setup):
            runtime = setup["setup"] != "fail" and any(c["type"] in chk.RUNTIME for c in group)
            try:
                with envmod.serving(project, run, repo, tree, opts.actor) if runtime else nullcontext():
                    for c in group:
                        if setup["setup"] == "fail":
                            out = {"cmd": c.get("run", c["type"]), "exit": setup.get("setup_exit"), "duration_s": 0.0, "output": setup.get("setup_tail", ""),
                                   "result": "setup", "kind": "setup", "reason": f"worktree setup failed (exit {setup.get('setup_exit')}); never a reproduction [L-06]"}
                            rec = evidence.append(run, c, b.spec(c), "reproduce", bp, tree, True, out, b.cfg["checks"]["tail_lines"], None, opts.actor)
                        else:
                            rec = b.run_one(c, "reproduce", tree, bp, True)
                        if not b.note(c, rec, mode="reproduce"):
                            break
            except envmod.EnvError as exc:
                b.env_problem = str(exc)
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
    if s.get("env_problem"):
        lines.append(f"env: {s['env_problem']}")
    for repo in s.get("dirty", []):
        lines.append(f"warning: the worktree of {repo} has uncommitted changes; this evidence will not count until they are committed [L-07]")
    return "\n".join(lines + [f"{s['mode']}: {'ok' if s['ok'] else 'FAILED'}"])
