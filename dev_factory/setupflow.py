"""The setup interview: the same questions as prompts in a terminal and as JSON for an agent; both write one config.
Every step but the workspace and the repo's name and base is optional: a skipped setting is asked at every run that needs it.
Secret values are never asked: the summary names the keys to write in the workspace's secrets.env."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import config, guide, home, schema
from .util import FactoryError, fwd

SKIP = {"", "-", "skip"}
KINDS = {"ticket": "tickets of the tracker", "change": "change requests of the code host", "change_list": "the changes waiting for your review",
         "pipeline": "CI pipelines of a change", "design_frame": "frames of the design tool", "design_image": "images of design frames"}
REPO_Q = re.compile(r"^repo\.(.+)\.(base|targets|env\.up|env\.base_url|env\.health|checks|notes|login|login_secrets|seed|reset)$")
DB_HINT = re.compile(r"seed|migrat|database|\bdb\b|connectionstring", re.I)
SERVE = re.compile(r"^(dev|start|serve|preview)$")


def question(qid: str, text: str, kind: str = "text", options=(), optional: bool = True, default=None) -> dict:
    return {"id": qid, "text": text, "kind": kind, "options": [{"label": lb, "description": d} for lb, d in options],
            "optional": optional, "default": default}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "check"


def _runner(path: Path) -> str:
    """The package manager the manifest names, else the runtime's own script runner."""
    try:
        pm = json.loads((path / "package.json").read_text(encoding="utf-8")).get("packageManager") or ""
    except (OSError, ValueError, AttributeError):
        pm = ""
    return f"{pm.split('@')[0]} run" if pm.split("@")[0] else "node --run"


def check_candidates(path: Path, f: dict) -> list[tuple[str, str, dict]]:
    """(id, command, vars) from the stack scripts and the CI steps of the repository."""
    out: list[tuple[str, str, dict]] = []
    for s in f["stacks"]:
        if s["stack"] == "node":
            out += [(_slug(k), f"{_runner(path)} {k}", {}) for k in s.get("check_candidates", [])]
        elif s["stack"] == "make":
            out += [(_slug(c.split(" ", 1)[1]), c, {}) for c in s.get("check_candidates", [])]
    for ci in f["ci"]:
        out += [("ci-" + _slug(step), step, ci.get("vars", {})) for step in ci["steps"]]
    seen, uniq = set(), []
    for cid, cmd, env in out:
        if cmd in {c for _, c, _ in uniq}:
            continue
        n, base = 2, cid
        while cid in seen:
            cid, n = f"{base}-{n}", n + 1
        seen.add(cid)
        uniq.append((cid, cmd, env))
    return uniq[:12]


def _scripts(f: dict) -> list[str]:
    return [k for s in f["stacks"] for k in (s.get("scripts") or {})]


def serves(f: dict) -> bool:
    return bool(f["ports"]) or any(SERVE.match(k) for k in _scripts(f)) or bool(f["app_settings"])


def has_ui(f: dict) -> bool:
    return bool(f["ports"]) or any(SERVE.match(k) for k in _scripts(f))


def has_db(path: Path, f: dict) -> bool:
    words = _scripts(f) + [v for s in f["stacks"] for v in (s.get("scripts") or {}).values() if isinstance(v, str)]
    words += [t for s in f["stacks"] for t in s.get("targets", [])] + [st for ci in f["ci"] for st in ci["steps"]] + f["env_files"]
    words += [guide._read(path / a, 50_000) for a in f["app_settings"]]
    return any(DB_HINT.search(w) for w in words)


def asked_every_run(cfg: dict) -> list[str]:
    """The optional settings left unset: the run asks them each time it needs one (decision 6)."""
    out = []
    for r in cfg["repo"]:
        path = Path(r["path"])
        if not path.is_dir():
            continue
        f, env, miss = guide.facts(path), r.get("env") or {}, []
        if not r.get("mirror"):
            miss.append("target branches")
        if not env and serves(f):
            miss.append("env (start command, base URL, health)")
        if not [c for c in r.get("check", []) if c.get("kind") != "guideline"]:
            miss.append("standing checks")
        if has_ui(f) and not env.get("login_spec"):
            miss.append("login steps")
        if has_db(path, f) and not env.get("seed"):
            miss.append("test database seed and reset")
        if miss:
            out.append(f"repo {r['name']}: " + ", ".join(miss))
    return out


