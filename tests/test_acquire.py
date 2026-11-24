"""fetch (user adapter) and ingest (agent route): raw storage, hashes, exit codes, isolation."""
import json
import os
import time
import unittest

from tests.helpers import FactoryCase, adapter_scenario, lesson, provider_toml, valid_ticket
from dev_factory import proc

TICKET_OUT = json.dumps({"contract": valid_ticket(), "raw": {"provider": "payload", "n": 1}}, indent=1)


class FetchTests(FactoryCase):
    def fetch_cfg(self, **scenario):
        scenario.setdefault("stdout", TICKET_OUT)
        self.write("factory.toml", provider_toml(adapter_scenario(self, **scenario)))
        return self.start("ABC-1")

    @lesson("L-121", "L-122")
    def test_fetch_stores_stdout_byte_for_byte_as_raw_with_its_hash_and_source_adapter(self):
        """L-121, L-122: the contract records source adapter and the sha256 of the untouched raw."""
        run = self.fetch_cfg()
        self.ok("fetch", "ticket", "ABC-1")
        raw = (run / "raw" / "ticket" / "ABC-1.json").read_bytes()
        self.assertEqual(raw, TICKET_OUT.encode())
        env = json.loads((run / "contracts" / "ticket" / "ABC-1.json").read_text())
        import hashlib
        self.assertEqual((env["schema"], env["source"], env["raw"]["path"]), ("ticket/1", "adapter", "raw/ticket/ABC-1.json"))
        self.assertEqual(env["raw"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(env["adapter"]["exit"], 0)
        self.assertEqual(env["data"]["title"], "Show overdue invoices")
        ev = self.events(run)[-1]
        self.assertEqual((ev["type"], ev["kind"], ev["id"], ev["source"], ev["raw_sha256"]), ("contract", "ticket", "ABC-1", "adapter", env["raw"]["sha256"]))

    def test_invalid_adapter_output_writes_nothing(self):
        run = self.fetch_cfg(stdout=json.dumps({"contract": valid_ticket(type="story")}))
        self.assertIn("data.type", self.fails("fetch", "ticket", "ABC-1"))
        self.assertFalse((run / "raw").exists())
        self.assertFalse((run / "contracts").exists())
        self.assertEqual(len(self.events(run)), 1)

    def test_output_that_is_not_one_object_with_a_contract_is_refused(self):
        for out in ("not json", "[1]", json.dumps({"data": {}}), json.dumps({"contract": {}, "extra": 1})):
            run = self.fetch_cfg(stdout=out)
            err = self.fails("fetch", "ticket", "ABC-1")
            self.assertRegex(err, "not one JSON object|must be exactly")
            self.ok("run", "delete", "ABC-1", "--yes")

    def test_exit_codes_not_found_auth_and_other(self):
        self.fetch_cfg(exit=3)
        self.assertIn("no such ticket ABC-1", self.fails("fetch", "ticket", "ABC-1", code=1))
        self.ok("run", "delete", "ABC-1", "--yes")
        self.write("factory.toml", provider_toml(adapter_scenario(self, exit=4), extra='secrets = ["TRACKER_TOKEN"]\n') + '\n[secrets]\nkeys = ["TRACKER_TOKEN"]\n')
        self.start("ABC-1")
        err = self.fails("fetch", "ticket", "ABC-1", code=1)
        self.assertIn("TRACKER_TOKEN", err)
        self.ok("run", "delete", "ABC-1", "--yes")
        self.fetch_cfg(exit=7, stderr="\n".join(f"line {i}" for i in range(30)))
        err = self.fails("fetch", "ticket", "ABC-1", code=1)
        self.assertIn("adapter failed (exit 7)", err)
        self.assertIn("line 29", err)
        self.assertNotIn("line 5\n", err)

    @lesson("L-42", "L-43")
    def test_adapter_timeout_kills_the_process_tree_and_reports_timeout(self):
        """L-42, L-43: the adapter and the process it started are gone after timeout_s."""
        pid_file = self.base / "grandchild.pid"
        self.write("factory.toml", provider_toml(adapter_scenario(self, sleep=120, grandchild_pid_file=pid_file.as_posix()), extra="timeout_s = 2\n"))
        self.start("ABC-1")
        err = self.fails("fetch", "ticket", "ABC-1", code=1)
        self.assertIn("timed out after 2s", err)
        pid = int(pid_file.read_text())
        for _ in range(50):
            if not proc.pid_alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(proc.pid_alive(pid))

    @lesson("L-44", "L-45", "L-80")
    def test_adapter_runs_with_closed_stdin_english_locale_secrets_in_env_only_and_no_shell(self):
        """L-44, L-45, L-80, L-130: stdin is closed, the locale is English, secrets are env vars never argv, no shell."""
        dump = self.base / "dump.json"
        os.environ["TRACKER_TOKEN"] = "tok-1234-secret"
        self.addCleanup(os.environ.pop, "TRACKER_TOKEN", None)
        template = adapter_scenario(self, stdout=TICKET_OUT, dump=dump.as_posix(), read_stdin=True).replace("{id}", "{id} & echo | %PATH%")
        self.write("factory.toml", provider_toml(template, extra='secrets = ["TRACKER_TOKEN"]\n') + '\n[secrets]\nkeys = ["TRACKER_TOKEN"]\n')
        run = self.start("ABC-1")
        self.ok("fetch", "ticket", "ABC-1")
        seen = json.loads(dump.read_text())
        self.assertEqual(seen["stdin_len"], 0)
        self.assertEqual((seen["env"]["LC_ALL"], seen["env"]["FACTORY_KIND"], seen["env"]["FACTORY_ID"]), ("C", "ticket", "ABC-1"))
        self.assertEqual(seen["env"]["FACTORY_RUN_DIR"], run.as_posix())
        self.assertEqual(seen["env"]["TRACKER_TOKEN"], "tok-1234-secret")
        self.assertNotIn("tok-1234-secret", " ".join(seen["argv"]))
        self.assertEqual(seen["argv"][1:], ["ABC-1", "&", "echo", "|", "%PATH%"])
        stored = (run / "contracts" / "ticket" / "ABC-1.json").read_text()
        self.assertNotIn("tok-1234-secret", stored)

    @lesson("L-80")
    def test_secret_values_are_redacted_from_stored_commands_and_error_tails(self):
        """L-80: a secret that leaks into stderr or the command is stored and shown as ***."""
        os.environ["TRACKER_TOKEN"] = "tok-5678-secret"
        self.addCleanup(os.environ.pop, "TRACKER_TOKEN", None)
        self.write("factory.toml", provider_toml(adapter_scenario(self, exit=9, stderr="denied for tok-5678-secret"),
                                                 extra='secrets = ["TRACKER_TOKEN"]\n') + '\n[secrets]\nkeys = ["TRACKER_TOKEN"]\n')
        self.start("ABC-1")
        err = self.fails("fetch", "ticket", "ABC-1", code=1)
        self.assertIn("denied for ***", err)
        self.assertNotIn("tok-5678-secret", err)

    def test_fetch_without_an_adapter_points_at_ingest(self):
        self.start("ABC-1")
        self.assertIn("factory ingest ticket --raw", self.fails("fetch", "ticket", "ABC-1"))

    def test_fetch_of_a_kind_with_no_validator_is_refused(self):
        self.write("factory.toml", provider_toml(adapter_scenario(self), kind="design_frame"))
        self.start("ABC-1")
        self.assertIn("not supported in this build", self.fails("fetch", "design_frame", "F-1"))

    @lesson("L-157")
    def test_two_contract_ids_mapping_to_one_file_name_are_an_error_naming_both(self):
        """L-157: provider ids that collide as file names are refused."""
        run = self.start("ABC-1")
        self.ingest_ticket(run, id="ABC-1")
        raw = run / "raw" / "other.json"
        raw.write_text("{}")
        self.fails("ingest", "ticket", "--raw", str(raw), "--stdin", stdin=json.dumps(valid_ticket(id="ABC/1")))
        run2 = self.start("X-1")
        for tid in ("g/4", "g:4"):
            raw = run2 / f"{tid.replace('/', '_').replace(':', '_')}.json"
            raw.write_text("{}")
            code, _, err = self.cli("ingest", "ticket", "--raw", str(raw), "--stdin", "--run", "X-1", stdin=json.dumps(valid_ticket(id=tid)))
        self.assertEqual(code, 2)
        self.assertIn("'g/4' and 'g:4'", err)


class IngestTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.start("ABC-1")

    @lesson("L-121")
    def test_ingest_records_source_agent_and_reads_the_data_from_stdin(self):
        """L-121, L-123, L-159: the agent route is explicit (`source: agent`); --stdin keeps payloads out of chat."""
        self.ingest_ticket(self.run_dir)
        env = json.loads((self.run_dir / "contracts" / "ticket" / "ABC-1.json").read_text())
        self.assertEqual((env["source"], env["raw"]["path"]), ("agent", "raw/ticket/ABC-1.json"))
        self.assertNotIn("adapter", env)

    def test_ingest_copies_a_raw_from_elsewhere_and_reads_a_contract_file(self):
        raw = self.base / "provider-dump.html"
        raw.write_text("<p>hi</p>")
        data = self.write("data.json", json.dumps(valid_ticket()), root=self.base)
        self.ok("ingest", "ticket", "--raw", str(raw), "--contract", str(data))
        self.assertEqual((self.run_dir / "raw" / "ticket" / "ABC-1.html").read_text(), "<p>hi</p>")

    def test_invalid_contract_writes_nothing_and_exits_2(self):
        raw = self.write("r.json", "{}", root=self.base)
        err = self.fails("ingest", "ticket", "--raw", str(raw), "--stdin", stdin=json.dumps(valid_ticket(type=None)))
        self.assertIn("rejected (nothing written)", err)
        self.assertFalse((self.run_dir / "contracts").exists())
        self.assertEqual(len(self.events(self.run_dir)), 1)

    def test_usage_errors(self):
        self.assertIn("usage: factory ingest", self.fails("ingest", "ticket"))
        self.assertIn("raw file not found", self.fails("ingest", "ticket", "--raw", str(self.base / "nope.json"), "--stdin", stdin="{}"))
        raw = self.write("r.json", "{}", root=self.base)
        self.assertIn("not valid JSON", self.fails("ingest", "ticket", "--raw", str(raw), "--stdin", stdin="{oops"))

    @lesson("L-122")
    def test_a_raw_changed_after_acquisition_is_reported_stale_by_ingest_check_and_the_gate(self):
        """L-122: the contract records the raw hash; ingest --check and gate intake fail when the raw differs."""
        self.ingest_ticket(self.run_dir)
        self.assertIn("match", self.ok("ingest", "--check"))
        self.ok("gate", "intake")
        (self.run_dir / "raw" / "ticket" / "ABC-1.json").write_text('{"payload": "tampered"}')
        code, out, _ = self.cli("ingest", "--check")
        self.assertEqual(code, 1)
        self.assertIn("ticket ABC-1: raw changed since it was acquired", out)
        code, out, _ = self.cli("gate", "intake")
        self.assertEqual(code, 1)
        self.assertIn("raw changed", out)
        self.assertEqual(json.loads(self.ok("status", "--json"))["phase"], "intake")

    def test_a_missing_raw_is_reported(self):
        self.ingest_ticket(self.run_dir)
        (self.run_dir / "raw" / "ticket" / "ABC-1.json").unlink()
        self.assertIn("raw file raw/ticket/ABC-1.json is missing", self.fails("ingest", "--check", code=1))


if __name__ == "__main__":
    unittest.main()
