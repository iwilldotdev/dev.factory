"""Test data and sessions around a running env: seed, reset, preflight, warm-up, env patterns, login state and login headers
[L-40, L-49, L-50, L-52, L-79, L-80, L-81]."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, localhttp, proc
from .checks import runner
from .util import FactoryError, fwd, redact, safe_id


def data_prefix(run_id: str) -> str:
    """The data prefix handed to seed, reset and the server: derived from the run id, so runs never share test data [L-50]."""
    return "fx-" + safe_id(run_id).lower()[:40]


def user_suffix(user: str | None) -> str:
    return "" if not user else "_" + re.sub(r"[^A-Z0-9]", "_", user.upper())


def child_env(project, rc: dict, run_id: str, base_url: str, user: str | None = None, keys: list[str] | None = None) -> dict:
    """Secrets plus the run-derived data prefix for a command of this repo's env. With `user`, a key K resolves to `K_<SLUG>` [L-81]."""
    cfg = project.cfg
    wanted = rc["env"].get("login_secrets", []) if keys is None else keys
    values, _ = config.resolve_secrets(cfg)
    extra = {k: v for k, v in values.items() if k not in wanted}
    if user:
        values, _ = config.resolve_secrets(cfg, [k + user_suffix(user) for k in wanted])
        extra.update({k: values[k + user_suffix(user)] for k in wanted if k + user_suffix(user) in values})
    else:
        extra.update({k: v for k, v in values.items() if k in wanted})
    return proc.child_env({**extra, **rc["env"].get("vars", {}), "FACTORY_RUN": run_id, "FACTORY_BASE_URL": base_url,
                           "FACTORY_DATA_PREFIX": data_prefix(run_id)})


def secret_values(project, rc: dict, user: str | None = None) -> list[str]:
    return list(config.resolve_secrets(project.cfg)[0].values()) + ([] if not user else list(config.resolve_secrets(
        project.cfg, [k + user_suffix(user) for k in rc["env"].get("login_secrets", [])])[0].values()))


def shell(project, rc: dict, line: str, tree: Path, run_id: str, base_url: str, timeout_s: int = 300) -> tuple[bool, str]:
    res = proc.run(line, cwd=tree, env=child_env(project, rc, run_id, base_url), timeout_s=timeout_s, shell=True)
    text = redact("\n".join((res.out + "\n" + res.err).strip().splitlines()[-15:]), secret_values(project, rc))
    return (not res.timed_out and res.exit == 0), (f"timed out after {timeout_s}s\n" if res.timed_out else f"exit {res.exit}\n") + text


def seed_or_reset(project, rc: dict, which: str, tree: Path, run_id: str, base_url: str) -> str:
    line = rc["env"].get(which)
    if not line:
        raise FactoryError(f"repo {rc['name']} has no [repo.env] {which} command")
    ok, text = shell(project, rc, line, tree, run_id, base_url)
    if not ok:
        raise FactoryError(f"{which} failed for {rc['name']}: {text}", 3)
    return f"{which} done for {rc['name']} with FACTORY_DATA_PREFIX={data_prefix(run_id)}"


def preflight(project, rc: dict, tree: Path, run_id: str, base_url: str) -> str | None:
    """A failing data check is env kind `data`, never a product failure [L-50]."""
    for line in rc["env"].get("preflight", []):
        ok, text = shell(project, rc, line, tree, run_id, base_url)
        if not ok:
            return f"preflight {line!r} failed: {text}"
    return None


def warmup(base_url: str, paths: list[str]) -> str | None:
    """Request each local path once so a cold server cannot fail the first check [L-40]; redirects are not followed."""
    for path in paths:
        try:
            status = localhttp.request("GET", localhttp.join(base_url, path), timeout=60)[0]
        except (OSError, FactoryError) as exc:
            return f"warm-up {path}: {exc}"
        if status >= 500:
            return f"warm-up {path}: status {status}"
    return None


def matches_env(patterns: list[str], text: str) -> str | None:
    """The first `env_patterns` regex that the failure output matches: the failure is the environment, not the product [L-49]."""
    return next((p for p in patterns if re.search(p, text, re.M)), None)


def state_file(project, repo: str, user: str | None = None) -> Path:
    return project.factory_dir / "env" / (f"{repo}.{safe_id(user)}.state.json" if user else f"{repo}.state.json")


def login(project, run, rc: dict, tree: Path, base_url: str, user: str | None = None) -> str | None:
    """Run the repo's `login_spec` in the bundled runner and keep the storage state for UI checks. Credentials come from
    env vars only; a redirect to another origin is env kind `env` [L-79, L-80, L-81]. Returns a problem or None."""
    spec = rc["env"].get("login_spec")
    if not spec:
        return None
    path = tree / spec
    if not path.is_file():
        return f"login_spec {spec} does not exist in {fwd(tree)}"
    missing = [k + user_suffix(user) for k in rc["env"].get("login_secrets", []) if k + user_suffix(user) not in config.resolve_secrets(
        project.cfg, [k + user_suffix(user)])[0]]
    if missing:
        return f"login secrets not set: {', '.join(missing)}"
    out = project.factory_dir / "env" / f"{rc['name']}.login"
    out.mkdir(parents=True, exist_ok=True)
    target = state_file(project, rc["name"], user)
    out_run = runner.invoke(project.cfg, ["login", "--spec", fwd(path), "--state-out", fwd(target), "--base-url", base_url], out, 120,
                            child_env(project, rc, run.id, base_url, user))
    if out_run.code != 0 or not target.is_file():
        why = (out_run.result or {}).get("summary") or out_run.text.strip()[-300:] or f"exit {out_run.code}"
        return f"login failed (exit {out_run.code}): {redact(why, secret_values(project, rc, user))}"
    return None


def login_headers(project, rc: dict, tree: Path, run_id: str, base_url: str, user: str | None) -> dict:
    """Headers printed by `login_http` for an `http` check with `session`; kept in memory, never stored [L-52]."""
    line = rc["env"].get("login_http")
    if not line:
        raise FactoryError(f"repo {rc['name']} has no [repo.env] login_http, so an http check cannot use a session")
    res = proc.run(line, cwd=tree, env=child_env(project, rc, run_id, base_url, user, rc["env"].get("login_secrets", [])), timeout_s=60, shell=True)
    try:
        headers = json.loads(res.out)
        if res.exit != 0 or not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise ValueError
    except ValueError:
        raise FactoryError(f"login_http of {rc['name']} must print a JSON object of header values and exit 0 (exit {res.exit})", 3) from None
    return headers
