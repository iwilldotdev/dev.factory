"""Quality checks: a command whose failure ids are compared with a baseline taken at the branch point. Only ids that are new
and stable fail; ids are normalized so a worktree path never makes an old failure look new [L-60, L-61, L-62]."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import git, proc
from .checks import cmd as cmdcheck
from .util import FactoryError, canonical, fwd, redact

RUNS = 3  # a baseline runs the command up to this many times at the branch point [L-61]
QUARANTINE_FLOOR = 20  # more quarantined ids than this and than stable ones: the parse reads noise, not failures
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PARSE_HELP = "lines, json:<dotted path to a list> or regex:<failure id regex>"


def parse_problem(parse, here: str) -> str | None:
    """`parse` is `lines`, `json:<path>` (a dotted path to a list in the stdout JSON) or `regex:<regex>` [L-60]."""
    if parse == "lines":
        return None
    mode, _, arg = parse.partition(":") if isinstance(parse, str) else ("", "", "")
    if mode not in ("json", "regex") or not arg:
        return f"{here}.parse: {parse!r} must be {PARSE_HELP}"
    if mode == "regex":
        try:
            re.compile(arg)
        except re.error:
            return f"{here}.parse: {arg!r} is not a valid regular expression"
    return None


def problems(c: dict, here: str) -> list[str]:
    """Plan and config problems of a `quality` check."""
    out = []
    if not (isinstance(c.get("run"), str) and c["run"].strip()):
        out.append(f"{here}.run: must be a non-empty command")
    if (msg := parse_problem(c.get("parse", "lines"), here)):
        out.append(msg)
    if "scoped" in c and not (isinstance(c["scoped"], str) and "{changed_files}" in c["scoped"]):
        out.append(f"{here}.scoped: must be a command template that contains {{changed_files}}")
    return out


def _tail_tree(path: Path) -> list[str]:
    back = str(path).replace("/", "\\")  # a Windows path in the output, whatever the platform reading it
    return sorted({str(path), fwd(path), back, back.replace("\\", "\\\\")}, key=len, reverse=True)


def normalize(text: str, trees: list[Path]) -> str:
    """No colors and no worktree or checkout path: ids of the baseline tree and of the run tree compare equal [L-62]."""
    text = ANSI.sub("", text).replace("\r\n", "\n")
    for tree in trees:
        for form in _tail_tree(tree):
            text = re.sub(re.escape(form) + r"[\\/]?", "", text, flags=re.I)
    return text


def _walk(data, path: str):
    for part in [p for p in path.removeprefix("$").strip(".").split(".") if p]:
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


POSITION = re.compile(r"(?<=\S)(?::\d+(?::\d+)?|\(\d+,\d+\))(?=[:\s]|$)")


def positionless(ids) -> list[str]:
    """Ids without line and column: code that only moves keeps its findings' ids. The same finding repeated in one file is
    numbered (`#2`, `#3`), so one more copy still counts as new. Applied to recorded baselines too, at comparison time."""
    seen: dict[str, int] = {}
    out = []
    for raw in sorted(ids):
        key = POSITION.sub("", raw)
        seen[key] = seen.get(key, 0) + 1
        out.append(key if seen[key] == 1 else f"{key}#{seen[key]}")
    return sorted(out)


def failure_ids(parse: str, stdout: str, text: str, code: int | None, trees: list[Path]) -> list[str]:
    """The sorted failure ids of one run. A failing exit with no id is the id `exit:<code>`, so it compares like any other [L-61]."""
    mode, _, arg = parse.partition(":")
    body = normalize(stdout if mode == "json" else text, trees)
    if mode == "lines":
        ids = [ln.strip() for ln in body.splitlines() if ln.strip()]
    elif mode == "regex":
        ids = [(m.group(1) if m.groups() else m.group(0)).strip() for m in re.finditer(arg, body, re.M)]
    else:
        try:
            found = _walk(json.loads(body), arg)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"the output has no list at {arg!r} ({exc})") from exc
        if not isinstance(found, list):
            raise ValueError(f"{arg!r} is not a list in the output")
        ids = [x if isinstance(x, str) else str(x.get("id")) if isinstance(x, dict) and x.get("id") else canonical(x) for x in found]
    if code not in (0, None) and not ids:
        ids = [f"exit:{code}"]
    return sorted(set(ids))


def quote(files: list[str]) -> str:
    return subprocess.list2cmdline(files) if sys.platform == "win32" else shlex.join(files)


def _execute(check: dict, tree: Path, line: str, secrets: list[str]):
    """(result fields when the run says nothing about the code, else None; the process result; the redacted text)."""
    res = proc.run(line, cwd=tree / check["cwd"], env=proc.child_env(check.get("vars") or {}), timeout_s=check["timeout_s"], shell=True)
    text = redact(res.out + ("\n" if res.out and res.err else "") + res.err, secrets).replace("\r\n", "\n")
    bad = None
    if res.timed_out:
        bad = ("timeout", "timeout", f"killed after {check['timeout_s']}s (the whole process tree)")
    elif res.exit in cmdcheck.SETUP_CODES:
        bad = ("setup", "setup", f"exit {res.exit}: the command was not found or cannot run")
    elif cmdcheck.ZERO_TESTS.search(text):
        bad = ("setup", "setup", "the run executed zero tests")
    elif res.exit not in (0, None) and cmdcheck.SETUP_TEXT.search(text):
        bad = ("setup", "setup", "the output reports a missing command, module or file")
    return bad, res, text


def warning(check: dict, code: int | None, ids: list[str], text: str) -> str | None:
    """A parse that likely reads more than failures, from generic signals only: ids under a successful exit, or `lines`
    turning most of a long output into ids."""
    real = [i for i in ids if not i.startswith("exit:")]
    if code == 0 and real:
        return f"exit 0 but {len(real)} failure id(s) parsed: the `parse` of this check likely matches non-failure output"
    lines = sum(1 for ln in text.splitlines() if ln.strip())
    if check.get("parse", "lines") == "lines" and len(real) >= QUARANTINE_FLOOR and len(real) * 2 > lines:
        return (f"parse = lines made {len(real)} of {lines} output line(s) failure ids; give the check a `regex:` or `json:` parse "
                "that matches only failures")
    return None


def _ids_of(check: dict, res, text: str, trees: list[Path]):
    try:
        return failure_ids(check["parse"], res.out, text, res.exit, trees), None
    except ValueError as exc:
        return None, ("setup", "setup", f"{check['parse']}: {exc}")


def compute_baseline(project, run, repo: dict, check: dict, bp: str, secrets: list[str], actor: str | None, runs: int = RUNS) -> dict:
    """Run the full command at `branch_point` up to `runs` times in a throwaway worktree; two runs that agree end it.
    Ids present in every run are stable, ids present in some are quarantined and never fail a later verify [L-61]."""
    sets: list[set[str]] = []
    warn = None
    with git.throwaway(project, run, repo, bp, "baseline", actor) as (tree, setup):
        if setup["setup"] == "fail":
            raise FactoryError(f"{check['id']}: the baseline worktree setup failed (exit {setup.get('setup_exit')}); a setup failure is never a baseline [L-06]")
        before = git.status_now(tree)
        for i in range(runs):
            bad, res, text = _execute(check, tree, check["run"], secrets)
            if bad:
                raise FactoryError(f"{check['id']}: the baseline run ended in {bad[0]}: {bad[2]}")
            if (wrote := git.changed_by(tree, before)):
                raise FactoryError(f"{check['id']}: the check changed {len(wrote)} path(s) in the worktree: {', '.join(wrote[:5])}; a check must only read")
            ids, err = _ids_of(check, res, text, [tree, Path(repo["path"])])
            if err:
                raise FactoryError(f"{check['id']}: baseline {err[2]}")
            warn = warn or warning(check, res.exit, ids, text)
            sets.append(set(ids))
            if i >= 1 and sets[-1] == sets[-2]:
                break
    stable = set.intersection(*sets)
    quarantined = set.union(*sets) - stable
    if len(quarantined) > max(len(stable), QUARANTINE_FLOOR):
        raise FactoryError(f"{check['id']}: {len(quarantined)} failure id(s) differ between identical runs at {bp[:7]} and only {len(stable)} "
                           "repeat: fix `parse` (likely: timestamps, durations and counters change every run) or the check's determinism")
    return {"check": check["id"], "repo": check["repo"], "spec_hash": None, "branch_point": bp, "runs": len(sets),
            "stable": sorted(stable), "quarantined": sorted(quarantined), **({"warning": warn} if warn else {})}


def find(st: dict, check_id: str, spec: str, bp: str) -> dict | None:
    return next((b for b in reversed(st["baselines"]) if b["check"] == check_id and b["spec_hash"] == spec and b["branch_point"] == bp), None)


def baseline_for(b, c: dict, tree: Path | None = None, force: bool = False) -> tuple[dict | None, dict | None]:
    """(baseline, failure outcome) of a quality check in this batch: the recorded one, else one taken now and recorded [L-61]."""
    spec, bp = b.spec(c), b.lock["branch_points"][c["repo"]]
    if tree is not None:  # after a merge of the base, the base commits are not this run's failures
        bp = git.integration_point(tree, b.repos[c["repo"]], bp)
    have = None if force else find(b.run.state(), c["id"], spec, bp)
    if have:
        return have, None
    try:
        data = compute_baseline(b.project, b.run, b.repos[c["repo"]], c, bp, b.secrets, b.opts.actor)
    except FactoryError as exc:
        return None, {"cmd": c["run"], "exit": None, "duration_s": 0.0, "output": str(exc), "result": "setup", "kind": "setup", "reason": str(exc)[:500]}
    data["spec_hash"] = spec
    ev = b.run.append({"type": "baseline", **data}, b.opts.actor)
    return ev, None


def run_check(check: dict, ctx) -> dict:
    """Run the full command (or, with `verify --scoped`, the scoped template on the files changed since the branch point) and
    fail on failure ids that the baseline does not know [L-60, L-61]."""
    line, scope = check["run"], "full"
    if ctx.scoped and check.get("scoped"):
        changed = git.changed_files(ctx.path, ctx.branch_point)
        scope = "scoped"
        if not changed:
            return {"cmd": check["scoped"], "exit": None, "duration_s": 0.0, "output": "no file changed since the branch point", "result": "pass",
                    "kind": None, "reason": "", "details": {"scope": scope, "new": [], "total": 0}}
        line = check["scoped"].replace("{changed_files}", quote(changed))
    bad, res, text = _execute(check, ctx.path, line, ctx.secrets)
    shown = redact(line, ctx.secrets)
    base = {"cmd": shown, "exit": res.exit, "duration_s": round(res.secs, 2), "output": text}
    if bad:
        return {**base, "result": bad[0], "kind": bad[1], "reason": bad[2]}
    ids, err = _ids_of(check, res, text, [ctx.path, Path(ctx.repo["path"])])
    if err:
        return {**base, "result": err[0], "kind": err[1], "reason": err[2]}
    known = set(positionless((ctx.baseline or {}).get("stable", []))) | set(positionless((ctx.baseline or {}).get("quarantined", [])))
    new = sorted(set(positionless(ids)) - known)
    details = {"scope": scope, "new": new[:50], "total": len(ids), "baseline_stable": len((ctx.baseline or {}).get("stable", [])),
               "quarantined": len((ctx.baseline or {}).get("quarantined", [])), **({"warning": w} if (w := warning(check, res.exit, ids, text)) else {})}
    if new:
        at = (ctx.baseline or {}).get("branch_point", ctx.branch_point)[:7]
        return {**base, "result": "fail", "kind": "assert", "details": details,
                "reason": f"{len(new)} new failure id(s) beyond the baseline at {at}: " + "; ".join(new[:3])[:300]}
    return {**base, "result": "pass", "kind": None, "reason": "", "details": details}


def quarantines(st: dict) -> dict[str, int]:
    """Quality checks whose latest baseline quarantines ids, with their count: those ids never fail a verify, so the check proves less."""
    latest = {b["check"]: b for b in st["baselines"]}
    return {k: len(b["quarantined"]) for k, b in sorted(latest.items()) if b.get("quarantined")}


def guidelines(cfg: dict, plan: dict) -> list[dict]:
    """The guideline rules of the planned repos: no check, their text goes into the code review input [parity row 39]."""
    by_name = {r["name"]: r for r in cfg["repo"]}
    return [{"id": f"{n}:{c['id']}", "text": c["text"]} for n in plan["repos"] for c in by_name[n].get("check", []) if c.get("kind") == "guideline"]
