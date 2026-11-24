"""Shared verdict of the runtime checks: env patterns, reproduce patterns, and results that never quote a response body [L-49, L-83]."""
from __future__ import annotations

import re

from .. import envdata


def verdict(check: dict, ctx, passed: bool, shown: str, hidden: str = "") -> tuple[str, str | None, str]:
    """(result, kind, reason). `shown` may appear in evidence; `hidden` (a response body) is only matched against `env_patterns`."""
    patterns = (ctx.repo.get("env") or {}).get("env_patterns", [])
    if not passed and (hit := envdata.matches_env(patterns, shown + "\n" + hidden)):
        return "env", "env", f"the failure output matches env_patterns {hit!r}: the environment, not the product [L-49]"
    if ctx.mode in ("reproduce", "red"):
        want = (check.get("reproduce") or {}).get("expect_fail_pattern")
        if passed:
            return "not_reproduced", "assert", "the check passed, so the bug did not show"
        if not (want and re.search(want, shown, re.M)):
            return "not_reproduced", "assert", f"it failed ({shown}) but not with expect_fail_pattern {want!r}"
        return "reproduced", None, f"{shown}; matches {want!r}"
    return ("pass", None, "") if passed else ("fail", "assert", shown)
