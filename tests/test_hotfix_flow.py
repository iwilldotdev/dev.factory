"""Hotfix flow: branch_format, run start --hotfix, run mirror, per-target evidence, ship plan (DESIGN Hotfix)."""

from __future__ import annotations

import sys
import unittest

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))  # a single-module run finds real_support

from real_support import RealCase, cli, commit_file, core, sh

PY = f'"{sys.executable}"'
DIAGNOSIS = "## Root cause\nx\n\n## Evidence\ny\n\n## Ruled out\nz\n"
CHECK = "import sys, mod\nif mod.v != 2:\n    print('ASSERT v == 2')\n    sys.exit(1)\n"


class HotfixCase(RealCase):
    repos = ("React",)

    def setUp(self) -> None:
        super().setUp()
        repo = self.repo_dirs["React"]
        commit_file(repo, "mod.py", "v = 1\n")
        commit_file(repo, "check.py", CHECK)
        for t in ("homolog", "pack"):
            sh("git", "branch", t, cwd=repo)
            sh("git", "update-ref", f"refs/remotes/origin/{t}", t, cwd=repo)
        self.ws["ship"] = {"targets": ["pack", "homolog"], "draft_default": True, "jira_comment": "mr-links",
                           "hotfix": {"base": "homolog", "mirror": ["pack"]}, "package": "REL_9",
                           "mr_title_format": "[{area} | {target_upper}] - {summary}"}
        self.ws["branch_format"] = {"feature": "feature/{key}-{slug}", "bug": "bugfix/{key}",
                                    "hotfix": "hotfix/{package}/{key}-{target_upper}"}
        self.save_ws()
        self.rid = "ws-bug-1"

    def start(self):
        self.ok("run", "start", "ws", "BUG-1", "--hotfix")
        self.ok("run", "branch", self.rid, "React", "--base", "homolog")
        core.write_json(core.run_dir(self.rid) / "remote" / "issue-BUG-1.json", {"key": "BUG-1"})
        self.wt = core.Path(core.run_load(self.rid)["repos"]["React"]["worktree"])

    def fix(self, content="v = 2\n"):
        commit_file(self.wt, "mod.py", content, "[Area] - fix")

    def acs(self):
        core.write_json(core.run_dir(self.rid) / "acs.json", {"schemaVersion": 1, "acs": [
            {"id": "AC-1", "ticket": "BUG-1", "text": "v is 2", "kind": "bug", "checks": ["CHK-1", "CHK-2"]}], "checks": [
            {"id": "CHK-1", "type": "test", "repo": "React", "cmd": f"{PY} check.py", "expect_fail_pattern": "ASSERT v"},
            {"id": "CHK-2", "type": "red-green", "repo": "React", "of": "CHK-1", "test_files": ["check.py"]}]})

    def through_build(self):
        self.start()
        self.acs()
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        (core.run_dir(self.rid) / "diagnosis.md").write_text(DIAGNOSIS, encoding="utf-8")
        self.ok("prove", self.rid, "--repro")
        self.ok("gate", self.rid, "reproduce")
        self.fix()
        self.ok("gate", self.rid, "build")


