"""`factory baseline` and `factory prove` (DESIGN §3, §4)."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from pathlib import Path

from ..collectors import browser as browser_collector
from ..collectors.test import (DEFAULT_TIMEOUT_S, drop_worktree, files_from_main_of, run_shell, setup_command,
                               temp_worktree, weak_pattern_reason)
from ..core import (SCHEMA_VERSION, check_target, code_repos, ensure_owner, Ctx, FactoryError, event, evidence_event, evidence_id, git,
                    git_dirty, git_head, hotfix_of, now, read_json, repo_conf,
                    repo_path_for_run, run_base_branch, run_dir, run_load, run_targets, run_update,
                    keep_history, load_evidence, repo_serve_path, target_worktree, workspace_load, write_evidence, write_json)
from .. import integrity, shots
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


def _quote_file(path: str) -> str:
    return subprocess.list2cmdline([path]) if os.name == "nt" else shlex.quote(path)


def changed_files(root: Path, merge_base: str | None) -> list[str]:
    """Existing files changed by the run: `git diff --name-only <merge-base>...HEAD`."""
    if not merge_base:
        return []
    try:
        names = git(root, "diff", "--name-only", f"{merge_base}...HEAD").splitlines()
    except FactoryError:
        return []
    return [n.strip() for n in names if n.strip() and (root / n.strip()).is_file()]


def _env_vars(ws: dict, run: dict | None, alias: str) -> dict | None:
    """The repo's `env_vars` with the run's slot placeholders resolved (raw when the slot env cannot be built)."""
    try:
        from .env import slot_env
        return slot_env(ws, run, alias).get("env_vars")
    except (ImportError, FactoryError):
        return (repo_conf(ws, alias).get("env") or {}).get("env_vars")


def run_quality(root: Path, quality: dict, env_vars: dict | None, changed: list[str] | None = None) -> dict:
    """`{changed}` in a command (repo `quality_fast`, fast mode) is the quoted list of changed files; with no changed
    file the command is skipped as a pass."""
    out = {}
    for name, cmd in quality.items():
        if name == "setup":  # worktree preparation, not a quality command
            continue
        if "{changed}" in cmd:
            if not changed:
                out[name] = {"cmd": cmd, "exit": 0, "failures": [], "skipped": "no changed files"}
                continue
            cmd = cmd.replace("{changed}", " ".join(_quote_file(f) for f in changed))
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
    base = gate.resolve_base(wt, run_base_branch(run, ws, alias))
    merge_base = git(wt, "merge-base", "HEAD", base)
    main = Path(conf.get("path") or wt)
    env_vars = _env_vars(ws, run, alias)
    try:
        tmp = temp_worktree(main if (main / ".git").exists() else wt, merge_base, quality.get("setup"), env_vars,
                            files_from_main_of(ws, alias))
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
        if alias not in code_repos(run):
            raise FactoryError(f"repo {alias} is env_only in run {run['id']} (serves the env, carries no code)")
        return [alias]
    return list(code_repos(run))


# ---------- prove ----------

def dispatch(check: dict, ctx: Ctx, before: bool = False) -> dict:
    module = "factory.collectors." + check["type"].replace("-", "_")
    try:
        collector = importlib.import_module(module)
    except ImportError as err:
        return {"status": "error", "summary": f"collector {module} not available: {err}"}
    try:
        return (collector.collect_before if before else collector.collect)(check, ctx)
    except Exception:  # collectors must not crash the run
        return {"status": "error", "summary": "collector crashed",
                "details": {"traceback": traceback.format_exc()[-3000:]}}


def log_evidence(run: dict, record: dict) -> None:
    """Append the `evidence` event to run.json under the run lock, merging into whatever is on disk now: a
    `run human/decide` that happened while the proof ran survives (the in-memory `run` is never saved)."""
    run_update(run["id"], lambda r: evidence_event(r, record))


def check_ctx(run: dict, ws: dict, check: dict, target: str | None) -> tuple[dict, Ctx, str | None, Path | None]:
    """(check with its evidence id, collector context, repo alias, worktree) of a check at a target."""
    alias = repo_conf(ws, check["repo"])["alias"] if check.get("repo") else None
    repo_path = target_worktree(run, ws, alias, target) if alias else None
    if target and hotfix_of(run):
        check = dict(check, id=evidence_id(run, check["id"], target))
    out_dir = run_dir(run["id"]) / "evidence" / check["id"]
    try:
        env = run_env(ws, run, alias) if alias else {}
    except FactoryError:
        env = {}  # collect_one records the slot error (setup) before any collector sees this ctx
    return check, Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=repo_path, env=env, out_dir=out_dir), alias, repo_path


def collect_one(run: dict, ws: dict, check: dict, target: str | None = None, skip_reason: str | None = None,
                mode: str = "default", retry: bool = False, pre: dict | None = None) -> dict:
    """`pre` is the result of this check inside a browser batch (`{"body", "duration_s", "started"}`): used instead of
    running the collector, everything else (digest, retry, evidence record) is the same."""
    digest = integrity.check_digest(run["id"], check)
    slot_err = run_env_error(ws, run, repo_conf(ws, check["repo"])["alias"]) if check.get("repo") else None
    check, ctx, alias, repo_path = check_ctx(run, ws, check, target)
    started, t0 = now(), time.monotonic()
    bound, hot = None, False
    runtime = check.get("type") in gate.RUNTIME_TYPES | {shots.SHOT_TYPE}
    if runtime and alias and repo_path and not slot_err and not skip_reason:
        head = git_head(repo_path)
        bound = env_binding(ws, alias, repo_path, head, run, strict=mode == "final")
        bound = bound or dep_binding(run, ws, alias, target, strict=mode == "final")
        hot = not bound and mode != "final" and hot_reload_drift(ws, alias, head, run)
    if skip_reason:
        body = {"status": "error", "summary": f"skipped: {skip_reason}", "details": {"skipped": skip_reason.split()[0]}}
    elif slot_err:
        body = {"status": "error", "summary": f"slot env of {alias}: {slot_err}",
                "details": {"fail_kind": "setup", "slot_error": slot_err}}
    elif bound:
        body = {"status": "error", "summary": bound,
                "details": {"fail_kind": "env", "env_kind": binding_kind(bound)}}
    else:
        body = pre["body"] if pre else dispatch(check, ctx)
        if pre:
            started = pre.get("started") or started
        if retry and check.get("type") == "browser" and body.get("status") == "fail":
            probe = {"type": "browser", "status": "fail", "summary": body.get("summary"), "details": body.get("details")}
            if (classify(probe) or ("",))[0] == "assert":  # an assert failure only: env/timeout/setup are not retried
                if dispatch(check, ctx).get("status") == "pass":  # same sha, passes now: flaky, never a pass
                    body = dict(body, summary=f"FLAKY: {body.get('summary', '')}",
                                details=dict(body.get("details") or {}, flaky=True))
    body = dict(body, details=dict(body.get("details") or {}, mode=mode, check_digest=digest))
    if check.get("type") in RUNTIME:
        body["details"]["run_heads"] = integrity.run_heads(run, target)
        if hot:  # served by a process started at another HEAD (env.hot_reload): gate prove refuses it, --final restarts
            body["details"]["hot_reload"] = True
        shared = shared_env_heads(ws, run)
        if shared:  # shared repos serve another run's code (DESIGN section 11)
            body["details"]["shared"] = shared
    duration = float(pre["duration_s"]) if pre and not (skip_reason or bound) else time.monotonic() - t0
    body = dict(body, failure=failure_of(check.get("type") or "", body))
    record = write_evidence(run["id"], check, body, repo_path, started, duration)
    log_evidence(run, record)
    return record


def binding_kind(message: str) -> str:
    """ENV-FAIL kind of an env_binding message."""
    return "stale" if re.search(r"started at|serves|worktree HEAD", message) else "start"


def run_env(ws: dict, run: dict | None, alias: str) -> dict:
    """The repo's env block as the run's slot sees it (ports, health, base_url of its slot, DESIGN section 11), with
    `base_url` resolved like `factory env` does (base_url, else the origin of health). A slot env that cannot be
    resolved raises FactoryError (never a silent fall back to slot 0: evidence would hit another run's instance)."""
    env = dict(repo_conf(ws, alias).get("env") or {})
    slot = 0
    try:
        from .env import run_slot, slot_env
    except ImportError:
        pass
    else:
        env, slot = slot_env(ws, run, alias), run_slot(run, repo_conf(ws, alias)["alias"])
    if not env.get("base_url"):
        try:
            from .env import base_url as env_base_url
            env["base_url"] = env_base_url(ws, alias, slot) if slot else env_base_url(ws, alias)
        except (ImportError, FactoryError):
            pass
    return env


def run_env_error(ws: dict, run: dict | None, alias: str) -> str | None:
    """Why the run's slot env of `alias` cannot be resolved (bad slot, missing port_stride, unknown {base_url:X}), else None."""
    try:
        run_env(ws, run, alias)
    except FactoryError as err:
        return str(err)
    return None


def hot_reload_drift(ws: dict, alias: str, head: str | None, run: dict | None) -> bool:
    try:
        from .env import hot_reload_drift as drift
        return bool(drift(ws, alias, head, run))
    except (ImportError, FactoryError):
        return False


def shared_env_heads(ws: dict, run: dict | None) -> dict:
    try:
        from .env import shared_heads
        return shared_heads(ws, run)
    except (ImportError, FactoryError):
        return {}


def dep_closure(ws: dict, aliases) -> list[str]:
    """Aliases in the env.depends_on closure of `aliases` (the aliases themselves excluded): those processes serve
    the runtime checks of `aliases` too."""
    seen, out, todo = set(aliases), [], list(aliases)
    while todo:
        try:
            deps = (repo_conf(ws, todo.pop()).get("env") or {}).get("depends_on") or []
        except FactoryError:
            continue
        for dep in deps:
            try:
                dep = repo_conf(ws, dep)["alias"]
            except FactoryError:
                continue
            if dep not in seen:
                seen.add(dep)
                out.append(dep)
                todo.append(dep)
    return sorted(out)


def dep_binding(run: dict, ws: dict, alias: str, target: str | None, strict: bool = False) -> str | None:
    """First binding error of the env.depends_on closure of `alias`, or None."""
    for dep in dep_closure(ws, [alias]):
        try:
            wt = repo_serve_path(run, ws, dep, target, warn=lambda msg: None)  # what `env up` serves
            head = git_head(wt) if (Path(wt) / ".git").exists() else None
        except FactoryError:
            continue
        err = env_binding(ws, dep, wt, head, run, strict=strict)
        if err:
            return err
    return None


def env_binding(ws: dict, alias: str, worktree: Path, head: str | None, run: dict | None = None,
                strict: bool = False) -> str | None:
    """Runtime evidence must come from the env `factory env up` started from this worktree at this HEAD (in the
    run's slot). `strict` (`--final`): `env.hot_reload` does not excuse a HEAD mismatch."""
    try:
        from .env import binding_error
    except ImportError as err:
        return f"env binding cannot be checked: {err}"
    import inspect
    try:
        takes_strict = "strict" in inspect.signature(binding_error).parameters
    except (TypeError, ValueError):
        takes_strict = False
    kw = {"strict": True} if strict and takes_strict else {}  # a stand-in env module without `strict` (tests)
    try:
        if run is not None and (run.get("env_slots") or (ws.get("env") or {}).get("lease")):
            return binding_error(ws, alias, worktree, head, run=run, **kw)
        return binding_error(ws, alias, worktree, head, **kw)
    except FactoryError as err:
        return str(err)


def quality_digest(run: dict, ws: dict, alias: str, mode: str = "default") -> str:
    """What a quality delta depends on besides the code: the commands and the baseline it is measured against."""
    import hashlib
    base = run_dir(run["id"]) / "baseline" / f"{alias}.json"
    merge_base = read_json(base).get("merge_base") if base.is_file() else None
    conf = repo_conf(ws, alias)
    body = {"q": conf.get("quality") or {}, "mb": merge_base}
    if mode == "fast" and conf.get("quality_fast"):
        body["qf"] = conf["quality_fast"]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def quality_delta(run: dict, ws: dict, alias: str, mode: str = "default") -> dict:
    conf = repo_conf(ws, alias)
    wt = repo_path_for_run(run, ws, alias)
    baseline = compute_baseline(run, ws, alias)
    started, t0 = now(), time.monotonic()
    fast = mode == "fast" and bool(conf.get("quality_fast"))
    current = run_quality(wt, conf["quality_fast"] if fast else conf.get("quality") or {},
                          _env_vars(ws, run, alias),
                          changed_files(wt, baseline.get("merge_base")) if fast else None)
    new, summary = {}, []
    for name, res in current.items():
        old = set((baseline["commands"].get(name) or {}).get("failures", []))
        added = [f for f in res["failures"] if f not in old]
        if added:
            new[name] = added[:50]
        summary.append(f"{name}: {res['skipped']}" if res.get("skipped")
                       else f"{name}: {len(res['failures'])} failing, {len(added)} new")
    body = {"status": "fail" if new else "pass", "summary": "; ".join(summary),
            "details": {"new_failures": new, "merge_base": baseline["merge_base"],
                        "current": {k: {"exit": v["exit"], "failures": len(v["failures"])} for k, v in current.items()},
                        "mode": mode, "check_digest": quality_digest(run, ws, alias, mode)}}
    check = {"id": gate.quality_id(alias), "type": "quality", "repo": alias}
    body["failure"] = failure_of("quality", body)
    record = write_evidence(run["id"], check, body, wt, started, time.monotonic() - t0)
    log_evidence(run, record)
    return record


REPRO_MAP = {"fail": "reproduced", "pass": "not-reproduced", "error": "error"}
LOCAL_TYPES = {"test"}  # run inside a temp worktree at the merge-base
ENV_TYPES = {"api", "browser"}  # run against the already-running env (--assume-env)


def repro_one(run: dict, ws: dict, check: dict, assume_env: bool, target: str | None = None,
              served: Path | None = None, serve_error: str | None = None) -> dict:
    """Run one check against the merge-base; evidence/REPRO-<id>.json (only the CLI writes it). `served`: the env of the
    repo was switched to a temporary worktree at the merge-base (`cmd_repro`, run HEAD moved after the fix), so
    `--assume-env` and a clean run worktree at the merge-base are not needed; `serve_error`: why that switch failed."""
    alias = repo_conf(ws, check["repo"])["alias"]
    wt = repo_path_for_run(run, ws, alias)
    mb = gate.merge_base_for(run, ws, alias, target)
    rid = gate.repro_id(evidence_id(run, check["id"], target))
    conf = repo_conf(ws, alias)
    main = Path(conf.get("path") or wt)
    main = main if (main / ".git").exists() else wt
    slot_err = run_env_error(ws, run, alias)
    env = {} if slot_err else run_env(ws, run, alias)
    started, t0 = now(), time.monotonic()
    inner = dict(check, id=rid)
    tmp = None
    ctype = check.get("type")
    try:
        if slot_err:
            body = {"status": "error", "summary": f"slot env of {alias}: {slot_err}",
                    "details": {"fail_kind": "setup", "slot_error": slot_err}}
        elif ctype in LOCAL_TYPES and not check.get("expect_fail_pattern"):
            body = {"status": "error", "summary": "test check has no expect_fail_pattern: a failure on base "
                    "cannot be told from a setup failure"}
        elif check.get("expect_fail_pattern") and (weak := weak_pattern_reason(check["expect_fail_pattern"])):
            body = {"status": "error", "summary": weak}
        elif ctype in LOCAL_TYPES:
            tmp = temp_worktree(main, mb, setup_command(ws, alias), env.get("env_vars"), files_from_main_of(ws, alias))
            ctx = Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=tmp, env=env,
                      out_dir=run_dir(run["id"]) / "evidence" / rid)
            body = dispatch(inner, ctx)
        elif ctype in ENV_TYPES:
            at = served if served is not None else wt
            if serve_error:
                body = {"status": "error", "summary": serve_error}
            elif served is None and not assume_env:
                body = {"status": "error", "summary": f"{ctype} check needs a running env serving the base; "
                        "rerun with --assume-env once the env is up on the unmodified run worktree"}
            elif served is None and (git_head(wt) != mb or git_dirty(wt)):
                body = {"status": "error", "summary": "--assume-env: run worktree must be clean and at the merge-base "
                        "(the running env would not serve the base code)"}
            elif (bound := env_binding(ws, alias, at, mb, run)):
                body = {"status": "error", "summary": bound}
            else:
                ctx = Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=at, env=env,
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
    keep_history(run["id"], rid)
    write_json(run_dir(run["id"]) / "evidence" / f"{rid}.json", record)
    log_evidence(run, record)
    return record


