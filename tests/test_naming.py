"""Branch and change-request title templates with ticket fields: contract, config, intake, branch, title, check, setup."""
import unittest

from dev_factory import contracts, naming
from dev_factory.util import FactoryError
from tests.helpers import FactoryCase, git, valid_ticket
from tests.m2_support import M2Case
from tests.m3_support import ShipCase

VERSIONED = '\n[ship]\ntitle = "[{version}] - [{key}] - {summary}"\n'


class TemplateTests(unittest.TestCase):
    def test_built_in_placeholders_win_and_other_names_are_ticket_fields(self):
        self.assertEqual(naming.fields_of("{kind}/{version}/{key}-{slug}", "branch"), ["version"])
        self.assertEqual(naming.fields_of("[{key}] - {summary}", "title"), [])
        ticket = valid_ticket(fields={"version": "PACK_9.1"})
        cfg = {"ship": {"title": "[{version}] - [{key}] - {summary}"}}
        self.assertEqual(naming.title(cfg, ticket), "[PACK_9.1] - [ABC-1] - Show overdue invoices")

    def test_a_missing_field_is_a_clear_error_and_never_an_empty_tag(self):
        with self.assertRaises(FactoryError) as cm:
            naming.title({"ship": {"title": "[{version}] - {summary}"}}, valid_ticket())
        self.assertIn("fields.version", str(cm.exception))

    def test_only_plain_placeholders_are_accepted(self):
        for bad in ("{key:>5}", "{fields[version]}", "{a.b}", "{key", "{}", "{Version}"):
            self.assertTrue(naming.problems(bad, "title", "ship.title"), bad)
        self.assertEqual(naming.problems("{{literal}} [{key}] {release_2}", "title", "ship.title"), [])


class ContractTests(unittest.TestCase):
    def test_ticket_fields_are_optional_named_non_empty_strings(self):
        self.assertEqual(contracts.validate("ticket", valid_ticket(fields={"version": "PACK_9.1"}))["fields"], {"version": "PACK_9.1"})
        self.assertNotIn("fields", contracts.validate("ticket", valid_ticket()))
        for bad in ({"version": ""}, {"version": 9}, {"Version": "x"}, {"key": "x"}, ["x"]):
            with self.assertRaises(FactoryError, msg=bad):
                contracts.validate("ticket", valid_ticket(fields=bad))


class ConfigTests(FactoryCase):
    def test_check_rejects_a_broken_template_and_names_the_fields_a_template_needs(self):
        self.write("factory.toml", self.TOML + 'branch = "{kind}/{key:>3}"\n')
        self.assertIn("repo[web].branch", self.fails("check"))
        self.write("factory.toml", self.TOML + 'branch = "{kind}/{version}/{key}-{slug}"\n' + VERSIONED)
        out = self.ok("check")
        self.assertIn("ticket field version is used by ship.title, repo web branch", out)

    def test_the_default_templates_need_no_field(self):
        self.assertNotIn("ticket field", self.ok("check"))


class BranchTests(M2Case):
    TOML = M2Case.TOML.replace('name = "web"\n', 'name = "web"\nbranch = "{kind}/{version}/{key}-{slug}"\n', 1)

    def test_intake_asks_for_the_field_and_the_branch_carries_it(self):
        run = self.start("ABC-1")
        self.ingest_ticket(run, id="ABC-1")
        self.assertIn("lacks fields.version, which repo web branch use", self.fails("gate", "intake", code=1))
        self.ingest_ticket(run, id="ABC-1", fields={"version": "PACK_9.1"})
        self.ok("gate", "intake")
        self.ok("run", "branch")
        self.assertEqual(git("branch", "--show-current", cwd=self.tree()), "feature/PACK_9.1/ABC-1-show-overdue-invoices")

    def test_a_template_changed_after_intake_does_not_reopen_the_run(self):
        self.write("factory.toml", M2Case.TOML)
        run = self.start("ABC-1")
        self.ingest_ticket(run, id="ABC-1")
        self.ok("gate", "intake")
        self.write("factory.toml", self.TOML)
        self.assertNotIn("phase intake", self.ok("next"))


class TitleTests(ShipCase):
    def test_a_title_field_the_ticket_lacks_stops_the_ship_plan_with_the_field_named(self):
        self.to_ship()
        self.write("factory.toml", self.TOML + VERSIONED)
        self.assertIn("fields.version", self.fails("ship", "plan"))


class SetupTests(unittest.TestCase):
    def test_the_naming_step_writes_only_what_differs_from_the_default(self):
        doc = {"repo": [{"name": "a"}, {"name": "b", "branch": "x/{key}"}], "ship": {}}
        self.assertEqual([q["id"] for q in naming.questions(doc, set())], ["naming.branch", "naming.title"])
        self.assertEqual([q["id"] for q in naming.questions(doc, {"naming.branch"})], ["naming.title"])
        naming.apply(doc, "naming.branch", naming.DEFAULTS["branch"])
        self.assertEqual([r.get("branch") for r in doc["repo"]], [None, None])
        naming.apply(doc, "naming.title", "[{version}] - [{key}] - {summary}")
        self.assertEqual(doc["ship"]["title"], "[{version}] - [{key}] - {summary}")
        with self.assertRaises(FactoryError):
            naming.apply(doc, "naming.title", "{key")


class ConfigSetTests(unittest.TestCase):
    def test_a_repo_name_with_dots_is_addressed_whole(self):
        from dev_factory.cli_home import key_path
        self.assertEqual(key_path("repo.app.ai.branch", ["app", "app.ai"])[:2], ("app.ai", ["branch"]))
        self.assertEqual(key_path("repo.app.env.up", ["app", "app.ai"])[:2], ("app", ["env", "up"]))


if __name__ == "__main__":
    unittest.main()
