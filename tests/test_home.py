"""Home workspaces: resolution by path and remote, the guided entry, the setup interview (terminal and JSON), config set."""
import json
import os
import tomllib
import unittest
from unittest import mock

from tests.helpers import FactoryCase, copy_git_fixture, git
from dev_factory import home


def feeder(answers: list[str]):
    """An input function that returns the given answers in order (the terminal path of the interview)."""
    queue = list(answers)

    def ask(_prompt: str = "") -> str:
        if not queue:
            raise AssertionError(f"no answer left for prompt {_prompt!r}")
        return queue.pop(0)
    ask.queue = queue
    return ask


class HomeCase(FactoryCase):
    def setUp(self):
        super().setUp()
        self.fhome = self.base / "fhome"
        self.web = self.repo("web", "https://bot:tok@scm.example.test/shop/web.git")

    def repo(self, name: str, remote: str | None = None):
        path = copy_git_fixture(self.base / "code" / name)
        if remote:
            git("remote", "add", "origin", remote, cwd=path)
        return path

    def workspace(self, name: str, repos: str) -> None:
        ws = home.create(name)
        (ws / "factory.toml").write_text(f'schema = 1\n\n[project]\nname = "{name}"\n{repos}', encoding="utf-8")

    def repo_entry(self, path, name="web", extra="") -> str:
        return f'\n[[repo]]\npath = "{path.as_posix()}"\nname = "{name}"\n{extra}'

    def at(self, cwd, *args, code=0, ask=None):
        with mock.patch("dev_factory.cli_home._prompt", return_value=ask):
            got, out, err = self.cli(*args, cwd=cwd, project=False)
        self.assertEqual(got, code, f"{args} -> {got}\nstdout: {out}\nstderr: {err}")
        return out + err


class ResolutionTests(HomeCase):
    def test_a_repository_is_found_in_its_home_workspace_by_path(self):
        self.workspace("shop", self.repo_entry(self.web))
        s = json.loads(self.at(self.web, "status", "--config", "--json"))
        self.assertEqual((s["project"], s["dir"]), ("shop", (self.fhome / "shop").as_posix()))
        self.assertEqual(s["worktree_root"], (self.fhome / "shop" / "worktrees").as_posix())

    def test_another_clone_is_found_by_remote_in_both_url_forms(self):
        self.assertEqual(home.norm_remote("git@SCM.example.test:shop/web.git"), home.norm_remote("https://u:p@scm.example.test/shop/web/"))
        self.assertEqual(home.norm_remote("ssh://git@scm.example.test/shop/web"), "scm.example.test/shop/web")
        self.assertNotEqual(home.norm_remote("https://scm.example.test/shop/web"), home.norm_remote("https://scm.example.test/shop/api"))
        gone = self.base / "elsewhere" / "web"
        self.workspace("shop", self.repo_entry(gone, extra='remote_url = ["git@SCM.example.test:shop/web.git"]\n'))
        s = json.loads(self.at(self.web, "status", "--config", "--json"))
        self.assertEqual(s["project"], "shop")
        self.assertEqual(s["repos"][0]["path"], self.web.as_posix())  # this clone is used: the recorded path is not on this machine

    def test_two_workspaces_listing_the_repo_are_an_ambiguity_unless_one_is_named(self):
        self.workspace("shop", self.repo_entry(self.web))
        self.workspace("store", self.repo_entry(self.web, extra='remote_url = "https://scm.example.test/shop/web"\n'))
        out = self.at(self.web, "status", "--config", code=2)
        self.assertIn("several workspaces (shop, store): pass --workspace NAME", out)
        self.assertIn("project store", self.at(self.web, "--workspace", "store", "status", "--config"))
        self.assertIn("project shop", self.at(self.web, "status", "--config", "--workspace", "shop"))
        self.assertIn("project store", self.at(self.web, "status", "--config", ask=feeder(["store"])))
        self.assertIn("no workspace 'nope'", self.at(self.web, "--workspace", "nope", "status", code=2))

    def test_a_folder_workspace_found_by_climbing_still_wins(self):
        self.workspace("shop", self.repo_entry(self.proj))
        self.assertIn("project demo", self.at(self.proj, "status", "--config"))


