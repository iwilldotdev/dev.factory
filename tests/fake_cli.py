"""Fake browser CLI for collector tests. Records argv in <out>/argv-<cmd>.json; behaviour via FAKE_STATUS."""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
out = Path(args[args.index("--out") + 1])
out.mkdir(parents=True, exist_ok=True)
(out / f"argv-{args[0]}.json").write_text(json.dumps(args))
if os.environ.get("FAKE_NO_RESULT"):
    sys.exit(3)
status = os.environ.get("FAKE_STATUS", "pass")
if args[0] == "capture":
    (out / "app.png").write_bytes(b"x")
if args[0] == "run-spec" and "--screenshot" in args and not os.environ.get("FAKE_NO_PNG"):
    (out / "artifacts" / "t").mkdir(parents=True, exist_ok=True)
    (out / "artifacts" / "t" / "test-finished-1.png").write_bytes(b"x")
(out / "result.json").write_text(json.dumps(
    {"status": status, "summary": f"fake {args[0]}", "artifacts": ["app.png"], "details": {"cmd": args[0]}}))
sys.exit(0 if status == "pass" else 1)
