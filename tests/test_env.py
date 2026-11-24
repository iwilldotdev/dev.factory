"""`factory env`: locks, process groups, health, files_from_main, depends_on, port diagnosis and `factory wait`."""
import json
import os
import subprocess
import time
import unittest

from dev_factory import proc
from tests.helpers import PY, git, lesson
from tests.m4_support import SERVER, M4Case, WorkspaceCase, env_toml


class UpDownTests(M4Case):
    @lesson("L-69")
    def test_up_serves_the_run_worktree_records_the_lock_and_down_stops_that_group(self):
        """L-69: the env serves the run worktree, and the lock records pid, create time, group, cwd, SHA and run; down stops that group."""
        run = self.branch()
        out = self.up()
        self.assertIn("healthy", out)
        lock = self.lock_of()
        tree = self.tree().as_posix()
        self.assertEqual((lock["cwd"], lock["sha"], lock["run"], lock["group"]), (tree, git("rev-parse", "HEAD", cwd=self.tree()), "ABC-1", lock["pid"]))
        self.assertIsNotNone(lock["create_time"])
        self.assertEqual(self.get("/whoami")[0], 200)
        self.assertEqual(json.loads(self.get("/whoami")[2])["cwd"], tree)
        self.assertTrue(any(e["type"] == "env" and e["action"] == "up" and e["pid"] == lock["pid"] for e in self.events(run)))
        self.assertIn("up", self.ok("env", "status"))
        self.assertIn("stopped", self.ok("env", "down", "web"))
        self.assertFalse(proc.pid_alive(lock["pid"]))
        self.assertFalse((self.proj / ".factory" / "env" / "web.lock").exists())

    def test_up_twice_is_a_no_op_and_restart_starts_a_new_process(self):
        self.branch()
        self.up()
        first = self.lock_of()["pid"]
        self.assertIn("already running", self.up())
        self.assertEqual(self.lock_of()["pid"], first)
        self.up("--restart")
        self.assertNotEqual(self.lock_of()["pid"], first)
        self.assertFalse(proc.pid_alive(first))

    @lesson("L-66")
    def test_the_log_of_an_env_started_through_a_shim_is_never_empty(self):
        """L-66: an env started through a .cmd (or shell) shim chain still produces a non-empty log: unbuffered output plus a factory header."""
        shim = self.base / ("shim.cmd" if os.name == "nt" else "shim.sh")
        shim.write_text((f'@"{PY}" "{SERVER}" %1\r\n' if os.name == "nt" else f'#!/bin/sh\nexec "{PY}" "{SERVER}" "$1"\n'), newline="")
        shim.chmod(0o755)
        self.configure()
        self.write("factory.toml", self.TOML + env_toml(self.port).replace(f'\'"{PY}" "{SERVER}" {self.port}\'', f'\'"{shim.as_posix()}" {self.port}\''))
        self.branch()
        self.up()
        log = (self.proj / ".factory" / "env" / "web.log").read_text(encoding="utf-8")
        self.assertIn("[factory]", log)
        self.assertIn("fake server listening", log)
        self.assertIn("fake server listening", self.ok("env", "logs", "web"))

    @lesson("L-70", "L-74")
    def test_up_fails_when_the_health_url_already_answers_and_names_the_owner_without_killing_it(self):
        """L-70: something already answering the health URL is not our env; L-74: the owning pid is named and never killed."""
        self.branch()
        other = self.foreign_server(self.port)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                self.get("/health")
                break
            except OSError:
                time.sleep(0.2)
        msg = self.fails("env", "up", "web", code=3)
        self.assertIn("did not start", msg)
        self.assertIn(f"pid {other.pid}", msg)
        self.assertIsNone(other.poll())
        self.assertEqual(self.get("/health")[0], 200)
        self.assertFalse((self.proj / ".factory" / "env" / "web.lock").exists())

    @lesson("L-73")
    def test_health_is_a_status_below_500_and_a_redirect_is_not_followed(self):
        """L-73: healthy means below 500 with redirects not followed; 500 is not healthy."""
        self.configure(extra_env='health = "/redir"\n')
        self.write("factory.toml", self.TOML + env_toml(self.port).replace('health = "/health"', 'health = "/redir"'))
        self.branch()
        self.up()
        self.assertIn("healthy", self.ok("env", "health", "web"))
        self.ok("env", "down", "web")
        os.environ["FAKE_HEALTH_STATUS"] = "500"
        self.addCleanup(os.environ.pop, "FAKE_HEALTH_STATUS", None)
        self.configure(ready=2)
        msg = self.fails("env", "up", "web", code=3)
        self.assertIn("(health)", msg)
        self.assertIn("did not answer below 500", msg)
        self.assertFalse((self.proj / ".factory" / "env" / "web.lock").exists())

    def test_a_process_that_exits_before_health_is_a_start_failure_with_the_log_tail(self):
        os.environ["FAKE_DIE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_DIE", None)
        run = self.branch()
        msg = self.fails("env", "up", "web", code=3)
        self.assertIn("(start)", msg)
        self.assertIn("dying on purpose", msg)
        fail = [e for e in self.events(run) if e["type"] == "env" and e["action"] == "fail"]
        self.assertEqual(fail[-1]["kind"], "start")

    @lesson("L-68")
    def test_a_recorded_pid_with_another_create_time_is_reported_and_never_killed(self):
        """L-68: down compares the create time; a pid that was reused by an unrelated process is left alone."""
        self.branch()
        bystander = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (bystander.kill(), bystander.wait(timeout=10)))
        env_dir = self.proj / ".factory" / "env"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "web.lock").write_text(json.dumps({"repo": "web", "pid": bystander.pid, "create_time": proc.create_time(bystander.pid) - 5000,
                                                      "run": "ABC-1", "cwd": "x", "sha": "0" * 40}))
        out = self.ok("env", "down", "web")
        self.assertIn("create time differs", out)
        self.assertIsNone(bystander.poll())
        self.assertFalse((env_dir / "web.lock").exists())

    def test_a_dead_holder_is_a_stale_lock_that_up_replaces(self):
        self.branch()
        env_dir = self.proj / ".factory" / "env"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "web.lock").write_text(json.dumps({"repo": "web", "pid": 2 ** 22 + 12345, "create_time": 1.0, "run": "OLD", "cwd": "x", "sha": "0" * 40}))
        self.assertIn("healthy", self.up())
        self.assertEqual(self.lock_of()["run"], "ABC-1")

    # L-75 is by-design (one lock, no leases); this documents the queue behaviour
    def test_a_live_holder_of_another_run_is_named_and_wait_queues_for_it(self):
        """A live lock is refused with the holder named; --wait queues up to the given seconds."""
        self.branch()
        holder = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (holder.kill(), holder.wait(timeout=10)))
        env_dir = self.proj / ".factory" / "env"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "web.lock").write_text(json.dumps({"repo": "web", "pid": holder.pid, "create_time": proc.create_time(holder.pid), "run": "OTHER-9",
                                                      "cwd": "elsewhere", "sha": "a" * 40}))
        msg = self.fails("env", "up", "web", code=3)
        self.assertIn("run OTHER-9", msg)
        start = time.monotonic()
        self.fails("env", "up", "web", "--wait", "1", code=3)
        self.assertGreaterEqual(time.monotonic() - start, 1.0)
        holder.kill()
        holder.wait(timeout=10)
        self.assertIn("healthy", self.up("--wait", "5"))


