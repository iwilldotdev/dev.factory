"""Handlers of the composite runs: `mirror` (hotfix). Epics, review runs and claims are added next to it."""
from __future__ import annotations

from pathlib import Path

from .util import FactoryError, fwd


def cmd_mirror(args) -> int:
    from . import cli, mirror
    proj = cli._project(args)
    run = cli._run(args, proj)
    words = args.words
    if words[0] != "diff":
        if len(words) != 1 or not args.to:
            raise FactoryError("usage: factory mirror <repo> --to <target> [--ticket KEY], or factory mirror diff [<repo>] [--to <target>] [--decision D-n]")
        m = mirror.start(proj, run, words[0], args.to, args.ticket, args.actor)
        cli._out(args, m, f"mirror {m['repo']} -> {m['target']}: {m['result']} ({len(m['commits'])} commit(s)) in {fwd(m['path'])} on branch {m['branch']}"
                 + ("; the conflict is a tooling blocker: resolve it in that worktree" if m["result"] == "conflict" else ""))
        return 1 if m["result"] == "conflict" else 0
    st = run.state()
    todo = [m for m in mirror.applied(st) if (len(words) < 2 or m["repo"] == words[1]) and (not args.to or m["target"] == args.to)]
    if not todo:
        raise FactoryError("no applied mirror matches: run factory mirror <repo> --to <target> first")
    if args.decision:
        if len(todo) != 1:
            raise FactoryError("--decision accepts the divergence of one mirror: name the repo and --to <target>")
        mirror.accept(run, todo[0]["repo"], todo[0]["target"], args.decision, args.actor)
    rows = [{"repo": m["repo"], "target": m["target"], **mirror.compare(run.state(), m)} for m in todo]
    cli._out(args, rows, "\n".join(f"{r['repo']} -> {r['target']}: " + ("equal" if r["equal"] else "DIFFERENT: " + ", ".join(f"{f} ({c})" for f, c in r["divergent"].items())) for r in rows))
    return 0 if all(r["equal"] for r in rows) else 1
