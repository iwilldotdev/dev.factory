"""Fixtures for review-run tests: a React repo with a change source branch and saved scm payloads (profile `acme`)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from factory import core, profiles
from tests.provider_support import FIXTURES
from tests.real_support import RealCase, commit_file, sh

SRC = "feature/DEV-9431-drawer"


def change_payload(number=669, sha="0" * 40, title="[DEV-9431] - Drawer", source=SRC, target="main", draft=False,
                   reviewers=("me",), approved=None, project="group/react", **extra) -> dict:
    """Raw single-change payload in the shape the `acme` profile maps (`approved=None`: no approval data at all)."""
    item = {"n": str(number), "link": f"https://scm.example/{project}/changes/{number}", "proj": project, "title": title,
            "text": None, "from": source, "into": target, "tip": sha, "status": "OPEN", "wip": draft,
            "reviewers": [{"login": r} for r in reviewers]}
    if approved is not None:
        item["approvals"] = [{"login": u} for u in approved]
    item.update(extra)
    return item


def native_item(number=1, sha="0" * 40, title="[DEV-9431] - Drawer", source=SRC, target="main", draft=False,
                author="alice", approved=None, project="group/react", state="open", repo="React") -> dict:
    """One item of a raw changes list in the shape the `acme` profile maps (it carries `author` and `project`).
    Name kept from when lists were stored as native contracts; `repo` is unused (the list is mapped by project)."""
    item = {"n": number, "link": f"https://scm.example/{project}/changes/{number}", "proj": project, "title": title,
            "text": "", "from": source, "into": target, "tip": sha,
            "status": {"open": "OPEN", "merged": "DONE", "closed": "DROPPED"}[state], "wip": draft}
    if approved is not None:
        item["approvals"] = [{"login": u} for u in approved]
    if author:
        item["by"] = author
    return item


def native_list(*items) -> dict:
    return {"results": list(items)}


class ReviewCase(RealCase):
    def setUp(self) -> None:
        super().setUp()
        for conf in self.ws["repos"]:
            conf["scm_project"] = f"group/{conf['name']}"
        shutil.copytree(FIXTURES, core.factory_home() / "profiles", dirs_exist_ok=True)
        for pdir in (core.factory_home() / "profiles").iterdir():
            profiles.seal(pdir, "test fixture")
        self.use_profile("acme")
        react = self.repo_dirs["React"]
        sh("git", "checkout", "-q", "-b", SRC, cwd=react)
        self.sha = commit_file(react, "src/drawer.txt", "drawer\n", "[DEV-9431] - drawer")
        sh("git", "checkout", "-q", "main", cwd=react)
        self.inbox = self.tmp / "inbox"
        self.inbox.mkdir()

    def use_profile(self, name: str) -> None:
        """Tickets of the workspace are recognised by this synthetic profile's `refs.ticket_key`; scm stays `acme`."""
        self.ws["providers"] = {"tickets": name, "scm": "acme"}
        self.save_ws()

    def save_change(self, alias="React", **kw) -> dict:
        """Save `<alias>-<number>.json` (raw) in the inbox folder, as an agent would."""
        kw.setdefault("project", f"group/{alias.lower()}")
        item = change_payload(**kw)
        core.write_json(self.inbox / f"{alias}-{item['n']}.json", item)
        return item

    def start_single(self, **kw) -> str:
        self.save_change(sha=kw.pop("sha", self.sha), **kw)
        out = self.ok("--json", "review", "start", "ws", "--change", "React#669", "--from", str(self.inbox))
        return json.loads(out)["run"]

    def write_acs(self, rid: str, checks: list[dict]) -> None:
        acs = [{"id": f"AC-{i}", "ticket": "DEV-9431", "text": f"AC {i}", "kind": "logic", "source": "ticket:DEV-9431",
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