class StartAndNaming(HotfixCase):
    def test_start_targets_order_and_bug(self):
        self.start()
        run = core.run_load(self.rid)
        self.assertEqual(run["targets"], ["homolog", "pack"])
        self.assertEqual(run["hotfix"], {"base": "homolog", "mirror": ["pack"]})
        self.assertEqual(run["tickets"][0]["type"], "bug")
        self.assertEqual(run["repos"]["React"]["branch"], "hotfix/REL_9/BUG-1-HOMOLOG")

    def test_feature_flag_and_default_config(self):
        del self.ws["ship"]["hotfix"]
        self.save_ws()
        self.ok("run", "start", "ws", "FEAT-1", "--hotfix", "--feature")
        run = core.run_load("ws-feat-1")
        self.assertEqual(run["tickets"][0]["type"], "feature")
        self.assertEqual(run["targets"], ["homolog", "pack"])

    def test_branch_format_for_plain_runs(self):
        self.ok("run", "start", "ws", "FEAT-2")
        self.ok("run", "branch", "ws-feat-2", "React", "--base", "pack")
        self.assertEqual(core.run_load("ws-feat-2")["repos"]["React"]["branch"], "feature/FEAT-2-react")
        self.ok("run", "start", "ws", "BUG-2", "--bug", "BUG-2")
        self.ok("run", "branch", "ws-bug-2", "React", "--base", "pack")
        self.assertEqual(core.run_load("ws-bug-2")["repos"]["React"]["branch"], "bugfix/BUG-2")

    def test_workspace_check_validates_hotfix(self):
        self.ws["ship"]["hotfix"] = {"base": "nope", "mirror": ["pack"]}
        self.ws["branch_format"] = {"hotfix": "{bogus}"}
        from factory.commands.workspace import hotfix_config_errors
        errs = hotfix_config_errors(self.ws)
        self.assertTrue(any("nope" in e for e in errs), errs)
        self.assertTrue(any("bogus" in e for e in errs), errs)


class Mirror(HotfixCase):
    def test_requires_build_gate(self):
        self.start()
        rc, out = cli("run", "mirror", self.rid, "React", "--to", "pack")
        self.assertEqual(rc, 1, out)
        self.assertIn("gate build", out)

    def test_clean_cherry_pick(self):
        self.through_build()
        self.fix_second = commit_file(self.wt, "other.txt", "x\n", "[Area] - second")
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        rec = core.run_load(self.rid)["repos"]["React"]
        self.assertEqual(rec["branches"]["pack"], "hotfix/REL_9/BUG-1-PACK")
        mwt = core.Path(rec["worktrees"]["pack"])
        log = sh("git", "log", "--format=%s", "origin/pack..HEAD", cwd=mwt).splitlines()
        self.assertEqual(log, ["[Area] - second", "[Area] - fix"])
        self.assertEqual((mwt / "mod.py").read_text(encoding="utf-8"), "v = 2\n")

    def test_conflict_leaves_cherry_pick_and_blocker(self):
        self.through_build()
        repo = self.repo_dirs["React"]
        sh("git", "checkout", "-q", "pack", cwd=repo)
        commit_file(repo, "mod.py", "v = 99\n", "[Area] - pack diverges")
        sh("git", "checkout", "-q", "main", cwd=repo)
        sh("git", "update-ref", "refs/remotes/origin/pack", "pack", cwd=repo)
        rc, out = cli("run", "mirror", self.rid, "React", "--to", "pack")
        self.assertEqual(rc, 1, out)
        self.assertIn("mod.py", out)
        self.assertIn("never --abort", out)
        run = core.run_load(self.rid)
        blocker = run["blockers"][-1]
        self.assertEqual(blocker["kind"], "conflict")
        self.assertEqual(blocker["files"], ["mod.py"])
        blocks = [e["data"] for e in run["events"] if e["kind"] == "block"]
        self.assertEqual(len(blocks), 1, "a conflict blocker is logged like `run block` does")
        self.assertEqual((blocks[0]["blocker_kind"], blocks[0]["n"], blocks[0]["text"]),
                         ("conflict", len(run["blockers"]) - 1, blocker["text"]))
        self.assertIn("conflict", [e["data"].get("status") for e in run["events"] if e["kind"] == "mirror"])
        mwt = core.Path(run["repos"]["React"]["worktrees"]["pack"])
        self.assertTrue(sh("git", "rev-parse", "--git-path", "CHERRY_PICK_HEAD", cwd=mwt))
        self.assertTrue((core.Path(sh("git", "rev-parse", "--absolute-git-dir", cwd=mwt)) / "CHERRY_PICK_HEAD").exists())
        self.assertIn("pack", run["repos"]["React"]["branches"])


