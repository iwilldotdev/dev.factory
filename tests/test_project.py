"""init, init --import, --version, import isolation, agents-md adapter."""
import json
import re
import tomllib
import unittest

from tests.helpers import FactoryCase, lesson
from dev_factory import config, harness
from dev_factory.cli import build_parser


class InitTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.fresh = self.base / "my repo's dir"
        self.fresh.mkdir()

    @lesson("L-162")
    def test_init_writes_a_valid_config_even_for_paths_with_spaces_and_apostrophes(self):
        """L-162: paths with spaces and apostrophes work; the project name is slugged."""
        out = self.ok("init", "--project", str(self.fresh), project=False)
        self.assertIn("wrote", out)
        cfg = config.load(self.fresh)
        self.assertEqual(cfg["project"]["name"], "my-repo-s-dir")
        self.assertEqual(cfg["repo"][0]["name"], "my-repo-s-dir")
        self.assertEqual(tomllib.loads((self.fresh / "factory.toml").read_text())["schema"], 1)

    def test_init_adds_the_state_folder_to_gitignore_once_and_never_overwrites(self):
        (self.fresh / ".gitignore").write_text("dist/")
        self.ok("init", "--project", str(self.fresh), project=False)
        self.assertEqual((self.fresh / ".gitignore").read_text().splitlines(), ["dist/", ".factory/"])
        self.assertIn("already exists", self.fails("init", "--project", str(self.fresh), project=False))
        self.assertEqual((self.fresh / ".gitignore").read_text().count(".factory/"), 1)

    def test_init_workspace_with_repo_paths_and_base(self):
        self.ok("init", "--project", str(self.fresh), "--name", "shop", "--repo", "../api", "--repo", "../web", "--base", "develop", project=False)
        doc = tomllib.loads((self.fresh / "factory.toml").read_text())
        self.assertEqual([(r["path"], r["name"], r["base"]) for r in doc["repo"]], [("../api", "api", "develop"), ("../web", "web", "develop")])

    def test_init_from_the_cwd(self):
        self.ok("init", cwd=self.fresh, project=False)
        self.assertTrue((self.fresh / "factory.toml").is_file())
        self.assertTrue((self.fresh / ".factory").is_dir())

    def test_init_writes_the_agents_md_adapter_and_keeps_existing_text(self):
        (self.fresh / "AGENTS.md").write_text("# Mine\n\nKeep this.\n")
        self.ok("init", "--project", str(self.fresh), "--harness", "agents-md", project=False)
        text = (self.fresh / "AGENTS.md").read_text()
        self.assertIn("Keep this.", text)
        self.assertIn("factory next", text)
        self.assertIn("unknown harness", self.fails("init", "--project", str(self.base / "x"), "--harness", "nope", project=False))

    def test_unknown_harness_does_not_leave_a_half_initialised_project(self):
        target = self.base / "half"
        self.fails("init", "--project", str(target), "--harness", "nope", project=False)
        self.assertFalse((target / "factory.toml").exists())


