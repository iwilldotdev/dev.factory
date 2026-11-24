"""plan.json: validation, effective checks (plan plus standing), and the hashes the lock is made of [L-04, L-33, L-132, L-167]."""
from __future__ import annotations

import re
from pathlib import Path

from . import checks as checks_pkg, plancheck, quality
from .util import canonical, read_json, sha256_bytes, sha256_file

AC_ID, CHK_ID = re.compile(r"^AC-[0-9]+$"), re.compile(r"^CHK-[0-9]+$")
TOP = {"schema", "kind", "repos", "acs", "checks", "fix_commit", "risk", "login_user", "evidence"}
AC_KEYS = {"id", "text", "source", "reproduce", "reason", "design"}
CHECK_KEYS = {"id", "type", "repo", "ac", "timeout_s", "files", "reproduce", "mirror", "run", "cwd", "expect_exit",
              "expect_pattern", "red_green"} | plancheck.NEW_KEYS
PLAN_KIND = {"feature": "feature", "bug": "bug", "hotfix": "bug"}  # run kind -> plan kind [L-31]
EXAMPLE = {"schema": 1, "kind": "feature", "repos": ["web"],
           "acs": [{"id": "AC-1", "text": "One verifiable behaviour from the ticket", "source": "ticket:description"}],
           "checks": [{"id": "CHK-1", "type": "cmd", "repo": "web", "ac": ["AC-1"], "run": "<command that fails until AC-1 holds>",
                       "expect_pattern": "<text the passing output must contain>"}]}


def chash(obj) -> str:
    return sha256_bytes(canonical(obj).encode("utf-8"))


def pattern_problem(pat, label: str) -> str | None:
    """A pattern needs 6 characters and 4 literal word characters, so `.*` or `\\w+` cannot match any failure [L-04]."""
    if not isinstance(pat, str):
        return f"{label}: must be a regular expression string"
    try:
        re.compile(pat)
    except re.error:
        return f"{label}: {pat!r} is not a valid regular expression"
    body = re.sub(r"\(\?(?:[a-zA-Z]+\)|[:=!]|P<\w+>|<[=!])", "", pat)
    body = re.sub(r"\[(?:\\.|[^\]\\])*\]|\{\d+(?:,\d*)?\}|\\.", "", body)
    if len(pat) < 6 or len(re.findall(r"\w", body)) < 4:
        return f"{label}: {pat!r} is too weak; it needs at least 6 characters with 4 literal word characters [L-04]"
    return None


def load_plan(run) -> tuple[dict | None, list[str]]:
    path = run.dir / "plan.json"
    if not path.is_file():
        return None, [f"plan.json is missing: write it at {path.as_posix()} (factory plan example prints a minimal one)"]
    try:
        return read_json(path), []
    except Exception as exc:  # noqa: BLE001 - any unreadable plan is one reason
        return None, [str(exc)]


def _ids(items, key: str, rx, name: str, errs: list[str]) -> set[str]:
    seen: set[str] = set()
    for i, it in enumerate(items):
        v = it.get(key) if isinstance(it, dict) else None
        if not isinstance(v, str) or not rx.match(v):
            errs.append(f"{name}[{i}].{key}: must match {rx.pattern}")
        elif v in seen:
            errs.append(f"{name}: duplicate id {v}")
        else:
            seen.add(v)
    return seen


