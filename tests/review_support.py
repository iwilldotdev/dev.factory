"""Fixtures for review-run tests: a React repo with an MR source branch and saved GitLab payloads."""

from __future__ import annotations

import json
from pathlib import Path

from factory import core
from tests.real_support import RealCase, commit_file, sh

SRC = "feature/DEV-9431-drawer"


def mr_payload(iid=669, sha="0" * 40, title="[DEV-9431] - Drawer", source=SRC, target="main", draft=False,
               author="alice", reviewers=("me",), approved=None, project="group/react", **extra) -> dict:
    item = {"iid": iid, "title": title, "description": "", "source_branch": source, "target_branch": target,
            "sha": sha, "diff_refs": {"head_sha": sha}, "draft": draft, "work_in_progress": draft,
            "author": {"username": author}, "reviewers": [{"username": r} for r in reviewers], "state": "opened",
            "web_url": f"https://git.example.com/{project}/-/merge_requests/{iid}"}
    if approved is not None:
        item["approved_by"] = [{"user": {"username": u}} for u in approved]
    item.update(extra)
    return item


class ReviewCase(RealCase):
    def setUp(self) -> None:
        super().setUp()
        for conf in self.ws["repos"]:
            conf["gitlab_project"] = f"group/{conf['name']}"
        self.save_ws()
        react = self.repo_dirs["React"]
        sh("git", "checkout", "-q", "-b", SRC, cwd=react)
        self.sha = commit_file(react, "src/drawer.txt", "drawer\n", "[DEV-9431] - drawer")
        sh("git", "checkout", "-q", "main", cwd=react)
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()

    def save_mr(self, alias="React", **kw) -> dict:
        kw.setdefault("project", f"group/{alias.lower()}")
        item = mr_payload(**kw)
        core.write_json(self.inbox / f"mr-{alias}-{item['iid']}.json", item)
        return item

    def start_single(self, **kw) -> str:
        self.save_mr(sha=kw.pop("sha", self.sha), **kw)
        out = self.ok("--json", "review", "start", "ws", "--mr", "React!669", "--from", str(self.inbox))
        return json.loads(out)["run"]

    def write_acs(self, rid: str, checks: list[dict]) -> None:
        acs = [{"id": f"AC-{i}", "ticket": "DEV-9431", "text": f"AC {i}", "kind": "logic", "source": "jira:DEV-9431",
                "checks": [c["id"]]} for i, c in enumerate(checks, 1)]
        core.write_json(core.run_dir(rid) / "acs.json", {"schemaVersion": 1, "acs": acs, "checks": checks})

    def record(self, rid: str, kind: str, verdict="APPROVE", findings=None) -> None:
        rec = {"verdict": verdict, "reviewer": "factory-auditor", "findings": findings or [],
               "completeness": {"done": 1, "gap": 0, "not_checked": 0}}
        if kind == "qa":
            acs = core.read_json(core.run_dir(rid) / "acs.json")["acs"]
            rec["ac_coverage"] = [{"ac": a["id"], "exercises": True, "note": "ok"} for a in acs]
        path = self.tmp / f"{kind}.json"
        core.write_json(path, rec)
        self.ok("review", "record", rid, "--kind", kind, "--file", str(path))
