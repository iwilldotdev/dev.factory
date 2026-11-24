"""A configurable stand-in for a user adapter: `fake_adapter.py <scenario.json> <id>`."""
import json
import os
import subprocess
import sys
import time

sc = json.load(open(sys.argv[1], encoding="utf-8"))
if sc.get("dump"):
    stdin_len = len(sys.stdin.read()) if sc.get("read_stdin") else None
    with open(sc["dump"], "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "stdin_len": stdin_len, "env": dict(os.environ)}, fh)
if sc.get("grandchild_pid_file"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sc["grandchild_pid_file"], "w") as fh:
        fh.write(str(child.pid))
if sc.get("sleep"):
    time.sleep(sc["sleep"])
sys.stderr.write(sc.get("stderr", ""))
sys.stdout.buffer.write(sc.get("stdout", "").encode("utf-8"))
sys.stdout.flush()
sys.exit(sc.get("exit", 0))