def before_one(run: dict, ws: dict, check: dict, assume_env: bool, served: Path | None = None,
               serve_error: str | None = None) -> dict:
    """Screenshot check on the unfixed base (bug runs): evidence/BEFORE-<id>.json, `pass` when images were captured.
    Same env rule as runtime repro checks: --assume-env, run worktree clean and at the merge-base, env bound to it."""
    alias = repo_conf(ws, check["repo"])["alias"]
    wt = repo_path_for_run(run, ws, alias)
    target = run_targets(run)[0]
    mb = gate.merge_base_for(run, ws, alias, target)
    bid = shots.before_id(evidence_id(run, check["id"], target))
    slot_err = run_env_error(ws, run, alias)
    env = {} if slot_err else run_env(ws, run, alias)
    started, t0 = now(), time.monotonic()
    if slot_err:
        body = {"status": "error", "summary": f"slot env of {alias}: {slot_err}",
                "details": {"fail_kind": "setup", "slot_error": slot_err}}
    elif serve_error:
        body = {"status": "error", "summary": serve_error}
    elif served is None and not assume_env:
        body = {"status": "error", "summary": "screenshot on base needs the env serving the base: rerun with "
                "--assume-env once the env is up on the unmodified run worktree"}
    elif served is None and (git_head(wt) != mb or git_dirty(wt)):
        body = {"status": "error", "summary": "--assume-env: run worktree must be clean and at the merge-base "
                "(before screenshots show the unfixed code)"}
    elif (bound := env_binding(ws, alias, served if served is not None else wt, mb, run)):
        body = {"status": "error", "summary": bound}
    else:
        ctx = Ctx(run=run, workspace=ws, run_dir=run_dir(run["id"]), repo_path=served if served is not None else wt, env=env,
                  out_dir=run_dir(run["id"]) / "evidence" / bid)
        body = dispatch(dict(check, id=bid), ctx, before=True)
    status = body.get("status") if body.get("status") in ("pass", "fail", "error") else "error"
    record = {
        "schemaVersion": SCHEMA_VERSION, "check_id": bid, "of": check["id"], "type": check.get("type"), "kind": "before",
        "status": status, "repo": alias, "git_sha": mb, "merge_base": mb, "dirty": False,
        "collector": "factory/before screenshot", "started": started, "duration_s": round(time.monotonic() - t0, 2),
        "summary": body.get("summary", ""), "artifacts": body.get("artifacts", []), "details": body.get("details", {}),
    }
    keep_history(run["id"], bid)
    write_json(run_dir(run["id"]) / "evidence" / f"{bid}.json", record)
    log_evidence(run, record)
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