class Interview:
    """The state is the workspace config itself plus `setup.json` (the questions already asked or skipped)."""

    def __init__(self, cwd: Path, workspace: str | None = None, explicit: str | None = None):
        from .project import find_root
        self.cwd = Path(cwd).resolve()
        self.top, self.remotes = home.here(self.cwd)
        self._facts: dict[str, dict] = {}
        try:
            self.root, self.by_remote = find_root(self.cwd, explicit, workspace)
        except home.NoWorkspace:
            self.root, self.by_remote = None, None

    # state ------------------------------------------------------------------------------------------------------
    def doc(self) -> dict:
        return config.read_toml(self.root / "factory.toml")

    def _state_file(self) -> Path:
        return home.state_dir(self.root) / "setup.json"

    def state(self) -> dict:
        try:
            return json.loads(self._state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"asked": [], "integrations": []}

    def _save_state(self, st: dict) -> None:
        self._state_file().parent.mkdir(parents=True, exist_ok=True)
        self._state_file().write_text(json.dumps(st, indent=2), encoding="utf-8")

    def facts(self, path: Path) -> dict:
        key = str(path)
        if key not in self._facts:
            self._facts[key] = guide.facts(path)
        return self._facts[key]

    def _name(self, e: dict) -> str:
        return e.get("name") or (self.root / e["path"]).resolve().name

    def _path(self, e: dict) -> Path:
        p = (self.root / e["path"]).resolve()
        return self.top if not p.is_dir() and self.by_remote == self._name(e) and self.top else p

    def _listed(self, doc: dict, path: Path) -> bool:
        return any(home._same_path(self._path(e), path) for e in doc.get("repo", [])) or bool(self.by_remote)

    def entry(self, path: Path, name: str, doc: dict) -> dict:
        f, taken = self.facts(path), {self._name(e) for e in doc.get("repo", [])}
        name, n = name, 2
        while name in taken:
            name, n = f"{name.rstrip('-0123456789') or name}-{n}", n + 1
        rel = fwd(path) if home.is_home(self.root) else fwd(os.path.relpath(path, self.root))
        e = {"path": rel, "name": name, "base": f["default_branch"] or f["current_branch"] or "main"}
        urls = [home.clean_url(u) for u in home.here(path)[1]]
        if urls:
            e["remote_url"] = urls[0] if len(urls) == 1 else urls
        return e

    # questions --------------------------------------------------------------------------------------------------
    def next_questions(self) -> list[dict]:
        if self.root is None:
            if self.top is None:
                raise FactoryError("run the setup inside a git repository of the workspace, or pass --workspace NAME")
            existing = [(n, "existing workspace") for n in home.names()]
            new = home.unique_name(self.top.name)
            return [question("workspace", "Which workspace does this repository belong to? Pick one, or name a new one (lowercase letters, "
                             "digits and -; one folder in " + fwd(home.factory_home()) + ")", "single", existing + [(new, "a new workspace")], False, new),
                    question("repo_name", f"Name of the repository at {fwd(self.top)} in the workspace", optional=False,
                             default=re.sub(r"[^A-Za-z0-9._-]", "-", self.top.name))]
        doc, asked = self.doc(), set(self.state()["asked"])
        if self.top is not None and not self._listed(doc, self.top) and home.is_home(self.root):
            return [question("repo_name", f"Name of the repository at {fwd(self.top)} in workspace {self.root.name}", optional=False,
                             default=re.sub(r"[^A-Za-z0-9._-]", "-", self.top.name))]
        for e in doc.get("repo", []):
            path = self._path(e)
            if path.is_dir():
                for batch in (self._repo_a(e, path), self._repo_b(e, path)):
                    batch = [q for q in batch if q["id"] not in asked]
                    if batch:
                        return batch
        sibs = self._siblings(doc)
        if sibs and "siblings" not in asked:
            return [question("siblings", "Other repositories of this workspace? (each one is then set up the same way)", "multi",
                             [(p.name, fwd(p)) for p in sibs])]
        if "integrations" not in asked:
            hosts = sorted({r["host"] for e in doc.get("repo", []) if self._path(e).is_dir() for r in self.facts(self._path(e))["remotes"] if r["host"]})
            return [question("integrations", "Which of these does the factory read through an adapter command of yours? Skip, and the agent "
                             "reads them with its own tools (tracker, code host, design)" + (f"; code host: {', '.join(hosts)}" if hosts else ""),
                             "multi", list(KINDS.items()))]
        return [question(f"providers.{k}.fetch", f"Command that prints the {k} contract for {{id}} (an adapter: see factory --help fetch)")
                for k in self.state()["integrations"] if f"providers.{k}.fetch" not in asked]

    def _repo_a(self, e: dict, path: Path) -> list[dict]:
        n, f = self._name(e), self.facts(path)
        bases = f["likely_bases"] or [e.get("base", "main")]
        qs = [question(f"repo.{n}.base", f"Base branch of {n} (changes start from it and target it)", "single", [(b, "") for b in bases], False,
                       e.get("base") or f["default_branch"]),
              question(f"repo.{n}.targets", f"Branches of {n} a hotfix is also mirrored to (comma separated)",
                       "multi" if len(bases) > 1 else "text", [(b, "") for b in bases if b != e.get("base")])]
        if serves(f):
            port = next((p for x in f["ports"] for p in x["ports"]), None)
            up = next((f"{_runner(path)} {k}" for k in _scripts(f) if SERVE.match(k)), None)
            qs += [question(f"repo.{n}.env.up", f"Command that starts {n} locally for the checks", default=up),
                   question(f"repo.{n}.env.base_url", f"Local base URL of {n} (loopback)", default=f"http://127.0.0.1:{port}" if port else None),
                   question(f"repo.{n}.env.health", f"Health path of {n} (it answers once the env is up)", default="/")]
        cands = check_candidates(path, f)
        qs.append(question(f"repo.{n}.checks", f"Standing checks of {n} (commands that must pass on every change; or id=command)",
                           "multi", [(cid, cmd + (f" (vars {', '.join(f'{k}={v}' for k, v in env.items())})" if env else "")) for cid, cmd, env in cands]))
        qs.append(question(f"repo.{n}.notes", f"Anything else the agent should know about {n} (free text, kept as instructions)"))
        return qs

    def _repo_b(self, e: dict, path: Path) -> list[dict]:
        n, f, qs = self._name(e), self.facts(path), []
        if not e.get("env"):
            return qs
        if has_ui(f):
            where = fwd(self.root / "login" / f"{n}.json") if home.is_home(self.root) else "e2e/login.json"
            qs += [question(f"repo.{n}.login", f"Login spec for the browser checks of {n} (a file such as {where})"),
                   question(f"repo.{n}.login_secrets", "Names of the secrets the login uses, comma separated (names only; values go in secrets.env)")]
        if has_db(path, f):
            qs += [question(f"repo.{n}.seed", f"Command that seeds the test database of {n}"),
                   question(f"repo.{n}.reset", f"Command that resets the test database of {n}")]
        return qs

    def _siblings(self, doc: dict) -> list[Path]:
        if self.top is None:
            return []
        mine = [self._path(e) for e in doc.get("repo", [])]
        return [p for p in guide.repos_in(self.top.parent) if not any(home._same_path(p, m) for m in mine)][:12]

    # answers ----------------------------------------------------------------------------------------------------
    def apply(self, answers: dict) -> None:
        """Apply answers to the pending questions, batch after batch, in the order the prompts would ask them."""
        left = {k: v for k, v in answers.items()}
        while left:
            pending = {q["id"]: q for q in self.next_questions()}
            now = {k: v for k, v in left.items() if k in pending}
            if not now:
                raise FactoryError(f"not a pending question: {', '.join(left)} (pending: {', '.join(pending) or 'none'})")
            self._apply(now, pending)
            for k in now:
                left.pop(k)
            self.__init__(self.cwd, self.root.name if self.root and home.is_home(self.root) else None,
                          None if self.root is None or home.is_home(self.root) else str(self.root))

    def _apply(self, now: dict, pending: dict) -> None:
        val = {k: _value(v, pending[k]) for k, v in now.items()}
        if "workspace" in pending:
            name = val.get("workspace") or pending["workspace"]["default"]
            self.root = home.create(name) if not (home.factory_home() / name / "factory.toml").is_file() else home.factory_home() / name
            doc = self.doc() if (self.root / "factory.toml").is_file() else {"schema": 1, "project": {"name": name}, "repo": []}
            doc["repo"].append(self.entry(self.top, val.get("repo_name") or pending["repo_name"]["default"], doc))
            home.save(self.root, doc)
            st = self.state()
            st["asked"] = sorted(set(st["asked"]) - {"repo_name", "siblings"})
            return self._save_state(st)
        doc, st = self.doc(), self.state()
        if "repo_name" in val:
            doc["repo"].append(self.entry(self.top, val["repo_name"] or pending["repo_name"]["default"], doc))
        by = {self._name(e): e for e in doc.get("repo", [])}
        for qid, v in val.items():
            m = REPO_Q.match(qid)
            if m and v not in (None, []):
                _set_repo(by[m.group(1)], m.group(2), v, val, self._path(by[m.group(1)]), self, doc)
            elif qid == "siblings":
                for label in v or []:
                    path = next((p for p in self._siblings(doc) if p.name == label), None)
                    if path is None:
                        raise FactoryError(f"siblings: {label!r} is not one of the offered repositories")
                    doc["repo"].append(self.entry(path, re.sub(r"[^A-Za-z0-9._-]", "-", label), doc))
            elif qid == "integrations":
                bad = [k for k in v or [] if k not in KINDS]
                if bad:
                    raise FactoryError(f"integrations: unknown {', '.join(bad)} (one of {', '.join(KINDS)})")
                st["integrations"] = list(v or [])
            elif qid.startswith("providers.") and v:
                doc.setdefault("providers", {}).setdefault(qid.split(".")[1], {})["fetch"] = v
        home.save(self.root, doc)
        no_env = {f"{q[:-2]}{x}" for q, v in val.items() if q.endswith(".env.up") and v is None for x in ("base_url", "health")}
        st["asked"] = sorted(set(st["asked"]) | set(now) | no_env - {"repo_name"})  # no start command: no URL or health to ask
        self._save_state(st)

    def summary(self) -> dict:
        cfg = config.load(self.root)
        _, missing = config.resolve_secrets(cfg)
        return {"done": True, "workspace": cfg["project"]["name"], "dir": fwd(self.root), "config": fwd(self.root / "factory.toml"),
                "repos": [r["name"] for r in cfg["repo"]], "secrets_file": fwd(config.secrets_path(cfg)), "secrets_to_fill": missing,
                "asked_every_run": asked_every_run(cfg)}


