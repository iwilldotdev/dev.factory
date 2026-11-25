"""Handlers of `factory baseline`, `confirm`, `waive`, `feedback`, `metrics` and `uat`; the work lives in quality.py, human.py, feedback.py and metrics.py."""
from __future__ import annotations

from .util import FactoryError


def cmd_baseline(args) -> int:
    """Take (or re-take with --fresh) the baseline of every quality check at the branch point [L-61]."""
    from . import cli, quality, verify
    proj = cli._project(args)
    b = verify.Batch(proj, cli._run(args, proj), verify.Opts(args.check, args.repo, args.fresh, True, args.actor))
    todo = [c for c in b.chosen() if c["type"] == "quality"]
    if not todo:
        raise FactoryError("no quality check is selected: declare type = \"quality\" in a [[repo.check]] of factory.toml or in plan.json")
    rows, code = [], 0
    for c in todo:
        have, bad = quality.baseline_for(b, c, force=args.fresh)
        if bad:
            rows.append({"check": c["id"], "error": bad["reason"]})
            code = 1
        else:
            rows.append({"check": c["id"], "branch_point": have["branch_point"], "runs": have["runs"], "stable": len(have["stable"]),
                         "quarantined": len(have["quarantined"])})
    cli._out(args, rows, "\n".join(f"{r['check']}: " + (f"FAILED: {r['error']}" if "error" in r else
             f"baseline at {r['branch_point'][:7]}: {r['stable']} stable failure id(s), {r['quarantined']} quarantined as flaky ({r['runs']} run(s))") for r in rows))
    return code


def cmd_confirm(args) -> int:
    from . import cli, human, review
    proj = cli._project(args)
    run = cli._run(args, proj)
    if args.cmd == "waive":
        ev = human.waive(run, args.target, args.user_said, args.actor)
        print(f"waived pipeline {ev['pipeline']}; a newer pipeline is not covered")
    elif args.target.partition(":")[0] in ("change", "pipeline"):
        st = run.state()
        ev = human.confirm(run, args.target, args.user_said, review.heads_of(st, st["repos"]), args.actor)
        print(f"recorded the user's confirmation of {ev['contract']}")
    else:
        ev = human.confirm_check(run, proj, args.target, args.user_said, args.actor)
        print(f"recorded the user's confirmation of {ev['check']} at {ev['sha'][:7]}; a new commit asks again")
    return 0


def cmd_metrics(args) -> int:
    from . import cli, gates, metrics
    proj = cli._project(args)
    if args.all:
        rows = metrics.report(proj, args.since)
        cli._out(args, rows, metrics.render_all(rows))
        return 0
    run = cli._run(args, proj, writable=bool(args.tokens))
    if args.tokens:
        ev = metrics.record_tokens(run, proj.cfg, proj.dir, args.session, args.actor)
        print(f"recorded tokens for session {args.session}: input {ev['input']}, output {ev['output']}" if ev else
              f"the adapter reported no tokens for session {args.session}; nothing recorded, earlier numbers stay")
        return 0
    m = metrics.compute(run.read(), gates.is_bug(run, run.state()))
    cli._out(args, {"run": run.id, **m}, metrics.render(run.id, m))
    return 0


def cmd_feedback(args) -> int:
    from . import cli, feedback
    proj = cli._project(args)
    if args.dismiss:
        ev = feedback.dismiss(cli._run(args, proj), args.dismiss, args.decision, args.actor)
        print(f"dismissed {ev['source']} on the user's decision {ev['decision']}")
        return 0
    res = feedback.ingest(cli._run(args, proj), proj, args.decision, not args.no_fetch, args.actor)
    cli._out(args, res, feedback.render(res))
    return 0


def cmd_uat(args) -> int:
    """Run one exploration script of the UAT in the browser and print where its screenshots and output are."""
    from . import cli, uat
    proj = cli._project(args)
    run = cli._run(args, proj)
    rec = uat.run_script(proj, run, args.script, args.repo, args.actor)
    files = [f"{run.dir.as_posix()}/{a}" for a in rec.get("artifacts", [])]
    cli._out(args, rec, f"{rec['check']} attempt {rec['attempt']}: {rec['result']}" + (f" ({rec['reason']})" if rec.get("reason") else "")
             + "".join(f"\n  {f}" for f in files) + "\nlook at every screenshot; what you see becomes the explored entries and findings of the uat review")
    return 0 if rec["result"] == "pass" else 1
