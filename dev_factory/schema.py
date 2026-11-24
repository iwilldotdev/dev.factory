"""Declarative `factory.toml` / `local.toml` schema: types, defaults, unknown-key rejection [L-156]."""
from __future__ import annotations

import copy
import re
from collections import namedtuple

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SECRET_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
KINDS = ("ticket", "change", "pipeline", "design_frame", "design_image")

# A leaf is a type (str, or a tuple starting with e/r/nr) or T(type, default). Types: s str, c non-empty str,
# i int, n number, b bool, ls list[str], lc list of non-empty str, ms str->str table, re regex, re+ list of
# regexes, key secret-key list, ("e", *choices), ("r", lo, hi) int range, ("nr", lo, hi) number range.
# A dict is a table; [spec] is an array of tables; the key "*" matches any name.
T = namedtuple("T", "type default")
CHECK = {"id": "c", "run": "c", "cwd": "s", "expect_exit": T("i", 0), "expect_pattern": "re", "timeout_s": "i"}
ENV = {"up": "c", "down": "c", "health": T("s", "/"), "base_url": "c", "ready_timeout_s": T("i", 120),
       "depends_on": T("ls", []), "vars": T("ms", {}), "login_spec": "c", "login_secrets": T("key", []),
       "login_http": "c", "seed": "c", "reset": "c", "preflight": T("lc", []), "warmup": T("ls", []),
       "env_patterns": T("re+", []), "shareable": T("b", False)}
REPO = {"path": "c", "name": "c", "base": T("c", "main"), "branch": T("c", "{kind}/{key}-{slug}"), "setup": "c",
        "files_from_main": T("ls", []), "commit_pattern": "re", "commit_forbidden_trailers": T("ls", []), "env": ENV, "check": [CHECK],
        "mirror": [{"name": "c", "branch": "c"}]}
APPROX = {"px": T("n", 4), "color_delta_e": T("n", 6), "font_size_px": T("n", 2), "max_diff_ratio": T(("nr", 0, 1), 0.10)}
SPEC = {
    "schema": ("e", 1),
    "project": {"name": "c", "cli": "c"},
    "repo": [REPO],
    "checks": {"cmd_timeout_s": T("i", 600), "http_timeout_s": T("i", 30), "browser_timeout_s": T("i", 300),
               "expect_timeout_ms": T("i", 10000), "tail_lines": T("i", 40), "browser": {"runner": "c"}},
    "limits": {"max_diff_ratio": T(("nr", 0, 1), 0.03), "tolerance_px": T("n", 2), "approx": APPROX},
    "providers": {k: {"fetch": "c", "timeout_s": T("i", 60), "secrets": T("key", [])} for k in KINDS},
    "ship": {"title": T("s", "[{key}] - {summary}"), "draft": T("b", True),
             "ticket": T(("e", "none", "comment", "transition"), "none"), "transition_to": "c",
             "reviewers": [{"scm": "c", "tracker": "c"}], "require_pipeline": T("b", False)},
    "autonomy": {"max_loops": T(("r", 0, 10), 3), "ship": T(("e", "ask", "draft-auto"), "ask")},
    "orchestration": {"max_parallel": T("i", 1)},
    "metrics": {"tokens": "c"},
    "hotfix": {"targets": [{"name": "c", "branch": "c"}]},
    "secrets": {"keys": T("key", [])},
}
LOCAL_SPEC = {
    "worktree_root": "c",
    "repo": {"*": {"path": "c", "env": {"base_url": "c", "health": "s", "vars": "ms"},
                   "slots": {"port_offset": "i", "count": "i"}}},
    "checks": {"browser": {"runner": "c", "bundled": "c", "headless": "b"}},
}


def _leaf(value, t, path: str, errs: list[str]) -> None:
    """Check one value against a leaf type; append messages to errs."""
    if isinstance(t, tuple):
        if t[0] == "e":
            if value not in t[1:] or isinstance(value, bool):
                errs.append(f"{path}: {value!r} is not one of {', '.join(map(str, t[1:]))}")
        else:
            kind = "an integer" if t[0] == "r" else "a number"
            ok = isinstance(value, int if t[0] == "r" else (int, float)) and not isinstance(value, bool)
            if not ok or not t[1] <= value <= t[2]:
                errs.append(f"{path}: must be {kind} from {t[1]} to {t[2]}")
        return
    num = isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "s" and not isinstance(value, str):
        errs.append(f"{path}: must be a string")
    elif t == "c" and not (isinstance(value, str) and value.strip()):
        errs.append(f"{path}: must be a non-empty string")
    elif t == "i" and not (isinstance(value, int) and not isinstance(value, bool)):
        errs.append(f"{path}: must be an integer")
    elif t == "n" and not num:
        errs.append(f"{path}: must be a number")
    elif t == "b" and not isinstance(value, bool):
        errs.append(f"{path}: must be true or false")
    elif t in ("ls", "lc", "key", "re+"):
        if not isinstance(value, list) or not all(isinstance(x, str) and (x or t == "ls") for x in value):
            errs.append(f"{path}: must be a list of {'' if t == 'ls' else 'non-empty '}strings")
        else:
            for x in value:
                if t == "key" and not SECRET_KEY.match(x):
                    errs.append(f"{path}: {x!r} is not a secret key name (^[A-Z][A-Z0-9_]*$)")
                elif t == "re+":
                    _leaf(x, "re", path, errs)
    elif t == "ms" and not (isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())):
        errs.append(f"{path}: must be a table of strings")
    elif t == "re":
        try:
            re.compile(value)
        except (re.error, TypeError):
            errs.append(f"{path}: is not a valid regular expression")


def validate(table, spec, path: str, errs: list[str]) -> None:
    """Reject unknown keys (with the key path) and wrong types in `table` against `spec`."""
    if not isinstance(table, dict):
        errs.append(f"{path or 'document'}: must be a table")
        return
    for key, value in table.items():
        here = f"{path}.{key}" if path else key
        sub = spec.get(key, spec.get("*"))
        if sub is None:
            errs.append(f"{here}: unknown key")
        elif isinstance(sub, dict):
            validate(value, sub, here, errs)
        elif isinstance(sub, list):
            if not isinstance(value, list):
                errs.append(f"{here}: must be an array of tables")
            else:
                for i, item in enumerate(value):
                    validate(item, sub[0], f"{here}[{i}]", errs)
        else:
            _leaf(value, sub.type if isinstance(sub, T) else sub, here, errs)


def _has_defaults(spec: dict) -> bool:
    return any(isinstance(s, T) or (isinstance(s, dict) and _has_defaults(s)) for s in spec.values())


def defaults(table: dict, spec: dict) -> None:
    """Fill defaults in place; a sub-table is created only when it holds defaults (`env` only when declared)."""
    for key, sub in spec.items():
        if isinstance(sub, T):
            table.setdefault(key, copy.deepcopy(sub.default))
        elif isinstance(sub, dict) and (key in table or (key != "env" and _has_defaults(sub))):
            defaults(table.setdefault(key, {}), sub)
        elif isinstance(sub, list):
            for item in table.get(key, []):
                defaults(item, sub[0])