class FilesFromMainTests(M4Case):
    def setUp(self):
        super().setUp()
        self.commit_ignore(".env.local")
        self.write("factory.toml", self.TOML + env_toml(self.port) .replace("[repo.env]", "[repo.env]") + "\n")
        text = (self.proj / "factory.toml").read_text(encoding="utf-8")
        self.write("factory.toml", text.replace('name = "web"\n', 'name = "web"\nfiles_from_main = [".env.local"]\n', 1))

    @lesson("L-64")
    def test_files_from_main_are_copied_into_the_worktree_and_a_missing_one_is_kind_files(self):
        """L-64: gitignored files named in files_from_main reach the worktree before the server starts; a missing one is env kind files."""
        run = self.branch()
        self.assertIn("(files)", self.fails("env", "up", "web", code=3))
        self.assertEqual([e["kind"] for e in self.events(run) if e["type"] == "env" and e["action"] == "fail"], ["files"])
        (self.proj / ".env.local").write_text("SECRET_TOKEN=abc\n")
        self.up()
        self.assertEqual((self.tree() / ".env.local").read_text(), "SECRET_TOKEN=abc\n")

    @lesson("L-65")
    def test_the_copy_refuses_links_and_removes_a_file_git_does_not_ignore(self):
        """L-65: no link is followed or written through, and a copy that git would add is removed again."""
        self.branch()
        (self.proj / ".env.local").write_text("x\n")
        # a destination directory that is a link out of the worktree
        outside = self.base / "outside"
        outside.mkdir()
        text = (self.proj / "factory.toml").read_text(encoding="utf-8").replace('".env.local"', '"linked/.env.local"')
        self.write("factory.toml", text)
        (self.proj / "linked").mkdir()
        (self.proj / "linked" / ".env.local").write_text("y\n")
        try:
            os.symlink(outside, self.tree() / "linked", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("links need privileges on this machine")
        msg = self.fails("env", "up", "web", code=3)
        self.assertIn("link", msg)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_copy_that_git_does_not_ignore_is_removed(self):
        self.branch()
        text = (self.proj / "factory.toml").read_text(encoding="utf-8").replace('".env.local"', '"notes.txt"')
        self.write("factory.toml", text)
        (self.proj / "notes.txt").write_text("not ignored\n")
        msg = self.fails("env", "up", "web", code=3)
        self.assertIn("does not ignore it", msg)
        self.assertFalse((self.tree() / "notes.txt").exists())


class DependsOnTests(WorkspaceCase):
    @lesson("L-71")
    def test_a_dependency_is_started_first_from_the_run_worktree_or_from_main_with_a_warning(self):
        """L-71: depends_on serves the dependency from the run worktree when the repo is in the run, else from its main path with a warning."""
        run = self.branch()
        out = self.up()
        self.assertLess(out.index("api: up"), out.index("web: up"))
        api_tree = self.tree("api").as_posix()
        self.assertEqual(json.loads(self.get("/whoami", self.ports["api"])[2])["cwd"], api_tree)
        self.assertNotIn("warning", out)
        self.ok("env", "down", "web")
        self.ok("env", "down", "api")
        self.ok("run", "repos", "web")  # api is no longer part of the run
        out = self.up()
        self.assertIn("warning: api is not in this run", out)
        self.assertEqual(json.loads(self.get("/whoami", self.ports["api"])[2])["cwd"], (self.base / "api").as_posix())
        self.assertTrue(run.exists())

    def test_a_depends_on_cycle_is_a_config_error(self):
        self.write("factory.toml", 'schema = 1\n[project]\nname = "shop"\n[[repo]]\npath = "../api"\n[repo.env]\ndepends_on = ["web"]\n'
                   '[[repo]]\npath = "../web"\n[repo.env]\ndepends_on = ["api"]\n')
        self.branch()
        self.assertIn("cycle", self.fails("env", "up", "web", code=2))


class WaitTests(M4Case):
    def test_wait_for_a_file_a_command_and_a_local_url(self):
        target = self.base / "ready.txt"
        self.fails("wait", "--file", str(target), "--timeout", "1", code=1)
        target.write_text("x")
        self.assertIn("ready", self.ok("wait", "--file", str(target), "--timeout", "5"))
        self.ok("wait", "--cmd", f'"{PY}" -c "import sys; sys.exit(0)"', "--timeout", "5")
        self.fails("wait", "--cmd", f'"{PY}" -c "import sys; sys.exit(1)"', "--timeout", "1", code=1)
        self.foreign_server(self.port)
        self.ok("wait", "--url", f"http://127.0.0.1:{self.port}/health", "--timeout", "20")
        self.assertIn("loopback", self.fails("wait", "--url", "http://example.com/", "--timeout", "1", code=2))

    def test_the_timeout_is_capped_at_600_seconds(self):
        from dev_factory import cli_env
        self.assertEqual(cli_env.WAIT_CAP_S, 600)


if __name__ == "__main__":
    unittest.main()
