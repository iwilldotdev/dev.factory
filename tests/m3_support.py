"""Fixtures for the review and ship tests: a run that has passed verify, review inputs and a recording helper."""
from __future__ import annotations

import json
from pathlib import Path

from tests.helpers import git
from tests.m2_support import M2Case


class M3Case(M2Case):
    """`ready()` leaves a locked plan, one fix commit, passing verify evidence and a passing verify gate."""

    def ready(self, kind: str = "feature", extra_toml: str = "", **kw) -> Path:
        if extra_toml:
            self.write("factory.toml", self.TOML + extra_toml)
        self.run_dir = self.prepare(kind, **kw)
        if kind == "bug":
            self.write("diagnosis.md", "## Root cause\nVALUE is 1 in app.py.\n\n## Evidence\ncheck.py prints VALUE=1.\n\n"
                                       "## Ruled out\nThe check itself.\n\n## Scenario\nTicket scenario used.\n", root=self.run_dir)
            self.ok("reproduce", "--actor", "builder-1")
            self.ok("gate", "reproduce")
        self.fix()
        self.ok("verify", "--actor", "builder-1")
        self.ok("gate", "verify")
        return self.run_dir

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.tree())

    def review_input(self, kind: str = "code", verdict: str = "APPROVE", **over) -> dict:
        data = {"verdict": verdict, "reviewer": "fresh-reviewer", "repos": {"web": self.head()[:7]}, "findings": [],
                "completeness": {"done": 3, "gap": 0, "not_checked": 0}}
        if kind == "code":
            data["changed_values"] = []
        else:
            data["exercises"] = [{"ac": "AC-1", "exercises": True, "note": "CHK-1 asserts the exact value"}]
        data.update(over)
        return data

    def write_review(self, data: dict, name: str = "review.json") -> Path:
        return self.write(name, json.dumps(data), root=self.base)

    def record(self, kind: str = "code", verdict: str = "APPROVE", *flags: str, code: int = 0, **over) -> str:
        path = self.write_review(self.review_input(kind, verdict, **over))
        args = ("review", "record", "--kind", kind, "--file", str(path), *flags)
        return self.ok(*args) if code == 0 else self.fails(*args, code=code)

    def approve_both(self, *flags: str) -> None:
        self.record("code", "APPROVE", *flags)
        self.record("qa", "APPROVE", *flags)

    def reviews(self) -> list[dict]:
        return [e for e in self.events(self.run_dir) if e["type"] == "review"]

    def loops(self) -> list[dict]:
        return [e for e in self.events(self.run_dir) if e["type"] == "loop"]


class ShipCase(M3Case):
    """`to_ship()` leaves both reviews APPROVE and the review gate passed; helpers build change readbacks."""

    def to_ship(self, kind: str = "feature", **kw) -> Path:
        self.ready(kind, **kw)
        self.approve_both()
        self.ok("gate", "review")
        return self.run_dir

    def ship_plan(self, *flags: str) -> dict:
        self.ok("ship", "plan", *flags)
        return self.sp()

    def sp(self) -> dict:
        return json.loads((self.run_dir / "ship" / "plan.json").read_text(encoding="utf-8"))

    def change_data(self, **over) -> dict:
        sp = self.sp()
        it = next(i for i in sp["intents"] if i["intent"] == "change_request")
        data = {"id": "42", "url": "https://scm.example/c/42", "repo": "web", "title": sp["title"], "source_branch": it["source_branch"],
                "target_branch": it["target_branch"], "head_sha": sp["heads"]["web"], "state": "open", "draft": True, "reviewers": [],
                "description_md": (self.run_dir / "ship" / "body.md").read_text(encoding="utf-8")}
        data.update(over)
        return data

    def ingest_change(self, kind: str = "change", **over) -> dict:
        data = self.change_data(**over) if kind == "change" else over
        raw = self.run_dir / "ship" / "readback" / f"web-{kind}.raw.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps({"payload": data}), encoding="utf-8")
        self.ok("ingest", kind, "--raw", str(raw), "--stdin", stdin=json.dumps(data))
        return data

    def ship_verify(self, code: int = 0) -> dict:
        got, out, err = self.cli("ship", "verify", "--json")
        self.assertEqual(got, code, out + err)
        return json.loads(out)

    def ship_events(self, kind: str) -> list[dict]:
        return [e for e in self.events(self.run_dir) if e["type"] == kind]
