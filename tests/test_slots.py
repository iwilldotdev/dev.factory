"""Env slots (a port offset, a lock and a login state per slot) and shareable envs."""
import json
import unittest

from dev_factory import config, envdata, slots
from dev_factory.project import Project
from tests.helpers import PY, lesson
from tests.m4_support import RUNNER, SERVER, M4Case, free_port


class SlotCase(M4Case):
    def slotted(self, shareable: bool = False, count: int = 3) -> int:
        """web serves `fake_server {port}`; slot 1 moves its port to a free one. Returns that port."""
        self.slot_port = free_port()
        up = json.dumps(f'"{PY}" "{SERVER}" {{port}}')
        self.write("factory.toml", self.TOML + f'\n[repo.env]\nup = {up}\nhealth = "/health"\nbase_url = "http://127.0.0.1:{self.port}"\n'
                   f'ready_timeout_s = 20\nshareable = {str(shareable).lower()}\nvars.SELF = "{{base_url}}"\n')
        self.write(".factory/local.toml", f'[checks.browser]\nbundled = "{RUNNER}"\n[repo.web.slots]\nport_offset = {self.slot_port - self.port}\ncount = {count}\n')
        return self.slot_port


class SlotTests(SlotCase):
    @lesson("L-78")
    def test_env_up_slot_applies_the_port_offset_takes_a_lock_per_slot_and_evidence_records_the_base_url(self):
        """L-78: `env up --slot 1` serves on port + offset with its own lock, `{port}` and `{base_url}` derived, and the evidence names the slot's base URL."""
        port = self.slotted()
        run = self.branch()
        self.assertIn("slot 9: web need", self.fails("env", "up", "web", "--slot", "9"))
        self.up("--slot", "1")
        env = self.proj / ".factory" / "env"
        self.assertEqual(sorted(p.name for p in env.glob("*.lock")), ["web@s1.lock"])
        self.assertEqual(json.loads((env / "web@s1.lock").read_text(encoding="utf-8"))["base_url"], f"http://127.0.0.1:{port}")
        self.assertEqual(self.get("/health", port)[0], 200)
        self.assertEqual([e["slot"] for e in self.events(run) if e["type"] == "slot"], [1])
        self.write_plan(run, {"schema": 1, "kind": "feature", "repos": ["web"], "acs": [{"id": "AC-1", "text": "It answers", "source": "ticket:description"}],
                              "checks": [{"id": "CHK-1", "type": "http", "repo": "web", "ac": ["AC-1"], "method": "GET", "path": "/health", "expect_status": 200}]})
        self.ok("gate", "plan")
        self.fix()
        self.ok("verify")
        (rec,) = self.evidence(run)
        self.assertEqual((rec["result"], rec["slot"], rec["base_url"]), ("pass", 1, f"http://127.0.0.1:{port}"))
        self.assertEqual(rec["served"]["web"]["path"], self.tree().as_posix())

    def test_the_slot_derives_placeholders_and_the_login_state_file_per_slot(self):
        port = self.slotted()
        proj = Project(self.proj, config.load(self.proj))
        slots.apply(proj, 1)
        env = proj.cfg["repo"][0]["env"]
        self.assertEqual((env["base_url"], env["vars"]["SELF"], f"{port}" in env["up"]), (f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}", True))
        slots.apply(proj, 0)
        self.assertEqual(proj.cfg["repo"][0]["env"]["base_url"], f"http://127.0.0.1:{self.port}")
        self.assertEqual(envdata.state_file(proj, "web", "ana").name, "web.ana.state.json")
        slots.apply(proj, 2)
        self.assertEqual(envdata.state_file(proj, "web", "ana").name, "web@s2.ana.state.json")

    def test_a_shareable_env_is_shared_by_a_run_at_the_same_commit_and_a_plain_one_is_not(self):
        self.slotted(shareable=True)
        self.branch(ticket="ABC-1")
        self.ok("run", "start", "ABC-2")
        data = {"id": "ABC-2", "title": "Second", "type": "feature", "status": "Open", "description_md": "x", "attachments": []}
        raw = self.write("raw.json", json.dumps({"payload": data}), root=self.base)
        self.ok("ingest", "ticket", "--raw", str(raw), "--stdin", "--run", "ABC-2", stdin=json.dumps(data))
        self.ok("gate", "intake", "--run", "ABC-2")
        self.ok("run", "branch", "--run", "ABC-2")
        self.ok("env", "up", "web", "--run", "ABC-1")
        out = self.ok("env", "up", "web", "--run", "ABC-2")
        self.assertIn("shared with run ABC-1", out)
        self.slotted(shareable=False)
        err = self.fails("env", "up", "web", "--run", "ABC-2", code=3)
        self.assertIn("held by run ABC-1", err)


if __name__ == "__main__":
    unittest.main()
