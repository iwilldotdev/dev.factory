"""Cross-process run.json safety, claims, `run log`, `batch status`, parent metrics attribution."""

import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_helpers import FactoryCase  # noqa: E402

from factory import core  # noqa: E402
from factory.cli import main  # noqa: E402
from factory.commands import metrics  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = main(list(argv))
    return rc, out.getvalue()


def spawn(code, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code), *args], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(proc):
    out, err = proc.communicate(timeout=120)
    return out.strip(), err


SAVE_RACE = """
    import sys, time
    from pathlib import Path
    from factory import core
    from factory.core import FactoryError
    barrier, mine, other, tag = sys.argv[1:5]
    run = core.run_load("ws-dev-1")
    Path(barrier, mine).write_text("1")
    while not Path(barrier, other).exists():
        time.sleep(0.01)
    run["decisions"].append({"text": tag})
    try:
        core.run_save(run)
        print("OK")
    except FactoryError as err:
        print("CONFLICT " + str(err))
"""

UPDATE_LOOP = """
    import sys
    from factory import core
    tag, n = sys.argv[1], int(sys.argv[2])
    for i in range(n):
        core.run_update("ws-dev-1", lambda r: core.event(r, "tick", who=tag, i=i))
"""


class ConcurrencyTests(FactoryCase):
    def test_write_json_tmp_names_unique_and_cleaned(self):
        target = self.tmp / "x" / "a.json"
        names = []
        real = os.replace

        def spy(src, dst):
            names.append(Path(src).name)
            return real(src, dst)

        with mock.patch("factory.core.os.replace", spy):
            core.write_json(target, {"a": 1})
            core.write_json(target, {"a": 2})
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(n.startswith("a.json.") and n.endswith(".tmp") and str(os.getpid()) in n for n in names))
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["a.json"])

    def test_write_json_retries_permission_error_and_cleans_up(self):
        target = self.tmp / "a.json"
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("sharing violation")
            return real(src, dst)

        with mock.patch("factory.core.os.replace", flaky):
            core.write_json(target, {"ok": True})
        self.assertEqual(json.loads(target.read_text()), {"ok": True})

        def always(src, dst):
            raise PermissionError("locked")

        with mock.patch("factory.core.os.replace", always), mock.patch("factory.core.time.sleep"):
            with self.assertRaises(PermissionError):
                core.write_json(target, {"ok": False})
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name != "home"], ["a.json"])

    def test_stale_lock_is_reclaimed(self):
        self.make_run()
        lock = core.run_dir("ws-dev-1") / "run.json.lock"
        lock.write_text("999")
        old = lock.stat().st_mtime - 3600
        os.utime(lock, (old, old))
        run = core.run_load("ws-dev-1")
        core.run_save(run)
        self.assertFalse(lock.exists())

    def test_live_lock_times_out(self):
        self.make_run()
        lock = core.run_dir("ws-dev-1") / "run.json.lock"
        lock.write_text("999")
        run = core.run_load("ws-dev-1")
        with mock.patch.object(core, "LOCK_TIMEOUT_S", 0.2):
            with self.assertRaises(core.FactoryError):
                core.run_save(run)

    def test_conflicting_saves_from_two_processes(self):
        self.make_run()
        barrier = self.tmp / "barrier"
        barrier.mkdir()
        a = spawn(SAVE_RACE, str(barrier), "a", "b", "A")
        b = spawn(SAVE_RACE, str(barrier), "b", "a", "B")
        results = [finish(a)[0], finish(b)[0]]
        self.assertEqual(sorted(r.split()[0] for r in results), ["CONFLICT", "OK"], results)
        conflict = next(r for r in results if r.startswith("CONFLICT"))
        self.assertIn("changed on disk by another process", conflict)
        winner = "A" if results[0] == "OK" else "B"
        disk = json.loads((core.run_dir("ws-dev-1") / "run.json").read_text())
        self.assertEqual([d["text"] for d in disk["decisions"]], [winner])  # nothing silently lost or merged
        self.assertNotIn("_loaded_rev", disk)

    def test_run_update_from_two_processes_loses_nothing(self):
        self.make_run()
        n = 15
        procs = [spawn(UPDATE_LOOP, tag, str(n)) for tag in ("a", "b")]
        for p in procs:
            out, err = finish(p)
            self.assertEqual(p.returncode, 0, err)
        disk = json.loads((core.run_dir("ws-dev-1") / "run.json").read_text())
        ticks = [e for e in disk["events"] if e["kind"] == "tick"]
        self.assertEqual(len(ticks), 2 * n)
        self.assertEqual(disk["rev"], 2 * n)
        self.assertFalse((core.run_dir("ws-dev-1") / "run.json.lock").exists())

    def test_same_process_stale_copy_conflicts(self):
        self.make_run()
        one, two = core.run_load("ws-dev-1"), core.run_load("ws-dev-1")
        core.run_save(one)
        with self.assertRaises(core.FactoryError):
            core.run_save(two)

    def test_resync_adopts_own_nested_write_only(self):
        self.make_run()
        outer = core.run_load("ws-dev-1")
        core.run_update("ws-dev-1", lambda r: core.event(r, "ship", verified=[]))
        core.run_resync(outer)
        self.assertEqual([e["kind"] for e in outer["events"]], ["ship"])
        core.run_save(outer)  # no conflict, nested event kept
        self.assertEqual([e["kind"] for e in core.run_load("ws-dev-1")["events"]], ["ship"])
        # a write by another process is not adopted
        outer = core.run_load("ws-dev-1")
        core.run_update("ws-dev-1", lambda r: None)
        core._SAVED_HERE["ws-dev-1"] = -1
        core.run_resync(outer)
        with self.assertRaises(core.FactoryError):
            core.run_save(outer)

    def test_new_run_saves_without_check(self):
        run = {"schemaVersion": 1, "id": "ws-new", "workspace": "ws", "phase": "intake"}
        core.run_save(run)
        core.run_save(core.run_load("ws-new"))
        self.assertEqual(core.run_load("ws-new")["rev"], 2)