def _value(raw, q: dict):
    """Text as given; a multi answer is a list (a JSON list, or comma separated); a skip is None."""
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    elif q["kind"] == "multi" or q["id"].endswith((".targets", ".login_secrets")):
        text = str(raw or "").strip()
        items = json.loads(text) if text.startswith("[") else text.split(",")
    else:
        text = str(raw or "").strip()
        return None if text in SKIP else text
    items = [str(x).strip() for x in items if str(x).strip() not in SKIP]
    return items or None


def _set_repo(e: dict, what: str, v, val: dict, path: Path, iv: Interview, doc: dict) -> None:
    n = iv._name(e)
    if what == "base":
        e["base"] = v
    elif what == "targets":
        e["mirror"] = [{"name": re.sub(r"[^A-Za-z0-9._-]", "-", b), "branch": b} for b in v]
    elif what == "notes":
        e["notes"] = v
    elif what == "env.up":
        if not val.get(f"repo.{n}.env.base_url"):
            raise FactoryError(f"repo.{n}.env: the start command needs the base URL too (or skip both)")
        e["env"] = {"up": v, "base_url": val[f"repo.{n}.env.base_url"], "health": val.get(f"repo.{n}.env.health") or "/"}
    elif what == "checks":
        cands = {cid: (cmd, env) for cid, cmd, env in check_candidates(path, iv.facts(path))}
        checks = []
        for item in v:
            cid, _, cmd = item.partition("=")
            if cid in cands and not cmd:
                cmd, env = cands[cid]
            elif cmd:
                cid, env = _slug(cid), {}
            else:
                raise FactoryError(f"repo.{n}.checks: {item!r} is neither an offered check ({', '.join(cands) or 'none'}) nor id=command")
            checks.append({"id": cid, "run": cmd, **({"vars": env} if env else {})})
        e["check"] = checks
    elif what in ("login", "seed", "reset", "login_secrets"):
        if "env" not in e:
            return
        if what == "login_secrets":
            keys = [k.upper() for k in v]
            bad = [k for k in keys if not schema.SECRET_KEY.match(k)]
            if bad:
                raise FactoryError(f"repo.{n}.login_secrets: {', '.join(bad)} are not secret key names (A-Z, 0-9, _)")
            e["env"]["login_secrets"] = keys
            doc.setdefault("secrets", {})["keys"] = sorted(set(doc.get("secrets", {}).get("keys", [])) | set(keys))
        else:
            e["env"]["login_spec" if what == "login" else what] = v