class EntryTests(HomeCase):
    def test_without_a_workspace_the_entry_starts_the_setup_interview(self):
        data = json.loads(self.at(self.web, code=4))
        self.assertEqual(data["need"], "setup")
        self.assertEqual([q["id"] for q in data["questions"]], ["workspace", "repo_name"])
        self.assertEqual(data["questions"][0]["default"], "web")

    def test_with_a_workspace_the_entry_asks_for_the_ticket_or_starts_its_run(self):
        self.workspace("shop", self.repo_entry(self.web))
        need = json.loads(self.at(self.web, "start", code=4))
        self.assertEqual((need["need"], need["workspace"]), ("ticket", "shop"))
        self.assertIn("repo web: target branches, standing checks", need["asked_every_run"])
        out = json.loads(self.at(self.web, "start", "https://tracker.example.test/browse/ABC-12?focus=1", "--json"))
        self.assertEqual(out["run"], "ABC-12")
        self.assertTrue((self.fhome / "shop" / "runs" / "ABC-12" / "events.jsonl").is_file())
        self.assertFalse((self.web / ".factory").exists())
        self.assertIn("already exists", self.at(self.web, "start", "ABC-12"))
        self.assertIn("started run ABC-13", self.at(self.web, ask=feeder(["ABC-13"])))
        self.assertEqual(home.ticket_key("https://tracker.example.test/issues/42"), "https://tracker.example.test/issues/42")

    def test_runs_and_worktrees_live_in_the_home_workspace(self):
        self.workspace("shop", self.repo_entry(self.web, extra='base = "main"\n'))
        self.at(self.web, "start", "ABC-1")
        self.assertTrue((self.fhome / "shop" / "runs" / "ABC-1" / "events.jsonl").is_file())
        s = json.loads(self.at(self.web, "status", "--config", "--json"))
        self.assertEqual(s["worktree_root"], (self.fhome / "shop" / "worktrees").as_posix())
        tree = self.fhome / "shop" / "worktrees" / "ABC-1" / "web"
        tree.mkdir(parents=True)  # a run worktree finds its workspace by climbing
        self.assertIn("ABC-1", self.at(tree, "runs"))


class SetupTests(HomeCase):
    ANSWERS = [("workspace", "shop"), ("repo_name", "web"),
               ("repo.web.base", "main"), ("repo.web.targets", "develop"), ("repo.web.env.up", "node --run dev"),
               ("repo.web.env.base_url", "http://127.0.0.1:5173"), ("repo.web.env.health", "/"), ("repo.web.checks", "test,ci-make-unit"),
               ("repo.web.notes", "Use the seeded admin."), ("repo.web.login", "login/web.json"), ("repo.web.login_secrets", "TEST_USER, TEST_PASSWORD"),
               ("repo.web.seed", "node --run db:seed"), ("repo.web.reset", "-"), ("siblings", "api"),
               ("repo.api.base", "main"), ("repo.api.targets", "-"), ("repo.api.checks", "-"), ("repo.api.notes", "-"), ("integrations", "-")]

    def setUp(self):
        super().setUp()
        self.write("package.json", json.dumps({"scripts": {"dev": "serve --port 5173", "test": "t", "lint": "l", "db:seed": "s"}}), self.web)
        self.write(".ci/workflows/main.yml", "env:\n  NODE_ENV: test\n  TOKEN: ${{ secrets.T }}\njobs:\n  t:\n    steps:\n      - run: make unit\n", self.web)
        git("branch", "develop", cwd=self.web)
        self.repo("api")

    def json_path(self) -> str:
        first = json.loads(self.at(self.web, "setup", "--questions", "--json", code=4))
        self.assertIsNone(first["workspace"])
        self.assertEqual(first["questions"][0]["options"], [{"label": "web", "description": "a new workspace"}])
        batch = json.loads(self.at(self.web, "setup", "--answer", "workspace=shop", "--answer", "repo_name=web", "--json", code=4))
        q = {x["id"]: x for x in batch["questions"]}
        self.assertEqual(list(q)[:3], ["repo.web.base", "repo.web.targets", "repo.web.env.up"])
        self.assertEqual(q["repo.web.checks"]["kind"], "multi")
        self.assertIn({"label": "ci-make-unit", "description": "make unit (vars NODE_ENV=test)"}, q["repo.web.checks"]["options"])
        self.assertEqual(q["repo.web.env.base_url"]["default"], "http://127.0.0.1:5173")
        args = [a for k, v in self.ANSWERS[2:] for a in ("--answer", f"{k}={'' if v == '-' else v}")]
        summary = json.loads(self.at(self.web, "setup", *args, "--json"))
        self.assertEqual(summary["secrets_to_fill"], ["TEST_PASSWORD", "TEST_USER"])
        self.assertEqual(summary["asked_every_run"], ["repo api: target branches, standing checks"])
        self.assertIn("asked at every run: repo api: target branches, standing checks", self.at(self.web, "setup", "--questions"))
        return (self.fhome / "shop" / "factory.toml").read_text(encoding="utf-8")

    def test_the_terminal_and_the_json_paths_write_the_same_config(self):
        by_json = self.json_path()
        doc = tomllib.loads(by_json)
        web = doc["repo"][0]
        self.assertEqual(web["remote_url"], "https://scm.example.test/shop/web.git")
        self.assertEqual(web["mirror"], [{"name": "develop", "branch": "develop"}])
        self.assertEqual(web["check"], [{"id": "test", "run": "node --run test"}, {"id": "ci-make-unit", "run": "make unit", "vars": {"NODE_ENV": "test"}}])
        self.assertEqual((web["env"]["login_spec"], web["env"]["seed"], web["notes"]), ("login/web.json", "node --run db:seed", "Use the seeded admin."))
        self.assertEqual(doc["secrets"]["keys"], ["TEST_PASSWORD", "TEST_USER"])
        self.assertEqual([r["name"] for r in doc["repo"]], ["web", "api"])
        for sub in ("scripts", "login", "runs", "worktrees", "secrets.env"):
            self.assertTrue((self.fhome / "shop" / sub).exists(), sub)
        self.assertNotIn("tok", by_json)
        with mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.base / "fhome2")}):
            ask = feeder([v for _, v in self.ANSWERS])
            out = self.at(self.web, "setup", ask=ask)
            self.assertEqual(ask.queue, [])
            self.assertIn("asked at every run: repo api: target branches, standing checks", out)
            self.assertEqual((self.base / "fhome2" / "shop" / "factory.toml").read_text(encoding="utf-8"), by_json)

    def test_skipping_the_env_start_command_skips_its_url_and_health(self):
        self.at(self.web, "setup", "--answer", "workspace=shop", "--answer", "repo_name=web", "--json", code=4)
        nxt = json.loads(self.at(self.web, "setup", "--answer", "repo.web.base=main", "--answer", "repo.web.targets=", "--answer", "repo.web.env.up=",
                                 "--answer", "repo.web.checks=", "--answer", "repo.web.notes=", "--json", code=4))
        self.assertFalse([q for q in nxt.get("questions", []) if ".env." in q["id"]], nxt)
        with mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.base / "fhome4")}):
            ask = feeder(["shop", "web", "main", "-", "-", "-", "-", "-", "-", "-"])
            self.at(self.web, "setup", ask=ask)
            self.assertNotIn("env", tomllib.loads((self.base / "fhome4" / "shop" / "factory.toml").read_text(encoding="utf-8"))["repo"][0])

    def test_workspace_names_are_unique_slugs(self):
        self.workspace("web", self.repo_entry(self.base / "x"))
        q = json.loads(self.at(self.web, "setup", "--questions", "--json", code=4))["questions"][0]
        self.assertEqual([o["label"] for o in q["options"]], ["web", "web-2"])
        self.assertEqual(q["default"], "web-2")
        self.assertIn("must match", self.at(self.web, "setup", "--answer", "workspace=Not Valid", code=2))
        self.assertIn("not a pending question: repo.web.base", self.at(self.web, "setup", "--answer", "repo.web.base=main", code=2))

    def test_a_terminal_without_a_workspace_runs_the_interview_then_asks_for_the_ticket(self):
        ask = feeder(["shop", "", ""] + ["-"] * 6)  # workspace, name, base, then 6 skips (no start command: no URL or health asked)
        with mock.patch.dict(os.environ, {"FACTORY_HOME": str(self.base / "fhome3")}):
            out = self.at(self.web, ask=lambda p="": ask(p) if ask.queue else "ABC-7")
        self.assertIn("workspace shop saved", out)
        self.assertIn("started run ABC-7", out)


class ConfigTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.workspace("shop", self.repo_entry(self.web))
        self.toml = self.fhome / "shop" / "factory.toml"

    def test_config_set_writes_valid_keys_and_refuses_unknown_ones(self):
        self.at(self.web, "config", "set", "repo.web.base", "develop")
        self.at(self.web, "config", "set", "ship.draft", "false")
        self.at(self.web, "config", "set", "repo.web.remote_url", "git@scm.example.test:shop/web.git")
        doc = tomllib.loads(self.toml.read_text(encoding="utf-8"))
        self.assertEqual((doc["repo"][0]["base"], doc["ship"]["draft"]), ("develop", False))
        before = self.toml.read_text(encoding="utf-8")
        self.assertIn("unknown key repo.nope", self.at(self.web, "config", "set", "repo.web.nope", "x", code=2))
        self.assertIn("autonomy.max_loops: must be an integer from 0 to 10", self.at(self.web, "config", "set", "autonomy.max_loops", "99", code=2))
        self.assertIn("no repo 'api'", self.at(self.web, "config", "set", "repo.api.base", "x", code=2))
        self.assertEqual(self.toml.read_text(encoding="utf-8"), before)

    def test_an_answer_saved_to_the_workspace_is_written_through_config_set(self):
        self.at(self.web, "start", "ABC-1")
        self.assertIn("unknown key", self.at(self.web, "ask", "Which base?", "--save-as", "repo.web.bogus", code=2))
        self.at(self.web, "ask", "Which branch is the base?", "--save-as", "repo.web.base")
        self.at(self.web, "ask", "Anything else?")
        self.assertIn("saved to repo.web.base", self.at(self.web, "answer", "Q-1", "--user-said", "develop", "--save"))
        self.assertEqual(tomllib.loads(self.toml.read_text(encoding="utf-8"))["repo"][0]["base"], "develop")
        self.assertIn("not opened with --save-as", self.at(self.web, "answer", "Q-2", "--user-said", "no", "--save", code=2))

    def test_the_home_workspace_reads_its_own_secrets_file(self):
        self.at(self.web, "config", "set", "secrets.keys", '["TEST_USER"]')
        (self.fhome / "shop" / "secrets.env").write_text("TEST_USER=private-value\n", encoding="utf-8")
        s = json.loads(self.at(self.web, "status", "--config", "--json"))
        self.assertEqual(s["secrets"], {"TEST_USER": "set"})


if __name__ == "__main__":
    unittest.main()
