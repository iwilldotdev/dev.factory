"""`factory setup`: workspace discovery facts and the config checklist; it reads only and never prints a secret."""
import json
import shutil
import unittest

from tests.helpers import FactoryCase, copy_git_fixture, git
from dev_factory import guide


class DiscoverTests(FactoryCase):
    def make_repo(self, name: str):
        repo = copy_git_fixture(self.base / "ws" / name)
        git("remote", "add", "origin", "https://bot:s3cr3t-token@scm.example.test/shop/" + name + ".git", cwd=repo)
        git("branch", "develop", cwd=repo)
        return repo

    def test_discover_lists_every_repo_with_remotes_without_credentials_stacks_ports_and_env_files(self):
        api, web = self.make_repo("api"), self.make_repo("web")
        self.write("package.json", json.dumps({"scripts": {"dev": "serve --port 5173", "test": "run-tests", "lint": "run-lint"}}), web)
        self.write(".env.example", "API_URL=\n", web)
        self.write("Makefile", "build:\n\techo b\ntest: build\n\techo t\n", api)
        self.write("Api/Properties/launchSettings.json", '{"profiles": {"a": {"applicationUrl": "http://localhost:5000"}}}', api)
        self.write("Api/Api.csproj", "<Project/>", api)
        self.write("Api.Tests/Api.Tests.csproj", "<Project/>", api)
        self.write(".ci/workflows/main.yml", "jobs:\n  t:\n    steps:\n      - run: make test\n", api)
        out = self.ok("setup", "--discover", str(self.base / "ws"), project=False)
        self.assertNotIn("s3cr3t", out)
        self.assertNotIn("bot", out)
        data = json.loads(out)
        self.assertFalse(data["configured"])
        by = {r["name"]: r for r in data["repos"]}
        self.assertEqual(sorted(by), ["api", "web"])
        self.assertEqual(by["api"]["remotes"], [{"name": "origin", "host": "scm.example.test", "path": "shop/api.git"}])
        self.assertEqual((by["api"]["current_branch"], by["api"]["likely_bases"]), ("main", ["develop", "main"]))
        stacks = {s["stack"]: s for s in by["web"]["stacks"]}
        self.assertEqual(stacks["node"]["check_candidates"], ["test", "lint"])
        self.assertEqual(by["web"]["env_files"], [".env.example"])
        self.assertIn({"file": "package.json", "ports": ["5173"]}, by["web"]["ports"])
        api_stacks = {s["stack"]: s for s in by["api"]["stacks"]}
        self.assertEqual(api_stacks["make"]["check_candidates"], ["make build", "make test"])
        self.assertEqual(api_stacks["csharp"]["test_projects"], ["Api.Tests/Api.Tests.csproj"])
        self.assertEqual(by["api"]["ports"], [{"file": "Api/Properties/launchSettings.json", "ports": ["5000"]}])
        self.assertEqual(by["api"]["ci"], [{"file": ".ci/workflows/main.yml", "steps": ["make test"]}])

    def test_remote_urls_lose_their_credentials(self):
        self.assertEqual(guide.remote_of("git@scm.example.test:shop/api.git"), {"host": "scm.example.test", "path": "shop/api.git"})
        self.assertEqual(guide.remote_of("ssh://u:p@scm.example.test:22/shop/api"), {"host": "scm.example.test", "path": "shop/api"})
        self.assertEqual(guide.remote_of("https://oauth2:tok@scm.example.test/a/b.git")["host"], "scm.example.test")


class ChecklistTests(FactoryCase):
    def setUp(self):
        super().setUp()
        shutil.rmtree(self.proj)
        copy_git_fixture(self.proj)

    def test_the_checklist_names_each_missing_key_with_an_example_and_exits_1_until_nothing_required_is_missing(self):
        self.write("package.json", json.dumps({"scripts": {"test": "run-tests"}}))
        self.write("factory.toml", self.TOML + 'base = "develop"\n')
        code, out, _ = self.cli("setup")
        self.assertEqual(code, 1, out)
        self.assertIn("[missing ] base branches: web: base develop does not exist", out)
        self.assertIn("key: [[repo]] base  e.g. base = \"develop\"", out)
        self.assertIn("[missing ] checks: web: no standing check (candidates: test)", out)
        self.assertIn("[optional] providers: ticket: no adapter", out)
        self.write("factory.toml", self.TOML + '[[repo.check]]\nid = "unit"\nrun = "run-tests"\n')
        out = self.ok("setup")
        self.assertIn("[done    ] base branches: web: base main", out)
        self.assertIn("0 required item(s) missing; then run factory check", out)
        data = json.loads(self.ok("setup", "--json"))
        self.assertEqual(data["missing"], 0)
        self.assertIn("browser runner", {i["area"] for i in data["items"]})

    def test_a_login_spec_needs_its_file_and_its_secrets_but_no_value_is_printed(self):
        env = '[repo.env]\nup = "serve"\nbase_url = "http://127.0.0.1:5000"\nlogin_spec = "e2e/login.json"\nlogin_secrets = ["TEST_USER"]\n'
        self.write("factory.toml", self.TOML + env + '[secrets]\nkeys = ["TEST_USER"]\n')
        out = self.fails("setup", code=1)
        self.assertIn("login spec e2e/login.json does not exist; secrets not set: TEST_USER", out)
        self.write("e2e/login.json", '{"actions": []}')
        secrets = self.base / "config" / "dev.factory" / "secrets" / "demo.env"
        secrets.parent.mkdir(parents=True)
        secrets.write_text("TEST_USER=very-private\n")
        out = self.cli("setup")[1]
        self.assertIn("[done    ] login: web: login spec e2e/login.json", out)
        self.assertNotIn("very-private", out)

    def test_without_a_factory_toml_setup_points_at_discovery(self):
        self.assertIn("factory setup --discover", self.fails("setup", "--project", str(self.proj), project=False))


if __name__ == "__main__":
    unittest.main()