def moved_repro_repos(run: dict, ws: dict, checks: list[dict], shot_checks: list[dict]) -> dict[str, str]:
    """{alias: merge-base} of the repos whose runtime repro (or BEFORE screenshot) checks need an env on the merge-base while
    the run worktree has moved on (HEAD != merge-base: the fix is committed, e.g. a check-only replan after build). Their
    env is switched to a temporary merge-base worktree (`env.serve_from`) instead of demanding a worktree at the merge-base."""
    base = run_targets(run)[0]
    out: dict[str, str] = {}
    runtime = [c for c in checks if c.get("type") in ENV_TYPES]
    for chk in [*runtime, *shot_checks]:
        if not chk.get("repo"):
            continue
        alias = repo_conf(ws, chk["repo"])["alias"]
        if alias in out or alias not in code_repos(run):
            continue
        mb = gate.merge_base_for(run, ws, alias, base)
        if git_head(repo_path_for_run(run, ws, alias)) != mb:
            out[alias] = mb
    return out


def _repo_main(run: dict, ws: dict, alias: str) -> Path:
    main = Path(repo_conf(ws, alias).get("path") or repo_path_for_run(run, ws, alias))
    return main if (main / ".git").exists() else repo_path_for_run(run, ws, alias)


def _base_worktree(run: dict, ws: dict, alias: str, mb: str, on_target=None) -> Path:
    """Temporary detached worktree of `alias` at the merge-base, prepared like every worktree (setup, `files_from_main`)."""
    env = {} if run_env_error(ws, run, alias) else run_env(ws, run, alias)
    return temp_worktree(_repo_main(run, ws, alias), mb, setup_command(ws, alias), env.get("env_vars"),
                         files_from_main_of(ws, alias), on_target)


