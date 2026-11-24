"""End-to-end with the REAL modules: workspace -> run -> gates -> prove -> review -> ship -> metrics."""

import json
import os
import struct
import sys
import zlib
from pathlib import Path
from unittest import mock

from factory import core
from factory.commands import env as env_mod
from tests.real_support import RealCase, cli, commit_file

FAKE_CLI = Path(__file__).resolve().parent / "fake_cli.py"


def tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = zlib.compress(b"\x00\xff\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def fake_env_up(alias: str, worktree: Path) -> None:
    """A pid record as `factory env up --run` writes it, pointing at this (live) test process."""
    path = env_mod.pid_path("ws", alias)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = core.git_head(worktree)
    path.write_text(json.dumps({"pid": os.getpid(), "cwd": str(Path(worktree).resolve()), "head": head,
                                "started": core.now(), "create_time": env_mod.process_create_time(os.getpid())}),
                    encoding="utf-8")


class TestIntegrationFlow(RealCase):
    repos = ("React",)

    def test_full_run(self):
        self.ws["repos"][0]["env"] = {"start": ["x"], "health": "http://127.0.0.1:1/"}  # base_url derived from health
        self.save_ws()
        patcher = mock.patch.dict(os.environ, {"FACTORY_NODE_CLI": str(FAKE_CLI)})
        patcher.start()
        self.addCleanup(patcher.stop)
        rid = "ws-dev-1"
        rd = core.run_dir(rid)

        self.ok("run", "start", "ws", "DEV-1")
        core.write_json(rd / "remote" / "issue-DEV-1.json", {"key": "DEV-1", "fields": {"summary": "Thing"}})
        self.ok("gate", rid, "intake")
        self.ok("run", "branch", rid, "React", "--base", "main")
        wt = Path(core.run_load(rid)["repos"]["React"]["worktree"])

        # acs: a test check and a style check with a real pack
        pack = rd / "figma"
        pack.mkdir(exist_ok=True)
        core.write_json(pack / "1-2.json", {"style": {"fontSize": 12}})
        (pack / "1-2.png").write_bytes(tiny_png())
        core.write_json(pack / "meta.json", {"fileKey": "abc", "lastModified": "2026-01-01"})
        core.write_json(rd / "checks" / "style.json", {
            "node": "1:2", "entries": [{"id": "t", "selector": "h1", "expect": {"fontSize": 12}}]})
        py = f'"{sys.executable}" -c "pass"'
        core.write_json(rd / "acs.json", {"schemaVersion": 1, "acs": [
            {"id": "AC-1", "ticket": "DEV-1", "text": "title is styled", "kind": "logic", "checks": ["CHK-1", "CHK-2"]}],
            "checks": [{"id": "CHK-1", "type": "test", "repo": "React", "cmd": py},
                       {"id": "CHK-2", "type": "style", "repo": "React", "node": "1:2", "path": "/x",
                        "map": "checks/style.json"}]})
        self.ok("run", "decide", rid, "no screenshots", "--by", "user", "--user-said", "no", "--visual", "no")
        self.ok("gate", rid, "plan")

        commit_file(wt, "src/a.txt", "x")
        self.ok("gate", rid, "build")

        # collectors resolve base_url via env.py and the storage state via env.storage_state_path
        state = env_mod.storage_state_path("ws", "React")
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("{}", encoding="utf-8")
        fake_env_up("React", wt)
        self.ok("prove", rid)
        argv = json.loads((rd / "evidence" / "CHK-2" / "argv-style-probe.json").read_text())
        self.assertEqual(argv[argv.index("--url") + 1], "http://127.0.0.1:1/x")
        self.assertEqual(argv[argv.index("--storage-state") + 1], str(state))
        self.ok("gate", rid, "prove")

        review = {"verdict": "APPROVE", "reviewer": "factory-auditor", "findings": [],
                  "completeness": {"done": 1, "gap": 0, "not_checked": 0},
                  "ac_coverage": [{"ac": "AC-1", "exercises": True}]}
        rf = self.tmp / "review.json"
        rf.write_text(json.dumps(review), encoding="utf-8")
        self.ok("review", "record", rid, "--kind", "code", "--file", str(rf))
        self.ok("review", "record", rid, "--kind", "qa", "--file", str(rf))
        self.assertIn("recorded", core.read_json(rd / "reviews" / "code-1.json"))
        self.ok("gate", rid, "review")

        plan = json.loads(self.ok("--json", "ship", "plan", rid))
        self.assertEqual(plan["mrs"][0]["source_branch"], core.run_load(rid)["repos"]["React"]["branch"])
        for mr in plan["mrs"]:
            core.write_json(rd / mr["readback"], {
                "source_branch": mr["source_branch"], "target_branch": mr["target_branch"],
                "title": mr["title"], "draft": True, "web_url": "http://x/mr/1", "iid": 1, "sha": mr["head_sha"]})
        self.ok("ship", "verify", rid)
        self.ok("gate", rid, "ship")
        self.assertEqual(core.run_load(rid)["phase"], "feedback")

        m = json.loads(self.ok("--json", "metrics", rid))
        self.assertEqual(m["review_rounds"], {"code": 1, "qa": 1})
        self.assertEqual(m["escaped_findings"]["count"], 0)
        self.assertEqual(m["gate_fail"], {})
        events = core.run_load(rid)["events"]
        self.assertTrue(all("phase" in e["data"] for e in events if e["kind"] in ("gate_pass", "gate_fail")))

    def test_escaped_findings_use_recorded_time(self):
        self.ok("run", "start", "ws", "DEV-2")
        rid = "ws-dev-2"
        run = core.run_load(rid)
        run["events"].append({"at": "2026-01-01T00:00:00+00:00", "kind": "gate_pass", "data": {"phase": "prove"}})
        core.run_save(run)
        core.write_json(core.run_dir(rid) / "reviews" / "code-1.json", {
            "kind": "code", "recorded": "2026-01-02T00:00:00+00:00",
            "findings": [{"id": "F1", "severity": "major", "text": "x"}]})
        m = json.loads(self.ok("--json", "metrics", rid))
        self.assertEqual(m["escaped_findings"], {"count": 1, "by_severity": {"major": 1}, "all_findings": 1})

    def test_per_target_branch_used_by_ship_plan(self):
        self.ws["ship"]["targets"] = ["main", "homolog"]
        self.save_ws()
        sh_repo = self.repo_dirs["React"]
        from tests.real_support import sh
        sh("git", "branch", "homolog", "main", cwd=sh_repo)
        sh("git", "update-ref", "refs/remotes/origin/homolog", "homolog", cwd=sh_repo)
        self.ok("run", "start", "ws", "DEV-3")
        rid = "ws-dev-3"
        self.ok("run", "branch", rid, "React", "--base", "main")
        self.ok("run", "branch", rid, "React", "--base", "homolog", "--for-target", "homolog", "--name", "feature/DEV-3-h")
        core.write_json(core.run_dir(rid) / "acs.json", {"schemaVersion": 1, "acs": [
            {"id": "AC-1", "text": "t", "kind": "logic", "checks": ["C1"]}],
            "checks": [{"id": "C1", "type": "test", "repo": "React", "cmd": f'"{sys.executable}" -c "pass"'}]})
        self.lock_and_prove(rid)
        plan = json.loads(self.ok("--json", "ship", "plan", rid))
        by_target = {m["target"]: m["source_branch"] for m in plan["mrs"]}
        self.assertEqual(by_target["homolog"], "feature/DEV-3-h")
        self.assertEqual(by_target["main"], core.run_load(rid)["repos"]["React"]["branch"])
