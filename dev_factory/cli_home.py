"""The guided entry (`factory`, `factory start`), the setup interview, and `factory config set`."""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

from . import config, home, schema
from .util import FactoryError, fwd

NEED_INPUT = 4  # exit code: the CLI needs an answer from the user (the JSON on stdout says which)


def _prompt():
    """`input` in a terminal, else None (the caller prints JSON for an agent). Tests replace it."""
    return input if sys.stdin.isatty() and sys.stdout.isatty() else None


def _ws(args):
    return getattr(args, "workspace", None) or getattr(args, "workspace_pre", None)


def _explicit(args):
    return getattr(args, "project", None) or getattr(args, "project_pre", None)


def _leaf(parts: list[str]):
    spec = schema.SPEC
    for i, part in enumerate(parts):
        if isinstance(spec, list):
            spec = spec[0]
        if not isinstance(spec, dict) or (part not in spec and "*" not in spec):
            raise FactoryError(f"unknown key {'.'.join(parts[:i + 1])} (see docs/schema/factory-toml.md)")
        spec = spec.get(part, spec.get("*"))
    return spec.type if isinstance(spec, schema.T) else spec


def key_path(dotted: str) -> tuple[str | None, list[str], object]:
    """`repo.<name>.<key...>` addresses a repo by name; any other key is a path from the top. Returns (repo, keys, leaf type)."""
    parts = [p for p in dotted.split(".") if p]
    if parts[:1] == ["repo"]:
        if len(parts) < 3:
            raise FactoryError("a repo key is repo.<name>.<key>, e.g. repo.web.base")
        return parts[1], parts[2:], _leaf(["repo", *parts[2:]])
    if not parts:
        raise FactoryError("an empty key")
    return None, parts, _leaf(parts)


def set_key(root: Path, dotted: str, text: str) -> object:
    """Write one value into the workspace factory.toml; the schema refuses unknown keys and wrong types."""
    repo, keys, leaf = key_path(dotted)
    if leaf in ("c", "s", "re") or (leaf == "sl" and not text.lstrip().startswith("[")):
        value = text
    else:
        try:
            value = tomllib.loads(f"v = {text}")["v"]
        except tomllib.TOMLDecodeError:
            value = text
    doc = config.read_toml(Path(root) / "factory.toml")
    table = doc
    if repo is not None:
        table = next((e for e in doc.get("repo", []) if (e.get("name") or (Path(root) / e.get("path", "")).resolve().name) == repo), None)
        if table is None:
            raise FactoryError(f"no repo {repo!r} in {fwd(Path(root) / 'factory.toml')}")
    for k in keys[:-1]:
        table = table.setdefault(k, {})
    table[keys[-1]] = value
    home.save(root, doc)
    return value


def cmd_config(args) -> int:
    from .project import find_root
    root = find_root(None, _explicit(args), _ws(args))[0]
    value = set_key(root, args.key, args.value)
    print(json.dumps({"set": args.key, "value": value, "file": fwd(root / "factory.toml")}) if args.json
          else f"{args.key} = {json.dumps(value, ensure_ascii=False)} in {fwd(root / 'factory.toml')}")
    return 0


def answer_save(proj, run, qid: str) -> str:
    """The key an answered question saves to (`factory ask --save-as`), checked before the answer is recorded."""
    q = run.state()["questions"].get(qid)
    if q is None or not q.get("save_as"):
        raise FactoryError(f"{qid} was not opened with --save-as <key>: there is nothing to save")
    return q["save_as"]


def cmd_setup_flow(args) -> int:
    from . import setupflow
    ask, cwd = _prompt(), Path(os.getcwd())
    if args.answer:
        answers: dict = {}
        for item in args.answer:
            qid, sep, val = item.partition("=")
            if not sep:
                raise FactoryError(f"--answer takes ID=VALUE, got {item!r}")
            if qid in answers:  # a repeated id adds to a multi answer
                prev = answers[qid]
                answers[qid] = (prev if isinstance(prev, list) else [prev]) + [val]
            else:
                answers[qid] = val
        setupflow.Interview(cwd, _ws(args), _explicit(args)).apply(answers)
    elif ask is not None and not args.questions:
        s = setupflow.interactive(cwd, _ws(args), _explicit(args), ask, print)
        print(setupflow.render_summary(s))
        return 0
    iv = setupflow.Interview(cwd, _ws(args), _explicit(args))
    qs = iv.next_questions()
    if qs:
        payload = {"workspace": iv.root.name if iv.root else None, "questions": qs,
                   "answer": "factory setup --answer ID=VALUE [--answer ID=VALUE ...]; an empty value skips (asked at every run)"}
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else setupflow.render_questions(qs))
        return NEED_INPUT
    s = iv.summary()
    print(json.dumps(s, indent=2, ensure_ascii=False) if args.json else setupflow.render_summary(s))
    return 0


def cmd_start(args) -> int:
    """`factory [start [TICKET]]`: the workspace of this directory, then the ticket; no workspace starts the setup interview."""
    from . import lifecycle, project, setupflow
    ask, ticket = _prompt(), getattr(args, "ticket", None)
    try:
        proj = project.discover(_explicit(args), None, _ws(args), ask)
    except home.NoWorkspace:
        if ask is None:
            iv = setupflow.Interview(Path(os.getcwd()), None, None)
            print(json.dumps({"need": "setup", "questions": iv.next_questions(),
                              "answer": "factory setup --answer ID=VALUE [...], then factory start <ticket>"}, indent=2, ensure_ascii=False))
            return NEED_INPUT
        print(setupflow.render_summary(setupflow.interactive(Path(os.getcwd()), None, None, ask, print)))
        proj = project.discover(None, None, None, ask)
    name = proj.cfg["project"]["name"]
    if not ticket and ask is not None:
        ticket = ask(f"workspace {name}: ticket key or URL> ").strip()
    if not ticket:
        print(json.dumps({"need": "ticket", "workspace": name, "dir": fwd(proj.dir),
                          "asked_every_run": setupflow.asked_every_run(proj.cfg)}, indent=2, ensure_ascii=False))
        return NEED_INPUT
    key = home.ticket_key(ticket)
    from .util import safe_id
    run = proj.run(safe_id(key))
    if run.exists() and run.state()["ticket"] == key:
        payload, text = {"run": run.id, "dir": fwd(run.dir), "resumed": True}, f"run {run.id} already exists at {fwd(run.dir)}; next: factory next {run.id}"
    else:
        run = lifecycle.start(proj, key, getattr(args, "kind", None), None, args.actor)
        payload, text = {"run": run.id, "dir": fwd(run.dir), "workspace": name}, f"started run {run.id} at {fwd(run.dir)} (workspace {name})"
    print(json.dumps(payload, indent=2) if args.json else text)
    return 0