class PerTargetEvidence(HotfixCase):
    def test_prove_ids_and_gate_requires_both(self):
        self.through_build()
        # repro evidence exists per target (test check)
        ev = core.run_dir(self.rid) / "evidence"
        self.assertTrue((ev / "REPRO-CHK-1@homolog.json").exists())
        self.assertTrue((ev / "REPRO-CHK-1@pack.json").exists())
        rc, out = cli("prove", self.rid)
        self.assertEqual(rc, 0, out)
        self.assertTrue((ev / "CHK-1@homolog.json").exists())
        self.assertTrue((ev / "CHK-2@homolog.json").exists())
        self.assertFalse((ev / "CHK-1.json").exists())
        rc, out = cli("gate", self.rid, "prove")
        self.assertEqual(rc, 1, out)
        self.assertIn("pack", out)  # no mirror worktree / evidence for pack yet
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        self.ok("prove", self.rid)
        self.assertTrue((ev / "CHK-1@pack.json").exists())
        self.ok("gate", self.rid, "prove")

    def test_reproduce_gate_needs_every_target(self):
        self.start()
        self.acs()
        self.ok("gate", self.rid, "intake")
        self.ok("gate", self.rid, "plan")
        (core.run_dir(self.rid) / "diagnosis.md").write_text(DIAGNOSIS, encoding="utf-8")
        self.ok("prove", self.rid, "--repro")
        (core.run_dir(self.rid) / "evidence" / "REPRO-CHK-1@pack.json").unlink()
        rc, out = cli("gate", self.rid, "reproduce")
        self.assertEqual(rc, 1, out)
        self.assertIn("CHK-1@pack", out)


class ShipPlan(HotfixCase):
    def test_order_and_cross_references(self):
        self.through_build()
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        self.ok("prove", self.rid)
        plan_rc, out = cli("--json", "ship", "plan", self.rid)
        self.assertEqual(plan_rc, 0, out)
        import json
        plan = json.loads(out)
        self.assertEqual([(m["repo"], m["target"]) for m in plan["mrs"]], [("React", "homolog"), ("React", "pack")])
        base, mirror = plan["mrs"]
        self.assertEqual(base["source_branch"], "hotfix/REL_9/BUG-1-HOMOLOG")
        self.assertEqual(mirror["source_branch"], "hotfix/REL_9/BUG-1-PACK")
        self.assertIn("Mirrored to {mr_url:React:pack}", base["description"])
        self.assertIn("Mirror of {mr_url:React:homolog}", mirror["description"])
        self.assertEqual(base["depends_on"], [])
        self.assertIn("CHK-1", mirror["description"])  # checks table; no local artifact links (F-28)
        self.assertNotIn("](evidence/", mirror["description"])
        self.assertEqual(sum(1 for a in plan["actions"] if a["type"] == "jira_comment"), 1)
        self.assertEqual(len(plan["jira_comment"]["adf"]["content"]), 2)


API_CHECK = "import os, sys\nif not os.path.exists('api.txt'):\n    print('ASSERT api.txt')\n    sys.exit(1)\n"


