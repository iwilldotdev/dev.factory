"""Evidence records: only `verify` and `reproduce` write them, as append-only `evidence` events; gates only read [L-15, L-16].
A record names the check, its spec hash, the repo, the full SHA and whether the worktree was clean [L-07, L-08, L-09]."""
from __future__ import annotations

from pathlib import Path

from . import __version__
from .checks import ui
from .run import Run
from .util import canonical, fwd, now_iso, safe_id, sha256_bytes, write_atomic

GOOD = {"verify": "pass", "green": "pass", "reproduce": "reproduced", "red": "reproduced"}
TAIL_BYTES = 4096


def tail_of(text: str, lines: int) -> str:
    cut = "\n".join(text.splitlines()[-max(lines, 1):])
    return cut.encode("utf-8")[-TAIL_BYTES:].decode("utf-8", "ignore")


def append(run: Run, check: dict, spec_hash: str, mode: str, sha: str, tree: Path, clean: bool, out: dict, tail_lines: int,
           extra: dict | None = None, actor: str | None = None) -> dict:
    """Append one record (one attempt) and write its full output under artifacts/. The attempt number is
    derived under the events lock, so concurrent writers cannot reuse one."""
    def build(events: list[dict]) -> dict:
        attempt = 1 + sum(1 for e in events if e.get("type") == "evidence" and e.get("check") == check["id"])
        folder = f"artifacts/{safe_id(check['id'])}/{attempt}"
        rel = f"{folder}/output.txt"
        write_atomic(run.dir / rel, out.get("output", ""))
        moved = [f"{folder}/{n}" for n in ui.move_artifacts(out["tmp_dir"], run.dir / folder)] if out.get("tmp_dir") and Path(out["tmp_dir"]).is_dir() else []
        rec = {"v": 1, "ts": now_iso(), "cli": __version__, "type": "evidence", "check": check["id"], "spec_hash": spec_hash,
               "repo": check["repo"], "mode": mode, "attempt": attempt, "sha": sha, "worktree": fwd(tree), "clean": clean,
               "cmd": out["cmd"], "exit": out["exit"], "duration_s": out["duration_s"], "tail": tail_of(out.get("output", ""), tail_lines),
               "result": out["result"], "kind": out["kind"], "reason": out["reason"], "artifacts": [rel, *moved],
               **({"details": out["details"]} if out.get("details") else {}), **(extra or {})}
        rec["hash"] = sha256_bytes(canonical(rec).encode("utf-8"))
        return rec
    return run.append(build, actor)


def last(st: dict, check: str, mode: str, spec_hash: str, sha: str | None, fix: str | None = None, heads: dict | None = None) -> dict | None:
    """The latest clean record of a check for this spec, SHA (any for red and green, which bind to `fix`) and mode; a runtime
    check also needs the same `heads` of every repo it served [L-12]. Dirty records never count [L-07]."""
    for e in reversed(st["evidence"]):
        if (e["check"] == check and e["mode"] == mode and e["spec_hash"] == spec_hash and (sha is None or e["sha"] == sha)
                and e["clean"] and e.get("fix") == fix and (heads is None or e.get("heads") == heads)
                and (e.get("details") or {}).get("scope") != "scoped"):  # a scoped quality run never satisfies a gate [L-60]
            return e
    return None


def satisfied(st: dict, check: str, mode: str, spec_hash: str, sha: str | None, fix: str | None = None, heads: dict | None = None) -> bool:
    """True when the latest record is the good result of its mode; `flaky` and anything else never satisfy [L-14]."""
    rec = last(st, check, mode, spec_hash, sha, fix, heads)
    return bool(rec and rec["result"] == GOOD[mode])


def last_any(st: dict, check: str) -> dict | None:
    return next((e for e in reversed(st["evidence"]) if e["check"] == check and e["mode"] == "verify"), None)


def signature(rec: dict) -> tuple:
    """Two failures are identical when kind, reason and first output line agree [L-47]."""
    first = next((ln for ln in rec["tail"].splitlines() if ln.strip()), "")
    return (rec["result"], rec["kind"], rec["reason"], first)
