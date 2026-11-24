"""Env slots: several runs of one repo at once, each on its own ports. `env up --slot n` records the slot in the run; every command of that
run then sees ports shifted by `n * port_offset` (`.factory/local.toml`, `[repo.<name>.slots]`), a lock, a log and a login state per slot.
`{port}`, `{base_url}`, `{<repo>.port}` and `{<repo>.base_url}` in an env's `up`, `down`, `seed`, `reset`, `health` and `vars` are derived [L-78]."""
from __future__ import annotations

import copy
import re

from . import localhttp
from .util import FactoryError

FIELDS = ("up", "down", "seed", "reset", "health")


def key(project, repo: str) -> str:
    """The lock, log and login-state name of a repo: one per slot."""
    return repo if not project.slot else f"{repo}@s{project.slot}"


def apply(project, n: int) -> None:
    """Derive the ports and placeholders of slot `n` from the configuration as loaded; calling it again re-derives, never shifts twice."""
    envs = {r["name"]: r["env"] for r in project.cfg["repo"] if r.get("env")}
    if project.pristine is None:
        project.pristine = copy.deepcopy(envs)
    project.slot, table = n, {}
    for name, env in envs.items():
        env.clear()
        env.update(copy.deepcopy(project.pristine[name]))
        rc = next(r for r in project.cfg["repo"] if r["name"] == name)
        port = localhttp.split(env["base_url"])[1]
        new = port + n * (rc.get("slots") or {}).get("port_offset", 0)
        for field in ("base_url", "health"):
            env[field] = re.sub(rf"(?<=:){port}(?=/|$)", str(new), env[field]) if not env[field].startswith("/") else env[field]
        table[name] = {"port": str(new), "base_url": env["base_url"]}
    for name, env in envs.items():
        tokens = {f"{{{k}}}": v for k, v in table[name].items()} | {f"{{{r}.{k}}}": v for r, t in table.items() for k, v in t.items()}
        for field in FIELDS:
            if env.get(field):
                env[field] = _fill(env[field], tokens)
        env["vars"] = {k: _fill(v, tokens) for k, v in env.get("vars", {}).items()}


def _fill(text: str, tokens: dict[str, str]) -> str:
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


def choose(project, run, n: int, actor: str | None) -> None:
    """Record the run's slot (before its plan locks: `up` may carry the port) and derive it. Slot 0 needs nothing; another slot needs a port offset."""
    st = run.state()
    bad = [r["name"] for r in project.cfg["repo"] if r.get("env") and r["name"] in st["repos"] and n
           and not ((r.get("slots") or {}).get("port_offset") and n < (r.get("slots") or {}).get("count", 2))]
    if n < 0 or bad:
        raise FactoryError(f"slot {n}: " + (f"{', '.join(bad)} need [repo.<name>.slots] port_offset (and a count above {n}) in .factory/local.toml" if bad else "slots count from 0"))
    if st["slot"] != n:
        run.append({"type": "slot", "slot": n}, actor)
    apply(project, n)