def cmd_repro(args) -> int:
    run = run_load(args.run)
    ensure_owner(run)
    ws = workspace_load(run["workspace"])
    if not gate.is_bug_run(run):
        raise FactoryError("--repro is for bug runs (run start --bug KEY)")
    checks = gate.repro_checks(run["id"])
    shot_checks = shots.shot_checks(shots.acs_of(run["id"]))
    if shots.decision(run) != "yes":
        shot_checks = []  # before images exist only when the user asked for visual evidence
    if args.check:
        checks = [c for c in checks if c["id"] == args.check]
        shot_checks = [c for c in shot_checks if c["id"] == args.check]
    if not checks and not shot_checks:
        raise FactoryError("no reproducible checks (kind: bug ACs need a test/api/browser check)")
    records = []
    hf = hotfix_of(run)
    for chk in [*checks, *shot_checks]:
        if not chk.get("repo"):
            raise FactoryError(f"{chk['id']}: repo is required")
    json_mode = bool(getattr(args, "json", False))

    def say(text: str) -> None:
        print(text, file=sys.stderr if json_mode else sys.stdout, flush=True)

    with contextlib.ExitStack() as stack:
        served: dict[str, Path] = {}
        outcomes: dict = {}
        serve_errors: dict[str, str] = {}
        for alias, mb in moved_repro_repos(run, ws, checks, shot_checks).items():
            say(f"{alias}: run worktree is past the merge-base {mb[:8]}: serving the merge-base from a temporary worktree "
                "for the runtime repro checks (the env is switched and restored)")
            main = _repo_main(run, ws, alias)
            try:  # the cleanup is registered before the worktree exists (an interrupt cannot leak it) and runs after the env restore
                tmp = _base_worktree(run, ws, alias, mb, lambda t, m=main: stack.callback(drop_worktree, m, t))
            except (FactoryError, RuntimeError) as err:
                serve_errors[alias] = f"could not serve the merge-base of {alias}: {err}"
                continue
            try:
                from .env import serve_from
                outcomes[alias] = stack.enter_context(serve_from(ws, run, alias, tmp, say))
            except FactoryError as err:
                serve_errors[alias] = str(err)
                continue
            served[alias] = tmp
        for chk in checks:
            alias = repo_conf(ws, chk["repo"])["alias"]
            for target in run_targets(run):
                if hf and target != hf["base"] and chk.get("type") not in LOCAL_TYPES:
                    continue  # runtime checks are reproduced on the base target only (env serves one worktree)
                records.append(repro_one(run, ws, chk, args.assume_env, target, served.get(alias), serve_errors.get(alias)))
        for chk in shot_checks:  # before images (DESIGN §12): same env, same steps as the after capture at HEAD
            alias = repo_conf(ws, chk["repo"])["alias"]
            records.append(before_one(run, ws, chk, args.assume_env, served.get(alias), serve_errors.get(alias)))
    bad = [r for r in records if r["status"] != ("pass" if r.get("kind") == "before" else "reproduced")]
    not_restored = [(a, o.detail) for a, o in outcomes.items() if o.restore_failed]  # evidence above stays as written
    if not_restored:
        from .env import env_fail_line
    for alias, detail in not_restored:
        say(env_fail_line(alias, "start", f"env not restored: {detail}"))
    if getattr(args, "json", False):
        print(json.dumps([{k: r[k] for k in ("check_id", "status", "summary", "git_sha")} for r in records], indent=2))
    else:
        for r in records:
            print(f"{r['status'].upper():14} {r['check_id']}  {r['summary']}")
        print(f"{len(records) - len(bad)}/{len(records)} " + ("ok (REPRO reproduced, BEFORE captured)" if shot_checks else "reproduced"))
    if not_restored:
        return EXIT_ENV
    return 1 if bad else 0


def _lease_heartbeat(run: dict) -> None:
    """Keep the env lease alive while this run proves (no-op when it does not hold it)."""
    try:
        from .env import lease_heartbeat
        lease_heartbeat(run["workspace"], run["id"])
    except (FactoryError, OSError, ImportError):  # ImportError: env stubbed/unavailable -> nothing to beat
        pass


# ---------- failure classification and fail-fast ----------

EXIT_FAIL, EXIT_USAGE, EXIT_ENV = 1, 2, 3
RUNTIME = gate.RUNTIME_TYPES | {shots.SHOT_TYPE}
ENV_RE = re.compile(
    r"ECONNREFUSED|ECONNRESET|ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|ERR_EMPTY_RESPONSE|ERR_SSL|connection refused|"
    r"env not reachable|not reachable|login (?:failed|did not|form)|session expired|storage ?state|"
    r"no storage state|health(?:check)? .{0,20}\b5\d\d\b|\b50[234] (?:bad gateway|service unavailable|gateway)", re.I)
# Text a runtime check can show that names an infrastructure cause, not a product bug: (pattern, env kind). The env kind
# becomes `ENV-FAIL repo=<alias> kind=<env kind>` and stops the repo's remaining runtime checks. Extend the table, not classify().
ENV_BODY_CLASSES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"SSL Authentication Error", re.I), "db"),
]
TIMEOUT_RE = re.compile(r"timed out after|Timeout \d+ms exceeded|\[timeout after|check cap", re.I)


def _first_line(text: str, limit: int = 200) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return "no detail"


def _text_of(record: dict) -> str:
    try:
        blob = json.dumps(record.get("details") or {}, ensure_ascii=False)[:6000]
    except (TypeError, ValueError):
        blob = ""
    return f"{record.get('summary', '')}\n{blob}"


def body_env_kind(text: str) -> str | None:
    """Env kind of the first `ENV_BODY_CLASSES` pattern found in `text` (a response body, a test error), else None."""
    for pattern, kind in ENV_BODY_CLASSES:
        if pattern.search(text or ""):
            return kind
    return None


def _body_is_server_side(record: dict) -> bool:
    """The body classes name a server-side cause, so they apply only to a 5xx answer, or (no HTTP status in the record) to a
    check that errored at the request level. A 200/4xx page, or a failed assertion that merely quotes the text, is not env."""
    status = (record.get("details") or {}).get("http_status")
    if isinstance(status, int) and not isinstance(status, bool):
        return 500 <= status <= 599
    return record.get("status") == "error"


def env_kind_of(text: str) -> str:
    if re.search(r"started at|serves|worktree HEAD", text):
        return "stale"
    if re.search(r"no pid record|not started by|not running|pid was reused", text):
        return "start"
    return "unhealthy"


def classify(record: dict, first_runtime: bool = False) -> tuple[str, str | None] | None:
    """(kind, env_kind) of a non-pass record: kind is timeout|assert|env|setup. A timeout on the first
    navigation of a repo's runtime batch is an env failure (kind env, env_kind timeout)."""
    status = record.get("status")
    if status == "pass":
        return None
    details = record.get("details") or {}
    if details.get("skipped"):
        return "env" if details["skipped"] == "env" else "setup", None
    text = _text_of(record)
    if record.get("type") in RUNTIME and details.get("fail_kind") == "env":
        return "env", details.get("env_kind") or env_kind_of(text)
    if record.get("type") in RUNTIME and _body_is_server_side(record) and (body_kind := body_env_kind(text)):
        return "env", body_kind
    if record.get("type") in RUNTIME and ENV_RE.search(text):
        return "env", env_kind_of(text)
    if details.get("timeout") or TIMEOUT_RE.search(text):
        if first_runtime and record.get("type") in RUNTIME:
            return "timeout", "timeout"
        return "timeout", None
    return ("assert" if status == "fail" else "setup"), None