class TargetBranchesBase(HotfixCase):
    """API's `pack` target lives on branch `dev`; React keeps `pack` (per-repo target_branches)."""

    repos = ("API", "React")

    def setUp(self) -> None:
        super().setUp()
        api = self.repo_dirs["API"]
        commit_file(api, "check_api.py", API_CHECK, "[Area] - api check")
        sh("git", "update-ref", "refs/remotes/origin/main", "main", cwd=api)
        sh("git", "branch", "homolog", cwd=api)
        sh("git", "checkout", "-q", "-b", "dev", cwd=api)
        commit_file(api, "dev.txt", "dev\n", "[Area] - dev only")
        sh("git", "checkout", "-q", "main", cwd=api)
        for t in ("homolog", "dev"):
            sh("git", "update-ref", f"refs/remotes/origin/{t}", t, cwd=api)
        self.ws["repos"][0]["target_branches"] = {"pack": "dev"}
        self.save_ws()

    def start(self):
        super().start()
        self.ok("run", "branch", self.rid, "API", "--base", "homolog")
        self.api_wt = core.Path(core.run_load(self.rid)["repos"]["API"]["worktree"])

    def fix(self, content="v = 2\n"):
        super().fix(content)
        commit_file(self.api_wt, "api.txt", "fix\n", "[Area] - api fix")

    def acs(self):
        super().acs()
        data = core.read_json(core.run_dir(self.rid) / "acs.json")
        data["acs"][0]["checks"].append("CHK-3")
        data["checks"].append({"id": "CHK-3", "type": "test", "repo": "API", "cmd": f"{PY} check_api.py",
                               "expect_fail_pattern": "ASSERT api.txt"})
        core.write_json(core.run_dir(self.rid) / "acs.json", data)

    def mirror_both(self):
        self.through_build()
        self.ok("run", "mirror", self.rid, "API", "--to", "pack")
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")


class TargetBranches(TargetBranchesBase):
    def test_mirror_branches_api_from_dev(self):
        self.mirror_both()
        rec = core.run_load(self.rid)["repos"]
        api_wt = core.Path(rec["API"]["worktrees"]["pack"])
        self.assertTrue((api_wt / "dev.txt").exists())
        self.assertEqual(sh("git", "log", "--format=%s", "origin/dev..HEAD", cwd=api_wt).splitlines(), ["[Area] - api fix"])
        react_wt = core.Path(rec["React"]["worktrees"]["pack"])
        self.assertEqual(sh("git", "merge-base", "HEAD", "origin/pack", cwd=react_wt), sh("git", "rev-parse", "origin/pack", cwd=react_wt))
        self.assertEqual(rec["API"]["branches"]["pack"], "hotfix/REL_9/BUG-1-PACK")  # naming keeps the logical target

    def test_gate_prove_accepts_mapped_mirror(self):
        self.mirror_both()
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")
        # the API mirror repro is bound to the tip of dev (not of a non-existent pack branch)
        repro = core.load_evidence(self.rid, "REPRO-CHK-3@pack")
        self.assertEqual(repro["status"], "reproduced")
        self.assertEqual(repro["git_sha"], sh("git", "rev-parse", "origin/dev", cwd=self.repo_dirs["API"]))
        self.assertEqual(core.load_evidence(self.rid, "CHK-3@pack")["status"], "pass")

    def test_ship_plan_target_branches(self):
        import json
        self.mirror_both()
        self.ok("prove", self.rid)
        rc, out = cli("--json", "ship", "plan", self.rid)
        self.assertEqual(rc, 0, out)
        got = {(m["repo"], m["target"]): m["target_branch"] for m in json.loads(out)["mrs"]}
        self.assertEqual(got, {("API", "homolog"): "homolog", ("API", "pack"): "dev",
                               ("React", "homolog"): "homolog", ("React", "pack"): "pack"})

    def test_ship_verify_compares_mapped_target_branch(self):
        import json
        self.mirror_both()
        self.ok("prove", self.rid)
        self.ok("ship", "plan", self.rid)
        plan = json.loads((core.run_dir(self.rid) / "ship-plan.json").read_text(encoding="utf-8"))
        mr = next(m for m in plan["mrs"] if (m["repo"], m["target"]) == ("API", "pack"))
        rb = {"source_branch": mr["source_branch"], "target_branch": "pack", "title": mr["title"], "draft": True,
              "sha": mr["head_sha"], "web_url": "u", "iid": 1}
        from factory.commands import ship
        for m in plan["mrs"]:
            core.write_json(core.run_dir(self.rid) / m["readback"], dict(rb, source_branch=m["source_branch"], title=m["title"],
                                                                        sha=m["head_sha"], target_branch=m["target_branch"]))
        core.write_json(core.run_dir(self.rid) / mr["readback"], dict(rb, source_branch=mr["source_branch"]))
        errs = ship.verify(self.rid)
        self.assertTrue(any("target_branch 'pack' != planned 'dev'" in e for e in errs), errs)

    def test_unmapped_workspace_unchanged(self):
        del self.ws["repos"][0]["target_branches"]
        self.save_ws()
        self.assertEqual(core.target_branch(self.ws, "API", "pack"), "pack")
        self.assertEqual(core.target_branch(self.ws, "API", None), None)
        self.assertEqual(core.target_branch(self.ws, "Nope", "pack"), "pack")
        sh("git", "branch", "pack", cwd=self.repo_dirs["API"])
        sh("git", "update-ref", "refs/remotes/origin/pack", "pack", cwd=self.repo_dirs["API"])
        self.through_build()
        self.ok("run", "mirror", self.rid, "API", "--to", "pack")
        api_wt = core.Path(core.run_load(self.rid)["repos"]["API"]["worktrees"]["pack"])
        self.assertFalse((api_wt / "dev.txt").exists())

    def test_workspace_check_rejects_invalid_mapping(self):
        from factory.commands.workspace import target_branches_errors
        self.assertEqual(target_branches_errors(self.ws), [])
        for bad in ({"nope": "dev"}, {"pack": ""}, {"pack": 3}, ["pack"], {"pack": " dev "}, {"pack": "de v"}):
            self.ws["repos"][0]["target_branches"] = bad
            self.assertTrue(target_branches_errors(self.ws), bad)
        self.save_ws()
        rc, out = cli("workspace", "check", "ws")
        self.assertEqual(rc, 1, out)
        self.assertIn("target_branches", out)

    def test_workspace_check_tolerates_unhashable_targets(self):
        from factory.commands.workspace import target_branches_errors
        self.ws["ship"]["hotfix"] = {"base": ["homolog"], "mirror": [["pack"], "pack"]}
        self.ws["ship"]["targets"] = [["x"], "pack"]
        self.assertEqual(target_branches_errors(self.ws), [])


