"""Run lifecycle: start, status, runs, log, retype, repos, close, delete."""
import json
import unittest

from tests.helpers import FactoryCase, lesson


class StartTests(FactoryCase):
    def test_start_creates_the_run_directory_and_one_event(self):
        run = self.start("ABC-1", "--kind", "bug")
        evs = self.events(run)
        self.assertEqual([e["type"] for e in evs], ["run_started"])
        self.assertEqual((evs[0]["ticket"], evs[0]["kind"], evs[0]["repos"]), ("ABC-1", "bug", ["web"]))
        self.assertFalse((run / "run.json").exists(), "there is no run.json: state is the events")

    @lesson("L-157")
    def test_two_tickets_mapping_to_one_folder_are_an_error_naming_both(self):
        """L-157: ids that map to the same safe name are refused, naming both ids."""
        self.ok("run", "start", "grp/proj#4")
        err = self.fails("run", "start", "grp-proj-4")
        self.assertIn("'grp/proj#4'", err)
        self.assertIn("'grp-proj-4'", err)
        self.assertTrue((self.proj / ".factory" / "runs" / "grp-proj-4" / "events.jsonl").is_file())

    def test_starting_the_same_ticket_twice_is_an_error(self):
        self.ok("run", "start", "ABC-1")
        self.assertIn("already exists", self.fails("run", "start", "ABC-1"))

    def test_invalid_kind_and_unknown_repo_are_refused_with_the_valid_values(self):
        self.assertIn("feature, bug, hotfix, epic, review", self.fails("run", "start", "A-1", "--kind", "story"))
        self.assertIn("repos: web", self.fails("run", "start", "A-1", "--repos", "nope"))
        self.assertEqual(self.cli("runs", "--json")[1].strip(), "[]")

    def test_repos_default_to_the_only_repo_and_stay_empty_in_a_workspace(self):
        ws = self.TOML + '\n[[repo]]\npath = "../other"\nname = "other"\n'
        self.write("factory.toml", ws)
        run = self.start("A-1")
        self.assertEqual(self.events(run)[0]["repos"], [])
        self.ok("run", "repos", "web,other", "--run", "A-1")
        self.assertEqual(self.cli("status", "A-1", "--json")[1].count('"web"'), 1)


class StatusListLogTests(FactoryCase):
    def test_runs_status_and_log(self):
        self.start("A-1")
        self.start("B-2", "--kind", "bug")
        rows = json.loads(self.ok("runs", "--json"))
        self.assertEqual([(r["run"], r["kind"], r["state"]) for r in rows], [("A-1", None, "open"), ("B-2", "bug", "open")])
        self.assertIn("pass a run id", self.ok("status"))
        status = json.loads(self.ok("status", "B-2", "--json"))
        self.assertEqual((status["phase"], status["kind"], status["ticket"]), ("intake", "bug", "B-2"))
        log = self.ok("log", "A-1")
        self.assertIn("run_started", log)
        self.assertIn("ticket=", log)

    def test_log_tail_and_single_open_run_is_implicit(self):
        self.start("A-1")
        self.ok("human", "--kind", "correction", "one")
        self.ok("human", "--kind", "correction", "two")
        self.assertEqual(len(self.ok("log").strip().splitlines()), 3)
        tail = self.ok("log", "--tail", "1").strip().splitlines()
        self.assertEqual(len(tail), 1)
        self.assertIn("two", tail[0])
        self.assertIn("run A-1", self.ok("status"))

    def test_several_open_runs_need_an_explicit_id_for_commands_that_write(self):
        self.start("A-1")
        self.start("B-2")
        self.assertIn("2 open runs (A-1, B-2)", self.fails("human", "--kind", "correction", "x"))

    def test_no_run_gives_a_hint(self):
        self.assertIn("factory run start", self.ok("status"))
        self.assertIn("no open run", self.fails("next"))

    @lesson("L-103")
    def test_status_names_a_missing_worktree_and_close_ends_the_run_without_gates(self):
        """L-103: status reports a missing worktree explicitly; `run close --reason` ends a run without gates."""
        run = self.start("A-1")
        gone = self.base / "wt" / "web"
        from dev_factory.run import Run
        Run(run.parent, "A-1").append({"type": "worktree", "repo": "web", "path": str(gone), "branch": "b", "branch_point": "0" * 40, "throwaway": False})
        out = self.ok("status", "A-1")
        self.assertIn("worktree is missing", out)
        self.assertIn(gone.as_posix(), out)
        self.assertIn("--reason", self.fails("run", "close"))
        self.ok("run", "close", "--reason", "branches were rebased and removed", "--run", "A-1")
        closed = json.loads(self.ok("status", "A-1", "--json"))
        self.assertEqual((closed["phase"], closed["closed"]), ("closed", "branches were rebased and removed"))
        self.assertEqual([e["type"] for e in self.events(run)][-1], "closed")
        self.assertIn("is closed", self.fails("run", "close", "--reason", "again", "--run", "A-1"))
        self.assertIn("is closed", self.fails("human", "--kind", "correction", "x", "--run", "A-1"))
        self.assertIn("the run is closed", self.ok("next", "A-1"))


