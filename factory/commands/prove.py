"""`factory baseline` and `factory prove` (DESIGN §3, §4)."""

from __future__ import annotations

import importlib
import json
import re
import time
import traceback
from pathlib import Path

from ..collectors.test import DEFAULT_TIMEOUT_S, drop_worktree, run_shell, temp_worktree
from ..core import (SCHEMA_VERSION, Ctx, FactoryError, event, git, git_dirty,
                    git_head, now, read_json, repo_conf, repo_path_for_run,
                    run_dir, run_load, run_save, workspace_load, write_evidence,
                    write_json)
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
    return sorted(ids)


def run_quality(root: Path, quality: dict, env_vars: dict | None) -> dict:
    out = {}
    for name, cmd in quality.items():
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
    tmp = temp_worktree(main if (main / ".git").exists() else wt, merge_base)
    try:
        results = run_quality(tmp, quality, (conf.get("env") or {}).get("env_vars"))
    finally:
        drop_worktree(main if (main / ".git").exists() else wt, tmp)
    data = {"schemaVersion": SCHEMA_VERSION, "repo": alias, "merge_base": merge_base,
            "base_ref": base, "computed": now(), "commands": results}
    write_json(path, data)
    return data


def cmd_baseline(args) -> int:
    run = run_load(args.run)
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


def collect_one(run: dict, ws: dict, check: dict) -> dict:
    alias = repo_conf(ws, check["repo"])["alias"] if check.get("repo") else None
    repo_path = repo_path_for_run(run, ws, alias) if alias else None
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
    body = dispatch(check, ctx)
    record = write_evidence(run["id"], check, body, repo_path, started, time.monotonic() - t0)
    event(run, "evidence", check=check["id"], status=record["status"], sha=record["git_sha"])
    return record


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
    event(run, "evidence", check=check["id"], status=record["status"], sha=record["git_sha"])
    return record


REPRO_MAP = {"fail": "reproduced", "pass": "not-reproduced", "error": "error"}
LOCAL_TYPES = {"test"}  # run inside a temp worktree at the merge-base
ENV_TYPES = {"api", "browser"}  # run against the already-running env (--assume-env)


def repro_one(run: dict, ws: dict, check: dict, assume_env: bool) -> dict:
    """Run one check against the merge-base; evidence/REPRO-<id>.json (only the CLI writes it)."""
    alias = repo_conf(ws, check["repo"])["alias"]
    wt = repo_path_for_run(run, ws, alias)
    mb = gate.merge_base_for(run, ws, alias)
    rid = gate.repro_id(check["id"])
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
        if ctype in LOCAL_TYPES:
            tmp = temp_worktree(main, mb)
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
            else:
                ctx = Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=wt, env=env,
                          out_dir=run_dir(run["id"]) / "evidence" / rid)
                body = dispatch(inner, ctx)
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
    event(run, "evidence", check=rid, status=status, sha=mb)
    return record


def cmd_repro(args) -> int:
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    if not gate.is_bug_run(run):
        raise FactoryError("--repro is for bug runs (run start --bug KEY)")
    checks = gate.repro_checks(run["id"])
    if args.check:
        checks = [c for c in checks if c["id"] == args.check]
    if not checks:
        raise FactoryError("no reproducible checks (kind: bug ACs need a test/api/browser check)")
    records = []
    for chk in checks:
        if not chk.get("repo"):
            raise FactoryError(f"{chk['id']}: repo is required")
        records.append(repro_one(run, ws, chk, args.assume_env))
    run_save(run)
    bad = [r for r in records if r["status"] != "reproduced"]
    if getattr(args, "json", False):
        print(json.dumps([{k: r[k] for k in ("check_id", "status", "summary", "git_sha")} for r in records], indent=2))
    else:
        for r in records:
            print(f"{r['status'].upper():14} {r['check_id']}  {r['summary']}")
        print(f"{len(records) - len(bad)}/{len(records)} reproduced")
    return 1 if bad else 0


def cmd_prove(args) -> int:
    if getattr(args, "repro", False):
        return cmd_repro(args)
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
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
        records.append(collect_one(run, ws, chk))
    if not args.check:
        for alias in _select_repos(run, ws, wanted_repo):
            if repo_conf(ws, alias).get("quality"):
                records.append(quality_delta(run, ws, alias))
        for rule in gate.rule_checks(ws, run):
            if wanted_repo and rule["repo"] != wanted_repo:
                continue
            records.append(collect_one(run, ws, rule))
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
