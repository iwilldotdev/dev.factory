"""Calling the bundled browser runner (`browser/`): argv, result file, exit-code mapping [L-42, L-56, L-57]."""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import proc
from ..util import FactoryError

DEFAULT = Path(__file__).resolve().parent.parent.parent / "browser" / "src" / "cli.js"
_INSTALL_FILE = DEFAULT.parent.parent / "install-command.txt"


def install_hint() -> str:
    try:
        return "install it from the dev.factory checkout with: " + _INSTALL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "install it from the browser/ folder of the dev.factory checkout"


RESULT = "result.json"


@dataclass
class Outcome:
    code: int | None
    result: dict | None  # the runner's result file: {status, summary, details, artifacts}
    text: str
    secs: float
    timed_out: bool


class RunnerMissing(FactoryError):
    pass


def prefix(cfg: dict) -> list[str]:
    path = Path(cfg["checks"].get("browser", {}).get("bundled") or DEFAULT)
    if not path.is_file():
        raise RunnerMissing(f"the bundled browser runner is missing ({path.as_posix()}); {install_hint()}, or set [checks.browser] bundled in .factory/local.toml")
    if path.suffix == ".py":
        return [sys.executable, str(path)]
    if path.suffix in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if not node:
            raise RunnerMissing(f"node is not on PATH, and the bundled browser runner needs it; {install_hint()}")
        return [node, str(path)]
    return [str(path)]


def invoke(cfg: dict, args: list[str], out: Path, timeout_s: float, env: dict | None = None, cwd: Path | None = None) -> Outcome:
    """Run `factory-browser <args> --out <out>`; the runner's own watchdog fires first, this is the hard cap on the tree [L-42]."""
    out.mkdir(parents=True, exist_ok=True)
    (out / RESULT).unlink(missing_ok=True)
    argv = prefix(cfg) + args + ["--out", str(out), "--timeout-ms", str(int(timeout_s * 1000))]
    if cfg["checks"].get("browser", {}).get("headless") is False:
        argv += ["--headed"]
    res = proc.run(argv, cwd=cwd or out, env=env or proc.child_env(), timeout_s=timeout_s + 15)
    try:
        result = json.loads((out / RESULT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = None
    return Outcome(res.exit, result, (res.out + ("\n" if res.out and res.err else "") + res.err).replace("\r\n", "\n"), res.secs, res.timed_out)


def classify(o: Outcome, limit: int) -> tuple[str, str | None, str]:
    """(result, kind, reason): exit 0 pass, 1 fail, 3 env (navigation or connection), 4 setup (browser missing) [DESIGN section 10]."""
    lines = o.text.strip().splitlines()
    why = (o.result or {}).get("summary") or (lines[-1] if lines else "")
    if o.timed_out:
        return "timeout", "timeout", f"killed after {limit}s (the whole process tree)"
    if o.code == 0 and o.result and o.result.get("status") == "pass":
        return "pass", None, why
    result, kind = {1: ("fail", "assert"), 3: ("env", "env"), 4: ("setup", "setup")}.get(o.code, ("error", "internal"))
    return result, kind, why or f"the runner exited {o.code} without a result"
