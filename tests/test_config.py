"""factory.toml: validation, discovery, workspace inheritance, local.toml, secrets."""
import json
import os
import unittest

from tests.helpers import FactoryCase, copy_git_fixture, lesson
from dev_factory import config, project
from dev_factory.util import FactoryError


class ValidationTests(FactoryCase):
    @lesson("L-156")
    def test_unknown_keys_are_rejected_with_their_key_path(self):
        """L-156: unknown keys and wrong types fail with the key path, all at once, exit 2."""
        self.write("factory.toml", self.TOML + '\n[checks]\ncmd_timeout = 5\ntail_lines = "x"\n[repo_extra]\na = 1\n')
        err = self.fails("check")
        for needle in ("checks.cmd_timeout: unknown key", "checks.tail_lines: must be an integer", "repo_extra: unknown key"):
            self.assertIn(needle, err)

    def test_schema_version_is_required(self):
        self.write("factory.toml", self.TOML.replace("schema = 1\n", ""))
        self.assertIn("schema: required", self.fails("check"))

    def test_defaults_are_filled(self):
        cfg = config.load(self.proj)
        self.assertEqual(cfg["checks"]["cmd_timeout_s"], 600)
        self.assertEqual(cfg["repo"][0]["base"], "main")
        self.assertEqual(cfg["autonomy"], {"max_loops": 3, "ship": "ask"})
        self.assertEqual(cfg["limits"]["approx"]["color_delta_e"], 6)
        self.assertEqual(cfg["repo"][0]["branch"], "{kind}/{key}-{slug}")

    @lesson("L-142")
    def test_empty_ship_title_falls_back_to_the_default(self):
        """L-142: an empty title format never yields a doubled key; it falls back to `[key] - summary`."""
        self.write("factory.toml", self.TOML + '\n[ship]\ntitle = ""\n')
        self.assertEqual(config.load(self.proj)["ship"]["title"], "[{key}] - {summary}")

    @lesson("L-143")
    def test_reviewers_need_both_identities(self):
        """L-143: reviewers carry separate SCM and tracker identities, validated at load."""
        self.write("factory.toml", self.TOML + '\n[ship]\nreviewers = [{ scm = "ana" }]\n')
        self.assertIn("scm and tracker", self.fails("check"))

    def test_transition_needs_a_target_and_enums_are_checked(self):
        self.write("factory.toml", self.TOML + '\n[ship]\nticket = "transition"\n')
        self.assertIn("transition_to", self.fails("check"))
        self.write("factory.toml", self.TOML + '\n[autonomy]\nship = "auto"\nmax_loops = 11\n')
        err = self.fails("check")
        self.assertIn("autonomy.ship", err)
        self.assertIn("autonomy.max_loops", err)

    def test_env_needs_up_and_a_loopback_base_url(self):
        self.write("factory.toml", self.TOML + '\n[repo.env]\nbase_url = "http://example.org:80"\n')
        err = self.fails("check")
        self.assertIn("env.up: required", err)
        self.assertIn("loopback", err)

    def test_invalid_regex_and_secret_key_names(self):
        self.write("factory.toml", self.TOML.replace('name = "web"', 'name = "web"\ncommit_pattern = "("') + '\n[secrets]\nkeys = ["bad-key"]\n')
        err = self.fails("check")
        self.assertIn("commit_pattern", err)
        self.assertIn("bad-key", err)

    def test_invalid_toml_is_a_clean_error(self):
        self.write("factory.toml", "schema = = 1")
        self.assertIn("invalid TOML", self.fails("check"))


class DiscoveryTests(FactoryCase):
    @lesson("L-141")
    def test_discovery_climbs_from_a_subdirectory_and_project_overrides(self):
        """L-141: the CLI works from any cwd; --project overrides the climb."""
        sub = self.proj / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(project.discover(None, sub).dir, self.proj.resolve())
        self.assertIn("project demo", self.ok("status", "--config", cwd=sub))
        other = self.base / "elsewhere"
        other.mkdir()
        self.assertIn("no factory.toml found", self.fails("status", "--config", cwd=other, project=False))
        self.assertIn("project demo", self.ok("status", "--config", "--project", str(self.proj), cwd=other, project=False))

    def test_explicit_project_without_a_config_is_an_error(self):
        self.assertIn("no factory.toml", self.fails("check", "--project", str(self.base)))

    def test_nearest_file_wins(self):
        inner = self.proj / "member"
        self.write("factory.toml", self.TOML.replace('"demo"', '"inner"'), root=inner)
        self.assertEqual(project.discover(None, inner).cfg["project"]["name"], "inner")