def validate(plan, cfg: dict, run_dir: Path, run_kind: str | None) -> list[str]:
    """Every structural problem of plan.json, in one list (nothing is checked against git here)."""
    if not isinstance(plan, dict):
        return ["plan.json: must be a JSON object"]
    errs = [f"plan.json: unknown key {k!r}" for k in sorted(set(plan) - TOP)]
    known = {r["name"] for r in cfg["repo"]}
    kind = plan.get("kind")
    if plan.get("schema") != 1:
        errs.append("plan.json: schema must be 1")
    if kind not in ("feature", "bug"):
        errs.append("plan.kind: must be feature or bug")
    elif run_kind in PLAN_KIND and PLAN_KIND[run_kind] != kind:
        errs.append(f"plan.kind is {kind} but the run kind is {run_kind} [L-31]")
    repos = plan.get("repos")
    if not (isinstance(repos, list) and repos and all(isinstance(r, str) for r in repos)):
        errs.append("plan.repos: must be a non-empty list of repo names")
        repos = []
    errs += [f"plan.repos: {r!r} is not a repo of factory.toml (repos: {', '.join(sorted(known))})" for r in repos if r not in known]
    if plan.get("risk", "low") not in ("low", "medium", "high"):
        errs.append("plan.risk: must be low, medium or high")
    if "fix_commit" in plan and not (isinstance(plan["fix_commit"], str) and plan["fix_commit"].strip()):
        errs.append("plan.fix_commit: must be a git ref")
    if "login_user" in plan and not (isinstance(plan["login_user"], str) and plancheck.SLUG.match(plan["login_user"])):
        errs.append("plan.login_user: must be a slug such as qa-admin")
    if "evidence" in plan and not (isinstance(plan["evidence"], dict) and set(plan["evidence"]) <= {"screenshots"}
                                   and isinstance(plan["evidence"].get("screenshots", False), bool)):
        errs.append("plan.evidence: must be {\"screenshots\": true or false}")
    acs, chks = plan.get("acs"), plan.get("checks")
    if not (isinstance(acs, list) and acs and all(isinstance(a, dict) for a in acs)):
        return errs + ["plan.acs: must be a non-empty list of objects"]
    if not (isinstance(chks, list) and chks and all(isinstance(c, dict) for c in chks)):
        return errs + ["plan.checks: must be a non-empty list of objects"]
    bug = kind == "bug"
    ac_ids = _ids(acs, "id", AC_ID, "plan.acs", errs)
    _ids(chks, "id", CHK_ID, "plan.checks", errs)
    for a in acs:
        here = f"plan.acs[{a.get('id')}]"
        errs += [f"{here}: unknown key {k!r}" for k in sorted(set(a) - AC_KEYS)]
        errs += [f"{here}.{k}: must be a non-empty string" for k in ("text", "source")
                 if not (isinstance(a.get(k), str) and a[k].strip())]
        if "design" in a and not (isinstance(a["design"], list) and a["design"] and all(isinstance(f, str) and f.strip() for f in a["design"])):
            errs.append(f"{here}.design: must be a non-empty list of design_frame ids")
        if "reproduce" in a and not (bug and isinstance(a["reproduce"], bool)):
            errs.append(f"{here}.reproduce: only a bug plan sets it, as true or false")
        if bug and a.get("reproduce") is False and not str(a.get("reason") or "").strip():
            errs.append(f"{here}: reproduce false needs a reason")
    covered: set[str] = set()
    for c in chks:
        here = f"plan.checks[{c.get('id')}]"
        errs += [f"{here}: unknown key {k!r}" for k in sorted(set(c) - CHECK_KEYS)]
        if c.get("type") not in checks_pkg.TYPES:
            errs.append(f"{here}.type: must be one of {', '.join(checks_pkg.TYPES)}")
        if c.get("repo") not in repos:
            errs.append(f"{here}.repo: {c.get('repo')!r} is not in plan.repos")
        names = c.get("ac")
        if c.get("type") == "screenshot" and not names:
            names = []
        elif not (isinstance(names, list) and names and all(isinstance(n, str) for n in names)):
            errs.append(f"{here}.ac: must list at least one AC id")
        else:
            errs += [f"{here}.ac: {n} is not an AC of this plan" for n in names if n not in ac_ids]
            covered |= set(names)
        errs += _cmd_problems(c, here, bug, run_dir) + plancheck.type_problems(c, here, run_dir)
    errs += [f"plan.acs[{i}]: no check names it [L-01]" for i in sorted(ac_ids - covered)]
    if bug:
        for a in acs:
            kinds = {c.get("type") for c in chks if a.get("id") in (c.get("ac") or [])}
            if kinds == {"human"}:
                errs.append(f"plan.acs[{a.get('id')}]: a human check never alone proves a bug AC; add a check that a command or the app can fail [L-24]")
            if a.get("reproduce", True) is True:
                mine = [c for c in chks if a.get("id") in (c.get("ac") or []) and c.get("reproduce")]
                if not mine:
                    errs.append(f"plan.acs[{a.get('id')}]: a bug AC needs a check with reproduce.expect_fail_pattern, or reproduce false with a reason [L-33]")
                elif all(c.get("type") == "cmd" and not c.get("red_green") for c in mine):
                    errs.append(f"plan.acs[{a.get('id')}]: its only reproduction is a cmd check, which needs red_green.test_files [L-34]")
    return errs


