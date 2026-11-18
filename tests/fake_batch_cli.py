"""Fake browser CLI for batch tests: `run-spec` (one check) and `run-specs` (a batch), scripted by the JSON file in
FAKE_CFG: {"statuses": {check id: pass|fail|error}, "single": {check id: status for a lone run-spec},
"mode": "ok"|"crash"|"garbage"|"interrupt"}. Every call is appended to FAKE_LOG as one JSON line."""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]


def arg(name):
    return args[args.index(name) + 1] if name in args else None


cfg = json.loads(Path(os.environ["FAKE_CFG"]).read_text(encoding="utf-8"))
out = Path(arg("--out"))
out.mkdir(parents=True, exist_ok=True)


def log(**kw):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"cmd": args[0], **kw}) + "\n")


def body(cid, status):
    if status == "pass":
        return {"status": "pass", "summary": "1 passed, 0 failed", "details": {"passed": 1, "failed": 0, "tests": []}}
    summary = "0 passed, 1 failed" if status == "fail" else "boom"
    err = cfg.get("error", "expect(locator).toHaveText(expected)")
    return {"status": status, "summary": summary, "details": {"failed": 1, "tests": [{"title": cid, "ok": False, "error": err}]}}


if args[0] == "run-spec":
    cid = Path(arg("--spec")).name.split(".")[0]
    log(ids=[cid])
    status = (cfg.get("single") or {}).get(cid) or (cfg.get("statuses") or {}).get(cid, "pass")
    (out / "result.json").write_text(json.dumps({**body(cid, status), "artifacts": ["report.json", "run.log"]}))
    sys.exit(0 if status == "pass" else 1)

if args[0] == "run-specs":
    manifest = json.loads(Path(arg("--manifest")).read_text(encoding="utf-8"))
    ids = [e["id"] for e in manifest]
    log(ids=ids, max_failures=arg("--max-failures"), timeout_ms=arg("--timeout-ms"), caps=[e["timeout_ms"] for e in manifest],
        state=arg("--storage-state"))
    mode = cfg.get("mode", "ok")
    if mode == "crash":
        sys.exit(3)
    if mode == "garbage":
        (out / "batch.json").write_text("{not json")
        sys.exit(0)
    results, failed = [], False
    for i, e in enumerate(manifest):
        status = (cfg.get("statuses") or {}).get(e["id"], "pass")
        ran = not (failed and arg("--max-failures")) and not (mode == "interrupt" and i >= 1)
        if ran:
            failed = failed or status != "pass"
            results.append({"id": e["id"], "ran": True, "durationMs": 1500 + i * 500, **body(e["id"], status)})
            Path(e["out"]).mkdir(parents=True, exist_ok=True)
            (Path(e["out"]) / "report.json").write_text("{}")
            (Path(e["out"]) / "run.log").write_text("log")
        else:
            results.append({"id": e["id"], "ran": False, "durationMs": 0, "status": "error", "summary": "not run", "details": {}})
    (out / "batch.json").write_text(json.dumps({"results": results}))
    sys.exit(0)

sys.exit(2)
