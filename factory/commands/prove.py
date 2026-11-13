"""`factory baseline` and `factory prove` (DESIGN §3, §4)."""

from __future__ import annotations

import importlib
import json
import re
import time
import traceback
from pathlib import Path

from ..collectors.test import (DEFAULT_TIMEOUT_S, drop_worktree, run_shell, setup_command, temp_worktree,
                               weak_pattern_reason)
from ..core import (SCHEMA_VERSION, ensure_owner, Ctx, FactoryError, evidence_event, evidence_id, git,
                    git_dirty, git_head, hotfix_of, now, read_json, repo_conf,
                    repo_path_for_run, run_dir, run_load, run_save, run_targets,
                    target_worktree, workspace_load, write_evidence, write_json)
from . import gate

TSC_RE = re.compile(r"^(?P<p>.+?)\((?P<l>\d+),(?P<c>\d+)\):\s*error\s+(?P<code>[A-Za-z]+\d+):\s*(?P<m>.*?)(?:\s*\[[^\]]*\])?$")
GENERIC_RE = re.compile(r"^(?P<p>[^\s:(][^:(]*?):(?P<l>\d+)(?::\d+)?:?\s*(?:error|Error)\b[:\s]*(?P<m>.*)$")
FAIL_RE = re.compile(r"^\s*(?:FAIL|✗|×|✕)\s+(?P<m>\S.*)$")


# ---------- failure parsing ----------

def _rel(path: str, root: Path) -> str:
    p = path.replace("\\", "/")
    for candidate in {str(root), str(root.resolve())}:
        r = candidate.replace("\\", "/").rstrip("/")
        if p.lower().startswith(r.lower() + "/"):
            return p[len(r) + 1:]
    return p


def _scrub(text: str, root: Path) -> str:
    out = text.replace("\\", "/")
    for candidate in {str(root), str(root.resolve())}:
        r = candidate.replace("\\", "/").rstrip("/")
        out = re.sub(re.escape(r) + "/", "", out, flags=re.IGNORECASE)
    return out