def _cmd_problems(c: dict, here: str, bug: bool, run_dir: Path) -> list[str]:
    errs: list[str] = quality.problems(c, here) if c.get("type") == "quality" else []
    if c.get("type") in ("cmd", "quality"):
        if c["type"] == "cmd" and not (isinstance(c.get("run"), str) and c["run"].strip()):
            errs.append(f"{here}.run: must be a non-empty command")
        cwd = c.get("cwd", ".")
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
            errs.append(f"{here}.cwd: must be a path inside the repo")
        if c["type"] == "cmd" and "expect_exit" in c and not (isinstance(c["expect_exit"], int) and not isinstance(c["expect_exit"], bool)):
            errs.append(f"{here}.expect_exit: must be an integer")
        if c["type"] == "cmd" and "expect_pattern" in c and (msg := pattern_problem(c["expect_pattern"], f"{here}.expect_pattern")):
            errs.append(msg)
    if "timeout_s" in c and not (isinstance(c["timeout_s"], int) and not isinstance(c["timeout_s"], bool) and c["timeout_s"] > 0):
        errs.append(f"{here}.timeout_s: must be a positive integer")
    for rel in c.get("files", []) if isinstance(c.get("files"), list) else []:
        p = Path(str(rel))
        if p.is_absolute() or ".." in p.parts or p.parts[:1] != ("checks",) or not (run_dir / p).is_file():
            errs.append(f"{here}.files: {rel!r} must be an existing file under checks/ in the run directory")
    rep, rg = c.get("reproduce"), c.get("red_green")
    if (rep or rg) and not bug:
        errs.append(f"{here}: reproduce and red_green belong to bug plans")
    if rep is not None:
        if not (isinstance(rep, dict) and set(rep) == {"expect_fail_pattern"}):
            errs.append(f"{here}.reproduce: must be {{\"expect_fail_pattern\": \"<regex>\"}}")
        elif msg := pattern_problem(rep["expect_fail_pattern"], f"{here}.reproduce.expect_fail_pattern"):
            errs.append(msg)
    if rg is not None:
        files = rg.get("test_files") if isinstance(rg, dict) else None
        if not (isinstance(files, list) and files and all(isinstance(f, str) and f and ".." not in Path(f).parts
                                                          and not Path(f).is_absolute() for f in files)):
            errs.append(f"{here}.red_green: must be {{\"test_files\": [<repo-relative path>, ...]}}")
        if c.get("type") != "cmd" or rep is None:
            errs.append(f"{here}.red_green: only a cmd check with reproduce can have it")
    return errs


def _norm(c: dict, cfg: dict, standing: bool) -> dict:
    kind = c.get("type", "cmd")
    out = {"id": c["id"], "type": kind, "repo": c["repo"], "ac": list(c.get("ac") or []),
           "timeout_s": c.get("timeout_s") or cfg["checks"][plancheck.timeout_key(kind)], "standing": standing}
    if kind in ("quality", "human"):
        out.update({k: c[k] for k in plancheck.FIELDS[kind] if k in c}, **({"cwd": c.get("cwd") or ".", "parse": c.get("parse") or "lines"} if kind == "quality" else {}))
    elif kind != "cmd":
        out.update(plancheck.norm(c, cfg))
    else:
        out.update(run=c["run"], cwd=c.get("cwd") or ".", expect_exit=c.get("expect_exit", 0), expect_pattern=c.get("expect_pattern"))
    for key in ("reproduce", "red_green", "mirror"):
        if c.get(key):
            out[key] = c[key]
    if c.get("files") and kind == "cmd":
        out["files"] = sorted(c["files"])
    if c.get("files") and kind != "cmd":
        out["files"] = sorted({*out.get("files", []), *c["files"]})
    return out


def effective_checks(plan: dict, cfg: dict) -> list[dict]:
    """Plan checks, then the standing checks of every planned repo, then each repo's implicit commit-format check."""
    out = [_norm(c, cfg, False) for c in plan["checks"]]
    by_name = {r["name"]: r for r in cfg["repo"]}
    for name in plan["repos"]:
        repo = by_name[name]
        out += [_norm({**c, "id": f"{name}:{c['id']}", "repo": name}, cfg, True) for c in repo.get("check", []) if c.get("kind") != "guideline"]
        if repo.get("commit_pattern") or repo.get("commit_forbidden_trailers"):
            out.append({"id": f"{name}:commits", "type": "commits", "repo": name, "ac": [], "standing": True, "base": repo["base"],
                        "pattern": repo.get("commit_pattern"), "forbidden": repo.get("commit_forbidden_trailers", [])})
    return out


def spec_hash(check: dict, run_dir: Path) -> str:
    """Canonical check entry, declared file hashes and, for design checks, the frame and image contract hashes [L-09, L-59]."""
    files = {f: sha256_file(run_dir / f) if (run_dir / f).is_file() else "missing" for f in check.get("files", [])}
    design = {}
    if check.get("frame"):
        from . import designflow
        design = designflow.contract_hashes(run_dir, check["frame"])
    return chash({"check": check, "files": files, **({"design": design} if design else {})})


def config_hash(cfg: dict) -> str:
    """The proof-relevant config: commands, caps, limits and loop budget; never paths, ports or base URLs [L-131, L-188]."""
    env_keys = ("up", "down", "ready_timeout_s", "depends_on", "login_spec", "login_secrets", "login_http", "seed", "reset", "preflight", "warmup",
                "env_patterns")
    repos = [{**{k: r.get(k) for k in ("name", "base", "branch", "setup", "files_from_main", "commit_pattern",
                                       "commit_forbidden_trailers", "check", "mirror")},
              "env": {k: r["env"].get(k) for k in env_keys} if r.get("env") else None} for r in cfg["repo"]]
    checks = {**cfg["checks"], "browser": {k: v for k, v in cfg["checks"].get("browser", {}).items() if k not in ("bundled", "headless")}}
    return chash({"repos": repos, "checks": checks, "limits": cfg["limits"], "max_loops": cfg["autonomy"]["max_loops"]})
