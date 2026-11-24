"""Handlers of `factory env` and `factory wait`; the work lives in env.py, envdata.py and localhttp.py."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import env, envdata, localhttp, proc
from .util import FactoryError

WAIT_CAP_S = 600


def cmd_env(args) -> int:
    from . import cli
    proj = cli._project(args)
    sub, repo = args.sub, args.repo
    if sub == "status":
        rows = env.status(proj)
        print(json.dumps(rows, indent=2) if args.json else "\n".join(
            f"{r['repo']:<16} {r['state']:<6} {r['base_url']}" + (f" healthy={r['healthy']} run={r.get('run')} pid={r.get('pid')} {r.get('cwd')}" if r.get("pid") else "")
            for r in rows) or "no repo declares [repo.env]")
        return 0
    if sub == "logs":
        print(env.logs(proj, repo or _only(proj), args.tail))
        return 0
    run = cli._run(args, proj, writable=sub != "health")
    if sub == "up" and args.slot is not None:
        from . import slots
        slots.choose(proj, run, args.slot, args.actor)
    names = [repo] if repo else _declared(proj, run)
    out: list[str] = []
    code = 0
    for name in names:
        try:
            if sub == "up":
                out += env.up(proj, run, name, args.restart, args.wait or 0, None, args.user, args.actor)
            elif sub == "down":
                out += env.down(proj, run, name, args.actor)
            elif sub == "health":
                healthy = localhttp.healthy(env.health_url(env.env_of(proj, name)), 3)
                out.append(f"{name}: {'healthy' if healthy else 'not healthy'}")
                code = code or (0 if healthy else 3)
            else:
                rc, st = env.repo_of(proj, name), run.state()
                tree = env.target(proj, st, name)[0]
                out.append(envdata.seed_or_reset(proj, rc, sub, tree, run.id, rc["env"]["base_url"]))
        except env.EnvError as exc:
            print(f"error: {exc}", file=sys.stderr)
            code = 3
            out.append(f"{name}: FAILED ({exc.kind})")
    if args.json:
        print(json.dumps({"results": out, "exit": code}, indent=2))
    else:
        print("\n".join(out))
    return code


def _declared(proj, run) -> list[str]:
    st = run.state()
    names = [r for r in st["repos"] if proj_repo_has_env(proj, r)]
    if not names:
        raise FactoryError("no repo of this run declares [repo.env]; name a repo: factory env <command> <repo>")
    return names


def proj_repo_has_env(proj, name: str) -> bool:
    return any(r["name"] == name and r.get("env") for r in proj.cfg["repo"])


def _only(proj) -> str:
    names = [r["name"] for r in proj.cfg["repo"] if r.get("env")]
    if len(names) != 1:
        raise FactoryError(f"name the repo (with [repo.env]: {', '.join(names) or 'none'})")
    return names[0]


def cmd_wait(args) -> int:
    """Poll one of three probes until it holds or `--timeout` passes (cap 600 s); a URL answers when its status is below 500,
    with redirects not followed [L-84, L-168]."""
    probes = [p for p in (args.file, args.cmd, args.url) if p]
    if len(probes) != 1:
        raise FactoryError("usage: factory wait exactly one of --file <path>, --cmd <command> or --url <loopback url>, with --timeout <seconds>")
    limit = min(args.timeout, WAIT_CAP_S)
    if args.file:
        ok = localhttp.wait_until(lambda: Path(args.file).exists(), limit)
    elif args.url:
        localhttp.split(args.url)
        ok = localhttp.wait_until(lambda: localhttp.healthy(args.url, 2), limit, 0.5)
    else:
        ok = _wait_cmd(args.cmd, limit)
    print(("ready: " if ok else f"timed out after {limit}s waiting for: ") + probes[0])
    return 0 if ok else 1


def _wait_cmd(line: str, limit: float) -> bool:
    end = time.monotonic() + limit
    while True:
        if proc.run(line, cwd=Path.cwd(), env=proc.child_env(), timeout_s=max(1, min(60, end - time.monotonic())), shell=True).exit == 0:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.5)