def _json_payload(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = sorted((text.find(o), o) for o in ("{", "[") if text.find(o) != -1)
    for start, opener in starts:
        end = text.rfind("}" if opener == "{" else "]")
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def parse_failures(output: str, exit_code: int, root: Path, name: str = "cmd") -> list[str]:
    """Stable identifiers of failures (no line numbers, paths relative to root)."""
    ids: set[str] = set()
    data = _json_payload(output)
    if isinstance(data, dict) and isinstance(data.get("testResults"), list):
        for tr in data["testResults"]:
            fname = _rel(str(tr.get("name") or tr.get("testFilePath") or ""), root)
            failed = [a for a in tr.get("assertionResults", []) if a.get("status") == "failed"]
            for a in failed:
                ids.add(f"test:{fname}::{a.get('fullName') or a.get('title')}")
            if not failed and tr.get("status") == "failed":
                ids.add(f"suite:{fname}")
    elif isinstance(data, list) and data and isinstance(data[0], dict) and "messages" in data[0]:
        for item in data:
            for m in item.get("messages", []):
                if m.get("severity") == 2:
                    ids.add(f"{_rel(str(item.get('filePath', '')), root)} {m.get('ruleId') or 'error'} {m.get('message', '')}")
    if not ids:
        for raw in output.splitlines():
            line = raw.strip()
            m = TSC_RE.match(line)
            if m:
                ids.add(f"{_rel(m['p'], root)} {m['code']} {m['m']}")
                continue
            m = GENERIC_RE.match(line)
            if m:
                ids.add(f"{_rel(m['p'], root)} {m['m']}")
                continue
            m = FAIL_RE.match(line)
            if m:
                ids.add(f"test:{_rel(m['m'], root)}")
    if not ids and exit_code != 0:
        ids.add(f"exit:{name}")
    return sorted({_scrub(i, root) for i in ids})


def run_quality(root: Path, quality: dict, env_vars: dict | None) -> dict:
    out = {}
    for name, cmd in quality.items():
        if name == "setup":  # worktree preparation, not a quality command
            continue
        code, text = run_shell(cmd, root, DEFAULT_TIMEOUT_S, env_vars)
        out[name] = {"cmd": cmd, "exit": code, "failures": parse_failures(text, code, root, name)}
    return out


# ---------- baseline ----------

def compute_baseline(run: dict, ws: dict, alias: str, force: bool = False) -> dict:
    path = run_dir(run["id"]) / "baseline" / f"{alias}.json"
    if path.is_file() and not force:
        return read_json(path)
    conf = repo_conf(ws, alias)
    quality = conf.get("quality") or {}
    wt = repo_path_for_run(run, ws, alias)
    base = gate.resolve_base(wt, run["repos"][alias].get("base", "main"))
    merge_base = git(wt, "merge-base", "HEAD", base)
    main = Path(conf.get("path") or wt)
    env_vars = (conf.get("env") or {}).get("env_vars")
    try:
        tmp = temp_worktree(main if (main / ".git").exists() else wt, merge_base, quality.get("setup"), env_vars)
    except RuntimeError as err:
        raise FactoryError(f"{alias}: baseline worktree: {err}") from err
    try:
        results = run_quality(tmp, quality, env_vars)
    finally:
        drop_worktree(main if (main / ".git").exists() else wt, tmp)
    data = {"schemaVersion": SCHEMA_VERSION, "repo": alias, "merge_base": merge_base,
            "base_ref": base, "computed": now(), "commands": results}
    write_json(path, data)
    return data


def cmd_baseline(args) -> int:
    run = run_load(args.run)
    ensure_owner(run)
    ws = workspace_load(run["workspace"])
    aliases = _select_repos(run, ws, args.repo)
    done = []
    for alias in aliases:
        if not repo_conf(ws, alias).get("quality"):
            print(f"{alias}: no quality commands, skipped")
            continue
        data = compute_baseline(run, ws, alias, args.force)
        counts = {k: len(v["failures"]) for k, v in data["commands"].items()}
        done.append({"repo": alias, "merge_base": data["merge_base"], "failures": counts})
        if not getattr(args, "json", False):
            print(f"{alias}: baseline at {data['merge_base'][:8]} failures {counts}")
    if getattr(args, "json", False):
        print(json.dumps(done, indent=2))
    return 0


def _select_repos(run: dict, ws: dict, repo: str | None) -> list[str]:
    if repo:
        alias = repo_conf(ws, repo)["alias"]
        if alias not in run["repos"]:
            raise FactoryError(f"repo {alias} has no branch in run {run['id']}")
        return [alias]
    return list(run["repos"])


# ---------- prove ----------

def dispatch(check: dict, ctx: Ctx) -> dict:
    module = "factory.collectors." + check["type"].replace("-", "_")
    try:
        collector = importlib.import_module(module)
    except ImportError as err:
        return {"status": "error", "summary": f"collector {module} not available: {err}"}
    try:
        return collector.collect(check, ctx)
    except Exception:  # collectors must not crash the run
        return {"status": "error", "summary": "collector crashed",
                "details": {"traceback": traceback.format_exc()[-3000:]}}


def collect_one(run: dict, ws: dict, check: dict, target: str | None = None) -> dict:
    alias = repo_conf(ws, check["repo"])["alias"] if check.get("repo") else None
    repo_path = target_worktree(run, ws, alias, target) if alias else None
    if target and hotfix_of(run):
        check = dict(check, id=evidence_id(run, check["id"], target))
    out_dir = run_dir(run["id"]) / "evidence" / check["id"]
    env = dict(repo_conf(ws, alias).get("env") or {}) if alias else {}
    if alias and not env.get("base_url"):
        try:  # same resolution as `factory env` (base_url, else origin of health)
            from .env import base_url as env_base_url
            env["base_url"] = env_base_url(ws, alias)
        except (ImportError, FactoryError):
            pass
    ctx = Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=repo_path, env=env, out_dir=out_dir)
    started, t0 = now(), time.monotonic()
    bound = None
    if check.get("type") in gate.RUNTIME_TYPES and alias and repo_path:
        bound = env_binding(ws, alias, repo_path, git_head(repo_path))
    body = {"status": "error", "summary": bound} if bound else dispatch(check, ctx)
    record = write_evidence(run["id"], check, body, repo_path, started, time.monotonic() - t0)
    evidence_event(run, record)
    return record


