"""Handlers of `factory design add/check/expect/review/refresh`; the work lives in designflow.py."""
from __future__ import annotations

from pathlib import Path

from . import designflow
from .util import FactoryError, fwd


def cmd_design(args) -> int:
    from . import cli
    proj = cli._project(args)
    run = cli._run(args, proj, writable=args.sub != "check")
    sub, a = args.sub, args.actor
    if sub == "add":
        res = designflow.add(run, proj, Path(args.png), args.name, args.id, a)
        text = [f"stored the approximate design image ({res['width']}x{res['height']}, raw sha256 {res['image']['sha256'][:12]})",
                f"complete the frame skeleton at {res['skeleton']} (every number and color must be flagged in `estimated`), then run:", res["ingest"]]
        cli._out(args, res, "\n".join(text))
    elif sub == "check":
        problems = designflow.check(run, proj, run.state())
        cli._out(args, problems, "\n".join(problems) or "every ingested design frame and image is valid and unchanged")
        return 1 if problems else 0
    elif sub == "expect":
        path = designflow.expect(run, proj, args.frame, args.node, args.selector, a)
        print(f"added an entry for node {args.node} to {fwd(path)}; remove the properties you do not check, never add values the frame does not state")
    elif sub == "review":
        ev = designflow.review(run, proj, args.frame, args.user_said, a)
        print(f"recorded the user's design review of approx frame {ev['frame']} (frame hash {ev['frame_hash'][:12]})")
    else:
        ev = designflow.refresh(run, proj, args.frame, args.user_said, a)
        if ev["changed"]:
            raise FactoryError(f"frame {args.frame} moved at its source (version {ev['version']}): run factory plan amend and verify again", 1)
        print(f"frame {args.frame} is unchanged at version {ev['version']}")
    return 0
