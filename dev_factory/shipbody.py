"""The change-request title and body, and the Gate S briefing: AC x check x status with evidence references [L-142, L-144, L-146]."""
from __future__ import annotations

import re

from . import evidence, review
from .util import FactoryError

LOCAL_LINK = re.compile(r"\]\((?:evidence/|artifacts/|[A-Za-z]:[\\/])|file://")  # run-local paths 404 in the SCM [L-144]
EXCERPT = 10


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
                        "ref": f"{c['id']} attempt {rec['attempt']} ({rec['hash'][:8]})" if rec else "-",
                        "tail": rec["tail"].splitlines()[-EXCERPT:] if rec else []})
    return out


def table(rs: list[dict]) -> list[str]:
    lines = ["| AC | Check | Command | Exit | Result | Evidence |", "|---|---|---|---|---|---|"]
    return lines + [f"| {r['ac']} | {r['check']} | `{_cell(r['cmd'], 70).replace('`', chr(39))}` | {'' if r['exit'] is None else r['exit']} | "
                    f"{r['result']} | {_cell(r['ref'])} |" for r in rs]


def body(st: dict, ticket: dict, plan: dict, lk: dict, rs: list[dict], heads: dict, behind: dict) -> str:
    """The change description; it names commands, results and evidence ids, never a path inside the run folder [L-144]."""
    out = [f"## Summary", f"{ticket['id']}: {ticket['title'].strip()}" + (f" ({ticket['url']})" if ticket.get("url") else ""), "",
           "## Acceptance criteria", ""]
    acs = {a["id"]: a["text"] for a in plan["acs"]}
    out += [f"- {i}: {_cell(t, 200)}" for i, t in acs.items()] + [""] + table(rs) + [""]
    shown = set()
    for r in rs:
        if r["tail"] and r["check"] not in shown:
            shown.add(r["check"])
            out += [f"<details><summary>{r['check']}: last {len(r['tail'])} output lines</summary>", "", "```text", *r["tail"], "```", "</details>", ""]
    flaky = [e for e in st["evidence"] if e.get("result") == "flaky" and e["sha"] == heads.get(e["repo"])]
    if flaky:
        out += ["## Flaky attempts", *[f"- {e['check']} attempt {e['attempt']} failed an assertion and passed on retry" for e in flaky], ""]
    changed = (review.latest(st, "code") or {}).get("changed_values") or []
    if changed:
        out += ["## Changed values (code review)", *[f"- {c['file']}: {c['what']} ({c['why']})" for c in changed], ""]
    asked = humans(st, heads)
    if asked:
        out += ["## Human checks (confirmed by the user)", *[f"- {k}: {_cell(e['cmd'].removeprefix('human: '), 120)}; the user said: {_cell(e['user_said'], 120)}" for k, e in asked.items()], ""]
    minors = review.open_minors(st)
    if minors:
        out += [f"## Follow-ups: {len(minors)} unresolved minor finding(s)"]
        out += [f"- {f.get('file', '')} {f['text']} (round {f['round']}, {f['review']} review)".replace("-  ", "- ") for f in minors[:review.CAP]]
        out += ([f"- and {len(minors) - review.CAP} more"] if len(minors) > review.CAP else []) + [""]
    seen: dict[str, str] = {}
    for r in st["design_reviews"]:
        seen[r["frame"]] = r["user_said"]
    if seen:
        out += ["## Design fidelity: approximate", *[f"- {f}: an approximate frame; the user reviewed it and said: {_cell(t, 120)}" for f, t in seen.items()], ""]
    shots = [(c["id"], n) for c in plan["checks"] if c.get("type") == "screenshot" and (e := next((e for e in reversed(st["evidence"]) if e["check"] == c["id"]
             and e["mode"] == "verify" and e["result"] == "pass" and e["sha"] == heads.get(c["repo"])), None)) for n in
             [a.rsplit("/", 1)[-1] for a in e["artifacts"] if a.endswith(".png")]]
    if shots:
        out += ["## Screenshots (attach the images from the run's artifacts)", *[f"- {c}: {n}" for c, n in shots], ""]
    out += ["## Review", *[f"- {k}: round {e['round']}, {e['verdict']} by {e['reviewer']}" for k in review.BOUND if (e := review.latest(st, k))], ""]
    out += ["## Branches", *[f"- {r}: {h[:7]}, {b['count']} commit(s) behind {b['ref'] or 'the target'}" if b["count"] is not None
                              else f"- {r}: {h[:7]}, behind count unknown ({b['note']})" for r, h in heads.items() for b in [behind[r]]]]
    text = "\n".join(out).rstrip() + "\n"
    if LOCAL_LINK.search(text):
        raise FactoryError("the generated change body links to a path inside the run folder [L-144]")
    return text


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
    lines.append(f"  follow-ups: {len(review.open_minors(st))} unresolved minor finding(s)")
    lines += [f"  ticket: {i['intent']} (needs the user)" for i in sp["intents"] if i["intent"].startswith("issue_")] or ["  ticket: no comment or transition"]
    lines.append(f"  approximate design frames: {', '.join(sorted({r['frame'] for r in st['design_reviews']})) or 'none'}; "
                 f"screenshots: {sum(1 for e in st['evidence'] if e['mode'] == 'verify' and e['result'] == 'pass' and any(a.endswith('.png') for a in e['artifacts']))} record(s) with images")
    asked = humans(st, sp["heads"])
    lines += [f"  human check {k}: the user confirmed ({_cell(e['user_said'], 80)})" for k, e in asked.items()] or ["  human checks: none"]
    return lines

