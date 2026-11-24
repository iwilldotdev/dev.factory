"""Child process of `profiles.map_docs`: reads {doc, kind, docs, ctx} as JSON on stdin, writes {result} or {errors} on
stdout. The parent kills it on timeout, so a pathological regex cannot hang the CLI."""

from __future__ import annotations

import json
import sys


def main() -> int:
    from . import mapping, profiles
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        res = profiles.map_docs_inline(req["doc"], req["kind"], req["docs"], req["ctx"])
        out = {"result": res}
    except mapping.MappingFailure as err:
        out = {"errors": err.errors}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