class WorkspaceTests(FactoryCase):
    API = ('schema = 1\n[project]\nname = "shop-api"\n[ship]\ndraft = false\n[[repo]]\npath = "."\nname = "api"\nsetup = "restore.sh"\n'
           'files_from_main = ["a.json"]\n[repo.env]\nup = "run-api.sh"\nbase_url = "http://127.0.0.1:5000"\nhealth = "/health"\n'
           '[[repo.check]]\nid = "unit"\nrun = "test.sh"\n[secrets]\nkeys = ["DB_PASSWORD"]\n')
    WEB = ('schema = 1\n[project]\nname = "shop-web"\n[[repo]]\npath = "."\nname = "web"\nsetup = "install.sh"\n'
           '[repo.env]\nup = "run-web.sh"\nbase_url = "http://127.0.0.1:5173"\n'
           '[[repo.check]]\nid = "lint"\nrun = "lint.sh"\n[[repo.check]]\nid = "unit"\nrun = "web-unit.sh"\n')
    WS = ('schema = 1\n[project]\nname = "shop"\n[[repo]]\npath = "../api"\nbase = "release/2.x"\n'
          '[[repo]]\npath = "../web"\n[repo.env]\ndepends_on = ["api"]\n[[repo.check]]\nid = "lint"\nrun = "lint.sh --strict"\n'
          '[secrets]\nkeys = ["TEST_USER"]\n')

    def setUp(self):
        super().setUp()
        self.ws = self.base / "code" / "shop"
        self.write("factory.toml", self.WS, root=self.ws)
        self.write("factory.toml", self.API, root=self.base / "code" / "api")
        self.write("factory.toml", self.WEB, root=self.base / "code" / "web")
        self.cfg = config.load(self.ws)

    def repo(self, name):
        return next(r for r in self.cfg["repo"] if r["name"] == name)

    def test_member_values_are_the_base_and_the_workspace_overrides_key_by_key(self):
        """A member's entry is the base; the workspace overrides only what it declares."""
        api, web = self.repo("api"), self.repo("web")
        self.assertEqual(self.cfg["_meta"]["mode"], "workspace")
        self.assertEqual((api["base"], api["setup"], api["files_from_main"]), ("release/2.x", "restore.sh", ["a.json"]))
        self.assertEqual(api["env"]["health"], "/health")
        self.assertEqual((web["setup"], web["base"]), ("install.sh", "main"))
        self.assertEqual(web["env"]["up"], "run-web.sh")
        self.assertEqual(web["env"]["depends_on"], ["api"])
        self.assertEqual(web["env"]["base_url"], "http://127.0.0.1:5173")

    def test_checks_merge_by_id_and_the_workspace_replaces_a_same_id_check(self):
        web = self.repo("web")
        self.assertEqual([(c["id"], c["run"]) for c in web["check"]],
                         [("lint", "lint.sh --strict"), ("unit", "web-unit.sh")])

    def test_secret_keys_are_the_union_and_member_sections_do_not_leak(self):
        self.assertEqual(self.cfg["secrets"]["keys"], ["DB_PASSWORD", "TEST_USER"])
        self.assertTrue(self.cfg["ship"]["draft"], "a member's [ship] applies only when the member is used alone")
        alone = config.load(self.base / "code" / "api")
        self.assertFalse(alone["ship"]["draft"])
        self.assertEqual(alone["_meta"]["mode"], "repo")

    def test_lists_are_replaced_not_merged(self):
        self.write("factory.toml", self.WS.replace('base = "release/2.x"', 'files_from_main = ["b.json"]'), root=self.ws)
        self.assertEqual(config.load(self.ws)["repo"][0]["files_from_main"], ["b.json"])

    def test_each_repo_names_its_own_base_branch(self):
        """L-94: each repo carries its own base branch."""
        self.assertEqual({r["name"]: r["base"] for r in self.cfg["repo"]}, {"api": "release/2.x", "web": "main"})

    def test_duplicate_repo_names_and_unknown_depends_on_fail(self):
        self.write("factory.toml", self.WS.replace('depends_on = ["api"]', 'depends_on = ["ghost"]'), root=self.ws)
        with self.assertRaises(FactoryError) as cm:
            config.load(self.ws)
        self.assertIn("ghost", str(cm.exception))