def failure_of(check_type: str, body: dict) -> dict | None:
    """`{kind, reason[, env_kind]}` of a non-pass result (kind: timeout|assert|env|setup|flaky, reason: its first summary
    line, <=160 chars), stored in the evidence record and on its run.json event; None for a pass."""
    if body.get("status") == "pass":
        return None
    kind, env_kind = classify({"type": check_type, "status": body.get("status"), "summary": body.get("summary"),
                               "details": body.get("details")}) or ("setup", None)
    if (body.get("details") or {}).get("flaky"):
        kind = "flaky"
    out = {"kind": kind, "reason": _first_line(body.get("summary") or "", 160)}
    if env_kind:
        out["env_kind"] = env_kind
    return out


class Reporter:
    """PROVE-FAIL / ENV-FAIL lines: stdout, or stderr with --json (stdout stays one JSON document)."""

    def __init__(self, json_mode: bool):
        self.json_mode = json_mode
        self.env_failed = False

    def line(self, text: str) -> None:
        print(text, file=sys.stderr if self.json_mode else sys.stdout, flush=True)

    def prove_fail(self, check_id: str, kind: str, detail: str) -> None:
        if kind == "env":
            self.env_failed = True
        self.line(f"PROVE-FAIL check={check_id} kind={kind} detail={_first_line(detail)}")

    def env_fail(self, alias: str, kind: str, detail: str) -> None:
        from .env import env_fail_line
        self.env_failed = True
        self.line(env_fail_line(alias, kind, detail))


def _preflight_envs(run: dict, ws: dict, aliases: list[str], rep: Reporter, stopped: dict, strict: bool = False,
                    target: str | None = None) -> None:
    """A running env of ours that serves another HEAD/worktree than the run is restarted once (env down + up)
    when the run holds the lease; nothing else is probed here (the first runtime check does that). `strict`
    (`--final`): a hot_reload repo started at another HEAD is stale too, so final evidence comes from HEAD."""
    try:
        from .env import deps_first, restart_blocker, restart_env, stale_reason
    except ImportError:
        return
    kw = {"strict": True} if strict else {}
    tkw = {"target": target} if target else {}
    todo: list[tuple[str, str]] = []
    for alias in aliases:
        try:
            reason = stale_reason(ws, run, alias, **kw, **tkw)
            if not reason:
                continue
            blocked = restart_blocker(ws, run, alias)
        except FactoryError as err:
            stopped[alias] = f"env restart refused: {err}"
            rep.env_fail(alias, "lease" if "lease" in str(err) else "start", str(err))
            continue
        if blocked:  # another run's (or a hand-started) env, or the lease is not ours: never touched
            stopped[alias] = f"env not restarted: {blocked}"
            rep.env_fail(alias, "stale", blocked)
            continue
        todo.append((alias, reason))
    # dependencies first (`depends_on` order; `aliases` is alphabetical): a dependent restarts onto its fresh dependency,
    # and one still serving another target would make `env up` of the dependent refuse
    reasons = dict(todo)
    for alias in deps_first(ws, [a for a, _ in todo]):
        reason = reasons[alias]
        try:
            print(f"env {alias}: stale ({reason}); auto-restarting once (env down + up)", flush=True)
            ok, lines = restart_env(ws, run, alias, **tkw)
        except FactoryError as err:
            stopped[alias] = f"env restart refused: {err}"
            rep.env_fail(alias, "lease" if "lease" in str(err) else "start", str(err))
            continue
        if not ok:
            detail = _first_line(next((l for l in reversed(lines) if l.strip() and not l.startswith("---")), "restart failed"))
            stopped[alias] = f"env restart failed: {detail}"
            rep.env_fail(alias, "start", f"restart failed: {detail}")


def _preflight_files(run: dict, ws: dict, aliases: list[str], rep: Reporter, stopped: dict,
                     target: str | None = None) -> None:
    """`env.files_from_main`: a repo whose served path lacks a listed (gitignored) file is an env failure; its runtime
    checks are not run (they would answer 500s that look like product bugs)."""
    try:
        from .env import files_missing
    except ImportError:
        return
    for alias in aliases:
        try:
            missing = files_missing(ws, run, alias, target)
        except FactoryError:
            continue
        if missing:
            stopped[alias] = missing[0]
            rep.env_fail(alias, "files", missing[0])


def _preflight_requests(run: dict, ws: dict, aliases: list[str], rep: Reporter, stopped: dict) -> None:
    """`env.preflight[].request` (test data over HTTP, e.g. the test user's project is not expired): a failing request is
    `ENV-FAIL kind=data` naming the preflight, and the repo's runtime checks are not run. Only against an env that
    answers (an env that is down is reported by its first runtime check, not as a data problem)."""
    try:
        from .env import _health_url, health_ok, is_shared, request_preflights, run_slot
    except ImportError:
        return
    for alias in aliases:
        if alias in stopped:
            continue
        try:
            slot = 0 if is_shared(ws, run, alias) else run_slot(run, alias)
            if not any(isinstance(i.get("request"), dict) for i in (repo_conf(ws, alias).get("env") or {}).get("preflight") or []):
                continue
            if not health_ok(_health_url(ws, alias, slot)):
                continue
            failed = request_preflights(ws, run, alias)
        except FactoryError:
            continue
        if failed:
            name, why = failed[0]
            stopped[alias] = f"preflight {name}: {why}"
            rep.env_fail(alias, "data", f"{name}: {why}")


def _reset_data(run: dict, ws: dict, checks: list[dict], aliases: list[str], reuse: bool, rep: Reporter,
                stopped: dict, prior=None, targets: list | None = None) -> None:
    """Once before the runtime batch: `env.seed.reset` of every runtime repo that declares it (and still has a runtime
    check to run). A failing reset is an env failure: the repo's runtime checks are not run."""
    try:
        from .env import data_run, seed_conf
    except ImportError:
        return
    for alias in aliases:
        if not seed_conf(ws, alias).get("reset"):
            continue
        pending = [c for c in checks if c.get("type") in RUNTIME and c.get("repo") and repo_conf(ws, c["repo"])["alias"] == alias
                   and any(not reuse or (prior(c, t) if prior else reusable(run, ws, c, t, integrity.check_digest(run["id"], c))) is None
                           for t in (targets or run_targets(run)))]
        if not pending:
            continue
        try:
            code, text = data_run(ws, run, alias, "reset")
        except FactoryError as err:
            code, text = 1, str(err)
        if code != 0:
            detail = f"env.seed.reset exited {code}: {_first_line(text)}"
            stopped[alias] = detail
            rep.env_fail(alias, "seed", detail)
        else:
            rep.line(f"env {alias}: test data reset (env_reset ok)")