class ClaimTests(FactoryCase):
    def setUp(self):
        super().setUp()
        for k in core.SESSION_ID_VARS:
            self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)
        self.make_run(phase="intake")

    def as_session(self, sid):
        os.environ["CLAUDE_CODE_SESSION_ID"] = sid

    def test_claim_refuse_and_takeover(self):
        self.as_session("aaaaaaaa-1111")
        rc, out = cli("run", "claim", "ws-dev-1")
        self.assertEqual(rc, 0, out)
        run = core.run_load("ws-dev-1")
        self.assertEqual(core.current_owner(run)["session"], "aaaaaaaa-1111")
        self.assertIn("pid", core.current_owner(run))
        self.assertEqual(run["events"][-1]["kind"], "claim")
        self.assertEqual(cli("run", "claim", "ws-dev-1")[0], 0)  # idempotent for the owner
        self.assertEqual(len(core.claims(core.run_load("ws-dev-1"))), 1)

        self.as_session("bbbbbbbb-2222")
        rc, out = cli("run", "claim", "ws-dev-1")
        self.assertEqual(rc, 2)
        self.assertIn("--takeover", out)
        rc, out = cli("run", "claim", "ws-dev-1", "--takeover")
        self.assertEqual(rc, 0, out)
        run = core.run_load("ws-dev-1")
        self.assertEqual(core.current_owner(run)["session"], "bbbbbbbb-2222")
        self.assertEqual(len(core.claims(run)), 2)
        self.assertTrue(run["events"][-1]["data"]["takeover"])

    def test_old_claim_is_not_refused(self):
        self.as_session("aaaaaaaa-1111")
        cli("run", "claim", "ws-dev-1")

        def age(run):
            run["sessions"][-1]["claimed_at"] = "2020-01-01T00:00:00+00:00"
        core.run_update("ws-dev-1", age)
        self.as_session("bbbbbbbb-2222")
        self.assertEqual(cli("run", "claim", "ws-dev-1")[0], 0)

    def test_claim_needs_session_id(self):
        rc, out = cli("run", "claim", "ws-dev-1")
        self.assertEqual(rc, 2)

    def test_ensure_owner_guard(self):
        run = core.run_load("ws-dev-1")
        core.ensure_owner(run)  # no claim
        self.as_session("aaaaaaaa-1111")
        cli("run", "claim", "ws-dev-1")
        run = core.run_load("ws-dev-1")
        core.ensure_owner(run)  # owner
        self.as_session("bbbbbbbb-2222")
        with self.assertRaises(core.FactoryError) as cm:
            core.ensure_owner(run)
        self.assertIn("aaaaaaaa", str(cm.exception))
        self.assertIn("--takeover", str(cm.exception))
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        core.ensure_owner(run)  # plain terminal: no guard

    def test_gate_refuses_other_session(self):
        self.as_session("aaaaaaaa-1111")
        cli("run", "claim", "ws-dev-1")
        self.as_session("bbbbbbbb-2222")
        rc, out = cli("gate", "ws-dev-1", "intake")
        self.assertEqual(rc, 2)
        self.assertIn("claimed by session aaaaaaaa", out)

    def test_prove_and_env_refuse_other_session(self):
        self.as_session("aaaaaaaa-1111")
        cli("run", "claim", "ws-dev-1")
        self.as_session("bbbbbbbb-2222")
        for argv in (("prove", "ws-dev-1"), ("baseline", "ws-dev-1")):
            rc, out = cli(*argv)
            self.assertEqual(rc, 2, (argv, out))
            self.assertIn("claimed by session aaaaaaaa", out, argv)

    def test_status_shows_owner(self):
        self.as_session("aaaaaaaa-1111")
        cli("run", "claim", "ws-dev-1")
        self.write("workspaces/ws/workspace.json", {"schemaVersion": 1, "name": "ws", "repos": []})
        rc, out = cli("run", "status", "ws-dev-1")
        self.assertIn("owner: session aaaaaaaa", out)

    def test_parent_side_mutators_use_update(self):
        for argv in (["run", "human", "ws-dev-1", "hands"], ["run", "block", "ws-dev-1", "env", "down"],
                     ["run", "decide", "ws-dev-1", "go"], ["run", "unblock", "ws-dev-1", "0"]):
            rc, out = cli(*argv)
            self.assertEqual(rc, 0, out)
        run = core.run_load("ws-dev-1")
        self.assertEqual(run["rev"], 4)
        self.assertEqual([e["kind"] for e in run["events"]], ["human", "block"])