class MappedBase(TargetBranchesBase):
    """The base target itself is mapped (homolog -> release; API has no homolog branch)."""

    def setUp(self) -> None:
        super().setUp()
        api = self.repo_dirs["API"]
        sh("git", "branch", "-D", "homolog", cwd=api)
        sh("git", "update-ref", "-d", "refs/remotes/origin/homolog", cwd=api)
        sh("git", "checkout", "-q", "-b", "release", cwd=api)
        commit_file(api, "release.txt", "r\n", "[Area] - release only")
        sh("git", "checkout", "-q", "main", cwd=api)
        sh("git", "update-ref", "refs/remotes/origin/release", "release", cwd=api)
        self.ws["repos"][0]["target_branches"] = {"pack": "dev", "homolog": "release"}
        self.ws["repos"][0]["quality"] = {"test": f'{PY} -c "pass"'}
        self.save_ws()

    def test_base_branch_resolves_through_mapping(self):
        self.through_build()  # run branch (--base homolog), repro, gate build (ahead of origin/release)
        rec = core.run_load(self.rid)["repos"]["API"]
        self.assertEqual(rec["base"], "homolog")
        self.assertEqual(rec["base_ref"], "origin/release")
        self.assertTrue((core.Path(rec["worktree"]) / "release.txt").exists())
        self.ok("baseline", self.rid, "--repo", "API")
        base = core.read_json(core.run_dir(self.rid) / "baseline" / "API.json")
        self.assertEqual(base["base_ref"], "origin/release")
        self.assertEqual(base["merge_base"], sh("git", "rev-parse", "origin/release", cwd=self.repo_dirs["API"]))
        self.ok("run", "mirror", self.rid, "API", "--to", "pack")
        self.ok("run", "mirror", self.rid, "React", "--to", "pack")
        self.ok("prove", self.rid)
        self.ok("gate", self.rid, "prove")


if __name__ == "__main__":
    unittest.main()
