"""Branch and change-request title templates: built-in placeholders plus the ticket's own `fields`, which an adapter or the agent
fills from the tracker. A template that uses a field the ticket lacks fails the intake gate, so the question goes to the intake round."""
from __future__ import annotations

import re
import string

from .util import FactoryError

BUILTIN = {"title": ("key", "summary"), "branch": ("kind", "key", "slug")}
RESERVED = frozenset(n for names in BUILTIN.values() for n in names)
FIELD = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DEFAULTS = {"title": "[{key}] - {summary}", "branch": "{kind}/{key}-{slug}"}


def placeholders(template: str) -> list[str]:
    """The names a template uses, in order; ValueError on a broken template or on anything but a plain {name}."""
    names = []
    for _, name, spec, conv in string.Formatter().parse(template):
        if name is None:
            continue
        if spec or conv or not FIELD.match(name):
            raise ValueError(f"{{{name}{'!' + conv if conv else ''}{':' + spec if spec else ''}}} is not a plain {{name}} placeholder")
        names.append(name)
    return names


def problems(template: str, what: str, path: str) -> list[str]:
    try:
        placeholders(template)
    except ValueError as exc:
        return [f"{path}: {exc} (built-in: {', '.join('{' + n + '}' for n in BUILTIN[what])}; any other {{name}} is a ticket field)"]
    return []


def fields_of(template: str, what: str) -> list[str]:
    """The ticket fields a valid template needs: every placeholder that is not built in."""
    return [n for n in placeholders(template) if n not in BUILTIN[what]]


def required(cfg: dict, repos: list[str] | None = None) -> dict[str, list[str]]:
    """field -> the settings that use it (ship.title, and the branch template of each repo in `repos`, default all)."""
    out: dict[str, list[str]] = {}
    uses = [("ship.title", cfg["ship"]["title"], "title")]
    uses += [(f"repo {r['name']} branch", r["branch"], "branch") for r in cfg["repo"] if repos is None or r["name"] in repos]
    for where, template, what in uses:
        try:
            for name in fields_of(template, what):
                out.setdefault(name, []).append(where)
        except ValueError:
            continue
    return out


def missing(cfg: dict, ticket: dict, repos: list[str]) -> list[str]:
    """Intake reasons: one per field a template uses and the ticket contract does not carry."""
    have = ticket.get("fields") or {}
    return [f"the ticket contract lacks fields.{name}, which {' and '.join(where)} use: the adapter or the agent must fill it from the "
            f"tracker; when the tracker has none or several, ask the user in the intake round and ingest the ticket again with it"
            for name, where in required(cfg, repos).items() if not have.get(name)]


def render(template: str, what: str, builtin: dict, fields: dict | None) -> str:
    values = {**(fields or {}), **builtin}
    lack = [n for n in placeholders(template) if n not in values]
    if lack:
        raise FactoryError(f"the {what} template {template!r} uses {', '.join(lack)}, which the ticket contract does not carry; "
                           "fill fields." + lack[0] + " (adapter, or factory ingest ticket) and run again")
    return template.format(**values)


def title(cfg: dict, ticket: dict) -> str:
    return render(cfg["ship"]["title"], "title", {"key": ticket["id"], "summary": ticket["title"].strip()}, ticket.get("fields"))


# setup interview ---------------------------------------------------------------------------------------------------
def questions(doc: dict, asked: set) -> list[dict]:
    """The optional naming step: one branch template for every repo and the change-request title."""
    from .setupflow import question
    branches = {r.get("branch") for r in doc.get("repo", [])} - {None}
    hint = " Other {name} placeholders are ticket fields, which the ticket adapter (or the agent) fills, e.g. {version}."
    qs = [question("naming.branch", "Branch name template of every repo (built-in: {kind}, {key}, {slug})." + hint,
                   default=branches.pop() if len(branches) == 1 else DEFAULTS["branch"]),
          question("naming.title", "Change-request title template (built-in: {key}, {summary})." + hint,
                   default=doc.get("ship", {}).get("title") or DEFAULTS["title"])]
    return [q for q in qs if q["id"] not in asked]


def apply(doc: dict, qid: str, value: str) -> None:
    what = qid.split(".", 1)[1]
    errs = problems(value, what, qid)
    if errs:
        raise FactoryError(errs[0])
    tables = [doc.setdefault("ship", {})] if what == "title" else doc.get("repo", [])
    for t in tables:  # the default template is not written: the config keeps only what differs
        if value == DEFAULTS[what]:
            t.pop(what, None)
        else:
            t[what] = value