class LogTests(FactoryCase):
    def test_log_formatting(self):
        events = [
            {"at": "2026-01-01T10:00:00+00:00", "kind": "gate_pass", "data": {"phase": "plan"}},
            {"at": "2026-01-01T10:05:00+00:00", "kind": "gate_fail", "data": {"phase": "build", "reasons": ["dirty", "x"]}},
            {"at": "2026-01-01T10:06:00+00:00", "kind": "evidence", "data": {"check": "CHK-1", "status": "pass"}},
            {"at": "2026-01-01T10:07:00+00:00", "kind": "review", "data": {"review_kind": "code", "round": 2, "verdict": "APPROVE", "findings": 0}},
            {"at": "2026-01-01T10:08:00+00:00", "kind": "human", "data": {"note": "typed"}},
            {"at": "2026-01-01T10:09:00+00:00", "kind": "rewind", "data": {"from": "review", "to": "build", "reason": "moved"}},
            {"at": "2026-01-01T10:10:00+00:00", "kind": "claim", "data": {"session": "abcdef123456", "takeover": True}},
            {"at": "2026-01-01T10:11:00+00:00", "kind": "weird", "data": {"a": 1, "b": "two", "c": None}},
        ]
        self.make_run(events=events)
        rc, out = cli("run", "log", "ws-dev-1")
        self.assertEqual(rc, 0, out)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d  gate_pass  gate plan passed$")
        self.assertIn("gate build failed: dirty (+1 more)", lines[1])
        self.assertIn("CHK-1 pass", lines[2])
        self.assertIn("code review round 2: APPROVE", lines[3])
        self.assertIn("human intervention: typed", lines[4])
        self.assertIn("rewind review -> build: moved", lines[5])
        self.assertIn("claimed by abcdef12 (takeover)", lines[6])
        self.assertTrue(lines[7].endswith("weird  a=1 b=two"), lines[7])
        rc, out = cli("run", "log", "ws-dev-1", "--tail", "2")
        self.assertEqual(len(out.strip().splitlines()), 2)
        rc, out = cli("run", "log", "ws-dev-1", "--since", "2026-01-01T10:09:30+00:00")
        self.assertEqual(len(out.strip().splitlines()), 2)
        rc, out = cli("--json", "run", "log", "ws-dev-1", "--tail", "1")
        self.assertEqual(json.loads(out)[0]["kind"], "weird")


