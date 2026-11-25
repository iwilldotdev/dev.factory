"""Runs: `events.jsonl` is the only run state; everything else is derived from it [L-15, L-164, L-165]."""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import __version__
from .util import FactoryError, canonical, now_iso

KINDS = ("feature", "bug", "hotfix", "epic", "review")
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def _locked(path: Path):
    """Exclusive lock on `<events>.lock` held while reading and appending."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise FactoryError("timed out waiting for the run's event lock", 1)
                time.sleep(0.01)
        try:
            yield
        finally:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Run:
    def __init__(self, runs_dir: Path, rid: str):
        self.id = rid
        self.dir = Path(runs_dir) / rid
        self.events_path = self.dir / "events.jsonl"

    def exists(self) -> bool:
        return self.events_path.is_file()

    def read(self) -> list[dict]:
        events: list[dict] = []
        try:
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return events
        import json
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError as exc:
                raise FactoryError(f"{self.events_path.as_posix()} line {n} is not valid JSON: {exc}") from exc
        return events

    def append(self, event: dict | Callable[[list[dict]], dict | None], actor: str | None = None) -> dict | None:
        """Append one event as one line, in one write, under the lock. A callable sees the current events
        under the lock and returns the event (or None to append nothing), so ids and de-duplication cannot race."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with _locked(self.dir / "events.lock"):
            ev = event(self.read()) if callable(event) else event
            if ev is None:
                return None
            ev = {"v": 1, "ts": now_iso(), "cli": __version__, **ev}
            if actor:
                ev["actor"] = actor
            data = (canonical(ev) + "\n").encode("utf-8")
            fd = os.open(str(self.events_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            return ev

    def state(self) -> dict:
        return derive(self.read())


def derive(events: list[dict]) -> dict:
    """Everything a command needs to know about a run, computed from its events."""
    st: dict = {"ticket": None, "kind": None, "repos": [], "closed": None, "questions": {}, "decisions": {},
                "blockers": {}, "contracts": {}, "media": {}, "gates": {}, "worktrees": [], "env_failures": [],
                "human": [], "diagnosis": None, "locks": [], "loops": [], "evidence": [], "reviews": [], "confirms": [], "waivers": [], "ships": [], "baselines": [],
                "shipped": [], "design_reviews": [], "design_refreshes": [], "lock_at": -1, "builders": set(), "review_at": -1, "ship_at": -1, "retype_at": -1, "contract_at": -1, "first_ts": None, "last_ts": None, "count": len(events),
                "mirrors": [], "parent": None, "epic": None, "owner": None, "reports": [], "slot": 0, "dismissals": []}
    consumed: set[str] = set()
    for i, ev in enumerate(events):
        t = ev.get("type")
        st["first_ts"] = st["first_ts"] or ev.get("ts")
        st["last_ts"] = ev.get("ts")
        if t == "run_started":
            st.update(ticket=ev.get("ticket"), kind=ev.get("kind"), repos=list(ev.get("repos") or []), parent=ev.get("parent"))
        elif t == "repos":
            st["repos"] = list(ev.get("repos") or [])
        elif t == "retype":
            st.update(kind=ev.get("kind"), retype_at=i)
        elif t == "contract":
            st["contracts"][(ev["kind"], ev["id"])] = {**ev, "at": i}
            if ev["kind"] == "ticket":  # a readback, a pipeline or a design frame is no intake input; the plan lock covers design contracts
                st["contract_at"] = i
        elif t == "media":
            st["media"][ev["attachment"]] = ev
        elif t == "question":
            st["questions"][ev["id"]] = {"id": ev["id"], "text": ev.get("text", ""), "ref": ev.get("ref"), "answered": None,
                                         "save_as": ev.get("save_as")}
        elif t == "answer" and ev.get("question") in st["questions"]:
            st["questions"][ev["question"]]["answered"] = ev.get("user_said")
            if ev.get("decision"):
                st["decisions"][ev["decision"]] = {"id": ev["decision"], "user_said": ev.get("user_said"), "via": "answer", "at": i}
        elif t == "decision":
            st["decisions"][ev["id"]] = {"id": ev["id"], "user_said": ev.get("user_said"), "via": "decide", "at": i}
        elif t == "human":
            st["human"].append(ev)
        elif t == "blocker":
            st["blockers"][ev["id"]] = {"id": ev["id"], "kind": ev.get("kind"), "reason": ev.get("reason"), "open": True}
        elif t == "unblock" and ev.get("blocker") in st["blockers"]:
            st["blockers"][ev["blocker"]].update(open=False, decision=ev.get("decision"))
        elif t == "gate":
            st["gates"][ev["name"]] = {"result": ev.get("result"), "reasons": ev.get("reasons", []), "at": i,
                                       "diagnosis": ev.get("diagnosis")}
            if ev["name"] == "reproduce" and ev.get("result") == "pass":
                st["diagnosis"] = ev.get("diagnosis")
        elif t == "plan_locked":
            st["locks"].append(ev)
            st["lock_at"] = i
            if (ev.get("amend") or {}).get("counts_loop") is False:  # an amend that needed no loop makes a budget blocker moot
                for b in st["blockers"].values():
                    if b["open"] and (b.get("reason") or "").startswith("loop budget exhausted"):
                        b["open"] = False
        elif t == "design_review":
            st["design_reviews"].append({**ev, "at": i})
        elif t == "design_refresh":
            st["design_refreshes"].append({**ev, "at": i})
        elif t == "loop":
            st["loops"].append(ev)
        elif t == "evidence":
            st["evidence"].append(ev)
            if ev.get("actor") and ev.get("mode") != "uat":  # exploring the screens is the uat reviewer's own work, not a build
                st["builders"].add(ev["actor"].strip().lower())
        elif t == "baseline":
            st["baselines"].append(ev)
        elif t == "review":
            st["reviews"].append({**ev, "at": i})
            if ev.get("kind") != "feedback":  # findings from the change after ship do not unpass the review and ship gates
                st["review_at"] = i
        elif t == "confirm":
            st["confirms"].append({**ev, "at": i})
        elif t == "waiver":
            st["waivers"].append({**ev, "at": i})
        elif t == "ship_planned":
            st["ships"].append({**ev, "at": i})
            st["ship_at"] = i
        elif t == "ship_verified":
            st["shipped"].append({**ev, "at": i})
            st["ship_at"] = i
        elif t == "worktree":
            st["worktrees"].append(ev)
            if ev.get("actor") and not ev.get("throwaway"):
                st["builders"].add(ev["actor"].strip().lower())
        elif t == "env":
            if ev.get("action") == "fail":
                st["env_failures"].append(ev)
            elif ev.get("action") in ("up", "restart"):
                st["env_failures"] = []
        elif t == "closed":
            st["closed"] = ev
        elif t in ("mirror", "review_report", "dismiss"):
            st[{"mirror": "mirrors", "review_report": "reports", "dismiss": "dismissals"}[t]].append({**ev, "at": i})
        elif t == "epic_planned":
            st["epic"] = ev
        elif t == "claim":
            st["owner"] = ev.get("session")
        elif t == "slot":
            st["slot"] = ev.get("slot", 0)
        if t in ("unblock", "loop", "ship_planned", "mirror", "dismiss", "plan_locked") and ev.get("decision"):
            consumed.add(ev["decision"])
    cut = _intake_cut(events)
    for did, d in st["decisions"].items():
        d["consumed"] = did in consumed
        d["intake"] = d.get("at", 0) < cut  # answers the plan was built on; listing them as pending is noise
    return st


def _intake_cut(events: list[dict]) -> int:
    """Decisions before the first plan lock, or before the first passing intake gate when no plan is locked yet, are intake history:
    the plan phase has nothing that consumes them, so listing them as pending sent authors looking for a way to consume them."""
    first = lambda ok: next((i for i, e in enumerate(events) if ok(e)), -1)  # noqa: E731
    return max(first(lambda e: e.get("type") == "plan_locked"),
               first(lambda e: e.get("type") == "gate" and e.get("name") == "intake" and e.get("result") == "pass"))


def next_id(prefix: str, events: list[dict], etype: str) -> str:
    return f"{prefix}-{sum(1 for e in events if e.get('type') == etype) + 1}"