class ImportTests(FactoryCase):
    V0 = {"schemaVersion": 3, "name": "Shop", "worktree_root": "D:/wt/shop",
          "repos": [{"alias": "api", "path": "", "scm_project": "g/api", "quality": {"lint": "run-lint", "test": "run-tests"},
                     "ui": False, "env": {"start": "serve-app", "health": "/h", "base_url": "http://127.0.0.1:5000", "hot_reload": True,
                                           "files_from_main": [".env"], "env_vars": {"A": "1"},
                                           "seed": {"cmd": ["python", "seed.py", "a b"], "reset": ["python", "reset.py"]}}}],
          "ship": {"targets": ["main"], "draft_default": False, "ticket_mode": "comment+transition"},
          "commit": {"header_regex": "^feat: ", "forbidden_trailers": ["Co-Authored-By"]},
          "autonomy": {"ship": "draft-auto", "allow_timing_amends": True}, "limits": {"max_diff_ratio": 0.02, "max_tolerance_px": 1},
          "rules": [{"id": "r1"}]}

    def test_import_converts_a_v0_workspace_and_lists_what_it_could_not_map(self):
        src = self.base / "workspace.json"
        src.write_text(json.dumps(dict(self.V0, repos=[dict(self.V0["repos"][0], path=str(self.base / "api"))])))
        (self.base / "api").mkdir()
        target = self.base / "ws"
        out = self.ok("init", "--project", str(target), "--import", str(src), project=False)
        doc = tomllib.loads((target / "factory.toml").read_text())
        repo = doc["repo"][0]
        self.assertEqual((doc["project"]["name"], repo["name"], repo["path"]), ("shop", "api", "../api"))
        self.assertEqual({c["id"]: c["run"] for c in repo["check"]}, {"lint": "run-lint", "test": "run-tests"})
        self.assertEqual((repo["env"]["up"], repo["env"]["vars"], repo["commit_pattern"]), ("serve-app", {"A": "1"}, "^feat: "))
        self.assertEqual((repo["env"]["seed"], repo["env"]["reset"]), ("python seed.py 'a b'", "python reset.py"))
        self.assertEqual((doc["ship"], doc["autonomy"], doc["limits"]),
                         ({"draft": False, "ticket": "transition"}, {"ship": "draft-auto"}, {"max_diff_ratio": 0.02, "tolerance_px": 1}))
        self.assertEqual(tomllib.loads((target / ".factory" / "local.toml").read_text()), {"worktree_root": "D:/wt/shop"})
        for note in ("scm_project", "hot_reload", "rules was not mapped", "ship.targets", "forbidden_trailers", "transition_to",
                     "allow_timing_amends", "base branch", "secrets"):
            self.assertIn(note, out)
        self.assertNotIn("Co-Authored-By", (target / "factory.toml").read_text())

    def test_import_with_a_missing_path_still_writes_and_says_what_to_edit(self):
        src = self.base / "p.json"
        src.write_text(json.dumps(self.V0))
        out = self.ok("init", "--project", str(self.base / "t"), "--import", str(src), project=False)
        self.assertIn("the source has no path", out)

    def test_import_rejects_a_non_object(self):
        src = self.base / "bad.json"
        src.write_text("[1]")
        self.assertIn("expected a JSON object", self.fails("init", "--project", str(self.base / "t"), "--import", str(src), project=False))


class VersionAndIsolationTests(FactoryCase):
    @lesson("L-150")
    def test_version_prints_the_build_identity(self):
        """L-150: --version prints the version and the build it runs."""
        out = self.cli_subprocess("--version").stdout
        self.assertRegex(out, r"^factory 1\.0\.0a1 \((git [0-9a-f]{12}|git unknown|release)\) python 3\.")

    @lesson("L-148")
    def test_the_cli_never_imports_from_the_cwd(self):
        """L-148: a decoy package in the cwd is ignored (see also the no-dynamic-import test in test_structure)."""
        decoy = self.base / "decoy"
        self.write("dev_factory/__init__.py", "raise SystemExit('DECOY IMPORTED')", root=decoy)
        self.write("dev_factory/__main__.py", "raise SystemExit('DECOY MAIN')", root=decoy)
        res = self.cli_subprocess("--version", cwd=decoy)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("DECOY", res.stdout + res.stderr)
        self.assertIn("factory 1.0.0a1", res.stdout)

    def test_help_and_no_arguments(self):
        code, out, _ = self.cli(project=False)
        self.assertEqual(code, 2)
        self.assertIn("usage: factory", out)


class HarnessTests(FactoryCase):
    @lesson("L-171")
    def test_the_adapter_is_thin_generated_and_names_only_real_commands(self):
        """L-171, L-160: at most 100 lines, generated from the CLI, and every `factory <command>` it names exists."""
        text = harness.agents_md_section()
        self.assertLessEqual(len(text.splitlines()), 100)
        commands = set(build_parser()._subparsers._group_actions[0].choices)
        for name in set(re.findall(r"`factory ([a-z]+)", text)):
            self.assertIn(name, commands)
        subs = {"media": {"seen", "unseen"}, "run": {"start", "close", "retype", "repos", "delete"}}
        for name, sub in re.findall(r"`factory (run|media) ([a-z]+)", text):
            self.assertIn(sub, subs[name])
        self.assertNotIn("TODO", text)

    def test_the_adapter_is_replaced_in_place_and_idempotent(self):
        root = self.base / "h"
        root.mkdir()
        (root / "AGENTS.md").write_text("top\n")
        harness.write(root, "agents-md")
        first = (root / "AGENTS.md").read_text()
        harness.write(root, "agents-md")
        self.assertEqual((root / "AGENTS.md").read_text(), first)
        (root / "AGENTS.md").write_text(first + "\nbottom\n")
        harness.write(root, "agents-md")
        text = (root / "AGENTS.md").read_text()
        self.assertTrue(text.startswith("top\n"))
        self.assertTrue(text.rstrip().endswith("bottom"))
        self.assertEqual(text.count(harness.BEGIN), 1)


if __name__ == "__main__":
    unittest.main()