class BatchStatusTests(FactoryCase):
    def setUp(self):
        super().setUp()
        self.write("workspaces/ws/workspace.json", {"schemaVersion": 1, "name": "ws", "repos": []})

    def child(self, rid, key, phase, **over):
        self.make_run(rid, tickets=[{"key": key, "type": "feature", "depends_on": over.pop("deps", [])}],
                      phase=phase, **over)

    def test_review_batch(self):
        mr = {"repo": "React", "iid": 7}
        self.child("ws-dev-1", "DEV-1", "blocked", kind="review", mrs=[mr], parent="ws-rb",
                   blockers=[{"kind": "env", "text": "x", "resolved": False}],
                   sessions=[{"session": "abcdef123456", "claimed_at": core.now(), "pid": 1}])
        self.child("ws-dev-2", "DEV-2", "done", kind="review", mrs=[{"repo": "Api", "iid": 9}], parent="ws-rb")
        self.make_run("ws-rb", kind="review-batch", phase="intake", children=["ws-dev-1", "ws-dev-2"])
        self.write("runs/ws-dev-1/acs.json", {"acs": [], "checks": [
            {"id": "CHK-1", "type": "human"}, {"id": "CHK-2", "type": "human"}, {"id": "CHK-3", "type": "test"}]})
        self.write("runs/ws-dev-1/evidence/CHK-1.json", {"status": "pass"})
        self.write("runs/ws-dev-1/evidence/CHK-2.json", {"status": "fail"})
        self.write("env/ws/lease.json", {"run": "ws-dev-2"})
        rc, out = cli("--json", "batch", "status", "ws-rb")
        self.assertEqual(rc, 0, out)
        rows = {r["id"]: r for r in json.loads(out)["children"]}
        a = rows["ws-dev-1"]
        self.assertEqual((a["key"], a["phase"], a["review_mrs"], a["open_blockers"]), ("DEV-1", "blocked", ["React!7"], 1))
        self.assertEqual(a["open_human_checks"], 1)
        self.assertEqual(a["owner"], "abcdef12")
        self.assertEqual(a["lease"], "ws-dev-2")
        self.assertIn("resolve blockers", a["next"])
        self.assertEqual(rows["ws-dev-2"]["review_mrs"], ["Api!9"])
        self.assertEqual(rows["ws-dev-2"]["open_human_checks"], 0)
        self.assertIsNone(rows["ws-dev-2"]["owner"])
        rc, out = cli("batch", "status", "ws-rb")
        self.assertIn("React!7", out)

    def test_epic_and_lease_absent(self):
        self.child("ws-dev-1", "DEV-1", "done", parent="ws-ep")
        self.child("ws-dev-2", "DEV-2", "build", parent="ws-ep", deps=["DEV-1"], wave=1)
        self.make_run("ws-ep", kind="epic", phase="plan", children=["ws-dev-1", "ws-dev-2"])
        rc, out = cli("--json", "batch", "status", "ws-ep")
        self.assertEqual(rc, 0, out)
        rows = {r["id"]: r for r in json.loads(out)["children"]}
        self.assertIsNone(rows["ws-dev-2"]["lease"])
        self.assertTrue(rows["ws-dev-2"]["ready"])
        self.assertEqual(rows["ws-dev-2"]["next"] != "", True)
        # epic status keeps its own compatible shape
        rc, out = cli("--json", "epic", "status", "ws-ep")
        self.assertEqual(rc, 0, out)
        erows = json.loads(out)["children"]
        self.assertEqual(sorted(erows[0]), ["depends_on", "id", "key", "loops", "mrs", "open_blockers", "phase", "ready", "wave"])
        self.assertEqual(json.loads(out)["ready"], ["DEV-2"])

    def test_rejects_other_kinds(self):
        self.make_run("ws-dev-1", kind="ticket")
        self.assertEqual(cli("batch", "status", "ws-dev-1")[0], 2)


class MetricsChildAttributionTests(FactoryCase):
    def test_parent_includes_child_transcripts_once(self):
        root = self.tmp / "claude" / "projects" / "proj"
        root.mkdir(parents=True)
        p = mock.patch.object(metrics, "claude_roots", lambda *_a: [root.parent])
        p.start()
        self.addCleanup(p.stop)
        self.make_run("ws-ep", kind="epic", children=["ws-dev-1", "ws-dev-2"], sessions=["s-parent"])

        def rec(text):
            return json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z",
                               "message": {"role": "user", "content": text}}) + "\n"
        (root / "s-parent.jsonl").write_text(rec("factory-run: ws-ep"), encoding="utf-8")
        (root / "c1.jsonl").write_text(rec("factory-run: ws-dev-1"), encoding="utf-8")
        (root / "c2.jsonl").write_text(rec("factory-run: ws-dev-2 and factory-run: ws-ep"), encoding="utf-8")
        (root / "other.jsonl").write_text(rec("factory-run: ws-zzz"), encoding="utf-8")
        run = core.run_load("ws-ep")
        names = sorted(Path(t).name for t in metrics.find_transcripts(run))
        self.assertEqual(names, ["c1.jsonl", "c2.jsonl", "s-parent.jsonl"])

    def test_claim_dicts_in_sessions_are_tolerated(self):
        self.make_run("ws-dev-1", sessions=[{"session": "sess1", "claimed_at": "x", "pid": 1}])
        self.assertEqual(metrics.find_transcripts(core.run_load("ws-dev-1")), [])


if __name__ == "__main__":
    unittest.main()