def env_binding(ws: dict, alias: str, worktree: Path, head: str | None) -> str | None:
    """Runtime evidence must come from the env `factory env up` started from this worktree at this HEAD."""
    try:
        from .env import binding_error
    except ImportError as err:
        return f"env binding cannot be checked: {err}"
    try:
        return binding_error(ws, alias, worktree, head)
    except FactoryError as err:
        return str(err)


def quality_delta(run: dict, ws: dict, alias: str) -> dict:
    conf = repo_conf(ws, alias)
    wt = repo_path_for_run(run, ws, alias)
    baseline = compute_baseline(run, ws, alias)
    started, t0 = now(), time.monotonic()
    current = run_quality(wt, conf.get("quality") or {}, (conf.get("env") or {}).get("env_vars"))
    new, summary = {}, []
    for name, res in current.items():
        old = set((baseline["commands"].get(name) or {}).get("failures", []))
        added = [f for f in res["failures"] if f not in old]
        if added:
            new[name] = added[:50]
        summary.append(f"{name}: {len(res['failures'])} failing, {len(added)} new")
    body = {"status": "fail" if new else "pass", "summary": "; ".join(summary),
            "details": {"new_failures": new, "merge_base": baseline["merge_base"],
                        "current": {k: {"exit": v["exit"], "failures": len(v["failures"])} for k, v in current.items()}}}
    check = {"id": gate.quality_id(alias), "type": "quality", "repo": alias}
    record = write_evidence(run["id"], check, body, wt, started, time.monotonic() - t0)
    evidence_event(run, record)
    return record


REPRO_MAP = {"fail": "reproduced", "pass": "not-reproduced", "error": "error"}
LOCAL_TYPES = {"test"}  # run inside a temp worktree at the merge-base
ENV_TYPES = {"api", "browser"}  # run against the already-running env (--assume-env)