def _quality_check(run: dict, ws: dict, wanted: str):
    for alias in code_repos(run):
        if gate.quality_id(alias) == wanted and repo_conf(ws, alias).get("quality"):
            return alias
    return None


# ---------- fast verification (DESIGN §14) ----------

COST_RANK = {"test": 0, "red-green": 1, "api": 2, "style": 3, "visual": 4, "screenshot": 5, "browser": 6}


def prove_mode(args) -> str:
    fast, final = bool(getattr(args, "fast", False)), bool(getattr(args, "final", False))
    if fast and final:
        raise FactoryError("--fast and --final are exclusive (--fast = bail + reuse; --final = run all, no reuse, no bail)")
    return "fast" if fast else "final" if final else "default"


def reusable(run: dict, ws: dict, chk: dict, target: str | None, digest: str, mode: str = "default") -> dict | None:
    """The latest evidence of `chk` when it is a pass at the current HEAD of a clean worktree for the same check digest
    and still valid for the gates (event digest, plan lock): `prove` does not run such a check again. Evidence from
    `--fast` is reused only by another `--fast` run (gate prove refuses it, so a default run must re-collect it)."""
    cid = evidence_id(run, chk["id"], target)
    ev = load_evidence(run["id"], cid)
    if not ev or ev.get("status") != "pass" or ev.get("dirty"):
        return None
    if (ev.get("details") or {}).get("mode") == "fast" and mode != "fast":
        return None
    if (ev.get("details") or {}).get("hot_reload") and mode != "fast":
        return None  # served by a process at another HEAD: only a --fast run may reuse it
    if (ev.get("details") or {}).get("check_digest") != digest:
        return None
    head = None
    if chk.get("repo"):
        alias = repo_conf(ws, chk["repo"])["alias"]
        try:
            wt = target_worktree(run, ws, alias, target)
            if wt is None or git_dirty(wt):
                return None
            head = git_head(wt)
        except FactoryError:
            return None
    if ev.get("git_sha") != head or integrity.evidence_error(run, cid):
        return None
    if chk.get("type") in RUNTIME and (ev.get("details") or {}).get("run_heads") != integrity.run_heads(run, target):
        return None  # runtime evidence also exercises the other repos of the run: reuse only if none of them moved
    return ev


def order_checks(run: dict, ws: dict, checks: list[dict]) -> list[dict]:
    """Risk order: (a) last evidence fail/error, (b) no evidence or check changed, (c) its repo moved since the
    evidence, (d) the rest; inside a group the cheapest type first, then the shortest last duration."""
    target = run_targets(run)[0]

    def key(item):
        idx, chk = item
        ev = load_evidence(run["id"], evidence_id(run, chk["id"], target))
        duration = float((ev or {}).get("duration_s") or 0)
        if ev and ev.get("status") != "pass":
            group = 0
        elif not ev or (ev.get("details") or {}).get("check_digest") != integrity.check_digest(run["id"], chk):
            group = 1
        else:
            group = 3
            if chk.get("repo"):
                try:
                    alias = repo_conf(ws, chk["repo"])["alias"]
                    wt = target_worktree(run, ws, alias, target)
                    if wt is not None and git_head(wt) != ev.get("git_sha"):
                        group = 2
                except FactoryError:
                    pass
        return (group, COST_RANK.get(chk.get("type"), 9), duration, idx)

    return [c for _, c in sorted(enumerate(checks), key=key)]


def batch_enabled(ws: dict) -> bool:
    """Workspace `limits.browser_batch: false` turns browser batching off."""
    limits = ws.get("limits")
    return not (isinstance(limits, dict) and limits.get("browser_batch") is False)


def quality_prior(run: dict, ws: dict, alias: str, mode: str) -> dict | None:
    """Latest quality evidence of `alias` when `prove` may reuse it (pass, clean, same HEAD, same digest)."""
    qid = gate.quality_id(alias)
    prior = load_evidence(run["id"], qid)
    wt = repo_path_for_run(run, ws, alias)
    if (prior and prior.get("status") == "pass" and not prior.get("dirty") and not git_dirty(wt)
            and ((prior.get("details") or {}).get("mode") != "fast" or mode == "fast")
            and prior.get("git_sha") == git_head(wt)
            and (prior.get("details") or {}).get("check_digest") == quality_digest(run, ws, alias, mode)
            and not integrity.evidence_error(run, qid)):
        return prior
    return None


# ---------- estimate (prove --estimate) ----------

DEFAULT_SECONDS = {"test": 10, "api": 15, "style": 25, "visual": 25, "screenshot": 30, "browser": 60}
QUALITY_DEFAULT_S = 30  # quality uses its last duration; this is the guess before any evidence
FALLBACK_S = 10


def _last_duration(run: dict, eid: str) -> float | None:
    ev = load_evidence(run["id"], eid)
    try:
        return float(ev["duration_s"]) if ev and ev.get("duration_s") is not None else None
    except (TypeError, ValueError):
        return None


def estimate_rows(run: dict, ws: dict, items: list[tuple[dict, str | None]], reuse: bool, mode: str) -> list[dict]:
    """One row per check in prove order: REUSED (0 s) or RUN with its expected seconds (last duration_s of its
    evidence, else the default of its type)."""
    rows = []
    for chk, target in items:
        ctype = chk.get("type")
        eid = chk["id"] if ctype == "quality" else evidence_id(run, chk["id"], target)
        last = _last_duration(run, eid)
        secs = last if last is not None else float(QUALITY_DEFAULT_S if ctype == "quality" else DEFAULT_SECONDS.get(ctype, FALLBACK_S))
        if not reuse:
            reused = False
        elif ctype == "quality":
            reused = quality_prior(run, ws, chk["repo"], mode) is not None
        else:
            reused = reusable(run, ws, chk, target, integrity.check_digest(run["id"], chk), mode) is not None
        rows.append({"check_id": eid, "type": ctype, "action": "REUSED" if reused else "RUN",
                     "expected_s": 0.0 if reused else round(secs, 1), "last_s": None if last is None else round(last, 1),
                     "full_s": round(secs, 1)})
    return rows


