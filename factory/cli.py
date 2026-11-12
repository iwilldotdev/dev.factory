"""factory CLI entry. Subcommands are discovered from factory/commands/*.py.

Each command module exposes `register(subparsers)` and sets
`parser.set_defaults(func=handler)`; a handler takes `args` and returns an int
exit code (0 ok, 1 gate/check failed, 2 usage/config error) or raises
core.GateFailed / core.FactoryError.
"""

from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys

from . import commands
from .core import VERSION, FactoryError, GateFailed, fetch_call


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="dev.factory CLI")
    parser.add_argument("--version", action="version", version=f"factory {VERSION}")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    for mod in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{commands.__name__}.{mod.name}").register(sub)
    return parser


def _print_fetch(items: list[dict], stream) -> None:
    """Ready-to-run dev.mcp calls for missing remote files (save_to is absolute, forward slashes)."""
    if not items:
        return
    print("fetch (run each dev.mcp call as-is; do not paste or rewrite the payload):", file=stream)
    for item in items:
        print(f"FETCH {fetch_call(item)}", file=stream)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except GateFailed as err:
        if getattr(args, "json", False) and err.fetch:
            print(json.dumps({"passed": False, "reasons": err.reasons, "fetch": err.fetch}, ensure_ascii=False))
            return 1
        print("FAIL")
        for reason in err.reasons:
            print(f"- {reason}")
        _print_fetch(err.fetch, sys.stdout)
        return 1
    except FactoryError as err:
        if getattr(args, "json", False) and err.fetch:
            print(json.dumps({"error": str(err), "fetch": err.fetch}, ensure_ascii=False))
        print(f"ERROR: {err}", file=sys.stderr)
        if not getattr(args, "json", False):
            _print_fetch(err.fetch, sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
