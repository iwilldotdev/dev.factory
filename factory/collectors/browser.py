"""Collector `browser`: Playwright Test spec via browser/dist/cli.js. Also hosts helpers shared by visual/style."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from factory.core import Ctx

FACTORY_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_S = 600  # last-resort cap for callers that pass none
DEFAULT_CAP_S = {"browser": 120, "visual": 120, "style": 120, "screenshot": 120, "api": 60}
GOTO_TIMEOUT_MS = 30000
CLI_MARGIN_S = 3  # the CLI stops itself this long before the cap, so it can still write result.json


def apply_expect_timeout(ctx: Ctx | None) -> None:
    """Workspace `limits.expect_timeout_ms` becomes the Playwright assertion timeout of browser specs
    (FACTORY_EXPECT_TIMEOUT_MS for the browser CLI); an explicit environment value wins."""
    if ctx is None or os.environ.get("FACTORY_EXPECT_TIMEOUT_MS"):
        return
    limits = (ctx.workspace or {}).get("limits")
    value = limits.get("expect_timeout_ms") if isinstance(limits, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        os.environ["FACTORY_EXPECT_TIMEOUT_MS"] = str(int(value))


def check_cap(check: dict, ctx: Ctx | None = None) -> float:
    """Wall-clock cap (seconds) of one check: its `timeout_s`, else workspace `limits.check_timeout_s`, else the
    default of its type (browser/visual/style/screenshot 120, api 60)."""
    apply_expect_timeout(ctx)

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None
    cap = num(check.get("timeout_s"))
    if cap is None and ctx is not None:
        limits = (ctx.workspace or {}).get("limits")
        cap = num(limits.get("check_timeout_s")) if isinstance(limits, dict) else None
    return cap if cap is not None else float(DEFAULT_CAP_S.get(check.get("type"), 120))


class Budget:
    """Wall-clock budget shared by the CLI calls of one check (visual: capture then compare)."""

    def __init__(self, cap: float):
        self.cap = float(cap)
        self.t0 = time.monotonic()

    def remaining(self) -> float:
        return max(1.0, self.cap - (time.monotonic() - self.t0))

    def cli_args(self, check: dict) -> list[str]:
        """`--timeout-ms` (CLI watchdog and Playwright per-test/global cap, below the Python cap),
        `--goto-timeout-ms`, and `--wait-until` when the check asks for it."""
        left = self.remaining()
        total_ms = int(max(1.0, left - CLI_MARGIN_S) * 1000)
        argv = ["--timeout-ms", str(total_ms), "--goto-timeout-ms", str(min(GOTO_TIMEOUT_MS, total_ms))]
        if check.get("wait_until") in ("load", "domcontentloaded", "networkidle", "commit"):
            argv += ["--wait-until", check["wait_until"]]
        return argv


def _error(summary: str, **details) -> dict:
    return {"status": "error", "summary": summary, "artifacts": [], "details": details}


def node_command() -> list[str] | dict:
    """argv prefix to run the browser CLI, or an error body."""
    override = os.environ.get("FACTORY_NODE_CLI")
    if override:
        if not Path(override).is_file():
            return _error(f"FACTORY_NODE_CLI not found: {override}")
        return [sys.executable, override] if override.endswith(".py") else ["node", override]
    if not shutil.which("node"):
        return _error("node not found on PATH (Node >= 20 required for browser/visual/style checks)")
    cli = FACTORY_ROOT / "browser" / "dist" / "cli.js"
    if not cli.is_file():
        return _error(f"browser CLI not built: run `npm install && npm run build` in {FACTORY_ROOT / 'browser'}")
    return ["node", str(cli)]


def base_url(ctx: Ctx) -> str | None:
    return (ctx.env or {}).get("base_url")


def full_url(check: dict, ctx: Ctx) -> str | dict:
    base = base_url(ctx)
    if not base:
        return _error("repo env has no base_url; cannot build the URL for this check")
    path = check.get("path") or ""
    return base.rstrip("/") + ("/" + path.lstrip("/") if path else "")


def storage_state(check: dict, ctx: Ctx) -> str | dict | None:
    """Storage state of the run's login_user (else the default user's, when it exists). An error body when the
    login_user's file is missing."""
    user = (ctx.run or {}).get("login_user")
    try:
        from factory.commands.env import login_command, storage_state_path
    except Exception:
        return None
    ws, alias = ctx.workspace.get("name"), check.get("repo")
    slot = 0
    try:
        from factory.commands.env import run_storage_slot
        slot = run_storage_slot(ctx.workspace, ctx.run, alias)
    except Exception:
        slot = 0
    if not isinstance(slot, int) or isinstance(slot, bool):
        slot = 0
    try:
        if slot:  # per slot: each slot logs in on its own origin (`<alias>@<k>.storage.json`)
            from factory.core import repo_conf
            path = Path(storage_state_path(ws, repo_conf(ctx.workspace, alias)["alias"], user, slot))
        else:
            path = Path(storage_state_path(ws, alias, user) if user else storage_state_path(ws, alias))
    except Exception:
        return None
    if path.is_file():
        return str(path)
    if user:
        return _error(f"no storage state for login user {user}: run `{login_command(ws, alias, user)}`")
    return None


