"""Ticket consumers read `ticket.v1` contracts: intake gate, run start refs and bug classification, ticket refs of review.

Runs the real CLI end to end on the two synthetic profiles (`acme`, `zeta`: different raw shapes, different refs)."""

from __future__ import annotations

import json
import shutil
import unittest

from tests.provider_support import ProviderCase, cli, core

# profile -> (ticket sample, key, url of the ticket, ticket is a bug)
CASES = {
    "acme": ("1.json", "ACME-12", "https://acme.example/browse/ACME-12", True),
    "zeta": ("1.json", "ZED-7", "https://zeta.example/t/ZED-7", True),
}


class TicketIntakeTests(ProviderCase):
    def setup_profile(self, name: str) -> tuple[str, str, str, bool]:
        sample, key, url, bug = CASES[name]
        self.make_workspace({"tickets": name, "scm": name, "design": name})
        return sample, key, url, bug

    def start(self, name: str) -> tuple[str, str, str]:
        sample, key, url, _ = self.setup_profile(name)
        rc, out = cli("run", "start", "ws", key, "--feature")
        self.assertEqual(rc, 0, out)
        return f"ws-{key.lower()}", key, sample

    def test_missing_raw_prints_fetch_from_the_profile_hint(self):
        for name in CASES:
            with self.subTest(name):
                rid, key, _ = self.start(name)
                rc, out = cli("gate", rid, "intake")
                self.assertEqual(rc, 1)
                raw = (core.run_dir(rid) / "raw" / "ticket" / f"{key}.json").as_posix()
                self.assertIn(f"raw/ticket/{key}.<ext> missing", out)
                self.assertIn(f"FETCH ticket {key} -> {raw} | ", out)
                self.assertIn(f"{name}", out.split("FETCH ticket", 1)[1])  # the hint names the profile's own tool
                self.assertNotIn("remote/issue", out)

    def test_hint_in_json_shape(self):
        rid, key, _ = self.start("acme")
        rc, out = cli("--json", "gate", rid, "intake")
        self.assertEqual(rc, 1)
        item = json.loads(out)["fetch"][0]
        self.assertEqual({"kind", "id", "raw_path", "hint"}, set(item))
        self.assertEqual((item["kind"], item["id"]), ("ticket", key))
        self.assertEqual(item["hint"], f"acme-cli ticket get {key} --out {item['raw_path']}")

    def test_raw_without_contract_names_the_ingest_command_and_ingest_passes_the_gate(self):
        for name in CASES:
            with self.subTest(name):
                rid, key, sample = self.start(name)
                rd = core.run_dir(rid)
                (rd / "raw" / "ticket").mkdir(parents=True, exist_ok=True)
                shutil.copy(self.fixture(name, "ticket", sample), rd / "raw" / "ticket" / f"{key}.json")
                rc, out = cli("gate", rid, "intake")
                self.assertEqual(rc, 1)
                self.assertIn(f"factory ingest {rid} ticket raw/ticket/{key}.json", out)
                self.assertNotIn("FETCH", out)
                rc, out = cli("ingest", rid, "ticket", f"raw/ticket/{key}.json", "--id", key)
                self.assertEqual(rc, 0, out)
                rc, out = cli("gate", rid, "intake")
                self.assertEqual(rc, 1)  # the provider types it as a bug but this is a feature run
                self.assertIn("is_bug but this run is not a bug run", out)
                self.assertIn(f"{key}: not a bug - <reason>", out)
                self.assertEqual(cli("run", "decide", rid, f"{key}: not a bug - sample payload is a bug fixture",
                                     "--by", "user", "--user-said", "it is not a bug")[0], 0)
                rc, out = cli("gate", rid, "intake")
                if name == "acme":  # the sample lists an image attachment: intake wants it downloaded and recorded
                    self.assertEqual(rc, 1)
                    self.assertIn(f"FETCH attachment f1 -> {(rd / 'raw/attachment/f1-spinner.png').as_posix()} | ", out)
                    (rd / "raw" / "attachment").mkdir(parents=True)
                    (rd / "raw/attachment/f1-spinner.png").write_bytes(b"PNG0")
                    core.write_json(rd / "raw/attachment/manifest.json", {"items": [
                        {"name": "spinner.png", "id": "f1", "type": "image/png", "source": "u", "seen": True,
                         "file": "raw/attachment/f1-spinner.png"}]})
                    rc, out = cli("gate", rid, "intake")
                self.assertEqual(rc, 0, out)
                self.assertIn("ticket unknowns", out)
                self.assertIn(f"- {key} ", out)

    def test_bug_ticket_in_a_bug_run_passes_intake_without_a_decision(self):
        sample, key, _, _ = self.setup_profile("zeta")
        self.assertEqual(cli("run", "start", "ws", key, "--feature")[0], 0)
        rid = f"ws-{key.lower()}"
        rd = core.run_dir(rid)
        (rd / "raw" / "ticket").mkdir(parents=True, exist_ok=True)
        shutil.copy(self.fixture("zeta", "ticket", sample), rd / "raw" / "ticket" / f"{key}.json")
        self.assertEqual(cli("ingest", rid, "ticket", f"raw/ticket/{key}.json", "--id", key)[0], 0)
        self.assertEqual(cli("gate", rid, "intake")[0], 1)
        self.assertEqual(cli("run", "retype", rid, "--bug")[0], 0)
        run = core.run_load(rid)
        self.assertEqual(run["tickets"][0]["type"], "bug")
        from factory.commands import gate
        self.assertEqual(gate._bug_typing_errors(rid, run), [])

    def test_stale_contract_fails_with_the_reingest_command(self):
        rid, key, sample = self.start("acme")
        rd = core.run_dir(rid)
        (rd / "raw" / "ticket").mkdir(parents=True)
        shutil.copy(self.fixture("acme", "ticket", sample), rd / "raw" / "ticket" / f"{key}.json")
        self.assertEqual(cli("ingest", rid, "ticket", f"raw/ticket/{key}.json", "--id", key)[0], 0)
        (rd / "raw" / "ticket" / f"{key}.json").write_text('{"id": "ACME-12", "head": {}}\n', encoding="utf-8")
        rc, out = cli("gate", rid, "intake")
        self.assertEqual(rc, 1)
        self.assertIn("changed since ingest", out)
        self.assertIn(f"factory ingest {rid} ticket raw/ticket/{key}.json", out)

    def test_contract_for_another_ticket_fails(self):
        rid, key, _ = self.start("acme")
        from tests.ticket_support import put_ticket
        path = put_ticket(rid, key)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["key"] = "ACME-99"
        path.write_text(json.dumps(doc), encoding="utf-8")
        rc, out = cli("gate", rid, "intake")
        self.assertEqual(rc, 1)
        self.assertIn("is for ticket 'ACME-99'", out)

    def test_run_start_accepts_a_ticket_url_and_derives_the_bug_type(self):
        for name in CASES:
            with self.subTest(name):
                sample, key, url, bug = self.setup_profile(name)
                rid = f"ws-{key.lower()}"
                rd = core.run_dir(rid)
                (rd / "raw" / "ticket").mkdir(parents=True, exist_ok=True)
                shutil.copy(self.fixture(name, "ticket", sample), rd / "raw" / "ticket" / f"{key}.json")
                # ingest needs run.json: create the contract through a throwaway run, then move it into place
                self.make_run(rid)
                self.assertEqual(cli("ingest", rid, "ticket", f"raw/ticket/{key}.json", "--id", key)[0], 0)
                (rd / "run.json").unlink()
                rc, out = cli("run", "start", "ws", url)
                self.assertEqual(rc, 0, out)
                run = core.run_load(rid)
                self.assertEqual(run["tickets"][0]["key"], key)
                self.assertEqual(run["tickets"][0]["type"], "bug" if bug else "feature")
                self.assertIn("ticket contract is_bug", out)

    def test_run_start_refuses_what_the_profile_cannot_parse(self):
        self.setup_profile("acme")
        rc, out = cli("run", "start", "ws", "https://other.example/browse/X-1")
        self.assertEqual(rc, 2)
        self.assertIn("is not a ticket key or URL of profile 'acme'", out)
        rc, out = cli("run", "start", "ws", "ZED-3")
        self.assertEqual(rc, 0, out)  # a key of the profile pattern: ZED-3 matches [A-Z][A-Z0-9]*-\d+

    def test_run_start_without_a_tickets_profile_takes_plain_keys_only(self):
        self.make_workspace({})
        self.assertEqual(cli("run", "start", "ws", "dev-5")[0], 0)
        self.assertEqual(core.run_load("ws-dev-5")["tickets"][0]["key"], "DEV-5")
        rc, out = cli("run", "start", "ws", "https://acme.example/browse/ACME-1")
        self.assertEqual(rc, 2)
        self.assertIn("providers.tickets", out)


class TicketRefsTests(ProviderCase):
    def test_review_ticket_key_uses_the_profile_refs(self):
        from factory.commands.review import ticket_key
        facts = {"title": "ACME-1 and ZED-5 thing", "source_branch": "x", "description": ""}
        self.make_workspace({"tickets": "zeta"})
        ws = core.workspace_load("ws")
        self.assertEqual(ticket_key(ws, facts), "ZED-5")
        self.make_workspace({"tickets": "acme"})
        self.assertEqual(ticket_key(core.workspace_load("ws"), facts), "ACME-1")
        self.make_workspace({})
        self.assertIsNone(ticket_key(core.workspace_load("ws"), facts))
        facts2 = {"title": "no key", "source_branch": "feature/acme-9-x", "description": ""}
        self.make_workspace({"tickets": "acme"})
        self.assertEqual(ticket_key(core.workspace_load("ws"), facts2), "ACME-9")


if __name__ == "__main__":
    unittest.main()
