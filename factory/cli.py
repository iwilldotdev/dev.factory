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

from . import commands, dist_info
from .core import VERSION, FactoryError, GateFailed, fetch_call


class _Version(argparse.Action):
    """`--version`: version, sha, snapshot or source, path; stderr notice when the snapshot is behind its source."""

    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, **kw)

    def __call__(self, parser, namespace, values, option_string=None):
        print(dist_info.describe(VERSION))
        notice = dist_info.behind_source()
        if notice:
            print(notice, file=sys.stderr)
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="dev.factory CLI")
    parser.add_argument("--version", action=_Version, help="show version, sha and where the CLI runs from")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    for mod in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{commands.__name__}.{mod.name}").register(sub)
    return parser


def _print_fetch(items: list[dict], stream) -> None:
    """Fetch lines for missing remote files (absolute paths, forward slashes)."""
    if not items:
        return
    print("fetch (save each payload at its path as-is, then `factory ingest`; do not paste or rewrite it):", file=stream)
    for item in items:
        print(f"FETCH {fetch_call(item)}", file=stream)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if getattr(args.func, "__name__", "") == "cmd_status" and args.command == "run":
        notice = dist_info.behind_source()  # `run status` is the cheap, frequent entry point; stderr, never fatal
        if notice:
            print(notice, file=sys.stderr)
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
