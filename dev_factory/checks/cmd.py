"""`cmd` checks: one command line through the platform shell in the worktree; the exit code decides first, then the
pattern; setup failures and zero tests never count as a result [L-02, L-03, L-05, L-42, L-45]."""
from __future__ import annotations

import re

from .. import proc
from ..util import FactoryError, redact

SETUP_CODES = (126, 127, 9009)  # not executable, not found (sh), not found (cmd.exe)
ZERO_TESTS = re.compile(
    r"\bno tests? (?:were )?(?:found|ran|run|to run|executed|collected)\b|\bcollected 0 items\b|\bran 0 tests?\b"
    r"|\b0 tests? (?:ran|run|executed|found|collected)\b|\bno (?:test )?(?:files|specs) found\b|\btests?: +0 total\b", re.I)
SETUP_TEXT = re.compile(
    r"command not found|is not recognized as an internal or external command|cannot find module|no such file or directory"
    r"|not found in path|n.o . reconhecido como um comando|comando n.o encontrado|arquivo ou diret.rio inexistente"
    r"|no se reconoce como un comando|n'est pas reconnu en tant que commande|wird nicht als interner oder externer befehl", re.I)


def classify(check: dict, code: int | None, timed_out: bool, text: str, mode: str) -> tuple[str, str | None, str]:
    """(result, kind, reason) of one run. reproduce and red modes need a failure that matches the expected pattern."""
    want = check["expect_exit"]
    if timed_out:
        return "timeout", "timeout", f"killed after {check['timeout_s']}s (the whole process tree)"
    if code in SETUP_CODES and want not in SETUP_CODES:
        return "setup", "setup", f"exit {code}: the command was not found or cannot run"
    if ZERO_TESTS.search(text):
        return "setup", "setup", "the run executed zero tests"
    reproducing = mode in ("reproduce", "red")
    pattern = (check.get("reproduce") or {}).get("expect_fail_pattern") if reproducing else check.get("expect_pattern")
    matched = bool(pattern and re.search(pattern, text, re.M))
    failed = code != want
    if failed and SETUP_TEXT.search(text) and not (reproducing and matched):
        return "setup", "setup", "the output reports a missing command, module or file"
    if reproducing:
        if not failed:
            return "not_reproduced", "assert", f"the check passed (exit {code}), so the bug did not show"
        if not matched:
            return "not_reproduced", "assert", f"failed with exit {code} but the output does not match expect_fail_pattern {pattern!r}"
        return "reproduced", None, f"exit {code} and the output matches {pattern!r}"
    if failed:
        return "fail", "assert", f"exit {code}, expected {want}"
    if pattern and not matched:
        return "fail", "assert", f"exit {code} as expected but the output does not match expect_pattern {pattern!r}"
    return "pass", None, ""


def run_check(check: dict, ctx) -> dict:
    shown = redact(check["run"], ctx.secrets)
    try:
        res = proc.run(check["run"], cwd=ctx.path / check["cwd"], env=proc.child_env(), timeout_s=check["timeout_s"], shell=True)
    except FactoryError as exc:
        return {"cmd": shown, "exit": None, "duration_s": 0.0, "output": str(exc), "result": "setup", "kind": "setup", "reason": str(exc)}
    text = redact(res.out + ("\n" if res.out and res.err else "") + res.err, ctx.secrets).replace("\r\n", "\n")
    result, kind, reason = classify(check, res.exit, res.timed_out, text, ctx.mode)
    return {"cmd": shown, "exit": res.exit, "duration_s": round(res.secs, 2), "output": text, "result": result, "kind": kind,
            "reason": reason}
