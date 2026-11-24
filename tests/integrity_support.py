"""Fixture for tests/test_integrity_*.py: a real single-repo ticket run walked through the real gates."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from factory import core
from tests.real_support import RealCase, cli, commit_file, sh
from tests.ticket_support import put_ticket

PY = f'"{sys.executable}"'
PASS = f'{PY} -c "pass"'


class IntegrityCase(RealCase):
    repos = ("React",)

    def start(self, ticket: str = "DEV-1", bug: bool = False) -> str:
        self.ok("run", "start", "ws", ticket, *(["--bug", ticket] if bug else []))
        self.rid = f"ws-{ticket.lower()}"
        self.ok("run", "branch", self.rid, "React", "--base", "main")
        self.wt = Path(core.run_load(self.rid)["repos"]["React"]["worktree"])
        put_ticket(self.rid, ticket)
        return self.rid

    @property
    def rd(self) -> Path:
        return core.run_dir(self.rid)

    def acs(self, checks=None, acs=None) -> None:
        checks = checks or [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": PASS}]
        acs = acs or [{"id": "AC-1", "ticket": "DEV-1", "text": "works", "kind": "logic",
                       "checks": [c["id"] for c in checks]}]
        core.write_json(self.rd / "acs.json", {"schemaVersion": 1, "acs": acs, "checks": checks})

    def gate(self, phase: str) -> tuple[int, str]:
        return cli("gate", self.rid, phase)

    def to_prove(self, checks=None) -> None:
        self.start()
        self.acs(checks)
        (self.rd / "checks").mkdir(exist_ok=True)
        (self.rd / "checks" / "note.txt").write_text("v1\n", encoding="utf-8")
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        commit_file(self.wt, "src/a.txt", "a\n", "[Area] - change")
        self.ok("gate", self.rid, "build")

    def proved(self, checks=None) -> None:
        self.to_prove(checks)
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")

    def review_file(self, kind: str, verdict: str = "APPROVE", findings=None, completeness=None) -> Path:
        rec = {"verdict": verdict, "reviewer": "factory-auditor", "findings": findings or [],
               "completeness": completeness or {"done": 1, "gap": 0, "not_checked": 0}}
        if kind == "qa":
            rec["ac_coverage"] = [{"ac": a["id"], "exercises": True}
                                  for a in core.read_json(self.rd / "acs.json")["acs"]]
        path = self.tmp / f"{kind}-review.json"
        core.write_json(path, rec)
        return path

    def record(self, kind: str, verdict: str = "APPROVE", findings=None, completeness=None) -> tuple[int, str]:
        return cli("review", "record", self.rid, "--kind", kind, "--file",
                   str(self.review_file(kind, verdict, findings, completeness)))

    def reviewed(self) -> None:
        self.proved()
        self.assertEqual(self.record("code")[0], 0)
        self.assertEqual(self.record("qa")[0], 0)
        self.ok("gate", self.rid, "review")

    def readbacks(self, plan: dict, sha: str | None = None) -> None:
        from tests.change_support import put_for_plan
        put_for_plan(self.rid, plan, **({"head_sha": sha} if sha else {}))

    def shipped(self) -> dict:
        self.reviewed()
        plan = json.loads(self.ok("--json", "ship", "plan", self.rid))
        self.readbacks(plan)
        self.ok("ship", "verify", self.rid)
        self.ok("gate", self.rid, "ship")
        return plan

    def set_autonomy(self, **kw) -> None:
        self.ws.setdefault("autonomy", {}).update(kw)
        self.save_ws()


def fake_env_record(alias: str, worktree: Path, head: str | None = None, **over) -> Path:
    """A pid record as `factory env up --run` writes it, pointing at this live test process."""
    from factory.commands import env as env_mod
    path = env_mod.pid_path("ws", alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"pid": os.getpid(), "cwd": str(Path(worktree).resolve()), "head": head or core.git_head(worktree),
           "started": core.now(), "create_time": env_mod.process_create_time(os.getpid())}
    rec.update(over)
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


__all__ = ["IntegrityCase", "PASS", "PY", "cli", "commit_file", "core", "fake_env_record", "sh"]
