"""`factory profile new|test|check`: author and validate provider profiles (docs/design/provider-agnostic.md section 6)."""

from __future__ import annotations

import difflib
import json

from .. import contracts, mapping, profiles
from ..core import FactoryError

WORKFLOW = """\
Authoring workflow (a worker writes DRAFTS only, under FACTORY_HOME/profile-drafts/<name>/; never profiles/):
  1. factory profile new <name> --role R        (done: {path})
  2. ask the user for the provider and REAL, anonymizable sample payloads per kind; save them under
     samples/<kind>/<n>.<ext> (never invent a sample)
  3. when a field is ambiguous, read the provider's public API docs and cite the page in `notes`;
     still ambiguous: stop and ask
  4. write kinds.<kind>.map in profile.json (operators: see docs/design/provider-agnostic.md section 4)
  5. factory profile test <name> --draft [--kind K] [--sample N]    show the contract and unknowns to the user
     factory profile test <name> --draft --golden                   after saving samples/<kind>/<n>.expected.json
  6. factory profile check <name> --draft      (prints the draft DIGEST and the guard fields)
  7. the user reviews the check output (guard fields + digest) and the samples; the MAIN SESSION runs
     factory profile approve <name> --digest <digest from check> --user-said "<gist>"
     (copies it to profiles/<name>/ with approved.json; prints guard fields and a diff against the current version)
     Accepted unknowns go in `accepted_unknowns`. Editing an approved profile.json by hand invalidates it."""


def _draft(args) -> bool:
    return bool(getattr(args, "draft", False))


