"""Per-repo env leases, slots, shared repos and the login hand-off rewrite (DESIGN section 11)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.commands import batch, env as envmod, prove, workspace as wsmod  # noqa: E402


def cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = main(list(argv))
    return rc, out.getvalue()


API_PORT, API_STRIDE, REACT_PORT = 41000, 10, 43000


class SlotCase(FactoryCase):
    api_slots = 2
    react_slots = 3
    shareable = False

    def setUp(self):
        super().setUp()
        self.wt = {"API": str(self.tmp / "wt-api"), "React": str(self.tmp / "wt-react")}
        api = {"start": [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1"], "cwd": ".",
               "health": f"http://127.0.0.1:{API_PORT}/health", "ports": [API_PORT],
               "base_url": f"http://127.0.0.1:{API_PORT}", "startup_timeout_s": 30}
        if self.api_slots > 1:
            api.update(slots=self.api_slots, port_stride=API_STRIDE)
        if self.shareable:
            api["shareable"] = True
        react = {"start": ["npm", "run", "dev", "--", "--port", str(REACT_PORT)], "cwd": ".",
                 "health": f"http://localhost:{REACT_PORT}/", "ports": [REACT_PORT], "base_url": f"http://localhost:{REACT_PORT}",
                 "depends_on": ["API"],
                 "login": {"url": "/login", "user_field": "#u", "pass_field": "#p", "submit": "button",
                           "success_url_contains": f"localhost:{REACT_PORT}/portal/coming", "handoff_rewrite": True}}
        if self.react_slots > 1:
            react.update(slots=self.react_slots, port_stride=100)
        self.ws = {"schemaVersion": 1, "name": "ws", "env": {"lease": True, "lease_ttl_s": 900},
                   "repos": [{"alias": "API", "name": "api", "order": 1, "path": str(self.tmp), "env": api},
                             {"alias": "React", "name": "react", "order": 2, "path": str(self.tmp), "env": react}]}
        self.write("workspaces/ws/workspace.json", self.ws)

    def run_for(self, rid, *changed):
        """A run that changes `changed` repos (has a worktree for them)."""
        return self.make_run(rid, phase="build", repos={a: {"worktree": self.wt[a], "branch": "b"} for a in changed})

    def acquire(self, rid, wait=False, repos=None):
        run = core.run_load(rid)
        return envmod.lease_try(self.ws, rid, 900, wait=wait, reqs=envmod.lease_requests(self.ws, run, repos))

    def holder(self, key):
        return envmod.lease_read("ws", key).get("run")


class DefaultRepoTests(SlotCase):
    def test_default_repos_are_run_repos_plus_dependency_closure(self):
        self.run_for("r1", "React")
        self.assertEqual(list(envmod.lease_requests(self.ws, core.run_load("r1"))), ["API", "React"])
        self.run_for("r2", "API")
        self.assertEqual(list(envmod.lease_requests(self.ws, core.run_load("r2"))), ["API"])

    def test_run_without_runtime_repos_needs_every_repo(self):
        self.make_run("r1", phase="build", repos={})
        self.assertEqual(list(envmod.lease_requests(self.ws, core.run_load("r1"))), ["API", "React"])

    def test_explicit_repos_are_ordered_by_repo_order(self):
        self.run_for("r1", "React")
        self.assertEqual(list(envmod.lease_requests(self.ws, core.run_load("r1"), ["React", "API"])), ["API", "React"])


class PerRepoLeaseTests(SlotCase):
    api_slots = 1
    react_slots = 1

    def test_runs_hold_different_repos_at_the_same_time(self):
        self.run_for("a", "API")
        self.run_for("b", "React")
        self.assertTrue(self.acquire("a")["granted"])  # API only
        res = self.acquire("b", repos=["React"])
        self.assertTrue(res["granted"], res)
        self.assertEqual((self.holder("API"), self.holder("React")), ("a", "b"))
        self.assertTrue(envmod.lease_path("ws", "API").is_file())
        self.assertTrue(envmod.lease_path("ws", "React").is_file())
        # the workspace-level file is only the marker older versions read (DESIGN section 11, legacy marker)
        self.assertTrue(envmod.is_legacy_marker(json.loads(envmod.lease_path("ws").read_text(encoding="utf-8"))))
        self.assertIsNone(envmod.lease_read("ws").get("run"))

    def test_same_repo_is_exclusive(self):
        self.run_for("a", "API")
        self.run_for("b", "API")
        self.assertTrue(self.acquire("a")["granted"])
        res = self.acquire("b")
        self.assertFalse(res["granted"])
        self.assertIn("held by a", res["blockers"][0])

    def test_all_or_nothing_holds_nothing_when_one_repo_is_taken(self):
        self.run_for("a", "React")
        self.run_for("b", "React")
        self.run_for("c", "API")
        self.assertTrue(self.acquire("a", repos=["React"])["granted"])
        res = self.acquire("b")  # needs API and React; React is taken
        self.assertFalse(res["granted"])
        self.assertIsNone(self.holder("API"))  # API was not grabbed on the way
        self.assertTrue(self.acquire("c")["granted"])

    def test_acquisition_is_in_repo_order_whatever_the_request_order(self):
        self.run_for("a", "React")
        res = self.acquire("a", repos=["React", "API"])
        self.assertEqual(list(res["slots"]), ["API", "React"])

    def test_queued_runs_cannot_deadlock_each_other(self):
        """X queues behind API only; after B takes React, Y queues on both: both queue positions are inverted per repo."""
        for rid in ("h1", "h2", "x", "y"):
            self.run_for(rid, "React")
        self.assertTrue(self.acquire("h1", repos=["API"])["granted"])
        self.assertFalse(self.acquire("x", wait=True)["granted"])  # needs API+React; API is held
        self.assertTrue(self.acquire("h2", repos=["React"])["granted"])  # x reserves nothing it cannot get
        self.assertFalse(self.acquire("y", wait=True)["granted"])
        self.assertFalse(self.acquire("x", wait=True)["granted"])
        envmod.lease_release(self.ws, "h1")
        envmod.lease_release(self.ws, "h2")
        self.assertFalse(self.acquire("y", wait=True)["granted"])  # x is ahead and can be served
        self.assertTrue(self.acquire("x", wait=True)["granted"])
        self.assertFalse(self.acquire("y", wait=True)["granted"])
        envmod.lease_release(self.ws, "x")
        self.assertTrue(self.acquire("y", wait=True)["granted"])

    def test_queue_ahead_cannot_starve_by_blocked_run(self):
        """A queued run that is itself blocked reserves nothing: a later single-repo run takes the free repo."""
        self.run_for("h", "API")
        self.run_for("x", "React")
        self.run_for("z", "React")
        self.acquire("h", repos=["API"])
        self.assertFalse(self.acquire("x", wait=True)["granted"])  # API+React, API held
        self.assertTrue(self.acquire("z", repos=["React"])["granted"])

    def test_release_some_repos_keeps_the_others(self):
        self.run_for("a", "React")
        self.acquire("a")
        self.assertIn("API", envmod.lease_release(self.ws, "a", repos=["API"]))
        self.assertEqual((self.holder("API"), self.holder("React")), (None, "a"))

    def test_require_lease_is_per_repo(self):
        self.run_for("a", "React")
        self.run_for("b", "API")
        self.acquire("a", repos=["React"])
        envmod.require_lease(self.ws, "a", "env up React", "React")
        with self.assertRaises(core.FactoryError) as ctx:
            envmod.require_lease(self.ws, "a", "env up API", "API")
        self.assertIn("does not hold the env lease of ws for API", str(ctx.exception))
        self.acquire("b", repos=["API"])
        envmod.require_lease(self.ws, "b", "env up API", "API")

    def test_heartbeat_refreshes_every_lease_of_the_run(self):
        self.run_for("a", "React")
        self.acquire("a")
        for key in ("API", "React"):
            lease = envmod.lease_read("ws", key)
            lease["heartbeat"] = "2020-01-01T00:00:00+00:00"
            self.write(f"env/ws/{key}/lease.json", lease)
        self.assertTrue(envmod.lease_heartbeat("ws", "a"))
        self.assertLess(max(r["heartbeat_age_s"] for r in envmod.lease_status(self.ws, 900)["repos"] if r["holder"]), 60)
        self.assertFalse(envmod.lease_heartbeat("ws", "zz"))

    def test_down_is_refused_on_a_repo_another_run_holds(self):
        self.run_for("a", "React")
        self.run_for("b", "API")
        self.acquire("a", repos=["React"])
        self.acquire("b", repos=["API"])
        with self.assertRaises(core.FactoryError):
            envmod.down(self.ws, "React", "b")
        self.assertIn("no pid file", envmod.down(self.ws, "React", "a"))
        self.assertIn("no pid file", envmod.down(self.ws, "API", "b"))

    def test_cli_acquire_release_and_status(self):
        self.run_for("a", "React")
        self.run_for("b", "API")
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "a", "--repo", "React")
        self.assertEqual(rc, 0, out)
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "b", "--repo", "API")
        self.assertEqual(rc, 0, out)
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "b", "--repo", "React")
        self.assertEqual(rc, 1, out)
        self.assertIn("held by a", out)
        rc, out = cli("env", "lease", "status", "ws", "--json")
        st = json.loads(out)
        self.assertEqual({r["repo"]: r["holder"] for r in st["repos"]}, {"API": "b", "React": "a"})
        rc, out = cli("env", "lease", "status", "ws", "--repo", "API")
        self.assertIn("ws/API: held by b", out)
        self.assertNotIn("React", out)
        rc, out = cli("env", "lease", "release", "ws", "--run", "a", "--keep-env")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.holder("React"))
        self.assertEqual(self.holder("API"), "b")


class LegacyLeaseTests(SlotCase):
    api_slots = 1
    react_slots = 1

    def legacy(self, rid, age=0, pid=None):
        import datetime as dt
        stamp = dt.datetime.fromtimestamp(time.time() - age, dt.timezone.utc).isoformat(timespec="seconds")
        lease = {"run": rid, "pid": pid if pid is not None else os.getpid(), "acquired_at": stamp, "heartbeat": stamp, "queue": []}
        self.write("env/ws/lease.json", lease)

    def test_live_legacy_lease_holds_every_repo(self):
        self.run_for("old", "API")
        self.run_for("new", "React")
        self.legacy("old")
        for repos in (["API"], ["React"], None):
            res = self.acquire("new", repos=repos)
            self.assertFalse(res["granted"], repos)
            self.assertIn("held by old (workspace lease)", res["blockers"][0])
        self.assertTrue(self.acquire("old")["granted"])  # the holder keeps it
        with self.assertRaises(core.FactoryError):
            envmod.require_lease(self.ws, "new", "env up API", "API")
        envmod.require_lease(self.ws, "old", "env up API", "API")

    def test_legacy_lease_heartbeat_by_its_holder(self):
        self.legacy("old", age=600, pid=1)
        self.run_for("old", "API")
        self.assertTrue(envmod.lease_heartbeat("ws", "old", "React"))
        self.assertLess(envmod.lease_status(self.ws, 900)["legacy"]["heartbeat_age_s"], 60)

    def test_stale_legacy_lease_is_reclaimed(self):
        self.run_for("new", "API")
        self.make_run("old", phase="build")
        self.legacy("old", age=5000, pid=1)
        res = self.acquire("new")
        self.assertTrue(res["granted"])
        self.assertEqual(res["reclaimed"][0][0], "old")
        marker = json.loads(envmod.lease_path("ws").read_text(encoding="utf-8"))  # replaced by the marker of "new"
        self.assertTrue(envmod.is_legacy_marker(marker))
        self.assertEqual(marker["run"], "new")

    def test_legacy_lease_of_a_finished_run_is_reclaimed(self):
        self.run_for("new", "API")
        self.make_run("old", phase="done")
        self.legacy("old")
        self.assertTrue(self.acquire("new")["granted"])

    def test_release_drops_the_legacy_lease_of_the_run(self):
        self.legacy("old")
        self.run_for("old", "API")
        envmod.lease_release(self.ws, "old")
        self.assertFalse(envmod.lease_path("ws").exists())

    def test_down_is_refused_while_a_legacy_holder_is_live(self):
        self.run_for("old", "API")
        self.legacy("old")
        self.run_for("new", "API")
        with self.assertRaises(core.FactoryError):
            envmod.down(self.ws, "API", "new")

    def test_batch_status_shows_the_legacy_holder(self):
        self.write("env/ws/lease.json", {"run": "old"})
        self.assertEqual(batch.lease_holder("ws"), "old")


class SlotMathTests(SlotCase):
    def test_slot_zero_is_unchanged(self):
        env = envmod.slot_env(self.ws, None, "API", 0)
        self.assertEqual(env["ports"], [API_PORT])
        self.assertEqual(env["health"], f"http://127.0.0.1:{API_PORT}/health")
        self.assertEqual(env["start"][3], str(API_PORT))  # the {port} placeholder resolves for slot 0 too

    def test_ports_urls_and_placeholder_start_move_by_stride(self):
        env = envmod.slot_env(self.ws, None, "API", 1)
        self.assertEqual(env["ports"], [API_PORT + API_STRIDE])
        self.assertEqual(env["health"], f"http://127.0.0.1:{API_PORT + API_STRIDE}/health")
        self.assertEqual(env["base_url"], f"http://127.0.0.1:{API_PORT + API_STRIDE}")
        self.assertEqual(env["start"], [sys.executable, "-m", "http.server", str(API_PORT + API_STRIDE), "--bind", "127.0.0.1"])
        self.assertEqual(envmod.base_url(self.ws, "API", 1), f"http://127.0.0.1:{API_PORT + API_STRIDE}")

    def test_literal_base_port_in_start_args_is_replaced_without_placeholders(self):
        env = envmod.slot_env(self.ws, None, "React", 2)
        self.assertEqual(env["start"], ["npm", "run", "dev", "--", "--port", str(REACT_PORT + 200)])
        self.assertEqual(env["base_url"], f"http://localhost:{REACT_PORT + 200}")
        # a number that merely contains the port is left alone
        self.assertEqual(envmod.slot_start(["x", f"1{REACT_PORT}", f"--p={REACT_PORT}"], [REACT_PORT], 1, 100),
                         ["x", f"1{REACT_PORT}", f"--p={REACT_PORT + 100}"])

    def test_numbered_placeholders_and_env_vars(self):
        self.assertEqual(envmod.slot_start("serve {port0} {port1} {slot}", [100, 200], 2, 20), "serve 120 220 2")
        self.ws["repos"][0]["env"].update(ports=[API_PORT, API_PORT + 1], env_vars={"URLS": "http://+:{port0};http://+:{port1}", "X": "1"})
        env = envmod.slot_env(self.ws, None, "API", 1)
        self.assertEqual(env["env_vars"], {"URLS": f"http://+:{API_PORT + 10};http://+:{API_PORT + 11}", "X": "1"})
        with self.assertRaises(core.FactoryError):
            envmod.slot_start("{port2}", [1], 0, 0)

    def test_slot_comes_from_the_run_and_must_exist(self):
        run = {"id": "r", "env_slots": {"API": 1}}
        self.assertEqual(envmod.slot_env(self.ws, run, "API")["ports"], [API_PORT + API_STRIDE])
        self.assertEqual(envmod.slot_env(self.ws, run, "React")["ports"], [REACT_PORT])  # no slot recorded: 0
        with self.assertRaises(core.FactoryError):
            envmod.slot_env(self.ws, None, "API", 2)
        self.ws["repos"][0]["env"].pop("slots"), self.ws["repos"][0]["env"].pop("port_stride")
        with self.assertRaises(core.FactoryError):
            envmod.slot_env(self.ws, None, "API", 1)

    def test_repo_without_slot_keys_is_untouched(self):
        self.ws["repos"][0]["env"].pop("slots"), self.ws["repos"][0]["env"].pop("port_stride")
        self.assertEqual(envmod.slot_env(self.ws, None, "API"), self.ws["repos"][0]["env"])

    def test_pid_log_and_lease_names_per_slot(self):
        self.assertEqual(envmod.pid_path("ws", "API").name, "API.pid")
        self.assertEqual(envmod.pid_path("ws", "API", 1).name, "API@1.pid")
        self.assertEqual(envmod.log_path("ws", "API", 1).name, "API@1.log")
        self.assertEqual(envmod.lease_path("ws", envmod.slot_key("API", 1)).parent.name, "API@1")

    def test_pid_record_per_slot(self):
        self.write("env/ws/API@1.pid", {"pid": 123456789})
        self.assertIsNone(envmod.read_record("ws", "API"))
        self.assertEqual(envmod.read_record("ws", "API", 1)["pid"], 123456789)


class CrossRepoPlaceholderTests(SlotCase):
    def setUp(self):
        super().setUp()
        react = self.ws["repos"][1]["env"]
        react["env_vars"] = {"API_URL": "{base_url:API}/v1", "API_PORT": "{port:API}", "PLAIN": "x"}
        react["start"] = ["npm", "run", "dev", "--", "--port", str(REACT_PORT), "--api", "{base_url:API}"]

    def test_slot_zero_resolves_with_the_default_slot(self):
        env = envmod.slot_env(self.ws, None, "React", 0)
        self.assertEqual(env["env_vars"], {"API_URL": f"http://127.0.0.1:{API_PORT}/v1", "API_PORT": str(API_PORT), "PLAIN": "x"})
        self.assertEqual(env["start"][-1], f"http://127.0.0.1:{API_PORT}")

    def test_alias_resolves_with_the_runs_slot_for_that_alias(self):
        run = {"id": "r", "env_slots": {"API": 1, "React": 2}}
        env = envmod.slot_env(self.ws, run, "React")
        self.assertEqual(env["env_vars"]["API_URL"], f"http://127.0.0.1:{API_PORT + API_STRIDE}/v1")
        self.assertEqual(env["env_vars"]["API_PORT"], str(API_PORT + API_STRIDE))
        self.assertEqual(env["start"][5], str(REACT_PORT + 200))  # its own slot still moves its own port
        self.assertEqual(env["start"][-1], f"http://127.0.0.1:{API_PORT + API_STRIDE}")

    def test_unknown_alias_is_an_error_and_base_urls_do_not_loop(self):
        self.ws["repos"][1]["env"]["env_vars"]["X"] = "{port:Nope}"
        with self.assertRaises(core.FactoryError):
            envmod.slot_env(self.ws, None, "React", 0)
        self.ws["repos"][0]["env"]["env_vars"] = {"BACK": "{base_url:React}"}  # A -> B and B -> A
        self.ws["repos"][1]["env"]["env_vars"].pop("X")
        self.assertEqual(envmod.slot_env(self.ws, None, "API", 0)["env_vars"]["BACK"], f"http://localhost:{REACT_PORT}")
        self.assertEqual(envmod.base_url(self.ws, "React", 1), f"http://localhost:{REACT_PORT + 100}")

    def test_run_env_for_prove_resolves_too(self):
        self.make_run("b", phase="build", repos={"React": {"worktree": self.wt["React"]}}, env_slots={"API": 1})
        env = prove.run_env(self.ws, core.run_load("b"), "React")
        self.assertEqual(env["env_vars"]["API_URL"], f"http://127.0.0.1:{API_PORT + API_STRIDE}/v1")

    def test_workspace_check_rejects_unknown_alias(self):
        self.assertEqual(wsmod._ref_errors(self.ws), [])
        self.ws["repos"][1]["env"]["env_vars"]["X"] = "{base_url:Nope}"
        errs = wsmod._ref_errors(self.ws)
        self.assertEqual(len(errs), 1)
        self.assertIn("Nope", errs[0])
        self.ws["repos"][1]["env"]["start"].append("{port:Gone}")
        self.assertEqual(len(wsmod._ref_errors(self.ws)), 2)


class SlotAllocationTests(SlotCase):
    def test_first_free_slot_and_env_slots_recorded(self):
        for rid in ("a", "b", "c"):
            self.run_for(rid, "API")
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "a", "--repo", "API")
        self.assertEqual(rc, 0, out)
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "b", "--repo", "API")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load("a")["env_slots"], {"API": 0})
        self.assertEqual(core.run_load("b")["env_slots"], {"API": 1})
        self.assertEqual((self.holder("API"), self.holder("API@1")), ("a", "b"))
        self.assertIn("API@1", out)
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "c", "--repo", "API")
        self.assertEqual(rc, 1, out)
        cli("env", "lease", "release", "ws", "--run", "a", "--keep-env")
        self.assertNotIn("env_slots", core.run_load("a"))
        rc, out = cli("env", "lease", "acquire", "ws", "--run", "c", "--repo", "API")
        self.assertEqual(rc, 0, out)
        self.assertEqual(core.run_load("c")["env_slots"], {"API": 0})

    def test_reacquire_keeps_the_slot(self):
        self.run_for("a", "API")
        self.run_for("b", "API")
        self.acquire("a")
        self.assertEqual(self.acquire("b")["slots"], {"API": 1})
        res = self.acquire("b")
        self.assertEqual((res["slots"], res["fresh"]), ({"API": 1}, False))

    def test_slot_lease_files_are_independent_and_removed_on_release(self):
        self.run_for("a", "API")
        self.run_for("b", "API")
        self.acquire("a")
        self.acquire("b")
        envmod.lease_release(self.ws, "a")
        self.assertFalse(envmod.lease_path("ws", "API").exists())
        self.assertTrue(envmod.lease_path("ws", "API@1").is_file())

    def test_prove_context_gets_the_slot_base_url(self):
        run = self.make_run("b", phase="build", repos={"API": {"worktree": self.wt["API"]}}, env_slots={"API": 1})
        env = prove.run_env(self.ws, run, "API")
        self.assertEqual(env["base_url"], f"http://127.0.0.1:{API_PORT + API_STRIDE}")
        self.assertEqual(env["health"], f"http://127.0.0.1:{API_PORT + API_STRIDE}/health")
        run0 = self.make_run("a", phase="build", repos={"API": {"worktree": self.wt["API"]}})
        self.assertEqual(prove.run_env(self.ws, run0, "API")["base_url"], f"http://127.0.0.1:{API_PORT}")

    def test_collector_ctx_carries_the_slot_env(self):
        seen = {}
        run = self.make_run("b", phase="build", repos={"API": {"worktree": self.wt["API"]}}, env_slots={"API": 1})
        check = {"id": "CHK-1", "type": "api", "repo": "API"}

        def fake_dispatch(chk, ctx):
            seen["base"] = ctx.env["base_url"]
            return {"status": "pass", "summary": "ok"}

        with mock.patch.object(prove, "dispatch", fake_dispatch), mock.patch.object(prove, "env_binding", return_value=None), \
                mock.patch.object(prove, "git_head", return_value="abc"), \
                mock.patch.object(prove, "target_worktree", return_value=Path(self.wt["API"])), \
                mock.patch.object(prove, "write_evidence", return_value={"id": "CHK-1", "status": "pass"}), \
                mock.patch.object(prove, "log_evidence"), \
                mock.patch.object(prove.integrity, "check_digest", return_value="d"), \
                mock.patch.object(prove.integrity, "run_heads", return_value={}):
            prove.collect_one(run, self.ws, check)
        self.assertEqual(seen["base"], f"http://127.0.0.1:{API_PORT + API_STRIDE}")

    def test_stale_reason_uses_the_run_slot(self):
        wt = self.tmp / "wt-api"
        wt.mkdir()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.kill)
        rec = {"pid": child.pid, "cwd": str(self.tmp / "elsewhere"), "head": "aaa", "started": core.now(),
               "create_time": envmod.process_create_time(child.pid)}
        self.write("env/ws/API@1.pid", rec)
        run1 = {"id": "b", "repos": {"API": {"worktree": str(wt)}}, "env_slots": {"API": 1}}
        run0 = {"id": "a", "repos": {"API": {"worktree": str(wt)}}}
        self.assertIn("serves", envmod.stale_reason(self.ws, run1, "API"))
        self.assertIsNone(envmod.stale_reason(self.ws, run0, "API"))  # slot 0 has no process
        self.assertIsNotNone(envmod.restart_blocker(self.ws, run1, "API"))  # serves a worktree that is not the run's own

    def test_env_state_probes_the_slot_health_url(self):
        probed = []
        run = {"id": "b", "repos": {}, "env_slots": {"API": 1}}
        with mock.patch.object(envmod, "health_status", side_effect=lambda url, t=3.0: probed.append(url)):
            st = envmod.env_state(self.ws, run, "API")
        self.assertEqual(probed, [f"http://127.0.0.1:{API_PORT + API_STRIDE}/health"])
        self.assertEqual(st["kind"], "unhealthy")

    def test_binding_error_reads_the_slot_record(self):
        wt = self.tmp / "wt-api"
        wt.mkdir()
        run1 = {"id": "b", "repos": {"API": {"worktree": str(wt)}}, "env_slots": {"API": 1}}
        self.assertIn("no pid record", envmod.binding_error(self.ws, "API", wt, "aaa", run=run1))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.kill)
        self.write("env/ws/API@1.pid", {"pid": child.pid, "cwd": str(wt.resolve()), "head": "aaa",
                                         "create_time": envmod.process_create_time(child.pid)})
        self.assertIsNone(envmod.binding_error(self.ws, "API", wt, "aaa", run=run1))
        self.assertIn("no pid record", envmod.binding_error(self.ws, "API", wt, "aaa"))  # slot 0 has none

    def test_batch_status_shows_each_childs_slots(self):
        self.make_run("p", kind="epic", phase="build", children=["a", "b"])
        self.make_run("a", phase="build", parent="p", env_slots={"API": 1, "React": 0},
                      tickets=[{"key": "DEV-1", "type": "feature"}])
        self.make_run("b", phase="build", parent="p", tickets=[{"key": "DEV-2", "type": "feature"}])
        rc, out = cli("--json", "batch", "status", "p")
        rows = {r["id"]: r for r in json.loads(out)["children"]}
        self.assertEqual((rows["a"]["slots"], rows["b"]["slots"]), ("API=1,React=0", "-"))
        rc, out = cli("batch", "status", "p")
        self.assertIn("lease\tslots\tprove", out)


class SharedRepoTests(SlotCase):
    api_slots = 1
    react_slots = 1
    shareable = True

    def test_runs_that_do_not_change_the_repo_share_it(self):
        for rid in ("s1", "s2"):
            self.run_for(rid, "React")
        self.assertEqual(envmod.lease_requests(self.ws, core.run_load("s1")), {"API": "shared", "React": "exclusive"})
        self.run_for("s2", "React")
        r1 = self.acquire("s1", repos=["API"])
        r2 = self.acquire("s2", repos=["API"])
        self.assertTrue(r1["granted"] and r2["granted"])
        self.assertEqual(r1["shared"], ["API"])
        shared = envmod.lease_read("ws", "API")["shared"]
        self.assertEqual([e["run"] for e in shared], ["s1", "s2"])
        self.assertIsNone(self.holder("API"))

    def test_exclusive_waits_while_shared_holders_exist_and_not_vice_versa(self):
        self.run_for("s1", "React")
        self.run_for("e", "API")
        self.run_for("s2", "React")
        self.acquire("s1", repos=["API"])
        res = self.acquire("e", wait=True)
        self.assertFalse(res["granted"])
        self.assertIn("shared by s1", res["blockers"][0])
        # a writer is waiting: new sharers queue behind it instead of starving it
        self.assertFalse(self.acquire("s2", repos=["API"])["granted"])
        envmod.lease_release(self.ws, "s1")
        self.assertTrue(self.acquire("e", wait=True)["granted"])
        self.assertEqual(self.holder("API"), "e")
        self.assertFalse(self.acquire("s2", repos=["API"])["granted"])  # exclusive holder: no sharers
        envmod.lease_release(self.ws, "e")
        self.assertTrue(self.acquire("s2", repos=["API"])["granted"])

    def test_exclusive_on_another_slot_does_not_wait_for_sharers(self):
        self.ws["repos"][0]["env"].update(slots=2, port_stride=API_STRIDE)
        self.write("workspaces/ws/workspace.json", self.ws)
        self.run_for("s1", "React")
        self.run_for("e", "API")
        self.acquire("s1", repos=["API"])
        res = self.acquire("e")
        self.assertEqual(res["slots"], {"API": 1})

    def test_stale_shared_holder_is_pruned(self):
        self.run_for("s1", "React")
        self.run_for("e", "API")
        self.acquire("s1", repos=["API"])
        lease = envmod.lease_read("ws", "API")
        lease["shared"][0].update(heartbeat="2020-01-01T00:00:00+00:00", pid=1)
        self.write("env/ws/API/lease.json", lease)
        res = self.acquire("e")
        self.assertTrue(res["granted"])
        self.assertEqual(res["reclaimed"][0][0], "s1")

    def test_up_of_a_shared_repo_never_starts_it(self):
        self.run_for("s1", "React")
        self.acquire("s1", repos=["API"])
        run = core.run_load("s1")
        self.assertTrue(envmod.is_shared(self.ws, run, "API"))
        with mock.patch.object(envmod, "_start_locked") as start, mock.patch.object(envmod, "health_ok", return_value=False):
            self.assertFalse(envmod._up_one(self.ws, run, "API", set(), out := []))
        start.assert_not_called()
        self.assertIn("ENV-FAIL repo=API kind=lease detail=shared repo API not up; ask its holder or start it with env up", out)
        with mock.patch.object(envmod, "_start_locked") as start, mock.patch.object(envmod, "health_ok", return_value=True):
            self.assertTrue(envmod._up_one(self.ws, run, "API", set(), out := []))
        start.assert_not_called()
        self.assertIn("shared", out[0])

    def test_shared_holder_cannot_stop_or_restart_it(self):
        self.run_for("s1", "React")
        self.acquire("s1", repos=["API"])
        run = core.run_load("s1")
        with self.assertRaises(core.FactoryError):
            envmod.down(self.ws, "API", "s1")
        self.assertIn("shared", envmod.restart_blocker(self.ws, run, "API"))
        self.assertIsNone(envmod.stale_reason(self.ws, run, "API"))
        self.assertEqual(envmod._stop_run_envs(self.ws, dict(run, repos={"API": {}}), {"API"}), [])

    def test_shared_binding_ignores_the_worktree(self):
        self.run_for("s1", "React")
        self.acquire("s1", repos=["API"])
        run = core.run_load("s1")
        self.assertIn("no pid record", envmod.binding_error(self.ws, "API", self.tmp, "aaa", run=run))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.kill)
        self.write("env/ws/API.pid", {"pid": child.pid, "cwd": str(self.tmp / "holder-wt"), "head": "zzz",
                                      "create_time": envmod.process_create_time(child.pid)})
        self.assertIsNone(envmod.binding_error(self.ws, "API", self.tmp, "aaa", run=run))

    def test_lease_off_keeps_shareable_inert(self):
        self.ws["env"]["lease"] = False
        run = self.run_for("s1", "React")
        self.assertFalse(envmod.is_shared(self.ws, run, "API"))

    def test_status_lists_sharers(self):
        self.run_for("s1", "React")
        self.acquire("s1", repos=["API"])
        rc, out = cli("env", "lease", "status", "ws", "--repo", "API")
        self.assertIn("shared by s1", out)


class HandoffTests(SlotCase):
    def test_rewrite_url_moves_only_the_fixed_origin(self):
        f = envmod.handoff_rewrite_url
        self.assertEqual(f("http://localhost:3300/portal/coming?token=a%2Fb&x=1#h", "localhost", 3300, 3400),
                         "http://localhost:3400/portal/coming?token=a%2Fb&x=1#h")
        self.assertEqual(f("http://localhost:3300", "localhost", 3300, 3400), "http://localhost:3400")
        self.assertEqual(f("http://localhost:3301/x", "localhost", 3300, 3400), "http://localhost:3301/x")
        self.assertEqual(f("http://other:3300/x", "localhost", 3300, 3400), "http://other:3300/x")
        self.assertEqual(f("http://localhost/x", "localhost", 3300, 3400), "http://localhost/x")
        self.assertEqual(f("https://LOCALHOST:3300/x", "localhost", 3300, 3400), "https://LOCALHOST:3400/x")
        self.assertEqual(f("http://other:3300/x", None, 3300, 3400), "http://other:3400/x")

    def test_origin_parsed_from_success_url_contains(self):
        self.assertEqual(envmod.handoff_origin("localhost:3300/portal/coming"), ("localhost", 3300))
        self.assertEqual(envmod.handoff_origin("http://127.0.0.1:3300/portal"), ("127.0.0.1", 3300))
        self.assertEqual(envmod.handoff_origin("/app"), (None, None))

    def test_login_config_for_a_slot(self):
        run = {"id": "b", "env_slots": {"React": 2}}
        cfg = envmod.login_config(self.ws, run, "React")
        self.assertEqual(cfg["success_url_contains"], f"localhost:{REACT_PORT + 200}/portal/coming")
        self.assertEqual(cfg["handoff"], {"host": "localhost", "from_port": REACT_PORT, "to_port": REACT_PORT + 200})

    def test_slot_zero_keeps_the_default_login(self):
        cfg = envmod.login_config(self.ws, {"id": "a"}, "React")
        self.assertNotIn("handoff", cfg)
        self.assertEqual(cfg, self.ws["repos"][1]["env"]["login"])

    def test_success_match_is_slot_aware_without_the_rewrite_flag(self):
        self.ws["repos"][1]["env"]["login"].pop("handoff_rewrite")
        cfg = envmod.login_config(self.ws, {"id": "b", "env_slots": {"React": 1}}, "React")
        self.assertNotIn("handoff", cfg)
        self.assertEqual(cfg["success_url_contains"], f"localhost:{REACT_PORT + 100}/portal/coming")

    def test_login_passes_the_slot_to_the_browser_cli(self):
        self.ws["env"]["lease"] = False
        sec = self.home / "secrets" / "ws.env"
        sec.parent.mkdir(parents=True, exist_ok=True)
        sec.write_text("REACT_USER=u@x.com\nREACT_PASS=pw\n", encoding="utf-8")
        seen = []

        def fake_run(cmd, **kw):
            seen.append((cmd, json.loads(Path(cmd[cmd.index("--login-json") + 1]).read_text())))
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        for slot in (0, 2):
            self.make_run("b", phase="build", repos={"React": {"worktree": self.wt["React"]}}, env_slots={"React": slot})
            with mock.patch.object(envmod.subprocess, "run", fake_run), mock.patch.object(envmod.shutil, "which", return_value="node"):
                ok, _ = envmod.login(self.ws, "React", None, "b")
            self.assertTrue(ok)
        (cmd0, cfg0), (cmd2, cfg2) = seen
        self.assertEqual(cmd0[cmd0.index("--base-url") + 1], f"http://localhost:{REACT_PORT}")
        self.assertNotIn("handoff", cfg0)
        self.assertEqual(cmd2[cmd2.index("--base-url") + 1], f"http://localhost:{REACT_PORT + 200}")
        self.assertEqual(cfg2["handoff"]["to_port"], REACT_PORT + 200)


class WorkspaceCheckTests(unittest.TestCase):
    def errors(self, **env):
        base = {"start": ["srv", "--port", "5000"], "health": "http://localhost:5000/", "base_url": "http://localhost:5000",
                "ports": [5000]}
        return wsmod._slot_errors("A", {**base, **env})

    def test_valid_blocks(self):
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.errors(slots=2, port_stride=10, shareable=True), [])
        self.assertEqual(self.errors(slots=2, port_stride=10, start=["srv", "--port", "{port}"]), [])
        self.assertEqual(self.errors(login={"success_url_contains": "localhost:3300/x", "handoff_rewrite": True}), [])

    def test_slots_rules(self):
        self.assertTrue(self.errors(slots=0))
        self.assertTrue(self.errors(slots=True))
        self.assertTrue(any("port_stride is required" in e for e in self.errors(slots=2)))
        self.assertTrue(self.errors(slots=2, port_stride=0))
        self.assertTrue(any("explicit port" in e for e in self.errors(slots=2, port_stride=5, base_url="http://localhost")))
        self.assertTrue(any("carries no port" in e for e in self.errors(slots=2, port_stride=5, start=["srv"])))
        self.assertTrue(any("{port1}" in e for e in self.errors(slots=2, port_stride=5, start=["srv", "{port1}"])))

    def test_shareable_and_handoff_rules(self):
        self.assertTrue(self.errors(shareable="yes"))
        self.assertTrue(self.errors(login={"handoff_rewrite": "yes"}))
        self.assertTrue(any("success_url_contains" in e for e in self.errors(login={"success_url_contains": "/app", "handoff_rewrite": True})))


if __name__ == "__main__":
    unittest.main()
