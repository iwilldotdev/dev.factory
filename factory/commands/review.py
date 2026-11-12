"""`factory review record` (DESIGN §2.5)."""

from __future__ import annotations

import json
from pathlib import Path

from ..core import (SCHEMA_VERSION, FactoryError, event, git_head, read_json,
                    repo_path_for_run, run_dir, run_load, run_save,
                    workspace_load, write_json)

SEVERITIES = ("blocker", "major", "minor")


def validate(rec: dict, kind: str, acs: list[str]) -> list[str]:
    errors = []
    if rec.get("verdict") not in ("APPROVE", "CHANGES"):
        errors.append("verdict must be APPROVE or CHANGES")
    if not rec.get("reviewer"):
        errors.append("reviewer is required")
    findings = rec.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be a list")
        findings = []
    ids = set()
    for f in findings:
        fid = f.get("id")
        if not fid or fid in ids:
            errors.append(f"finding id missing or duplicate: {fid!r}")
        ids.add(fid)
        if f.get("severity") not in SEVERITIES:
            errors.append(f"finding {fid}: severity must be one of {SEVERITIES}")
        if not f.get("text"):
            errors.append(f"finding {fid}: text is required")
    comp = rec.get("completeness")
    if not isinstance(comp, dict) or not all(isinstance(comp.get(k), int) for k in ("done", "gap", "not_checked")):
        errors.append("completeness needs integer done, gap, not_checked")
    if rec.get("verdict") == "APPROVE" and any(f.get("severity") in ("blocker", "major") and not f.get("resolved")
                                               for f in findings):
        errors.append("APPROVE with open blocker/major findings")
    if kind == "qa" and acs:
        cover = {c.get("ac"): c for c in rec.get("ac_coverage") or []}
        for ac in acs:
            if ac not in cover:
                errors.append(f"qa review must state for {ac} whether its checks exercise it (ac_coverage)")
            elif cover[ac].get("exercises") is False and rec.get("verdict") == "APPROVE":
                errors.append(f"{ac}: checks do not exercise the AC; verdict cannot be APPROVE")
    return errors


def cmd_record(args) -> int:
    run = run_load(args.run)
    ws = workspace_load(run["workspace"])
    rec = read_json(Path(args.file))
    if not isinstance(rec, dict):
        raise FactoryError("review file must be a JSON object")
    if not run.get("repos"):
        raise FactoryError("run has no repos to bind the review to")
    acs: list[str] = []
    acs_path = run_dir(run["id"]) / "acs.json"
    if acs_path.is_file():
        acs = [a["id"] for a in read_json(acs_path).get("acs", []) if a.get("id")]
    errors = validate(rec, args.kind, acs)
    shas = {alias: git_head(repo_path_for_run(run, ws, alias)) for alias in run["repos"]}
    claimed = rec.get("repos")
    if claimed:
        for alias, sha in shas.items():
            if claimed.get(alias) not in (None, sha):
                errors.append(f"review was made against {alias}@{str(claimed[alias])[:8]}, HEAD is {sha[:8]}")
    if errors:
        from ..core import GateFailed
        raise GateFailed(errors)
    rdir = run_dir(run["id"]) / "reviews"
    existing = [int(p.stem.rsplit("-", 1)[1]) for p in rdir.glob(f"{args.kind}-*.json")
                if p.stem.rsplit("-", 1)[1].isdigit()]
    rnd = max(existing, default=0) + 1
    out = dict(rec, schemaVersion=SCHEMA_VERSION, kind=args.kind, round=rnd, repos=shas)
    write_json(rdir / f"{args.kind}-{rnd}.json", out)
    event(run, "review", review_kind=args.kind, round=rnd, verdict=out["verdict"], findings=len(out["findings"]))
    run_save(run)
    if getattr(args, "json", False):
        print(json.dumps({"kind": args.kind, "round": rnd, "verdict": out["verdict"]}))
    else:
        print(f"recorded {args.kind} review round {rnd}: {out['verdict']}")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("review", help="record reviews")
    rs = parser.add_subparsers(dest="review_cmd", required=True)
    p = rs.add_parser("record")
    p.add_argument("run")
    p.add_argument("--kind", choices=["code", "qa"], required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_record)