def _out(args, payload, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(text)


def cmd_new(args) -> int:
    path = profiles.scaffold(args.name, args.role)
    _out(args, {"profile": args.name, "path": str(path), "draft": True},
         f"created draft {path / 'profile.json'}\n" + WORKFLOW.format(path=path))
    return 0


def _guard_lines(guard: dict) -> list[str]:
    out = []
    for key in profiles.GUARD_FIELDS:
        vals = guard.get(key) or []
        out.append(f"  {key}: " + ("(none)" if not vals else ""))
        out += [f"    - {v}" for v in vals]
    return out


def cmd_approve(args) -> int:
    res = profiles.approve(args.name, args.user_said, args.digest)
    lines = [f"approved profile {args.name} -> {res['path']} (digest {res['digest']})"
             + (f"; previous version backed up at {res['backup']}" if res["backup"] else ""),
             "guard fields now in force:"] + _guard_lines(res["guard"]) + [
             "diff against the previous approved profile.json:", res["diff"]]
    _out(args, res, "\n".join(lines))
    return 0


def cmd_test(args) -> int:
    if getattr(args, "write_golden", False) and not _draft(args):
        raise FactoryError("--write-golden writes into the profile folder: approved profiles are read-only, use --draft")
    doc = profiles.load_profile(args.name, _draft(args))
    kinds = [args.kind] if args.kind else profiles.declared_kinds(doc)
    if args.kind and args.kind not in profiles.declared_kinds(doc):
        raise FactoryError(f"profile {args.name!r} declares no kind {args.kind!r} (declared: "
                           f"{', '.join(profiles.declared_kinds(doc)) or 'none'})")
    results, failed, lines = [], 0, []
    for kind in kinds:
        ext = profiles.kind_ext(doc, kind)
        files = profiles.sample_files(args.name, kind, ext, _draft(args))
        if args.sample:
            files = [p for p in files if p.stem == args.sample]
        if not files:
            lines.append(f"-- {kind}: no samples" + (f" named {args.sample!r}" if args.sample else ""))
            failed += 1
            results.append({"kind": kind, "sample": args.sample, "error": "no samples"})
            continue
        for p in files:
            head = f"== {kind}/{p.stem}"
            rec: dict = {"kind": kind, "sample": p.stem}
            try:
                if kind == "design_image":
                    w, h = contracts.png_info(p.read_bytes())
                    rec["contract"] = {"width": w, "height": h}
                    lines += [head, f"PNG {w}x{h}"]
                else:
                    names = profiles.kind_inputs(doc, kind)
                    docs = {"main": profiles.load_raw_json(p)}
                    for n, ip in profiles.sample_inputs(p, names, profiles.kind_optional_inputs(doc, kind)).items():
                        docs[n] = profiles.load_raw_json(ip)
                    res = profiles.map_docs(doc, kind, docs, profiles.sample_ctx(p))
                    rec.update(contract=res["contract"], unknowns=res["unknowns"], sources=res["sources"])
                    lines += [head, json.dumps(res["contract"], indent=2, ensure_ascii=False)]
                    lines.append("unknowns: " + (", ".join(f"{u['field']} ({u['reason']})" for u in res["unknowns"]) or "none"))
                    lines.append("sources:")
                    lines += [f"  {f} <- {src}" for f, src in res["sources"].items()]
                    if args.write_golden:
                        gp = p.with_name(f"{p.stem}.expected.json")
                        gp.write_text(json.dumps(res["contract"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                        lines.append(f"golden written: {gp.name}")
                    if args.golden:
                        gp = p.with_name(f"{p.stem}.expected.json")
                        if not gp.is_file():
                            raise FactoryError(f"no golden file {gp.name}")
                        want = profiles.load_raw_json(gp)
                        if want != res["contract"]:
                            a = json.dumps(want, indent=2, sort_keys=True, ensure_ascii=False).splitlines()
                            b = json.dumps(res["contract"], indent=2, sort_keys=True, ensure_ascii=False).splitlines()
                            diff = list(difflib.unified_diff(a, b, "expected", "actual", lineterm="", n=2))
                            raise FactoryError("golden differs:\n" + "\n".join(diff[:60]))
                        lines.append("golden: match")
                        rec["golden"] = "match"
            except mapping.MappingFailure as err:
                failed += 1
                rec["errors"] = err.errors
                lines += [head] + [f"ERROR {e}" for e in err.errors]
            except (FactoryError, ValueError, OSError) as err:
                failed += 1
                rec["errors"] = [str(err)]
                lines += [head, f"ERROR {err}"]
            results.append(rec)
    _out(args, {"profile": args.name, "results": results, "failed": failed}, "\n".join(lines))
    if failed:
        raise FactoryError(f"profile test: {failed} sample(s) failed")
    return 0


def cmd_check(args) -> int:
    rep = profiles.check_profile(args.name, _draft(args))
    lines = [("OK profile " if rep["valid"] else "INVALID profile ") + args.name]
    if rep.get("digest"):
        lines.append(f"DIGEST {rep['digest']}" + ("  <- pass this to `factory profile approve --digest`" if _draft(args) else ""))
        lines.append("guard fields (what the worker guard will trust; review them before approving):")
        lines += _guard_lines(rep["guard"])
    lines += [f"  warning: {w}" for w in rep.get("warnings", [])]
    for kind, k in rep["kinds"].items():
        lines.append(f"  {kind}: {k['ok']}/{k['samples']} sample(s) map")
    lines += [f"  problem: {p}" for p in rep["problems"]]
    _out(args, {"profile": args.name, **rep}, "\n".join(lines))
    return 0 if rep["valid"] else 2


def register(sub) -> None:
    parser = sub.add_parser("profile", help="create (drafts), test, check and approve provider profiles")
    psub = parser.add_subparsers(dest="profile_cmd", required=True)
    p = psub.add_parser("new", help="scaffold a DRAFT profile-drafts/<name>/ and print the authoring workflow")
    p.add_argument("name")
    p.add_argument("--role", required=True, choices=list(contracts.ROLES))
    p.add_argument("--draft", action="store_true", default=True, help="(default, the only mode) create a draft")
    p.set_defaults(func=cmd_new)
    p = psub.add_parser("test", help="map the samples; print contract, unknowns and per-field source expressions")
    p.add_argument("name")
    p.add_argument("--draft", action="store_true", help="test the draft in profile-drafts/ (default: the approved profile, read-only)")
    p.add_argument("--kind", choices=list(contracts.KINDS))
    p.add_argument("--sample", help="sample stem, e.g. 1")
    p.add_argument("--golden", action="store_true", help="diff against <n>.expected.json")
    p.add_argument("--write-golden", dest="write_golden", action="store_true",
                   help="write the mapped contract as <n>.expected.json (drafts only; review it before approving)")
    p.set_defaults(func=cmd_test)
    p = psub.add_parser("check", help="validity (every declared kind maps a sample) and lint; exit 2 when invalid")
    p.add_argument("name")
    p.add_argument("--draft", action="store_true", help="check the draft in profile-drafts/ (default: the approved profile)")
    p.set_defaults(func=cmd_check)
    p = psub.add_parser("approve", help="MAIN SESSION ONLY: promote a valid draft to profiles/<name>/ with approved.json")
    p.add_argument("name")
    p.add_argument("--digest", required=True, help="draft digest printed by `profile check <name> --draft` (the user approved that content)")
    p.add_argument("--user-said", dest="user_said", required=True, help="gist of the user's approval message")
    p.set_defaults(func=cmd_approve)