def cmd_estimate(args, run: dict, ws: dict, checks: list[dict], only_quality, only_rule, wanted_repo, mode: str,
                 targets: list | None = None, base_only: bool = True) -> int:
    """`prove --estimate`: nothing runs, nothing is written. Per check in prove order: REUSED or RUN and the expected
    seconds; totals for the chosen mode, `--fast` (reuse) and `--final` (everything runs)."""
    reuse = mode != "final" and not getattr(args, "all", False)
    items: list[tuple[dict, str | None]] = []
    for chk in checks:
        for target in targets or run_targets(run):
            if chk.get("repo") and target_worktree(run, ws, repo_conf(ws, chk["repo"])["alias"], target) is None:
                continue
            items.append((chk, target))
    if not base_only:
        only_quality, only_rule = None, None
    if only_quality:
        qaliases = [only_quality]
    elif args.check or not base_only:
        qaliases = []
    else:
        qaliases = [a for a in _select_repos(run, ws, wanted_repo) if repo_conf(ws, a).get("quality")]
    items += [({"id": gate.quality_id(a), "type": "quality", "repo": a}, None) for a in qaliases]
    if only_rule:
        rules = only_rule
    elif args.check or not base_only:
        rules = []
    else:
        rules = [r for r in gate.rule_checks(ws, run) if not wanted_repo or r["repo"] == wanted_repo]
    items += [(r, None) for r in rules]
    rows = estimate_rows(run, ws, items, reuse, mode)
    total = round(sum(r["expected_s"] for r in rows), 1)
    fast = round(sum(r["expected_s"] for r in estimate_rows(run, ws, items, True, "fast")), 1)
    final = round(sum(r["full_s"] for r in rows), 1)
    out = {"run": run["id"], "mode": mode,
           "checks": [{k: r[k] for k in ("check_id", "type", "action", "expected_s", "last_s")} for r in rows],
           "total_s": total, "fast_s": fast, "final_s": final}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
        return 0
    for r in rows:
        last = f"last {r['last_s']:g}s" if r["last_s"] is not None else "default"
        print(f"{r['action']:6} {r['check_id']:24} {r['type']:10} ~{r['expected_s']:g}s ({last})")
    print(f"total ~{total:g}s ({mode}); --fast ~{fast:g}s (reuse passing evidence); --final ~{final:g}s (everything runs)")
    return 0


