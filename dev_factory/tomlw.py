"""Minimal TOML writer for the documents the CLI generates (`init`)."""
from __future__ import annotations

import json
import re

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k)


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} as TOML")


def _tables(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _emit(table: dict, prefix: str, out: list[str]) -> None:
    for k, v in table.items():
        if not isinstance(v, dict) and not _tables(v):
            out.append(f"{_key(k)} = {_val(v)}")
    for k, v in table.items():
        name = f"{prefix}.{_key(k)}" if prefix else _key(k)
        if isinstance(v, dict) and v:
            out += ["", f"[{name}]"]
            _emit(v, name, out)
        elif _tables(v):
            for item in v:
                out += ["", f"[[{name}]]"]
                _emit(item, name, out)


def dumps(doc: dict) -> str:
    out: list[str] = []
    _emit(doc, "", out)
    return "\n".join(out).lstrip("\n") + "\n"
