"""The change-request title and body, and the Gate S briefing. The body is for the human reviewer of the change: what it does, in the
project's words; the AC x check x evidence table is the user's, in the briefing and the run [L-142, L-144, L-146]."""
from __future__ import annotations

import re

from . import evidence, review
from .util import FactoryError

LOCAL_LINK = re.compile(r"\]\((?:evidence/|artifacts/|[A-Za-z]:[\\/])|file://")  # run-local paths 404 in the SCM [L-144]
# the run's bookkeeping means nothing to a reviewer of the change: ids of checks, ACs and decisions, attempts, the run folder
JARGON = re.compile(r"\b(?:CHK|AC|Q|F|B|I)-\d+\b|\battempt \d+\b|\.factory[\\/]|\bfactory (?:verify|gate|next|review|run|ship)\b")
SUMMARY = "summary.md"


def title(cfg: dict, ticket: dict) -> str:
    """`[key] - summary` unless the project sets another template; an empty template already fell back in config [L-142]."""
    return cfg["ship"]["title"].format(key=ticket["id"], summary=ticket["title"].strip())


def _cell(text, width: int = 60) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= width else text[:width - 1] + "..."


def _short(cmd: str) -> str:
    """A command as people read it: quoted absolute paths shrink to their file names."""
    return re.sub(r'"[^"]*[\\/]([^"\\/]+)"', r"\1", cmd)


def rows(st: dict, checks: list[dict], lk: dict, heads: dict[str, str]) -> list[dict]:
    """One row per (AC, check) of the effective checks, from the verify record at the current HEAD of the check's repo."""
    out = []
    for c in checks:
        rec = evidence.last(st, c["id"], "verify", lk["spec_hashes"][c["id"]], heads.get(c["repo"]))
        for ac in c["ac"] or ["-"]:
            out.append({"ac": ac, "check": c["id"], "repo": c["repo"], "cmd": _short(rec["cmd"] if rec else c.get("run", c["type"])),
                        "exit": rec["exit"] if rec else None, "result": rec["result"] if rec else "none",
                        "ref": f"{c['id']} attempt {rec['attempt']} ({rec['hash'][:8]})" if rec else "-"})
    return out


def body(summary: str, ticket: dict) -> str:
    """The change description is the run's `ship/summary.md`, written for the human reviewer in the project's language; it is
    refused while it is empty, misses the ticket key, links into the run folder or carries the run's bookkeeping [L-144]."""
    text = summary.strip()
    where = f"ship/{SUMMARY}"
    if not text:
        raise FactoryError(f"{where} is missing or empty: " + GUIDE)
    if ticket["id"] not in text:
        raise FactoryError(f"{where} does not name the ticket {ticket['id']}: start with the key and its link, then " + GUIDE)
    if LOCAL_LINK.search(text):
        raise FactoryError(f"{where} links to a path inside the run folder, which the SCM cannot open [L-144]")
    if (m := JARGON.search(text)):
        raise FactoryError(f"{where} carries the run's bookkeeping ({m.group(0)!r}); the reviewer of the change never sees the run: " + GUIDE)
    return text + "\n"


GUIDE = ("write it for the reviewer of the change, in the language of the project (its commits and ticket): the ticket key and link; "
         "what changes for the user; behaviour that changes beyond the ticket (shared components, values a reviewer flagged); how it was "
         "tested, in plain words (the kinds of tests added, how to try it by hand). No check, AC or decision ids, no evidence, no run paths.")


def humans(st: dict, heads: dict) -> dict[str, dict]:
    """The user's confirmations of human checks at the current commits, newest per check [L-24]."""
    return {e["check"]: e for e in st["evidence"] if e.get("human_heads") and e["sha"] == heads.get(e["repo"])}


def briefing(sp: dict, rs: list[dict], st: dict) -> list[str]:
    """What the user sees before approving ship: changes, AC x evidence, follow-ups and ticket intents."""
    lines = [f"Gate S briefing for {sp['ticket']}: {sp['title']} (draft: {sp['draft']}, mode {sp['mode']})"]
    for i in sp["intents"]:
        if i["intent"] == "change_request":
            lines.append(f"  change {i['repo']}: {i['action']} {i['source_branch']} -> {i['target_branch']} at {sp['heads'][i['repo']][:7]}")
    lines += [f"  {r['ac']} x {r['check']}: {r['result']} ({r['ref']})" for r in rs]
    out = [f for f in review.open_minors(st) if f.get("scope") == "out"]
    lines.append(f"  findings outside this delivery (kept in the run, not in the change): {len(out)}")
    lines += [f"  ticket: {i['intent']} (needs the user)" for i in sp["intents"] if i["intent"].startswith("issue_")] or ["  ticket: no comment or transition"]
    lines.append(f"  approximate design frames: {', '.join(sorted({r['frame'] for r in st['design_reviews']})) or 'none'}; "
                 f"screenshots: {sum(1 for e in st['evidence'] if e['mode'] == 'verify' and e['result'] == 'pass' and any(a.endswith('.png') for a in e['artifacts']))} record(s) with images")
    asked = humans(st, sp["heads"])
    lines += [f"  human check {k}: the user confirmed ({_cell(e['user_said'], 80)})" for k, e in asked.items()] or ["  human checks: none"]
    return lines

