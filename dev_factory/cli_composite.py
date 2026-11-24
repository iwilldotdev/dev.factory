"""Handlers of the composite runs: `mirror` (hotfix), `epic`, the review-run commands and `claim`."""
from __future__ import annotations

import json
import os
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


def cmd_review_run(args) -> int:
    from . import cli, review_run
    proj = cli._project(args)
    if args.sub == "queue":
        items = review_run.queue(proj, args.file)
        cli._out(args, items, "\n".join(f"{i['id']:<8} {i['repo']:<12} {i['author']:<12} {i['title']}" + (f" ({i['ticket']})" if i.get("ticket") else "") for i in items) or "nothing to review")
        return 0
    if args.sub == "start":
        runs = review_run.start(proj, args.changes, args.repo, args.ticket, args.actor)
        cli._out(args, [r.id for r in runs], "\n".join(f"started review run {r.id} (next: factory next {r.id})" for r in runs))
        return 0
    run = cli._run(args, proj)
    if args.sub == "fix":
        review_run.fix(proj, run, args.actor)
        print(f"run {run.id} is now a ticket run on the change branch; the plan, ACs and evidence stay. Next: factory next")
        return 0
    ev = review_run.report(run, proj, json.loads(Path(args.file).read_text(encoding="utf-8")), args.actor)
    cli._out(args, ev, f"review report {ev['verdict']} recorded; intents in {fwd(run.dir / 'report' / 'plan.json')} (none runs by itself): {', '.join(ev['actions'])}")
    return 0


def cmd_epic(args) -> int:
    from . import cli, epic
    proj = cli._project(args)
    run = cli._run(args, proj)
    if args.sub == "plan":
        ev = epic.plan(proj, run, None, args.actor)
        cli._out(args, ev, "waves: " + " -> ".join("[" + ", ".join(w) + "]" for w in ev["waves"]))
    elif args.sub == "status":
        rows = epic.children(proj, run.state())
        cli._out(args, rows, epic.render(rows))
    elif args.ship == "plan":
        sp = epic.ship_plan(proj, run, args.actor)
        cli._out(args, sp, f"epic ship plan: {len(sp['intents'])} intent(s) for {len(sp['children'])} child run(s) in {fwd(run.dir / 'ship' / 'plan.json')}")
    else:
        res = epic.ship_verify(proj, run, args.actor)
        cli._out(args, res, f"epic ship verify: {res['result']}" + "".join(f"\n  - {m}" for m in res["mismatches"]))
        return 0 if res["result"] == "pass" else 1
    return 0


def cmd_claim(args) -> int:
    from . import cli, lifecycle
    session = args.session or os.environ.get("FACTORY_SESSION")
    if not session:
        raise FactoryError("name your session: --session <hint> (or FACTORY_SESSION)")
    run = cli._run(args, cli._project(args))
    ev = lifecycle.claim(run, session, args.takeover, args.actor)
    print(f"run {run.id} is claimed by session {session!r}" if ev else f"run {run.id} was already yours")
    return 0
