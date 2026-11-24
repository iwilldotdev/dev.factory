"""Acquiring contracts: user adapter (`fetch`), agent route (`ingest`), raw-hash re-checks [L-121, L-122, L-123]."""
from __future__ import annotations

import json
from pathlib import Path

from . import config, contracts, proc
from .run import Run
from .util import FactoryError, fwd, read_json, redact, safe_id, sha256_bytes, sha256_file, write_atomic, now_iso

PLACEHOLDERS = ("id", "repo", "repo_path", "branch", "sha", "run_dir", "project_dir", "out")


def _rel(run: Run, path: Path) -> str:
    return path.relative_to(run.dir).as_posix()


def store(run: Run, cfg: dict, kind: str, data: dict, raw: bytes, ext: str, source: str, adapter: dict | None,
          raw_src: Path | None = None) -> dict:
    """Validate, write raw + contract files, append the `contract` event. Nothing is written when invalid [L-156]."""
    data = contracts.validate(kind, data, [r["name"] for r in cfg["repo"]])
    sid = safe_id(data["id"])
    cpath = run.dir / "contracts" / kind / f"{sid}.json"
    if cpath.exists():
        prev = read_json(cpath)["data"]["id"]
        if prev != data["id"]:
            raise FactoryError(f"contract ids {prev!r} and {data['id']!r} map to the same file name {sid!r} [L-157]")
    rpath = run.dir / "raw" / kind / f"{sid}{ext}"
    if raw_src is None or raw_src.resolve() != rpath.resolve():
        write_atomic(rpath, raw)
    envelope = {"schema": contracts.SCHEMAS[kind], "source": source, "acquired_at": now_iso(),
                "raw": {"path": _rel(run, rpath), "sha256": sha256_bytes(raw)}, "data": data}
    if adapter:
        envelope["adapter"] = adapter
    write_atomic(cpath, json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    run.append({"type": "contract", "kind": kind, "id": data["id"], "path": _rel(run, cpath), "source": source,
                "raw_sha256": envelope["raw"]["sha256"]})
    return envelope


def fetch(run: Run, cfg: dict, project_dir: Path, kind: str, ident: str, repo: str | None = None) -> dict:
    """Run the configured adapter for `kind`; stdout is the raw, validated into a `source: adapter` contract."""
    prov = cfg["providers"].get(kind, {})
    if not prov.get("fetch"):
        raise FactoryError(f"no adapter for {kind}: set [providers.{kind}] fetch in factory.toml, "
                           f"or save the raw and run: factory ingest {kind} --raw <file> --contract <file>")
    if kind not in contracts.SPECS:
        raise FactoryError(f"contract kind {kind!r} is not supported in this build")
    repo_cfg = next((r for r in cfg["repo"] if r["name"] == repo), None) if repo else None
    if repo and repo_cfg is None:
        raise FactoryError(f"unknown repo {repo!r}")
    values = {"id": ident, "run_dir": fwd(run.dir), "project_dir": fwd(project_dir)}
    if repo_cfg:
        values.update(repo=repo_cfg["name"], repo_path=fwd(repo_cfg["path"]))
    argv = proc.substitute(proc.split_template(prov["fetch"]), values)
    keys = prov.get("secrets", [])
    secret_values, missing = config.resolve_secrets(cfg, keys)
    env = proc.child_env({**secret_values, "FACTORY_KIND": kind, "FACTORY_ID": ident, "FACTORY_RUN_DIR": fwd(run.dir)})
    res = proc.run(argv, cwd=project_dir, env=env, timeout_s=prov.get("timeout_s", 60))
    shown = redact(" ".join(argv), secret_values.values())
    if res.timed_out:
        raise FactoryError(f"adapter timed out after {prov.get('timeout_s', 60)}s (process tree killed): {shown}", 1)
    if res.exit == 3:
        raise FactoryError(f"no such {kind} {ident}", 1)
    if res.exit == 4:
        raise FactoryError(f"adapter reports missing credentials; unset secret keys: {', '.join(missing) or 'none of the declared keys'}", 1)
    if res.exit != 0:
        tail = "\n".join(redact(res.err, secret_values.values()).splitlines()[-20:])
        raise FactoryError(f"adapter failed (exit {res.exit}): {shown}\n{tail}", 1)
    raw = res.raw
    try:
        payload = json.loads(res.out)
    except ValueError as exc:
        raise FactoryError(f"adapter output is not one JSON object: {exc}") from exc
    if not isinstance(payload, dict) or "contract" not in payload or set(payload) - {"contract", "raw"}:
        raise FactoryError('adapter output must be exactly {"contract": {...}, "raw": <optional>}')
    adapter = {"cmd": shown, "exit": 0, "duration_s": round(res.secs, 2)}
    return store(run, cfg, kind, payload["contract"], raw, ".json", "adapter", adapter)


def ingest(run: Run, cfg: dict, kind: str, raw_file: Path, data: dict) -> dict:
    """Agent route: the agent saved the raw and wrote the contract data; validate and record `source: agent`."""
    if not raw_file.is_file():
        raise FactoryError(f"raw file not found: {fwd(raw_file)}")
    return store(run, cfg, kind, data, raw_file.read_bytes(), raw_file.suffix or ".json", "agent", None, raw_src=raw_file)


def load(run: Run, kind: str, ident: str) -> tuple[dict | None, list[str]]:
    """The recorded contract with problems: missing file, raw missing, or raw changed since acquisition [L-122]."""
    cpath = run.dir / "contracts" / kind / f"{safe_id(ident)}.json"
    if not cpath.is_file():
        return None, [f"{kind} contract {ident} is missing"]
    env = read_json(cpath)
    raw = run.dir / env["raw"]["path"]
    if not raw.is_file():
        return env, [f"{kind} {ident}: raw file {env['raw']['path']} is missing"]
    if sha256_file(raw) != env["raw"]["sha256"]:
        return env, [f"{kind} {ident}: raw changed since it was acquired (stale); re-run fetch or ingest"]
    return env, []


def stale(run: Run) -> list[str]:
    """Every recorded contract whose raw no longer matches its hash."""
    out: list[str] = []
    for (kind, ident) in sorted(run.state()["contracts"]):
        out += load(run, kind, ident)[1]
    return out