class LocalTomlTests(FactoryCase):
    ENV = '\n[repo.env]\nup = "serve.sh"\nbase_url = "http://127.0.0.1:5173"\nvars = { A = "1" }\n'

    @lesson("L-131")
    def test_local_toml_may_only_set_machine_facts(self):
        """L-131: any key that could loosen proof (limits, checks timeouts, commands) is an error in local.toml."""
        for body in ('[limits]\nmax_diff_ratio = 0.9\n', '[checks]\ncmd_timeout_s = 1\n', '[[repo.check]]\nid = "x"\nrun = "y"\n',
                     '[repo.web]\nsetup = "evil.sh"\n', '[repo.web.env]\nup = "other.sh"\n'):
            self.write(".factory/local.toml", body)
            err = self.fails("check")
            self.assertIn("local.toml", err, body)
            self.assertRegex(err, "unknown key|must be", body)

    def test_local_overrides_ports_paths_and_worktree_root(self):
        self.write("factory.toml", self.TOML + self.ENV)
        self.write(".factory/local.toml", 'worktree_root = "wt"\n[repo.web.env]\nbase_url = "http://127.0.0.1:5999"\n'
                   'vars = { B = "2" }\n[repo.web.slots]\nport_offset = 10\n')
        cfg = config.load(self.proj)
        self.assertEqual(cfg["repo"][0]["env"]["base_url"], "http://127.0.0.1:5999")
        self.assertEqual(cfg["repo"][0]["env"]["vars"], {"A": "1", "B": "2"})
        self.assertEqual(cfg["repo"][0]["slots"]["port_offset"], 10)
        self.assertEqual(config.worktree_root(cfg), (self.proj / "wt").resolve())

    def test_local_repo_path_relocates_a_workspace_member(self):
        ws = self.base / "ws"
        self.write("factory.toml", 'schema = 1\n[project]\nname = "ws"\n[[repo]]\npath = "../gone"\nname = "gone"\n', root=ws)
        self.write(".factory/local.toml", '[repo.gone]\npath = "../here"\n', root=ws)
        self.write("factory.toml", self.TOML.replace('"web"', '"gone"'), root=self.base / "here")
        self.assertEqual(config.load(ws)["repo"][0]["path"], str((self.base / "here").resolve()))

    def test_local_naming_an_unknown_repo_fails(self):
        self.write(".factory/local.toml", '[repo.ghost]\npath = "x"\n')
        self.assertIn("no such repo", self.fails("check"))

    def test_default_worktree_root_is_outside_the_repo(self):
        root = config.worktree_root(config.load(self.proj))
        self.assertNotIn(self.proj.resolve(), root.parents)
        self.assertEqual(root.name, "demo")


class SecretsTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.write("factory.toml", self.TOML + '\n[secrets]\nkeys = ["TRACKER_TOKEN", "OTHER_KEY"]\n')
        self.secret_file = config.secrets_file("demo")

    @lesson("L-129", "L-80")
    def test_values_come_from_env_then_file_and_never_show_in_output(self):
        """L-129, L-80: keys are names in factory.toml; values come from the environment or the user file and are never printed."""
        self.secret_file.parent.mkdir(parents=True)
        self.secret_file.write_text('# comment\nTRACKER_TOKEN="from-file-value"\nOTHER_KEY=file-other\n')
        os.environ["TRACKER_TOKEN"] = "from-env-value"
        self.addCleanup(os.environ.pop, "TRACKER_TOKEN", None)
        values, missing = config.resolve_secrets(config.load(self.proj))
        self.assertEqual(values, {"TRACKER_TOKEN": "from-env-value", "OTHER_KEY": "file-other"})
        self.assertEqual(missing, [])
        out = self.ok("status", "--config") + self.ok("status", "--config", "--json") + self.ok("check")
        for value in ("from-env-value", "from-file-value", "file-other"):
            self.assertNotIn(value, out)
        self.assertIn("secret TRACKER_TOKEN: set", out)

    def test_check_reports_missing_keys_by_name_only(self):
        out = self.ok("check")
        self.assertIn("TRACKER_TOKEN", out)
        self.assertIn("OTHER_KEY", out)
        self.assertEqual(json.loads(self.ok("check", "--json"))["errors"], [])

    def test_secret_values_in_files_under_the_project_are_not_written_by_init(self):
        fresh = self.base / "fresh"
        os.environ["TRACKER_TOKEN"] = "super-secret-1234"
        self.addCleanup(os.environ.pop, "TRACKER_TOKEN", None)
        self.ok("init", "--project", str(fresh), project=False)
        for p in fresh.rglob("*"):
            if p.is_file():
                self.assertNotIn("super-secret-1234", p.read_text(errors="replace"))


class CheckTests(FactoryCase):
    def test_check_fails_when_the_cli_is_outside_the_project_pin(self):
        """L-149: a project can pin the CLI version; check fails outside the range."""
        self.write("factory.toml", self.TOML.replace('name = "demo"', 'name = "demo"\ncli = ">=9.0"'))
        self.assertIn("requires >=9.0", self.fails("check"))
        self.write("factory.toml", self.TOML.replace('name = "demo"', 'name = "demo"\ncli = ">=1.0.0a1,<1.1"'))
        self.ok("check")

    def test_unknown_placeholder_in_an_adapter_template_fails_check(self):
        self.write("factory.toml", self.TOML + "\n[providers.ticket]\nfetch = 'tool {id} {bogus}'\n")
        self.assertIn("{bogus}", self.fails("check"))

    def test_missing_repo_path_fails_check_and_workspace_repo_is_found_in_a_fixture_repo(self):
        ws = self.base / "ws"
        self.write("factory.toml", 'schema = 1\n[project]\nname = "ws"\n[[repo]]\npath = "../r1"\nname = "r1"\n', root=ws)
        self.assertIn("does not exist", self.fails("check", "--project", str(ws), project=False))
        copy_git_fixture(self.base / "r1")
        self.ok("check", "--project", str(ws), project=False)


if __name__ == "__main__":
    unittest.main()