def repro_one(run: dict, ws: dict, check: dict, assume_env: bool, target: str | None = None) -> dict:
    """Run one check against the merge-base; evidence/REPRO-<id>.json (only the CLI writes it)."""
    alias = repo_conf(ws, check["repo"])["alias"]
    wt = repo_path_for_run(run, ws, alias)
    mb = gate.merge_base_for(run, ws, alias, target)
    rid = gate.repro_id(evidence_id(run, check["id"], target))
    conf = repo_conf(ws, alias)
    main = Path(conf.get("path") or wt)
    main = main if (main / ".git").exists() else wt
    env = dict(conf.get("env") or {})
    if not env.get("base_url"):
        try:
            from .env import base_url as env_base_url
            env["base_url"] = env_base_url(ws, alias)
        except (ImportError, FactoryError):
            pass
    started, t0 = now(), time.monotonic()
    inner = dict(check, id=rid)
    tmp = None
    ctype = check.get("type")
    try:
        if ctype in LOCAL_TYPES and not check.get("expect_fail_pattern"):
            body = {"status": "error", "summary": "test check has no expect_fail_pattern: a failure on base "
                    "cannot be told from a setup failure"}
        elif check.get("expect_fail_pattern") and (weak := weak_pattern_reason(check["expect_fail_pattern"])):
            body = {"status": "error", "summary": weak}
        elif ctype in LOCAL_TYPES:
            tmp = temp_worktree(main, mb, setup_command(ws, alias), env.get("env_vars"))
            ctx = Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=tmp, env=env,
                      out_dir=run_dir(run["id"]) / "evidence" / rid)
            body = dispatch(inner, ctx)
        elif ctype in ENV_TYPES:
            if not assume_env:
                body = {"status": "error", "summary": f"{ctype} check needs a running env serving the base; "
                        "rerun with --assume-env once the env is up on the unmodified run worktree"}
            elif git_head(wt) != mb or git_dirty(wt):
                body = {"status": "error", "summary": "--assume-env: run worktree must be clean and at the merge-base "
                        "(the running env would not serve the base code)"}
            elif (bound := env_binding(ws, alias, wt, mb)):
                body = {"status": "error", "summary": bound}
            else:
                ctx = Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=wt, env=env,
                          out_dir=run_dir(run["id"]) / "evidence" / rid)
                body = dispatch(inner, ctx)
                why = runtime_failure_error(ctype, check, body)
                if why:
                    body = dict(body, status="error", summary=f"could not reproduce: {why}")
        else:
            body = {"status": "error", "summary": f"{ctype} checks cannot be reproduced on base"}
    except RuntimeError as err:
        body = {"status": "error", "summary": str(err)}
    finally:
        if tmp is not None:
            drop_worktree(main, tmp)
    status = REPRO_MAP.get(body.get("status"), "error")
    record = {
        "schemaVersion": SCHEMA_VERSION, "check_id": rid, "of": check["id"], "type": ctype, "kind": "repro",
        "status": status, "repo": alias, "git_sha": mb, "merge_base": mb, "dirty": False,
        "collector": f"factory/repro {ctype}", "started": started,
        "duration_s": round(time.monotonic() - t0, 2),
        "summary": f"{status}: {body.get('summary', '')}",
        "artifacts": body.get("artifacts", []), "details": body.get("details", {}),
    }
    write_json(run_dir(run["id"]) / "evidence" / f"{rid}.json", record)
    evidence_event(run, record)
    return record


ASSERTION_RE = re.compile(r"expect\(|AssertionError|\bto(?:Be|Equal|Have|Contain|Match)\w*")


def runtime_failure_error(ctype: str, check: dict, body: dict) -> str | None:
    """A `fail` on base counts as a reproduction only when it is a real assertion or response mismatch."""
    if body.get("status") != "fail":
        return None
    details = body.get("details") or {}
    pattern = check.get("expect_fail_pattern")
    if ctype == "api":
        if details.get("http_status") is None or not details.get("problems"):
            return "api check failed without a response to compare (env error)"
        text = " ".join(map(str, details["problems"])) + " " + str(details.get("body_tail", ""))
        if pattern and not re.search(pattern, text, re.MULTILINE):
            return f"api response mismatch does not show expect_fail_pattern {pattern!r}"
        return None
    if ctype == "browser":
        failed = [t for t in details.get("tests") or [] if isinstance(t, dict) and not t.get("ok")]
        if not failed:
            return "browser spec reported fail without a failed test"
        text = "\n".join(f"{t.get('title', '')}: {t.get('error') or ''}" for t in failed)
        if pattern:
            if not re.search(pattern, text, re.MULTILINE):
                return f"failed test output does not show expect_fail_pattern {pattern!r}"
        elif not ASSERTION_RE.search(text):
            return "browser spec failed without an assertion failure (navigation or env error)"
    return None


def cmd_repro(args) -> int:
    run = run_load(args.run)
    ensure_owner(run)
    ws = workspace_load(run["workspace"])
    if not gate.is_bug_run(run):
        raise FactoryError("--repro is for bug runs (run start --bug KEY)")
    checks = gate.repro_checks(run["id"])
    if args.check:
        checks = [c for c in checks if c["id"] == args.check]
    if not checks:
        raise FactoryError("no reproducible checks (kind: bug ACs need a test/api/browser check)")
    records = []
    hf = hotfix_of(run)
    for chk in checks:
        if not chk.get("repo"):
            raise FactoryError(f"{chk['id']}: repo is required")
        for target in run_targets(run):
            if hf and target != hf["base"] and chk.get("type") not in LOCAL_TYPES:
                continue  # runtime checks are reproduced on the base target only (env serves one worktree)
            records.append(repro_one(run, ws, chk, args.assume_env, target))
    run_save(run)
    bad = [r for r in records if r["status"] != "reproduced"]
    if getattr(args, "json", False):
        print(json.dumps([{k: r[k] for k in ("check_id", "status", "summary", "git_sha")} for r in records], indent=2))
    else:
        for r in records:
            print(f"{r['status'].upper():14} {r['check_id']}  {r['summary']}")
        print(f"{len(records) - len(bad)}/{len(records)} reproduced")
    return 1 if bad else 0