def cmd_prove(args) -> int:
    if getattr(args, "repro", False):
        return cmd_repro(args)
    mode = prove_mode(args)
    estimate = bool(getattr(args, "estimate", False))
    reuse = mode != "final" and not getattr(args, "all", False)
    bail = mode == "fast" or (bool(getattr(args, "bail", False)) and mode != "final")
    retry = not getattr(args, "no_retry", False)
    wall0 = time.monotonic()
    run = run_load(args.run)
    if not estimate:
        ensure_owner(run)
    ws = workspace_load(run["workspace"])
    if not estimate:
        _lease_heartbeat(run)
    acs = gate.load_acs(run["id"])
    wanted_repo = repo_conf(ws, args.repo)["alias"] if args.repo else None
    checks = [c for c in acs.get("checks", []) if c.get("type") != "human"]
    only_quality, only_rule = None, None
    if args.check:
        picked = [c for c in checks if c["id"] == args.check]
        if not picked and args.check.startswith("QUALITY-"):
            only_quality = _quality_check(run, ws, args.check)
            if only_quality is None:
                raise FactoryError(f"no runnable check {args.check} (repo has no quality commands or is not in the run)")
        elif not picked and args.check.startswith("RULE-"):
            only_rule = [r for r in gate.rule_checks(ws, run) if r["id"] == args.check]
            if not only_rule:
                raise FactoryError(f"no runnable check {args.check}")
        elif not picked:
            raise FactoryError(f"no runnable check {args.check}")
        checks = picked
    if wanted_repo:
        checks = [c for c in checks if c.get("repo") and repo_conf(ws, c["repo"])["alias"] == wanted_repo]
    checks = order_checks(run, ws, checks)
    only_target = check_target(run, getattr(args, "target", None))
    targets = [only_target or run_targets(run)[0]] if getattr(args, "target", None) else run_targets(run)
    base_only = not only_target
    if not base_only and (only_quality or only_rule):
        raise FactoryError(f"{args.check}: quality and rule checks run only for the base target "
                           f"({run_targets(run)[0]}); --target {args.target} would run nothing")
    if estimate:
        return cmd_estimate(args, run, ws, checks, only_quality, only_rule, wanted_repo, mode, targets, base_only)
    json_mode = bool(getattr(args, "json", False))
    rep = Reporter(json_mode)
    records: list[dict] = []
    reused: set[str] = set()
    bail_stop: str | None = None
    for alias in {repo_conf(ws, c["repo"])["alias"] for c in checks if c.get("repo")}:
        if alias in code_repos(run) and git_dirty(repo_path_for_run(run, ws, alias)):
            print(f"warning: {alias} worktree is dirty; evidence will be rejected by the gate")
    stopped: dict[str, str] = {}  # repo alias -> why its remaining runtime checks are skipped
    runtime_aliases = sorted({repo_conf(ws, c["repo"])["alias"] for c in checks
                              if c.get("repo") and c.get("type") in RUNTIME and repo_conf(ws, c["repo"])["alias"] in run["repos"]})
    prior_cache: dict[tuple, dict | None] = {}

    def prior_of(chk: dict, target) -> dict | None:
        """reusable() once per (check, target) per prove invocation: batch planning asks for every later check, and
        nothing a check's reuse depends on (its evidence, the worktree HEADs) changes before that check runs."""
        key = (chk["id"], target)
        if key not in prior_cache:
            prior_cache[key] = reusable(run, ws, chk, target, integrity.check_digest(run["id"], chk), mode)
        return prior_cache[key]

    def take_reused(prior: dict | None) -> bool:
        if prior is None:
            return False
        records.append(prior)
        reused.add(prior["check_id"])
        rep.line(f"REUSED {prior['check_id']}")
        return True

    _preflight_envs(run, ws, runtime_aliases + [d for d in dep_closure(ws, runtime_aliases) if d not in runtime_aliases],
                    rep, stopped, strict=mode == "final",
                    target=(only_target or targets[0]) if hotfix_of(run) else None)
    _preflight_files(run, ws, runtime_aliases, rep, stopped, target=(only_target or targets[0]) if hotfix_of(run) else None)
    if not args.check:
        _reset_data(run, ws, checks, runtime_aliases, reuse, rep, stopped, prior=prior_of, targets=targets)
    _preflight_requests(run, ws, runtime_aliases, rep, stopped)  # after the reset: it checks the data the checks will use
    seen_runtime: set[str] = set()
    last_sig: tuple | None = None
    batch_stop: str | None = None

    def handle(record: dict, chk: dict) -> None:
        nonlocal last_sig, batch_stop, bail_stop
        runtime = chk.get("type") in RUNTIME
        alias = repo_conf(ws, chk["repo"])["alias"] if chk.get("repo") else "-"
        first = runtime and alias not in seen_runtime
        if runtime:
            seen_runtime.add(alias)
        cls = classify(record, first_runtime=first)
        if cls is None:
            last_sig = None
            return
        kind, env_kind = cls
        detail = _first_line(record.get("summary") or "")
        rep.prove_fail(record["check_id"], "flaky" if (record.get("details") or {}).get("flaky") else kind, detail)
        if record.get("details", {}).get("skipped"):
            return
        if runtime and env_kind:
            rep.env_fail(alias, env_kind, detail)
            stopped[alias] = f"{record['check_id']}: {detail}"
            last_sig = None
        elif runtime:
            if bail and kind == "assert" and not bail_stop:
                bail_stop = f"{record['check_id']} failed: {detail}"
            sig = (kind, detail)
            if sig == last_sig:
                batch_stop = f"two identical failures ({kind}: {detail})"
            last_sig = sig
        else:
            last_sig = None

    batch_on = batch_enabled(ws)
    batched: dict[str, dict] = {}  # evidence id -> result of the check inside a batch
    attempted: set[str] = set()    # evidence ids a batch was already tried for (a failed batch is not retried)

    def batch_members(chk: dict, target) -> list[dict]:
        """`chk` plus the later browser checks of the same repo and target that will run too (not reused)."""
        alias = repo_conf(ws, chk["repo"])["alias"]
        members = [chk]
        for other in checks[checks.index(chk) + 1:]:
            if other.get("type") != "browser" or not other.get("repo") or repo_conf(ws, other["repo"])["alias"] != alias:
                continue
            if reuse and prior_of(other, target) is not None:
                continue
            members.append(other)
        return members

    def run_batch(members: list[dict], target) -> None:
        items = []
        for m in members:
            m2, ctx, alias, wt = check_ctx(run, ws, m, target)
            items.append((m2, ctx))
        attempted.update(m2["id"] for m2, _ in items)
        if run_env_error(ws, run, alias):  # unresolvable slot env: every check records its own setup error
            return
        if (env_binding(ws, alias, wt, git_head(wt), run, strict=mode == "final")
                or dep_binding(run, ws, alias, target, strict=mode == "final")):  # stale/missing env: every check records its own env failure
            return
        started = now()
        _lease_heartbeat(run)
        try:
            res = browser_collector.collect_batch(items, bail=bail)
        except Exception:  # a crashing batch must not crash the run
            res = None
            print(f"batch: crashed ({traceback.format_exc().splitlines()[-1]})", file=sys.stderr)
        if res is None or len(res) != len(items):
            rep.line(f"batch: {len(items)} browser checks of {alias} could not run as one process; running them one by one")
            return
        for (m2, _), r in zip(items, res):
            batched[m2["id"]] = dict(r, started=started)

    def run_checks(chk: dict, target) -> None:
        alias = repo_conf(ws, chk["repo"])["alias"] if chk.get("repo") else None
        runtime = chk.get("type") in RUNTIME
        reason = None
        if runtime and alias and alias in stopped:
            reason = f"env (not run: {stopped[alias]})"
        elif runtime and bail_stop:
            reason = f"bail (not run: {bail_stop})"
        elif runtime and batch_stop:
            reason = f"batch-stopped (not run: {batch_stop})"
        if reason is None and reuse and take_reused(prior_of(chk, target)):
            return
        pre = None
        cid = evidence_id(run, chk["id"], target)
        if reason is None and batch_on and chk.get("type") == "browser" and alias:
            if cid not in batched and cid not in attempted:
                members = batch_members(chk, target)
                if len(members) >= 2:
                    run_batch(members, target)
            pre = batched.pop(cid, None)
            if pre is not None and not pre.get("ran"):
                pre = None  # Playwright did not get to it (bail/global timeout) and nothing stops it now: run it alone
        elif reason:
            batched.pop(cid, None)  # it ran in a batch but a stop applies now: recorded as skipped, as when run one by one
        _lease_heartbeat(run)
        record = collect_one(run, ws, chk, target, skip_reason=reason, mode=mode, retry=retry, pre=pre)
        records.append(record)
        handle(record, chk)

    for chk in checks:
        for target in targets:
            if chk.get("repo") and target_worktree(run, ws, repo_conf(ws, chk["repo"])["alias"], target) is None:
                print(f"warning: {chk['id']}: no worktree for target {target} (factory run mirror); skipped")
                continue
            run_checks(chk, target)
    def quality(alias: str) -> None:
        if reuse and take_reused(quality_prior(run, ws, alias, mode)):
            return
        _lease_heartbeat(run)
        records.append(quality_delta(run, ws, alias, mode=mode))
        handle(records[-1], {"type": "quality", "repo": alias})

    def rule_check(rule: dict) -> None:
        if reuse and take_reused(prior_of(rule, None)):
            return
        _lease_heartbeat(run)
        records.append(collect_one(run, ws, rule, mode=mode))
        handle(records[-1], rule)

    if only_quality:
        quality(only_quality)
    for rule in only_rule or []:
        rule_check(rule)
    if not args.check and base_only:
        for alias in _select_repos(run, ws, wanted_repo):
            if repo_conf(ws, alias).get("quality"):
                quality(alias)
        for rule in gate.rule_checks(ws, run):
            if wanted_repo and rule["repo"] != wanted_repo:
                continue
            rule_check(rule)
    _lease_heartbeat(run)
    failed = [r for r in records if r["status"] != "pass"]
    skipped = [r for r in records if (r.get("details") or {}).get("skipped")]
    counts = {"ran": len(records) - len(reused), "reused": len(reused), "skipped": len(skipped),
              "failed": len(failed) - len(skipped)}
    try:
        run_update(run["id"], lambda r: event(r, "prove", mode=mode, **counts, wall_s=round(time.monotonic() - wall0, 1),
                                              bail=bool(bail_stop)))
    except FactoryError:
        pass
    if json_mode:
        print(json.dumps([{k: r[k] for k in ("check_id", "status", "summary", "git_sha", "dirty")} for r in records], indent=2))
    else:
        for r in records:
            if r["check_id"] not in reused:
                print(f"{r['status'].upper():5} {r['check_id']}  {r['summary']}")
        print(f"{len(records) - len(failed)}/{len(records)} passed" + (f" ({len(reused)} reused)" if reused else ""))
        if skipped:
            print(f"fail-fast: {len(skipped)} runtime check(s) not run; fix the cause, then `factory prove {run['id']}` again")
    flaky = [r["check_id"] for r in records if r.get("flaky")]
    if flaky:  # no gate effect: a pass that failed before at the same sha and check digest is reported, not blocked
        rep.line(f"FLAKY: {', '.join(flaky)} failed earlier at the same sha with the same check and pass now (see evidence/history/)")
    if rep.env_failed:
        return EXIT_ENV
    return EXIT_FAIL if failed else 0


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
    p.add_argument("--target", help="hotfix run: only the checks of this target (a mirror also skips quality and rules); "
                   "start its env first: `env up --run <run> --target <target>`")
    p.add_argument("--fast", action="store_true", help="bail at the first runtime assert failure + reuse passing evidence "
                   "(gate prove refuses fast evidence: finish with --final)")
    p.add_argument("--final", action="store_true", help="run every check: no reuse, no bail (the run gate prove wants)")
    p.add_argument("--bail", action="store_true", help="skip the remaining runtime checks after the first assert failure")
    p.add_argument("--no-retry", action="store_true", help="do not re-run a failed browser assert once to detect a flaky check")
    p.add_argument("--all", action="store_true", help="do not reuse evidence of the same HEAD and check digest")
    p.add_argument("--estimate", action="store_true", help="run nothing: per check REUSED or RUN with its expected seconds, "
                   "and the --fast / --final totals (--json supported)")
    p.add_argument("--repro", action="store_true", help="bug runs: run bug-AC checks against the merge-base (must fail)")
    p.add_argument("--assume-env", action="store_true", help="with --repro: api/browser checks use the running env (must serve the unmodified base)")
    p.set_defaults(func=cmd_prove)
