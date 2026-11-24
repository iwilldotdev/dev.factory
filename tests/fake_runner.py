"""A fake of the bundled browser runner (DESIGN section 10): `fake_runner.py <command> [flags] --out <dir>`.
Every start appends one line of JSON to $FAKE_RUNNER_LOG; the result of a command comes from $FAKE_RUNNER_RESULTS
({"<command>": {"exit": n, "status": s, "summary": t, "details": {...}}}), defaulting to a pass."""
import json
import os
import sys
import time
from pathlib import Path


def flags(argv):
    out, i = {}, 0
    while i < len(argv):
        if argv[i].startswith("--"):
            key = argv[i][2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                out.setdefault(key, []).append(argv[i + 1])
                i += 2
                continue
            out.setdefault(key, []).append(True)
        i += 1
    return out


def note(entry):
    if os.environ.get("FAKE_RUNNER_LOG"):
        with open(os.environ["FAKE_RUNNER_LOG"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def one(argv, log=True):
    cmd, f = argv[0], flags(argv[1:])
    out = Path(f["out"][0])
    out.mkdir(parents=True, exist_ok=True)
    if log:
        note({"cmd": cmd, "argv": argv[1:], "pid": os.getpid(), "user": os.environ.get("TEST_USER"), "pass": os.environ.get("TEST_PASSWORD"),
              "expect_ms": os.environ.get("FACTORY_EXPECT_TIMEOUT_MS")})
    spec = json.loads(os.environ.get("FAKE_RUNNER_RESULTS", "{}")).get(cmd, {})
    if cmd == "spec" and f.get("spec") and Path(f["spec"][0]).suffix == ".json":
        spec = {**spec, **json.loads(Path(f["spec"][0]).read_text(encoding="utf-8"))}
    if spec.get("pause"):
        time.sleep(spec["pause"])
    code = spec.get("exit", 0)
    if spec.get("fail_once"):  # fails while the marker file does not exist yet, then passes
        marker = Path(spec["fail_once"])
        code = 0 if marker.exists() else 1
        marker.write_text("x")
    status = spec.get("status", "pass" if code == 0 else "fail")
    if cmd == "login" and code == 0:
        Path(f["state-out"][0]).write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    if cmd in ("screenshot", "visual"):
        (out / ("shot.png" if cmd == "screenshot" else "app.png")).write_bytes(b"\x89PNG fake")
    summary = spec.get("summary", f"{cmd} {status}")
    (out / "result.json").write_text(json.dumps({"status": status, "summary": summary, "details": spec.get("details", {}), "artifacts": []}), encoding="utf-8")
    return code


def main(argv):
    if argv[0] != "batch":
        return one(argv)
    f = flags(argv[1:])
    entries = json.loads(Path(f["manifest"][0]).read_text(encoding="utf-8"))
    res = []
    for e in entries:
        code = one([*e["argv"], "--out", e["out"]], log=False)
        res.append({"id": e["id"], "exit": code, "status": "pass" if code == 0 else "fail", "secs": 0.1})
    out = Path(f["out"][0])
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps({"status": "pass", "summary": f"batch of {len(res)}", "details": {"entries": res}, "artifacts": []}), encoding="utf-8")
    note({"cmd": "batch", "entries": [e["id"] for e in entries], "pid": os.getpid()})
    return 0


sys.exit(main(sys.argv[1:]))
