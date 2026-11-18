"""Regression tests for the audit of DESIGN sections 11 and 14 (per-repo leases, slots, fast verification).

One class per finding: F1 shared spec amend, F2 workspace absorbed by amend, F3 storage per slot, F4 resolved env
binding, F5 hot reload never in final evidence, F6 fast evidence reuse, F7 slot env errors, F8 legacy lease marker,
F9 shared repo heads, F10 one reusable() per check per prove.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_support  # noqa: E402
from test_env_slots import API_PORT, API_STRIDE, REACT_PORT, SlotCase  # noqa: E402
from test_failfast_prove import ProveCase, cli  # noqa: E402
from test_prove_batch import BatchCase  # noqa: E402
from test_prove_fast import AmendTests as _Amend  # noqa: E402

from factory import core, integrity  # noqa: E402
from factory.collectors import api as api_collector  # noqa: E402
from factory.collectors import browser  # noqa: E402
from factory.core import Ctx  # noqa: E402
from factory.commands import env as envmod  # noqa: E402
from factory.commands import gate, prove  # noqa: E402


class AmendCase(_Amend):
    """AmendTests fixtures only (its own tests run in test_prove_fast)."""


for _name in [n for n in dir(_Amend) if n.startswith("test_")]:
    setattr(AmendCase, _name, None)
del _Amend  # its tests are collected from test_prove_fast only


def _evidence(run_id: str, cid: str, body: dict) -> None:
    run = core.run_load(run_id)
    core.write_json(core.evidence_file(run_id, cid), body)
    core.event(run, "evidence", check=cid, digest=core.file_sha256(core.evidence_file(run_id, cid)))
    core.write_json(core.run_dir(run_id) / "run.json", run)


class F1SharedSpecTests(AmendCase):
    def two_checks_one_spec(self):
        b2 = dict(self.browser(), id="CHK-2")
        self.start([self.browser(), b2])
        digest = integrity.check_digest(self.run_id, b2)
        _evidence(self.run_id, "CHK-2", {"status": "pass", "type": "browser", "details": {"check_digest": digest}})
        self.assertIsNone(integrity.evidence_error(core.run_load(self.run_id), "CHK-2"))

    def test_selector_amend_refuses_a_spec_read_by_another_check(self):
        self.two_checks_one_spec()
        (core.run_dir(self.run_id) / "checks" / "a.spec.ts").write_text("CHANGED", encoding="utf-8")
        rc, out = self.amend("selector")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("checks/a.spec.ts (read by CHK-2)", out)
        self.assertFalse([e for e in core.run_load(self.run_id)["events"] if e["kind"] == "amend_check"])

    def test_evidence_is_stale_once_a_file_it_read_changed_even_after_a_keep_seq_relock(self):
        self.two_checks_one_spec()
        (core.run_dir(self.run_id) / "checks" / "a.spec.ts").write_text("CHANGED", encoding="utf-8")
        run = core.run_load(self.run_id)
        integrity.lock_plan(run, "simulated relock", keep_seq=True)  # any path that relocks without a replan
        self.assertEqual(integrity.lock_errors(run), [])
        self.assertIn("check_digest", integrity.evidence_error(run, "CHK-2"))

    def test_evidence_without_check_digest_is_judged_as_before(self):
        self.start([self.browser()])
        _evidence(self.run_id, "CHK-1", {"status": "pass", "type": "browser"})
        (core.run_dir(self.run_id) / "checks" / "a.spec.ts").write_text("CHANGED", encoding="utf-8")
        self.assertIsNone(integrity.evidence_error(core.run_load(self.run_id), "CHK-1"))

    def test_selector_amend_of_an_unshared_spec_still_works(self):
        self.start([self.browser(), {"id": "CHK-2", "type": "test", "repo": "React", "cmd": "true"}])
        (core.run_dir(self.run_id) / "checks" / "a.spec.ts").write_text("two", encoding="utf-8")
        rc, out = self.amend("selector")
        self.assertEqual(rc, 0, out)


class F2WorkspaceTests(AmendCase):
    def test_amend_is_refused_after_a_workspace_edit_and_does_not_absorb_it(self):
        self.start([self.browser()])
        ws = core.workspace_load("ws")
        ws["autonomy"]["max_replans"] = 99
        ws["autonomy"]["allow_timing_amends"] = True
        core.write_json(core.workspace_path("ws"), ws)
        self.edit(timeout_s=120)
        rc, out = self.amend()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("workspace changed since plan lock: replan", out)
        run = core.run_load(self.run_id)
        self.assertTrue(any("workspace changed" in e for e in integrity.lock_errors(run)))
        self.assertNotEqual(integrity.locked_autonomy(run).get("max_replans"), 99)

    def test_relock_for_amend_carries_the_locked_workspace(self):
        self.start([self.browser()])
        run = core.run_load(self.run_id)
        before = dict(run["plan_lock"])
        ws = core.workspace_load("ws")
        ws["autonomy"]["max_replans"] = 99
        core.write_json(core.workspace_path("ws"), ws)
        integrity.lock_plan(run, "amend-check CHK-1", keep_seq=True, keep_workspace=True)
        self.assertEqual(run["plan_lock"]["workspace"], before["workspace"])
        self.assertEqual(run["plan_lock"]["autonomy"], before["autonomy"])
        self.assertIsNotNone(integrity.workspace_error(run))


class F3StorageSlotTests(SlotCase):
    def test_storage_and_login_files_are_keyed_by_slot(self):
        self.assertEqual(envmod.storage_state_path("ws", "React").name, "React.storage.json")
        self.assertEqual(envmod.storage_state_path("ws", "React", None, 0).name, "React.storage.json")
        self.assertEqual(envmod.storage_state_path("ws", "React", None, 2).name, "React@2.storage.json")
        self.assertEqual(envmod.storage_state_path("ws", "React", "A@b.c", 2).name, "React@2.a_b_c.storage.json")
        self.assertEqual(envmod.login_json_path("ws", "React").name, "React.login.json")
        self.assertEqual(envmod.login_json_path("ws", "React", 1).name, "React@1.login.json")

    def test_login_writes_the_slot_files(self):
        self.ws["env"]["lease"] = False
        sec = self.home / "secrets" / "ws.env"
        sec.parent.mkdir(parents=True, exist_ok=True)
        sec.write_text("REACT_USER=u@x.com\nREACT_PASS=pw\n", encoding="utf-8")
        seen = []

        def fake_run(cmd, **kw):
            seen.append((Path(cmd[cmd.index("--login-json") + 1]).name, Path(cmd[cmd.index("--out") + 1]).name))
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        for slot in (0, 2):
            self.make_run("b", phase="build", repos={"React": {"worktree": self.wt["React"]}}, env_slots={"React": slot})
            with mock.patch.object(envmod.subprocess, "run", fake_run), mock.patch.object(envmod.shutil, "which", return_value="node"):
                self.assertTrue(envmod.login(self.ws, "React", None, "b")[0])
        self.assertEqual(seen, [("React.login.json", "React.storage.json"), ("React@2.login.json", "React@2.storage.json")])

    def test_browser_collector_uses_the_slot_storage_only(self):
        run = self.make_run("b", phase="build", repos={"React": {"worktree": self.wt["React"]}}, env_slots={"React": 2})
        ctx = Ctx(run=run, workspace=self.ws, run_dir=core.run_dir("b"), repo_path=None, env={}, out_dir=None)
        slot0 = envmod.storage_state_path("ws", "React")
        slot0.parent.mkdir(parents=True, exist_ok=True)
        slot0.write_text("{}", encoding="utf-8")
        self.assertIsNone(browser.storage_state({"repo": "React"}, ctx))  # never slot 0's session
        slot2 = envmod.storage_state_path("ws", "React", None, 2)
        slot2.write_text("{}", encoding="utf-8")
        self.assertEqual(browser.storage_state({"repo": "React"}, ctx), str(slot2))
        ctx0 = Ctx(run=dict(run, env_slots={"React": 0}), workspace=self.ws, run_dir=core.run_dir("b"), repo_path=None,
                   env={}, out_dir=None)
        self.assertEqual(browser.storage_state({"repo": "React"}, ctx0), str(slot0))


class F4EnvDigestTests(SlotCase):
    def setUp(self):
        super().setUp()
        react = self.ws["repos"][1]["env"]
        react["env_vars"] = {"API_URL": "{base_url:API}/v1"}
        Path(self.wt["React"]).mkdir(parents=True, exist_ok=True)

    def record(self, run, **extra):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        rec = {"pid": child.pid, "cwd": str(Path(self.wt["React"]).resolve()), "head": "h1",
               "create_time": envmod.process_create_time(child.pid), **extra}
        key = envmod.slot_key("React", envmod.run_slot(run, "React"))
        self.write(f"env/ws/{key}.pid", rec)

    def test_moved_dependency_slot_makes_the_binding_and_the_env_stale(self):
        started = {"id": "b", "env_slots": {"API": 0, "React": 1}}
        digest = envmod.env_digest(envmod.slot_env(self.ws, started, "React"))
        self.record(started, env_digest=digest)
        now_run = self.make_run("b", phase="build", repos={"React": {"worktree": self.wt["React"]}},
                                env_slots={"API": 0, "React": 1})
        self.assertIsNone(envmod.binding_error(self.ws, "React", Path(self.wt["React"]), "h1", run=now_run))
        moved = self.make_run("b", phase="build", repos={"React": {"worktree": self.wt["React"]}},
                              env_slots={"API": 1, "React": 1})
        err = envmod.binding_error(self.ws, "React", Path(self.wt["React"]), "h1", run=moved)
        self.assertIn("another resolved env", err)
        self.assertEqual(prove.binding_kind(err), "stale")
        with mock.patch.object(envmod, "_head_of", return_value="h1"):
            self.assertIn("another resolved env", envmod.stale_reason(self.ws, moved, "React"))

    def test_record_without_digest_keeps_todays_behaviour(self):
        moved = self.make_run("b", phase="build", repos={"React": {"worktree": self.wt["React"]}},
                              env_slots={"API": 1, "React": 1})
        self.record(moved)
        self.assertIsNone(envmod.binding_error(self.ws, "React", Path(self.wt["React"]), "h1", run=moved))
        with mock.patch.object(envmod, "_head_of", return_value="h1"):
            self.assertIsNone(envmod.stale_reason(self.ws, moved, "React"))

    def test_up_records_the_digest(self):
        self.ws["env"]["lease"] = False
        env = envmod.slot_env(self.ws, None, "API", 0)
        Path(self.wt["API"]).mkdir(parents=True, exist_ok=True)
        with mock.patch.object(envmod.subprocess, "Popen") as popen, \
                mock.patch.object(envmod, "port_in_use", return_value=False), \
                mock.patch.object(envmod, "health_ok", return_value=False), \
                mock.patch.object(envmod, "process_create_time", return_value="t"):
            popen.return_value.pid = 4242
            status, _ = envmod._start_locked(env, "ws", "API", "http://x/", Path(self.wt["API"]), "h", [], 0)
        self.assertEqual(status, "started")
        self.assertEqual(envmod.read_record("ws", "API")["env_digest"], envmod.env_digest(env))


class F5HotReloadTests(unittest.TestCase):
    def run_stale(self, strict):
        ws = {"name": "ws", "repos": [{"alias": "Web", "name": "web", "path": "/p", "env": {"hot_reload": True}}]}
        rec = {"pid": 1, "cwd": str(Path("/wt")), "head": "old"}
        with mock.patch.object(envmod, "read_record", return_value=rec), \
                mock.patch.object(envmod, "record_state", return_value="same"), \
                mock.patch.object(envmod, "is_protected_pid", return_value=False), \
                mock.patch.object(envmod, "_cwd_for", return_value=Path("/wt")), \
                mock.patch.object(envmod, "_same_path", return_value=True), \
                mock.patch.object(Path, "is_dir", return_value=True), \
                mock.patch.object(envmod, "_head_of", return_value="new"):
            stale = envmod.stale_reason(ws, {"id": "r"}, "Web", strict=strict)
            bound = envmod.binding_error(ws, "Web", Path("/wt"), "new", strict=strict)
            drift = envmod.hot_reload_drift(ws, "Web", "new")
        return stale, bound, drift

    def test_final_mode_treats_hot_reload_like_a_normal_repo(self):
        stale, bound, drift = self.run_stale(strict=True)
        self.assertIn("started at", stale)
        self.assertIn("started at", bound)
        self.assertTrue(drift)

    def test_default_mode_skips_the_restart(self):
        stale, bound, drift = self.run_stale(strict=False)
        self.assertIsNone(stale)
        self.assertIsNone(bound)
        self.assertTrue(drift)


class F5HotReloadProveTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live, hot_reload=True)

    def errors(self, chk):
        return gate._evidence_errors("r1", core.run_load("r1"), core.workspace_load("ws"), chk)

    def test_hot_reload_evidence_is_refused_until_final(self):
        chk = self.api_check("A1")
        self.acs([chk])
        with mock.patch.object(prove, "hot_reload_drift", return_value=True):
            self.assertEqual(cli("prove", "r1")[0], 0)
            self.assertTrue(self.evidence("A1")["details"]["hot_reload"])
            self.assertTrue(any("hot-reloaded" in e and "--final" in e for e in self.errors(chk)), self.errors(chk))
            self.assertEqual(cli("prove", "r1", "--final")[0], 0)
        self.assertNotIn("hot_reload", self.evidence("A1")["details"])
        self.assertTrue(any(c.kwargs.get("strict") for c in prove.env_binding.call_args_list))
        self.assertEqual(self.errors(chk), [])

    def test_final_preflight_is_strict(self):
        self.acs([self.api_check("A1")])
        with mock.patch.object(envmod, "stale_reason", return_value=None) as stale:
            cli("prove", "r1", "--final")
            self.assertTrue(stale.call_args.kwargs.get("strict"))
            cli("prove", "r1")
            self.assertFalse(stale.call_args.kwargs.get("strict"))


class F6FastReuseTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)

    def test_fast_evidence_is_reused_only_by_fast(self):
        self.acs([self.api_check("A1")])
        self.assertEqual(cli("prove", "r1", "--fast")[0], 0)
        with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
            rc, out, _ = cli("prove", "r1", "--fast")
        self.assertEqual(spy.call_count, 0, out)
        with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(self.evidence("A1")["details"]["mode"], "default")


class F7SlotErrorTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live)
        self.make_run("r1", phase="prove", repos={"Web": {"worktree": str(self.web), "branch": "b"}}, env_slots={"Web": 3})

    def test_run_env_raises_instead_of_falling_back_to_slot_zero(self):
        with self.assertRaises(core.FactoryError):
            prove.run_env(core.workspace_load("ws"), core.run_load("r1"), "Web")

    def test_prove_records_a_setup_error(self):
        self.acs([self.api_check("A1")])
        with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 1, out)
        self.assertEqual(spy.call_count, 0)
        ev = self.evidence("A1")
        self.assertEqual(ev["status"], "error")
        self.assertIn("slot 3", ev["summary"])
        self.assertEqual(ev["details"]["fail_kind"], "setup")
        self.assertIn("PROVE-FAIL check=A1 kind=setup", out)


class F8LegacyMarkerTests(SlotCase):
    api_slots = 1
    react_slots = 1

    def marker(self):
        path = envmod.lease_path("ws")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def test_marker_follows_per_repo_holders(self):
        self.run_for("a", "API")
        self.run_for("b", "React")
        self.assertTrue(self.acquire("a")["granted"])
        m = self.marker()
        self.assertTrue(envmod.is_legacy_marker(m))
        self.assertEqual(m["run"], "a")
        # an old factory version's view: held by a live run, not stale
        self.assertIsNone(envmod.lease_stale(m, 900))
        self.assertTrue(self.acquire("b", repos=["React"])["granted"])
        self.assertEqual(self.marker()["holders"], ["a", "b"])
        envmod.lease_release(self.ws, "a")
        self.assertEqual(self.marker()["run"], "b")  # moves to a live holder
        envmod.lease_release(self.ws, "b")
        self.assertIsNone(self.marker())

    def test_new_code_reads_the_marker_as_free(self):
        self.run_for("a", "API")
        self.run_for("b", "React")
        self.acquire("a")
        self.assertIsNone(envmod.lease_read("ws").get("run"))
        self.assertFalse(envmod.lease_held(self.ws, "a")["legacy"])
        self.assertIsNone(envmod.lease_status(self.ws, 900)["legacy"])
        with self.assertRaises(core.FactoryError):  # the marker never grants another repo
            envmod.require_lease(self.ws, "a", "env up React", "React")
        self.assertTrue(self.acquire("b", repos=["React"])["granted"])  # and never blocks one

    def test_a_real_legacy_holder_is_never_overwritten(self):
        self.run_for("old", "API")
        self.write("env/ws/lease.json", {"run": "old", "pid": 0, "heartbeat": core.now(), "queue": []})
        self.assertTrue(self.acquire("old")["granted"])
        self.assertFalse(envmod.is_legacy_marker(self.marker()))


class F9SharedHeadsTests(SlotCase):
    api_slots = 1
    react_slots = 1
    shareable = True

    def test_shared_heads_name_the_serving_code(self):
        from ops_helpers import make_repo
        holder = make_repo(self.tmp / "holder-wt").resolve()
        self.write("env/ws/API.pid", {"pid": 1, "cwd": str(holder), "head": "x"})
        run = self.run_for("s1", "React")
        self.assertEqual(envmod.shared_heads(self.ws, run), {"API": core.git_head(holder)})
        self.ws["env"]["lease"] = False
        self.assertEqual(envmod.shared_heads(self.ws, run), {})

    def test_runtime_evidence_records_shared(self):
        rec = {}

        def fake_write(run_id, check, body, *a, **kw):
            rec.update(body)
            return {"check_id": check["id"], "status": body["status"], "details": body["details"]}

        run = self.run_for("s1", "React")
        with mock.patch.object(prove, "shared_env_heads", return_value={"API": "abc"}), \
                mock.patch.object(prove, "dispatch", return_value={"status": "pass", "summary": "ok"}), \
                mock.patch.object(prove, "env_binding", return_value=None), \
                mock.patch.object(prove, "git_head", return_value="h"), \
                mock.patch.object(prove, "target_worktree", return_value=Path(self.wt["React"])), \
                mock.patch.object(prove, "write_evidence", side_effect=fake_write), \
                mock.patch.object(prove, "log_evidence"):
            prove.collect_one(run, self.ws, {"id": "CHK-1", "type": "browser", "repo": "React", "spec": "s"})
        self.assertEqual(rec["details"]["shared"], {"API": "abc"})


class F10ReuseCacheTests(BatchCase):
    def test_reusable_runs_once_per_check(self):
        self.acs([self.bcheck("B1"), self.bcheck("B2"), self.bcheck("B3"), self.bcheck("B4")])
        self.assertEqual(cli("prove", "r1")[0], 0)
        self.acs([self.bcheck("B1", timeout_s=21), self.bcheck("B2", timeout_s=21), self.bcheck("B3"), self.bcheck("B4")])
        with mock.patch.object(prove, "reusable", wraps=prove.reusable) as spy:
            rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(spy.call_count, 4)  # was O(n^2): batch planning asked again for every later check
        self.assertIn("REUSED B3", out)


class A1QualityModeTests(unittest.TestCase):
    def prior(self, mode_of_evidence, mode):
        ev = {"status": "pass", "dirty": False, "git_sha": "abc",
              "details": {"mode": mode_of_evidence, "check_digest": "d"}}
        with mock.patch.object(prove, "load_evidence", return_value=ev),                 mock.patch.object(prove, "repo_path_for_run", return_value=Path(".")),                 mock.patch.object(prove, "git_dirty", return_value=False),                 mock.patch.object(prove, "git_head", return_value="abc"),                 mock.patch.object(prove, "quality_digest", return_value="d"),                 mock.patch.object(prove.integrity, "evidence_error", return_value=None):
            return prove.quality_prior({"id": "r1"}, {}, "Web", mode)

    def test_fast_quality_evidence_is_reused_only_by_fast(self):
        self.assertIsNotNone(self.prior("fast", "fast"))
        self.assertIsNone(self.prior("fast", "default"))
        self.assertIsNone(self.prior("fast", "final"))
        self.assertIsNotNone(self.prior("default", "default"))


class A2HotReloadReuseTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live, hot_reload=True)

    def test_hot_reload_evidence_is_not_reused_outside_fast(self):
        self.acs([self.api_check("A1")])
        with mock.patch.object(prove, "hot_reload_drift", return_value=True):
            self.assertEqual(cli("prove", "r1")[0], 0)
            self.assertTrue(self.evidence("A1")["details"]["hot_reload"])
            with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
                self.assertEqual(cli("prove", "r1")[0], 0)
            self.assertEqual(spy.call_count, 1)  # re-collected, not reused
            with mock.patch.object(api_collector, "collect", wraps=api_collector.collect) as spy:
                self.assertEqual(cli("prove", "r1", "--fast")[0], 0)
            self.assertEqual(spy.call_count, 0)  # fast may reuse it


class A3DependencyBindingTests(ProveCase):
    def setUp(self):
        super().setUp()
        self.set_ws(self.live, depends_on=["Dep"])
        self.ws["repos"].append({"alias": "Dep", "name": "dep", "path": str(self.web), "env": {"start": ["x"], "cwd": "."}})
        self.write("workspaces/ws/workspace.json", self.ws)

    def test_closure(self):
        self.assertEqual(prove.dep_closure(self.ws, ["Web"]), ["Dep"])
        self.assertEqual(prove.dep_closure(self.ws, ["Dep"]), [])

    def test_dependency_binding_failure_is_an_env_error(self):
        stale = "env Dep: started at abcd1234, worktree HEAD is ffff0000; `factory env down`"
        prove.env_binding.side_effect = lambda ws, alias, *a, **k: stale if alias == "Dep" else None
        self.acs([self.api_check("A1"), self.api_check("A2")])
        rc, out, _ = cli("prove", "r1")
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.evidence("A1")["details"]["fail_kind"], "env")
        self.assertIn("env Dep", self.evidence("A1")["summary"])
        self.assertIn("ENV-FAIL repo=Web kind=stale", out)

    def test_preflight_covers_dependencies_and_final_is_strict(self):
        self.acs([self.api_check("A1")])
        with mock.patch.object(envmod, "stale_reason", return_value=None) as stale:
            cli("prove", "r1", "--final")
        self.assertEqual([c.args[2] for c in stale.call_args_list], ["Web", "Dep"])
        self.assertTrue(all(c.kwargs.get("strict") for c in stale.call_args_list))


if __name__ == "__main__":
    unittest.main()
