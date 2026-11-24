"""Command line: argument parsing and thin handlers; the work lives in the other modules."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import __version__
from .util import FactoryError, build_identity, fwd


def _out(args, payload, text: str) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else text)


def _project(args):
    from . import project
    return project.discover(args.project or args.project_pre)


def _run(args, project, writable: bool = True):
    from . import lifecycle
    name = getattr(args, "run_id", None) or getattr(args, "run", None)
    return lifecycle.writable(project, name) if writable else lifecycle.resolve(project, name, allow_closed=True)


def cmd_init(args) -> int:
    from . import harness, importer, project
    if args.harness not in harness.IDS:
        raise FactoryError(f"unknown harness {args.harness!r}; available: {', '.join(harness.IDS)}")
    root = Path(args.project or args.project_pre or os.getcwd()).resolve()
    doc = local = None
    notes: list[str] = []
    name = args.name
    if args.import_file:
        src = json.loads(Path(args.import_file).read_text(encoding="utf-8"))
        if not isinstance(src, dict):
            raise FactoryError(f"{fwd(args.import_file)}: expected a JSON object")
        name = project._slug(args.name or src.get("name") or root.name, lower=True)
        doc, local, notes = importer.convert(src, root, name)
        for r in doc["repo"]:
            r.setdefault("base", args.base)
    done = project.init(root, name, args.repo or ["."], args.base, doc, local)
    done += harness.write(root, args.harness)
    if args.import_file:
        from . import config
        try:
            config.load(root)
        except FactoryError as exc:
            notes.append(f"the imported config needs edits before it loads: {exc}")
    _out(args, {"done": done, "not_imported": notes}, "\n".join(done + (["not imported / to review:"] + [f"  - {n}" for n in notes] if notes else [])))
    return 0


def cmd_check(args) -> int:
    from . import project
    proj = _project(args)
    errs, warns = project.check(proj)
    _out(args, {"errors": errs, "warnings": warns},
         "\n".join([f"error: {e}" for e in errs] + [f"warning: {w}" for w in warns] + ([] if errs else [f"ok: {proj.cfg['project']['name']} ({proj.cfg['_meta']['mode']}, {len(proj.cfg['repo'])} repo(s))"])))
    return 2 if errs else 0


def _rows(rows: list[dict]) -> str:
    return "\n".join(f"{r['run']:<24} {r['state']:<7} {str(r['kind']):<8} {r['phase']:<8} {r['updated']}" for r in rows) or "no runs"


def cmd_status(args) -> int:
    from . import lifecycle, project
    proj = _project(args)
    if args.config:
        s = project.summary(proj)
        text = [f"project {s['project']} ({s['mode']}) at {s['dir']}", f"cli {s['cli']}" + (f" (pin {s['cli_pin']})" if s["cli_pin"] else ""),
                f"worktree_root {s['worktree_root']}", f"local.toml {s['local_toml'] or 'none'}"]
        text += [f"repo {r['name']}: {r['path']} base={r['base']} env={'yes' if r['env'] else 'no'} checks={','.join(r['checks']) or '-'}" for r in s["repos"]]
        text += [f"providers: {', '.join(s['providers']) or 'none'}"] + [f"secret {k}: {v}" for k, v in s["secrets"].items()]
        _out(args, s, "\n".join(text))
        return 0
    ids = [i for i in proj.run_ids() if not proj.run(i).state()["closed"]]
    if not args.run_id and len(ids) != 1:
        rows = lifecycle.listing(proj)
        _out(args, rows, _rows(rows) + ("\npass a run id to see one" if rows else "\nstart one with: factory run start <ticket-id>"))
        return 0
    s = lifecycle.status(proj, _run(args, proj, writable=False))
    text = [f"run {s['run']}: ticket {s['ticket']} kind {s['kind']} phase {s['phase']}" + (f" (closed: {s['closed']})" if s["closed"] else ""),
            f"repos: {', '.join(s['repos']) or 'none declared'}", f"events: {s['events']}  updated {s['updated']}",
            f"gates: {', '.join(f'{k}={v}' for k, v in s['gates'].items()) or 'none run'}",
            f"plan lock: {s['plan_lock'] or 'not locked'}  loops used: {s['loops']}"]
    for key in ("open_questions", "open_blockers", "unconsumed_decisions"):
        if s[key]:
            text.append(f"{key.replace('_', ' ')}: {', '.join(s[key])}")
    _out(args, s, "\n".join(text + s["problems"]))
    return 0


def cmd_runs(args) -> int:
    from . import lifecycle
    rows = lifecycle.listing(_project(args))
    _out(args, rows, _rows(rows))
    return 0


def cmd_log(args) -> int:
    from . import lifecycle
    run = _run(args, _project(args), writable=False)
    lines = lifecycle.log_lines(run, args.tail)
    _out(args, lines, "\n".join(lines))
    return 0


def cmd_next(args) -> int:
    from . import next as nxt
    proj = _project(args)
    n = nxt.compute(proj, _run(args, proj, writable=False))
    text = [f"run {n['run']} (kind {n['kind'] or 'not set'}): phase {n['phase']}", f"action: {n['action']}"]
    if n["command"]:
        text.append(f"command: {n['command']}")
    text += [f"expects file: {f}" for f in n["files"]] + [f"role: {n['role']}"]
    if n["reasons"]:
        text += [f"pending gate {n['gate']} would fail:"] + [f"  - {r}" for r in n["reasons"]]
    if n.get("decisions_note"):
        text.append(n["decisions_note"])
    text += [f"do not run: {c}" for c in n["refused"]]
    _out(args, n, "\n".join(text))
    return 0


def cmd_run(args) -> int:
    from . import lifecycle
    proj = _project(args)
    if args.sub == "start":
        run = lifecycle.start(proj, args.ticket, args.kind, args.repos.split(",") if args.repos else None, args.actor)
        _out(args, {"run": run.id, "dir": fwd(run.dir)}, f"started run {run.id} at {fwd(run.dir)}")
    elif args.sub == "retype":
        lifecycle.retype(_run(args, proj), args.kind, args.actor)
        print(f"run kind is now {args.kind}; factory gate intake has to pass again")
    elif args.sub == "repos":
        lifecycle.set_repos(proj, _run(args, proj), args.names.split(","), args.actor)
        print(f"affected repos: {args.names}")
    elif args.sub == "branch":
        evs = lifecycle.branch(proj, _run(args, proj), args.repos.split(",") if args.repos else None, args.actor)
        text = [f"worktree {e['repo']}: {e['path']} (branch {e['branch']}, branch point {e['branch_point'][:7]}, setup {e['setup']})" for e in evs]
        _out(args, evs, "\n".join(text) or "every repo already has its worktree")
        bad = [e for e in evs if e["setup"] == "fail"]
        for e in bad:
            print(f"setup failed in {e['repo']} (exit {e.get('setup_exit')}); it never counts as a result, fix it and run "
                  f"this again:\n{e.get('setup_tail', '')}", file=sys.stderr)
        return 1 if bad else 0
    elif args.sub == "close":
        run = _run(args, proj)
        lifecycle.close(run, args.reason, args.actor)
        print(f"closed run {run.id}")
    else:
        run = _run(args, proj, writable=False)
        trees = lifecycle.delete(run, args.yes)
        print(f"deleted run {run.id}")
        for t in trees:
            print(f"remove this worktree yourself if you no longer need it: {fwd(t)}")
    return 0


def cmd_plan(args) -> int:
    from . import gates, lock, plan as planmod
    if args.sub == "example":
        print(json.dumps(planmod.EXAMPLE, indent=2))
        return 0
    proj = _project(args)
    run = _run(args, proj)
    ev = lock.amend(run, proj.cfg, gates.plan_reasons(run, proj, run.state(), with_lock=False), args.reason, args.decision, args.actor)
    a = ev["amend"]
    _out(args, ev, f"plan re-locked (seq {ev['seq']}); changed ACs: {', '.join(a['changed_acs']) or 'none'}; changed checks: "
                   f"{', '.join(a['changed_checks']) or 'none'}; " + ("consumed " + a["decision"] if a["decision"] else "counts against the loop budget"))
    return 0


def _batch(args, fn) -> int:
    from . import verify
    proj = _project(args)
    s = fn(proj, _run(args, proj), verify.Opts(args.check, args.repo, args.fresh, not getattr(args, "no_retry", False), args.actor))
    _out(args, s, verify.render(s))
    return s["exit"]


def cmd_verify(args) -> int:
    from . import verify
    return _batch(args, verify.verify)


def cmd_reproduce(args) -> int:
    from . import verify
    return _batch(args, verify.reproduce)


def cmd_metrics(args) -> int:
    from . import gates, metrics
    run = _run(args, _project(args), writable=False)
    m = metrics.compute(run.read(), gates.is_bug(run, run.state()))
    _out(args, {"run": run.id, **m}, metrics.render(run.id, m))
    return 0


def cmd_human(args) -> int:
    from . import human
    proj = _project(args)
    run = _run(args, proj)
    cmd, a = args.cmd, args.actor
    if cmd == "ask":
        ev = human.ask(run, args.text, args.ref, a)
        print(f"opened {ev['id']}; the user answers it, then: factory answer {ev['id']} --user-said \"<their words>\"")
    elif cmd == "answer":
        human.answer(run, args.question, args.user_said, a)
        print(f"{args.question} answered; its decision can be consumed once with --decision {args.question}")
    elif cmd == "decide":
        print(f"recorded {human.decide(run, args.user_said, a)['id']}")
    elif cmd == "human":
        human.human(run, args.kind, args.text, a)
        print(f"recorded human {args.kind}")
    elif cmd == "block":
        print(f"blocked: {human.block(run, args.kind, args.reason, a)['id']}")
    else:
        human.unblock(run, args.blocker, args.decision, a)
        print(f"{args.blocker} resolved")
    return 0


def cmd_fetch(args) -> int:
    from . import acquire
    proj = _project(args)
    env = acquire.fetch(_run(args, proj), proj.cfg, proj.dir, args.kind, args.id, args.repo)
    _out(args, {"contract": env["data"]["id"], "source": env["source"], "raw": env["raw"]},
         f"stored {args.kind} {env['data']['id']} (source adapter, raw sha256 {env['raw']['sha256'][:12]})")
    return 0


def cmd_ingest(args) -> int:
    from . import acquire
    proj = _project(args)
    if args.check:
        bad = acquire.stale(_run(args, proj, writable=False))
        _out(args, bad, "\n".join(bad) or "all raws match their recorded hashes")
        return 1 if bad else 0
    if not args.kind or not args.raw or bool(args.contract) == bool(args.stdin):
        raise FactoryError("usage: factory ingest <kind> --raw <file> (--contract <file> | --stdin), or factory ingest --check")
    text = sys.stdin.read() if args.stdin else Path(args.contract).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise FactoryError(f"contract data is not valid JSON: {exc}") from exc
    env = acquire.ingest(_run(args, proj), proj.cfg, args.kind, Path(args.raw), data)
    _out(args, {"contract": env["data"]["id"], "source": env["source"], "raw": env["raw"]},
         f"stored {args.kind} {env['data']['id']} (source agent, raw sha256 {env['raw']['sha256'][:12]})")
    return 0


def cmd_media(args) -> int:
    from . import media
    run = _run(args, _project(args))
    if args.sub == "seen":
        media.seen(run, args.attachment, args.file, args.actor)
    else:
        media.unseen(run, args.attachment, args.question, args.actor)
    print(f"recorded {args.attachment} as {args.sub}")
    return 0


def cmd_gate(args) -> int:
    from . import gates
    proj = _project(args)
    ok, reasons = gates.run_gate(_run(args, proj), proj, args.name, args.actor)
    _out(args, {"gate": args.name, "result": "pass" if ok else "fail", "reasons": reasons},
         f"gate {args.name}: pass" if ok else f"gate {args.name}: fail\n" + "\n".join(f"  - {r}" for r in reasons))
    return 0 if ok else 1


def build_parser():
    from .parser import build_parser as build
    return build()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.version:
        print(f"factory {__version__} ({build_identity()}) python {sys.version.split()[0]}")
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    for attr, default in (("json", False), ("actor", None), ("project", None), ("run", None)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        return args.func(args) or 0
    except FactoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