def prompt(q: dict, ask, say) -> str | list:
    """One question in the terminal: numbered options, Enter takes the default (none: skips), '-' skips."""
    say("")
    say(q["text"] + (" ['-' skips: asked at every run]" if q["optional"] else ""))
    for i, o in enumerate(q["options"], 1):
        say(f"  {i}) {o['label']}" + (f"  {o['description']}" if o["description"] else ""))
    raw = ask(("numbers or names, comma separated" if q["kind"] == "multi" else "answer") + (f" [{q['default']}]" if q["default"] else "") + "> ").strip()
    if not raw:
        raw = "" if q["default"] is None else str(q["default"])
    labels = [o["label"] for o in q["options"]]
    parts = [p.strip() for p in raw.split(",")] if q["kind"] == "multi" else [raw]
    parts = [labels[int(p) - 1] if p.isdigit() and 0 < int(p) <= len(labels) else p for p in parts]
    return [p for p in parts if p] if q["kind"] == "multi" else parts[0]


def interactive(cwd: Path, workspace: str | None, explicit: str | None, ask, say) -> dict:
    """The terminal interview: ask each pending batch, apply it, until nothing is pending; then the summary."""
    errors = 0
    while True:
        iv = Interview(cwd, workspace, explicit)
        batch = iv.next_questions()
        if not batch:
            return iv.summary()
        got: dict = {}
        for q in batch:  # no start command: its base URL and health are not asked
            if not (q["id"].endswith((".env.base_url", ".env.health")) and got.get(q["id"].rsplit(".", 1)[0] + ".up", "x") in ("", "-", None)):
                got[q["id"]] = prompt(q, ask, say)
        try:
            iv.apply(got)
        except FactoryError as exc:
            errors += 1
            if errors > 3:
                raise
            say(f"error: {exc}")


def render_summary(s: dict) -> str:
    lines = [f"workspace {s['workspace']} saved in {s['dir']} (repos: {', '.join(s['repos'])})", f"config: {s['config']}"]
    if s["secrets_to_fill"]:
        lines.append(f"write these secrets in {s['secrets_file']} (KEY=value, never in a chat): {', '.join(s['secrets_to_fill'])}")
    lines += ["asked at every run: " + "; ".join(s["asked_every_run"])] if s["asked_every_run"] else ["nothing is left to ask at every run"]
    return "\n".join(lines)


def render_questions(qs: list[dict]) -> str:
    out = []
    for q in qs:
        out.append(f"{q['id']} ({q['kind']}{', optional' if q['optional'] else ''}{', default ' + str(q['default']) if q['default'] else ''}): {q['text']}")
        out += [f"  - {o['label']}" + (f": {o['description']}" if o["description"] else "") for o in q["options"]]
    return "\n".join(out + ["answer with: factory setup --answer ID=VALUE [--answer ID=VALUE ...] (an empty value skips)"])