class RetypeTests(FactoryCase):
    @lesson("L-31")
    def test_retype_changes_the_kind_archives_nothing_and_reruns_intake(self):
        """L-32, L-31: a kind change is an event on the same run; the intake gate has to pass again."""
        run = self.start("A-1", "--kind", "feature")
        self.ingest_ticket(run, id="A-1", type="bug")
        code, out, _ = self.cli("gate", "intake")
        self.assertEqual(code, 1)
        self.assertIn("ticket contract type is bug", out)
        self.ok("run", "retype", "--kind", "bug")
        self.ok("gate", "intake")
        self.assertEqual(json.loads(self.ok("status", "--json"))["phase"], "plan")
        self.ok("run", "retype", "--kind", "feature")
        self.assertEqual(json.loads(self.ok("status", "--json"))["phase"], "intake")
        self.assertEqual(self.cli("gate", "intake")[0], 1)
        self.assertTrue((run / "contracts" / "ticket" / "A-1.json").is_file())
        self.assertEqual([p.name for p in run.parent.iterdir()], ["A-1"])
        self.assertEqual([e["type"] for e in self.events(run)].count("retype"), 2)

    def test_retype_rejects_an_invalid_kind(self):
        self.start("A-1")
        self.assertIn("valid kinds", self.fails("run", "retype", "--kind", "chore"))


class DeleteTests(FactoryCase):
    def test_delete_needs_yes_removes_one_directory_and_lists_worktrees(self):
        """L-105: delete removes exactly the run directory (no zip); a worktree of a repo no longer configured is kept and named."""
        run = self.start("A-1")
        other = self.start("B-2")
        from dev_factory.run import Run
        Run(run.parent, "A-1").append({"type": "worktree", "repo": "gone", "path": "/wt/web", "branch": "b", "branch_point": "0" * 40, "throwaway": False})
        self.assertIn("--yes", self.fails("run", "delete", "A-1"))
        self.assertTrue(run.exists())
        out = self.ok("run", "delete", "A-1", "--yes")
        self.assertIn("/wt/web", out)
        self.assertFalse(run.exists())
        self.assertTrue(other.exists())
        self.assertEqual([p.name for p in run.parent.iterdir()], ["B-2"])

    def test_delete_never_wipes_spent_loops_or_blockers_without_the_users_decision(self):
        """Review m8: deleting a run resets its loop budget and blockers, so it needs a recorded decision."""
        from dev_factory.run import Run
        run = self.start("A-1")
        Run(run.parent, "A-1").append({"type": "blocker", "id": "B-1", "kind": "infra", "reason": "env down"})
        self.assertIn("--decision", self.fails("run", "delete", "A-1", "--yes"))
        self.assertTrue(run.exists())
        self.assertIn("not recorded", self.fails("run", "delete", "A-1", "--yes", "--decision", "D-1"))
        Run(run.parent, "A-1").append({"type": "decision", "id": "D-1", "user_said": "yes, delete it"})
        self.ok("run", "delete", "A-1", "--yes", "--decision", "D-1")
        self.assertFalse(run.exists())

    def test_the_cli_never_moves_or_archives_runs_on_its_own(self):
        """L-104: closing a run leaves its directory where it is."""
        run = self.start("A-1")
        self.ok("run", "close", "--reason", "done", "--run", "A-1")
        self.assertTrue((run / "events.jsonl").is_file())
        self.assertEqual([p.name for p in run.parent.iterdir()], ["A-1"])
        self.assertEqual(json.loads(self.ok("runs", "--json"))[0]["state"], "closed")


if __name__ == "__main__":
    unittest.main()
