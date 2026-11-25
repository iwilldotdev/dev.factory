"""Parallel test runner (stdlib): one process per test module, hard per-module timeout, full output in files.

    python tests/run_tests.py [-j N] [--timeout S] [--budget S] [--tests-dir DIR] [--list] [substring ...]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dev_factory import proc  # noqa: E402  (tree kill is shared with the CLI)


def run_module(path: Path, out_dir: Path, timeout: float) -> dict:
    log = out_dir / f"{path.stem}.log"
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
               # no background git maintenance: its lock files appear and vanish while a fixture repo is copied
               GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="maintenance.auto", GIT_CONFIG_VALUE_0="false",
               GIT_CONFIG_KEY_1="gc.auto", GIT_CONFIG_VALUE_1="0")
    kwargs = {"creationflags": 0x00000200 | 0x08000000} if proc.WINDOWS else {"start_new_session": True}
    start = time.monotonic()
    with open(log, "wb") as fh:
        p = subprocess.Popen([sys.executable, "-P", str(path)], cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                             stdout=fh, stderr=subprocess.STDOUT, **kwargs)
        try:
            code, timed_out = p.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            proc.kill_tree(p)
            p.wait()
            code, timed_out = -1, True
    text = log.read_text(encoding="utf-8", errors="replace")
    ran = sum(int(n) for n in re.findall(r"^Ran (\d+) tests?", text, re.M))
    return {"name": path.stem, "code": code, "timed_out": timed_out, "secs": time.monotonic() - start, "tests": ran, "log": log}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("patterns", nargs="*", help="run only modules whose name contains one of these")
    ap.add_argument("-j", "--jobs", type=int, default=min(os.cpu_count() or 2, 8))
    ap.add_argument("--timeout", type=float, default=120, help="hard cap per module in seconds")
    ap.add_argument("--budget", type=float, default=300, help="fail when the whole run takes longer than this")
    ap.add_argument("--tests-dir", type=Path, default=ROOT / "tests")
    ap.add_argument("--out", type=Path, default=ROOT / ".tmp" / "test-output")
    ap.add_argument("--list", action="store_true", help="print the modules and exit")
    ap.add_argument("--save-hints", action="store_true", help="write the measured module times to tests/durations.json (the scheduling hint)")
    args = ap.parse_args()
    hints_file = args.tests_dir / "durations.json"
    hints = json.loads(hints_file.read_text(encoding="utf-8")) if hints_file.is_file() else {}
    # the slowest modules start first, so the last ones to finish are short (a hint only: any module missing from it runs last)
    mods = sorted((p for p in args.tests_dir.glob("test_*.py") if not args.patterns or any(s in p.stem for s in args.patterns)),
                  key=lambda p: (-hints.get(p.stem, 0), p.stem))
    if args.list:
        print("\n".join(m.stem for m in mods))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(pool.map(lambda m: run_module(m, args.out, args.timeout), mods))
    total = time.monotonic() - start
    if args.save_hints and not args.patterns:
        hints_file.write_text(json.dumps({r["name"]: round(r["secs"]) for r in sorted(results, key=lambda r: r["name"])}, indent=0) + "\n",
                              encoding="utf-8", newline="\n")
    for r in sorted(results, key=lambda r: r["name"]):
        state = "TIMEOUT" if r["timed_out"] else "ok" if r["code"] == 0 else "FAIL"
        print(f"{state:<8}{r['name']:<28}{r['tests']:>4} tests {r['secs']:6.1f}s")
    failed = [r for r in results if r["code"] != 0]
    for r in failed:
        print(f"\n--- {r['name']} (full output: {r['log']}) ---")
        print("\n".join(r["log"].read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
    print("\nslowest: " + ", ".join(f"{r['name']} {r['secs']:.1f}s" for r in sorted(results, key=lambda r: -r["secs"])[:5]))
    print(f"{sum(r['tests'] for r in results)} tests in {len(results)} modules, {total:.1f}s wall (budget {args.budget:.0f}s, "
          f"{args.jobs} jobs), {len(failed)} failed")
    if total > args.budget:
        print(f"FAIL: the suite took longer than its {args.budget:.0f}s budget")
        return 1
    return 1 if failed or not results else 0


if __name__ == "__main__":
    sys.exit(main())