def adapter_args(check: dict, ctx: Ctx) -> list[str] | dict:
    """`--adapter <file>` for a check's `adapter` (run-relative JSON of goto/click/fill/wait actions run after the
    page loads and before `wait_for`), [] without one, or an error body."""
    rel = check.get("adapter")
    if not rel:
        return []
    path = (ctx.run_dir / rel).resolve()
    try:
        path.relative_to(ctx.run_dir.resolve())
    except ValueError:
        return _error(f"adapter must stay inside the run dir: {rel}")
    if not path.is_file():
        return _error(f"adapter not found: {rel}")
    return ["--adapter", str(path)]


def out_dir(ctx: Ctx) -> Path:
    out = ctx.out_dir or (ctx.run_dir / "evidence" / "tmp")
    out.mkdir(parents=True, exist_ok=True)
    return out


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill `proc` and everything it spawned (Playwright browsers outlive a plain kill)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_capped(cmd: list[str], timeout: float) -> tuple[int | None, str, str, bool]:
    """Run `cmd` in its own process group; past `timeout` seconds kill the whole tree.
    Returns (returncode, stdout, stderr, timed_out)."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return proc.returncode, out or "", err or "", True


def run_cli(argv: list[str], out: Path, timeout: float = TIMEOUT_S) -> dict:
    """Run the CLI (killed with its children past `timeout`), return its result.json (or an error body).
    Never decides pass/fail itself."""
    prefix = node_command()
    if isinstance(prefix, dict):
        return prefix
    result_file = out / "result.json"
    if result_file.exists():
        result_file.unlink()
    try:
        rc, stdout, stderr, timed_out = run_capped([*prefix, *argv], timeout)
    except OSError as err:
        return _error(f"cannot run browser CLI: {err}")
    if timed_out and not result_file.is_file():
        tail = (stderr or stdout or "").strip()[-800:]
        return _error(f"browser CLI timed out after {int(timeout)}s (check cap); process tree killed",
                      timeout=True, output=tail)
    if not result_file.is_file():
        tail = (stderr or stdout or "").strip()[-800:]
        return _error(f"browser CLI exit {rc} without result.json", output=tail)
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        return _error(f"invalid result.json: {err}")
    if data.get("status") not in ("pass", "fail", "error"):
        return _error("result.json has no valid status", result=data)
    return data


def relativize(artifacts: list[str], out: Path, ctx: Ctx) -> list[str]:
    rels = []
    for a in artifacts:
        p = Path(a)
        p = p if p.is_absolute() else out / a
        try:
            rels.append(p.resolve().relative_to(ctx.run_dir.resolve()).as_posix())
        except ValueError:
            rels.append(str(p))
    return rels


def finish(data: dict, out: Path, ctx: Ctx) -> dict:
    return {
        "status": data["status"],
        "summary": data.get("summary", ""),
        "artifacts": relativize(data.get("artifacts", []), out, ctx),
        "details": data.get("details", {}),
    }


def _prepare(check: dict, ctx: Ctx):
    """(spec path, base url, storage state or None) of a browser check, or an error body."""
    url = base_url(ctx)
    if not url:
        return _error("repo env has no base_url; cannot run the browser spec")
    spec = ctx.run_dir / check["spec"]
    if not spec.is_file():
        return _error(f"spec not found: {check['spec']}")
    state = storage_state(check, ctx)
    if isinstance(state, dict):
        return state
    return spec, url, state


def collect(check: dict, ctx: Ctx) -> dict:
    prep = _prepare(check, ctx)
    if isinstance(prep, dict):
        return prep
    spec, url, state = prep
    out = out_dir(ctx)
    budget = Budget(check_cap(check, ctx))
    argv = ["run-spec", "--spec", str(spec), "--base-url", url, "--out", str(out), *budget.cli_args(check)]
    if state:
        argv += ["--storage-state", state]
    return finish(run_cli(argv, out, budget.cap), out, ctx)


def collect_batch(items: list[tuple[dict, Ctx]], bail: bool = False) -> list[dict] | None:
    """Run the specs of several checks (same repo, same target) in ONE Playwright process (`run-specs`: one worker, one
    project per check, shared storage state). One entry per item: `{"body": <collector body or None>, "duration_s",
    "ran"}`; a check whose spec/base_url/login is unusable gets its error body without joining the batch, a check
    Playwright did not run (`--max-failures` under `bail`, global timeout) has `ran` False. Bodies of run checks carry
    `details.batched: true`. Returns None when the batch cannot be used (CLI missing or crashed, no/unmappable
    batch.json, fewer than two usable specs): the caller runs the checks one by one."""
    prefix = node_command()
    if isinstance(prefix, dict) or not items:
        return None
    result: list[dict | None] = [None] * len(items)
    members: list[tuple[int, dict, Ctx, Path, Path]] = []
    shared: tuple | None = None
    for i, (check, ctx) in enumerate(items):
        prep = _prepare(check, ctx)
        if isinstance(prep, dict):
            result[i] = {"body": prep, "duration_s": 0.0, "ran": True}
            continue
        spec, url, state = prep
        if shared is not None and shared != (url, state):
            return None  # one process has one base URL and one login
        shared = (url, state)
        members.append((i, check, ctx, spec, out_dir(ctx)))
    if len(members) < 2:
        return None
    url, state = shared
    caps = {i: check_cap(check, ctx) for i, check, ctx, _, _ in members}
    manifest = []
    for i, check, ctx, spec, out in members:
        total_ms = int(max(1.0, caps[i] - CLI_MARGIN_S) * 1000)
        manifest.append({"id": check["id"], "spec": str(spec), "out": str(out), "timeout_ms": total_ms,
                         "goto_timeout_ms": min(GOTO_TIMEOUT_MS, total_ms)})
    total_cap = sum(caps.values())
    first_ctx = members[0][2]
    batch_out = first_ctx.run_dir / "evidence" / "_batch" / members[0][1]["id"]
    batch_out.mkdir(parents=True, exist_ok=True)
    manifest_file = batch_out / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    batch_file = batch_out / "batch.json"
    if batch_file.exists():
        batch_file.unlink()
    argv = ["run-specs", "--manifest", str(manifest_file), "--base-url", url, "--out", str(batch_out),
            "--timeout-ms", str(int(max(1.0, total_cap - CLI_MARGIN_S) * 1000))]
    if state:
        argv += ["--storage-state", state]
    if bail:
        argv += ["--max-failures", "1"]
    try:
        run_capped([*prefix, *argv], total_cap)
    except OSError:
        return None
    try:
        data = json.loads(batch_file.read_text(encoding="utf-8"))
        by_id = {r["id"]: r for r in data["results"]}
        for i, check, ctx, spec, out in members:
            r = by_id[check["id"]]
            if r["status"] not in ("pass", "fail", "error") or not isinstance(r["ran"], bool):
                return None
            if r["ran"]:
                details = dict(r.get("details") or {}, batched=True)
                body = {"status": r["status"], "summary": r.get("summary", ""),
                        "artifacts": relativize(["report.json", "run.log"], out, ctx), "details": details}
                result[i] = {"body": body, "duration_s": round(float(r.get("durationMs") or 0) / 1000, 2), "ran": True}
            else:
                result[i] = {"body": None, "duration_s": 0.0, "ran": False}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return result  # type: ignore[return-value]