def _lease_heartbeat(run: dict) -> None:
    """Keep the env lease alive while this run proves (no-op when it does not hold it)."""
    try:
        from .env import lease_heartbeat
        lease_heartbeat(run["workspace"], run["id"])
    except (FactoryError, OSError, ImportError):  # ImportError: env stubbed/unavailable -> nothing to beat
        pass


def cmd_prove(args) -> int:
    if getattr(args, "repro", False):
        return cmd_repro(args)
    run = run_load(args.run)
    ensure_owner(run)
    ws = workspace_load(run["workspace"])
    _lease_heartbeat(run)
    acs = gate.load_acs(run["id"])
    wanted_repo = repo_conf(ws, args.repo)["alias"] if args.repo else None
    checks = [c for c in acs.get("checks", []) if c.get("type") != "human"]
    if args.check:
        checks = [c for c in checks if c["id"] == args.check]
        if not checks:
            raise FactoryError(f"no runnable check {args.check}")
    if wanted_repo:
        checks = [c for c in checks if c.get("repo") and repo_conf(ws, c["repo"])["alias"] == wanted_repo]
    records = []
    for alias in {repo_conf(ws, c["repo"])["alias"] for c in checks if c.get("repo")}:
        if alias in run["repos"] and git_dirty(repo_path_for_run(run, ws, alias)):
            print(f"warning: {alias} worktree is dirty; evidence will be rejected by the gate")
    for chk in checks:
        for target in run_targets(run):
            if chk.get("repo") and target_worktree(run, ws, repo_conf(ws, chk["repo"])["alias"], target) is None:
                print(f"warning: {chk['id']}: no worktree for target {target} (factory run mirror); skipped")
                continue
            records.append(collect_one(run, ws, chk, target))
    if not args.check:
        for alias in _select_repos(run, ws, wanted_repo):
            if repo_conf(ws, alias).get("quality"):
                records.append(quality_delta(run, ws, alias))
        for rule in gate.rule_checks(ws, run):
            if wanted_repo and rule["repo"] != wanted_repo:
                continue
            records.append(collect_one(run, ws, rule))
    _lease_heartbeat(run)
    run_save(run)
    failed = [r for r in records if r["status"] != "pass"]
    if getattr(args, "json", False):
        print(json.dumps([{k: r[k] for k in ("check_id", "status", "summary", "git_sha", "dirty")} for r in records], indent=2))
    else:
        for r in records:
            print(f"{r['status'].upper():5} {r['check_id']}  {r['summary']}")
        print(f"{len(records) - len(failed)}/{len(records)} passed")
    return 1 if failed else 0


def register(sub) -> None:
    p = sub.add_parser("baseline", help="run quality commands on the merge-base once")
    p.add_argument("run")
    p.add_argument("--repo")
    p.add_argument("--force", action="store_true", help="recompute an existing baseline")
    p.set_defaults(func=cmd_baseline)
    p = sub.add_parser("prove", help="run collectors, quality delta and rules; write evidence")
    p.add_argument("run")
    p.add_argument("--check")
    p.add_argument("--repo")
    p.add_argument("--repro", action="store_true", help="bug runs: run bug-AC checks against the merge-base (must fail)")
    p.add_argument("--assume-env", action="store_true", help="with --repro: api/browser checks use the running env (must serve the unmodified base)")
    p.set_defaults(func=cmd_prove)
