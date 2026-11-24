"""Reviews: `code` and `qa` verdicts bound to full SHAs and plan hashes, loops, follow-ups [L-18, L-22, L-23, L-39, L-107, L-146, L-147]."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import git, lock, plan as planmod
from .run import Run
from .util import FactoryError, canonical, sha256_bytes

BOUND = {"code": "core_hash", "qa": "plan_hash"}  # a code review survives a checks-only amend [L-137]
SEVERITIES, VERDICTS, CAP = ("blocker", "major", "minor"), ("APPROVE", "CHANGES"), 20
HEX = re.compile(r"^[0-9a-fA-F]{7,64}$")
KEYS = {"verdict", "reviewer", "repos", "findings", "resolved", "completeness", "exercises", "changed_values"}
EXAMPLE = {"verdict": "APPROVE", "reviewer": "fresh-reviewer-1", "repos": {"web": "3f9c2ab"},
           "findings": [{"id": "F1", "severity": "minor", "file": "src/app.py:42", "text": "name the constant", "ac": "AC-1"}],
           "resolved": ["1:F2"], "completeness": {"done": 5, "gap": 0, "not_checked": 0},
           "exercises": [{"ac": "AC-1", "exercises": True, "note": "CHK-1 asserts the exact value"}],
           "changed_values": [{"file": "src/ui/toast.ts:12", "what": "duration 5s -> 20s", "why": "the ticket asks for it"}]}
REPOS_HINT = 'repos must be an object of repo name to commit id (7 or more hex characters), e.g. {"web": "3f9c2ab"}'


def norm(text) -> str:
    return " ".join(str(text).lower().split())


def _strs(obj, keys: tuple[str, ...], where: str, errs: list[str]) -> None:
    errs += [f"{where}.{k}: must be a non-empty string" for k in keys if not (isinstance(obj.get(k), str) and obj[k].strip())]


def parse(data, kind: str, plan: dict, round_no: int, earlier: list[dict]) -> tuple[dict, list[str]]:
    """The normalized review input and every problem with it [L-22, L-23, L-39, L-107, L-147]."""
    if not isinstance(data, dict):
        return {}, ["the review file must be a JSON object (factory review example prints one)"]
    errs = [f"unknown key {k!r}" for k in sorted(set(data) - KEYS)]
    if data.get("verdict") not in VERDICTS:
        errs.append(f"verdict: must be {' or '.join(VERDICTS)}")
    _strs(data, ("reviewer",), "review", errs)
    repos = data.get("repos")
    if not (isinstance(repos, dict) and repos and all(isinstance(v, str) and HEX.match(v) for v in repos.values())):
        errs.append(f"repos: {REPOS_HINT} [L-107]")
    else:
        errs += [f"repos: {r!r} is not a repo of this plan ({', '.join(plan['repos'])})" for r in repos if r not in plan["repos"]]
        errs += [f"repos: the commit of repo {r!r} is missing" for r in plan["repos"] if r not in repos]
    acs = {a["id"] for a in plan["acs"]}
    findings = data.get("findings", [])
    if not (isinstance(findings, list) and all(isinstance(f, dict) for f in findings)):
        errs.append("findings: must be a list of {id, severity, text, file?, ac?}")
        findings = []
    ids = set()
    for i, f in enumerate(findings):
        here = f"findings[{i}]"
        errs += [f"{here}: unknown key {k!r}" for k in sorted(set(f) - {"id", "severity", "file", "text", "ac"})]
        _strs(f, ("id", "text"), here, errs)
        if f.get("severity") not in SEVERITIES:
            errs.append(f"{here}.severity: must be one of {', '.join(SEVERITIES)}")
        if f.get("ac") is not None and f["ac"] not in acs:
            errs.append(f"{here}.ac: {f['ac']!r} is not an AC of this plan")
        if f.get("id") in ids:
            errs.append(f"{here}.id: duplicate {f.get('id')!r}")
        ids.add(f.get("id"))
    comp = data.get("completeness")
    if not (isinstance(comp, dict) and set(comp) == {"done", "gap", "not_checked"}
            and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in comp.values())):
        errs.append('completeness: must be {"done": n, "gap": n, "not_checked": n}')
        comp = {"done": 0, "gap": 0, "not_checked": 0}
    errs += _resolved(data.get("resolved", []), round_no, earlier)
    ex = data.get("exercises")
    if kind == "qa" or ex is not None:
        errs += _exercises(ex, acs, kind == "qa")
    cv = data.get("changed_values")
    if kind == "code" or cv is not None:
        if not isinstance(cv, list) or not all(isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k].strip() for k in ("file", "what", "why"))
                                               and set(c) == {"file", "what", "why"} for c in cv):
            errs.append("changed_values: a code review lists every duration, timeout, retry, flag or copy value changed in the diff as "
                        '{"file", "what", "why"}; an explicit [] says none [L-39]')
    sev = [f.get("severity") for f in findings]
    if data.get("verdict") == "APPROVE":
        if comp["gap"] or comp["not_checked"]:
            errs.append("APPROVE with completeness gap or not_checked above 0 is refused [L-23]")
        if "blocker" in sev or "major" in sev:
            errs.append("APPROVE with a blocker or major finding is refused [L-23]")
        if isinstance(ex, list) and any(isinstance(e, dict) and e.get("exercises") is False for e in ex):
            errs.append("APPROVE with exercises: false is refused: a check that cannot fail does not prove its AC [L-22]")
    elif data.get("verdict") == "CHANGES" and not findings:
        errs.append("CHANGES needs at least one finding")
    return data, errs


def _exercises(ex, acs: set[str], required: bool) -> list[str]:
    ok = isinstance(ex, list) and all(isinstance(e, dict) and isinstance(e.get("ac"), str) and isinstance(e.get("exercises"), bool)
                                      and set(e) <= {"ac", "exercises", "note"} for e in ex)
    if not ok:
        return ['exercises: a list with one {"ac", "exercises": true|false, "note"} per AC [L-22]']
    got = [e["ac"] for e in ex]
    out = [f"exercises: {a} has no entry [L-22]" for a in sorted(acs - set(got))] if required else []
    return out + [f"exercises: {a!r} is not an AC of this plan or is listed twice" for a in got if a not in acs or got.count(a) > 1]


def _resolved(items, round_no: int, earlier: list[dict]) -> list[str]:
    if not (isinstance(items, list) and all(isinstance(i, str) for i in items)):
        return ["resolved: must be a list of round:id strings (a bare id means the previous round) [L-147]"]
    have = {(e["round"], f["id"]) for e in earlier for f in e.get("findings", [])}
    return [f"resolved: {i!r} names no finding of an earlier round (use round:id)" for i in items if _full(i, round_no) not in have]


def _full(item: str, round_no: int) -> tuple[int, str]:
    head, _, tail = item.partition(":")
    return (int(head), tail) if tail and head.isdigit() else (round_no - 1, item)


def latest(st: dict, kind: str) -> dict | None:
    return next((e for e in reversed(st["reviews"]) if e.get("kind") == kind), None)


def heads_of(st: dict, repos: list[str]) -> dict[str, str]:
    return {r: git.head(Path(w["path"])) for r in repos if (w := git.current(st, r)) and Path(w["path"]).is_dir()}


def record(run: Run, project, kind: str, data, decision: str | None, actor: str | None) -> dict:
    """Validate and append one review. A CHANGES with a blocker or major finding spends a loop of the single budget [L-135, L-138]."""
    from . import gates
    if kind not in BOUND:
        raise FactoryError(f"invalid review kind {kind!r}; valid kinds: code, qa")
    st = run.state()
    blocking = gates.verify_reasons(run, project, st)
    if blocking:
        raise FactoryError("a review needs a locked plan and a passing verify first:\n" + "\n".join(f"  - {r}" for r in blocking))
    plan, lk = planmod.load_plan(run)[0], lock.current(st)
    earlier = [e for e in st["reviews"] if e.get("kind") == kind]
    round_no = len(earlier) + 1
    data, errs = parse(data, kind, plan, round_no, earlier)
    if errs:
        raise FactoryError("review rejected (nothing recorded):\n" + "\n".join(f"  - {e}" for e in errs))
    heads, repos = heads_of(st, plan["repos"]), {}
    for r, prefix in data["repos"].items():
        if not heads[r].startswith(prefix.lower()):
            errs.append(f"repos.{r}: {prefix} is not the current HEAD {heads[r][:7]}: review the commit that verify passed on [L-18]")
        repos[r] = heads[r]
    reviewer = data["reviewer"].strip()
    clash = {reviewer.lower(), (actor or "").strip().lower()} & st["builders"]
    if clash:
        errs.append(f"reviewer {', '.join(sorted(clash))!r} also built or verified in this run (an actor hint on evidence or the worktree): "
                    "a fresh reviewer who did not build it records the review")
    if errs:
        raise FactoryError("review rejected (nothing recorded):\n" + "\n".join(f"  - {e}" for e in errs))
    findings = [{k: v for k, v in f.items() if v is not None} for f in data.get("findings", [])]
    if data["verdict"] == "CHANGES" and any(f["severity"] in ("blocker", "major") for f in findings):
        last = [e for e in st["loops"] if e["kind"] == "review"]
        if not last or last[-1].get("heads") != repos:  # one fix round, whatever the number of reviewers that found it
            lock.spend(run, project.cfg, st, "review", decision, actor, {"heads": repos, "round": round_no, "review": kind})
    rec = {"type": "review", "kind": kind, "round": round_no, "verdict": data["verdict"], "reviewer": reviewer, "repos": repos,
           "findings": findings, "resolved": ["%d:%s" % _full(i, round_no) for i in data.get("resolved", [])],
           "completeness": data["completeness"], "bound": {BOUND[kind]: lk[BOUND[kind]]}, "lock_seq": lk["seq"],
           "independent_of": sorted(st["builders"])}
    for key in ("exercises", "changed_values"):
        if key in data:
            rec[key] = data[key]
    rec["hash"] = sha256_bytes(canonical(rec).encode("utf-8"))
    return run.append(rec, actor)


def load_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FactoryError(f"cannot read the review file {path.as_posix()}: {exc}") from exc
    except ValueError as exc:
        raise FactoryError(f"the review file is not valid JSON: {exc}") from exc


def reasons(st: dict, plan: dict, lk: dict) -> list[str]:
    """Why `gate review` fails once verify holds: a review is missing, stale (SHA or plan hash) or not APPROVE [L-18, L-137]."""
    heads, out = heads_of(st, plan["repos"]), []
    for kind, key in BOUND.items():
        rv = latest(st, kind)
        cmd = f"record one with factory review record --kind {kind} --file <review.json> (a fresh reviewer, not the builder)"
        if rv is None:
            out.append(f"no {kind} review: {cmd}")
        elif rv["repos"] != heads:
            moved = ", ".join(f"{r} {rv['repos'].get(r, '?')[:7]} -> {h[:7]}" for r, h in heads.items() if rv["repos"].get(r) != h)
            out.append(f"the {kind} review (round {rv['round']}) is stale, HEAD moved ({moved}): {cmd} [L-18]")
        elif rv["bound"].get(key) != lk[key]:
            out.append(f"the {kind} review (round {rv['round']}) is stale, the plan changed ({key}): {cmd} [L-18, L-137]")
        elif rv["verdict"] != "APPROVE":
            blocking = [f"{f['id']} ({f['severity']}) {f['text']}" for f in rv["findings"] if f["severity"] != "minor"]
            out.append(f"the {kind} review (round {rv['round']}) asked for changes: " + ("; ".join(blocking) or "see its findings")
                       + ". Fix, commit and run verify, then record a new review")
    return out


def open_minors(st: dict) -> list[dict]:
    """Unresolved minor findings from every round of both kinds, de-duplicated, in first-seen order [L-146, L-147]."""
    out, seen = [], set()
    for kind in BOUND:
        rounds = [e for e in st["reviews"] if e.get("kind") == kind]
        cleared: set[tuple[int, str]] = set()
        for e in rounds:
            text = {(r["round"], f["id"]): norm(f["text"]) for r in rounds for f in r["findings"]}
            for item in e.get("resolved", []):
                rn, fid = _full(item, e["round"])
                cleared |= {k for k, t in text.items() if k[1] == fid and k[0] <= rn and t == text.get((rn, fid))}
        for e in rounds:
            for f in e["findings"]:
                key = (f.get("file", ""), norm(f["text"]))
                if f["severity"] == "minor" and (e["round"], f["id"]) not in cleared and key not in seen:
                    seen.add(key)
                    out.append({**f, "round": e["round"], "review": kind})
    return out
