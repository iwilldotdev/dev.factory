#!/usr/bin/env python
"""Install the dev.factory Claude Code layer into a Claude profile. Stdlib only.

    python install.py --profile ~/.claude [--factory-home DIR] [--mcp-config FILE] [--remove-harness] [--yes] [--dry-run]

Installs: skills/factory, agents/factory-*.md (hook paths substituted), hooks/*.js,
the protect.js PreToolUse entry in settings.json, the delegation snippet in CLAUDE.md
(between markers), bin/factory(.cmd) shims, and the FACTORY_HOME directories.

--mcp-config (default: ~/.claude.json for the profile ~/.claude, <profile>/.claude.json for any other
profile; when that default file is absent it is skipped with a note, exit 0) is the Claude MCP config. When it has a dev.mcp server entry, its `env` gets the
save_to settings (DEV_MCP_SAVE_ROOTS, DEV_MCP_SAVE_ALLOW_DOT, DEV_MCP_SPILL_KB) unless already set; other
keys are untouched, the file is backed up first, and --dry-run prints the diff (secret-looking values
are printed as ***). A running Claude Code app may overwrite .claude.json, so editing it needs --yes
while one is running.

Before overwriting, settings.json, CLAUDE.md, existing agents/factory-*.md, skills/factory and the
hooks are copied to <profile>/backups/factory-<timestamp>/.

--remove-harness backs up and removes the dev.harness layer first: dev-* skills
(links are unlinked, never followed), agents/dev-*.md, hooks/dev-worker-guard.js,
dev-harness-delegation.md, and the harness sections of CLAUDE.md.

A profile folder (skills/, agents/, hooks/) that is itself a symlink or junction is
shared with other profiles: changes inside it need --yes.

Exit codes: 0 ok, 1 something was skipped (see SKIP lines), 2 error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import difflib
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = REPO / "claude"
MARK_START = "<!-- dev.factory:start -->"
MARK_END = "<!-- dev.factory:end -->"
HARNESS_SECTIONS = ("## Delegation to workers", "## dev.harness flows")
HOOK_FILES = ("protect.js", "protect-lib.js", "worker-guard.js")
# regex: file/shell tools plus every MCP tool (protect.js checks `save_to` on mcp__* calls)
PROTECT_MATCHER = "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|mcp__.*"
FACTORY_DIRS = ("workspaces", "runs", "env", "secrets")
SECRET_KEY = re.compile(r"TOKEN|SECRET|PASS|KEY|AUTH", re.I)
_JSON_PAIR = re.compile(r'("(?:[^"\\]|\\.)*")(\s*:\s*)("(?:[^"\\]|\\.)*"|[^,\n}\]]+)')
_ENV_PAIR = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(=)(.*)$")


def mask_secrets(text: str) -> str:
    """Replace values of secret-looking keys (TOKEN|SECRET|PASS|KEY|AUTH, any case) with ***."""
    def pair(m: re.Match) -> str:
        if SECRET_KEY.search(m.group(1)) and not m.group(3).lstrip().startswith(("{", "[")):
            return f'{m.group(1)}{m.group(2)}"***"'
        return m.group(0)

    def line(ln: str) -> str:
        env = _ENV_PAIR.match(ln)
        if env and SECRET_KEY.search(env.group(2)):
            return f"{env.group(1)}{env.group(2)}=***"
        return _JSON_PAIR.sub(pair, ln)

    return "\n".join(line(ln) for ln in text.split("\n"))


def claude_running() -> bool:
    """True when a Claude Code / Claude desktop process seems to be running (best effort)."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15).stdout
            return any(re.match(r'^"claude(?:\.exe)?"', ln, re.I) for ln in out.splitlines())
        return subprocess.run(["pgrep", "-x", "-i", "claude"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def path_hint(bin_dir: Path) -> list[str]:
    """Printable commands that add bin_dir to the user PATH. Never executed (setx truncates long PATHs)."""
    if os.name == "nt":
        d = str(bin_dir).replace("'", "''")
        return [
            "[Environment]::SetEnvironmentVariable('Path', "
            f"[Environment]::GetEnvironmentVariable('Path','User').TrimEnd(';') + ';{d}', 'User')",
            "(PowerShell; then open a new terminal. Do not use setx: it truncates PATH at 1024 characters.)",
        ]
    return [f'export PATH="{fwd(bin_dir)}:$PATH"   # add to ~/.profile or your shell rc']


def fwd(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def is_link(path: Path) -> bool:
    """Symlink or Windows junction (any reparse point), without following it."""
    if os.path.islink(path):
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def link_target(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return "<unreadable reparse point>"


def remove_link(path: Path) -> None:
    """Remove a symlink or junction itself; never the target's contents."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(path)  # directory symlinks and junctions on Windows


class Installer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.dry = args.dry_run
        self.profile = Path(args.profile).expanduser().resolve()
        home = args.factory_home or os.environ.get("FACTORY_HOME") or str(Path.home() / ".factory")
        self.factory_home = Path(home).expanduser().resolve()
        self.explicit_home = bool(args.factory_home)
        self.hooks_dir = self.profile / "hooks"
        self.skipped: list[str] = []
        stamp = f"{_dt.datetime.now():%Y%m%d-%H%M%S}"
        self.backup_dir = self.profile / "backups" / f"harness-{stamp}"
        self.install_backup_dir = self.profile / "backups" / f"factory-{stamp}"
        n = 1
        while self.install_backup_dir.exists():  # never overwrite an earlier backup
            n += 1
            self.install_backup_dir = self.profile / "backups" / f"factory-{stamp}-{n}"

    # ---------- output ----------

    def act(self, text: str, fn=None) -> None:
        print(("DRY  " if self.dry else "DO   ") + text)
        if fn is not None and not self.dry:
            fn()

    def skip(self, text: str) -> None:
        print("SKIP " + text)
        self.skipped.append(text)

    def note(self, text: str) -> None:
        print("NOTE " + text)

    def shared_ok(self, folder: Path) -> bool:
        """False when folder is a link/junction (shared) and --yes was not given."""
        if not is_link(folder):
            return True
        target = link_target(folder)
        if self.args.yes:
            self.note(f"{folder} is a link to {target} (shared with other profiles); --yes given, changing entries inside")
            return True
        self.skip(f"{folder} is a link to {target} (shared with other profiles); re-run with --yes to change entries inside")
        return False

    # ---------- helpers ----------

    def write_text(self, path: Path, text: str, what: str) -> None:
        def go():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.act(f"write {what}: {path}", go)

    def backup(self, path: Path, rel: str, base: Path | None = None) -> None:
        base = base or self.backup_dir
        dest = base / rel
        if is_link(path):
            def go():
                dest.parent.mkdir(parents=True, exist_ok=True)
                links = base / "links.json"
                data = json.loads(links.read_text(encoding="utf-8")) if links.is_file() else {}
                data[str(path)] = link_target(path)
                links.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            self.act(f"backup link {path} -> {link_target(path)} (recorded in links.json)", go)
        elif path.is_dir():
            self.act(f"backup {path} -> {dest}", lambda: shutil.copytree(path, dest, symlinks=True))
        elif path.is_file():
            def go():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            self.act(f"backup {path} -> {dest}", go)

    def remove(self, path: Path) -> None:
        if is_link(path):
            self.act(f"remove link {path} (target {link_target(path)} untouched)", lambda: remove_link(path))
        elif path.is_dir():
            self.act(f"remove dir {path}", lambda: shutil.rmtree(path))
        elif path.exists():
            self.act(f"remove file {path}", path.unlink)

    # ---------- remove harness ----------

    def remove_harness(self) -> None:
        print(f"== remove dev.harness from {self.profile} (backup: {self.backup_dir})")
        skills = self.profile / "skills"
        if skills.exists() and self.shared_ok(skills):
            for entry in sorted(skills.iterdir(), key=lambda p: p.name):
                if entry.name.startswith("dev-"):
                    self.backup(entry, f"skills/{entry.name}")
                    self.remove(entry)
        agents = self.profile / "agents"
        if agents.exists() and self.shared_ok(agents):
            for entry in sorted(agents.glob("dev-*.md")):
                self.backup(entry, f"agents/{entry.name}")
                self.remove(entry)
        guard = self.hooks_dir / "dev-worker-guard.js"
        if guard.exists() or is_link(guard):
            if self.shared_ok(self.hooks_dir):
                self.backup(guard, "hooks/dev-worker-guard.js")
                self.remove(guard)
        deleg = self.profile / "dev-harness-delegation.md"
        if deleg.exists() or is_link(deleg):
            self.backup(deleg, "dev-harness-delegation.md")
            self.remove(deleg)
        claude_md = self.profile / "CLAUDE.md"
        if claude_md.is_file():
            text = claude_md.read_text(encoding="utf-8")
            new = strip_harness_sections(text)
            if new != text:
                self.backup(claude_md, "CLAUDE.md")
                self.write_text(claude_md, new, "CLAUDE.md without harness sections")
        settings = self.profile / "settings.json"
        if settings.is_file() and "dev-worker-guard" in settings.read_text(encoding="utf-8"):
            self.note(f"{settings} still references dev-worker-guard; review it by hand")

    # ---------- install ----------

    def install(self) -> None:
        print(f"== install dev.factory into {self.profile} (FACTORY_HOME {self.factory_home})")
        if not self.dry:
            self.profile.mkdir(parents=True, exist_ok=True)
        self.backup_install_targets()
        self.install_skill()
        self.install_agents()
        self.install_hooks()
        self.merge_settings()
        self.write_snippet()
        self.write_shims()
        self.merge_mcp_config()
        for sub in FACTORY_DIRS:
            d = self.factory_home / sub
            if not d.is_dir():
                self.act(f"mkdir {d}", lambda d=d: d.mkdir(parents=True, exist_ok=True))

    def backup_install_targets(self) -> None:
        """Copy what install will overwrite to <profile>/backups/factory-<timestamp>/."""
        b = self.install_backup_dir
        for rel in ("settings.json", "CLAUDE.md"):
            if (self.profile / rel).is_file():
                self.backup(self.profile / rel, rel, b)
        for src in sorted((SRC / "agents").glob("factory-*.md")):
            old = self.profile / "agents" / src.name
            if old.is_file():
                self.backup(old, f"agents/{src.name}", b)
        skill = self.profile / "skills" / "factory"
        if skill.exists() or is_link(skill):
            self.backup(skill, "skills/factory", b)
        for name in HOOK_FILES:
            old = self.hooks_dir / name
            if old.is_file():
                self.backup(old, f"hooks/{name}", b)

    def install_skill(self) -> None:
        skills = self.profile / "skills"
        if skills.exists() and not self.shared_ok(skills):
            return
        dest = skills / "factory"
        if dest.exists() or is_link(dest):
            self.remove(dest)
        self.act(f"copy skill {SRC / 'skills' / 'factory'} -> {dest}",
                 lambda: shutil.copytree(SRC / "skills" / "factory", dest))

    def install_agents(self) -> None:
        agents = self.profile / "agents"
        if agents.exists() and not self.shared_ok(agents):
            return
        for src in sorted((SRC / "agents").glob("factory-*.md")):
            text = (src.read_text(encoding="utf-8")
                    .replace("{{HOOKS_DIR}}", fwd(self.hooks_dir))
                    .replace("{{FACTORY_HOME}}", fwd(self.factory_home)))
            self.write_text(agents / src.name, text, "agent")

    def install_hooks(self) -> None:
        if self.hooks_dir.exists() and not self.shared_ok(self.hooks_dir):
            return
        for name in HOOK_FILES:
            src, dest = SRC / "hooks" / name, self.hooks_dir / name

            def go(src=src, dest=dest):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            self.act(f"copy hook {src.name} -> {dest}", go)

    def protect_command(self) -> str:
        return f'node "{fwd(self.hooks_dir / "protect.js")}" --factory-home "{fwd(self.factory_home)}"'

    def merge_settings(self) -> None:
        path = self.profile / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except json.JSONDecodeError as err:
            raise SystemExit(f"ERROR {path} is not valid JSON ({err}); fix it first")
        if not isinstance(data, dict):
            raise SystemExit(f"ERROR {path} is not a JSON object")
        new = merge_settings(data, self.protect_command(), fwd(self.factory_home) if self.explicit_home else None)
        if new == data and path.is_file():
            self.note(f"settings.json already has the protect.js hook: {path}")
            return
        self.write_text(path, json.dumps(new, indent=2, ensure_ascii=False) + "\n", "settings.json (protect.js PreToolUse)")

    def mcp_config_path(self) -> Path | None:
        if self.args.mcp_config:
            return Path(self.args.mcp_config).expanduser().resolve()
        if self.profile == (Path.home() / ".claude").resolve():
            return Path.home() / ".claude.json"
        return self.profile / ".claude.json"

    def mcp_env(self) -> dict[str, str]:
        return {
            "DEV_MCP_SAVE_ROOTS": fwd(self.factory_home / "runs") + os.pathsep + fwd(Path(tempfile.gettempdir()) / "dev.mcp"),
            "DEV_MCP_SAVE_ALLOW_DOT": ".factory",
            "DEV_MCP_SPILL_KB": "64",
        }

    def merge_mcp_config(self) -> None:
        path = self.mcp_config_path()
        if path is None:
            self.note("no Claude MCP config found for this profile; pass --mcp-config <file> to set the dev.mcp save_to env")
            return
        if not path.is_file():
            if self.args.mcp_config:
                self.skip(f"--mcp-config {path} does not exist")
            else:
                self.note(f"default MCP config {path} not found; skipping dev.mcp env (pass --mcp-config <file> to set it)")
            return
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            self.skip(f"{path} is not valid JSON ({err}); dev.mcp env not set")
            return
        new, added = merge_mcp_env(data, self.mcp_env())
        if added is None:
            self.note(f"no dev.mcp entry in mcpServers of {path}; set DEV_MCP_SAVE_ROOTS={self.mcp_env()['DEV_MCP_SAVE_ROOTS']} "
                      "DEV_MCP_SAVE_ALLOW_DOT=.factory DEV_MCP_SPILL_KB=64 in its env by hand")
            return
        if not added:
            self.note(f"dev.mcp env already has the save_to settings: {path}")
            return
        out = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
        diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), out.splitlines(keepends=True),
                                            fromfile=str(path), tofile=str(path) + " (new)", n=1))
        print(f"MCP  dev.mcp env additions: {', '.join(added)}")
        print(mask_secrets(diff.rstrip("\n")) if diff else "(no textual change)")
        if claude_running():
            msg = (f"a running Claude Code / Claude app was detected; it may overwrite {path} when it exits "
                   "and lose this change. Close it first")
            if not (self.args.yes or self.dry):
                self.skip(f"{msg}, then re-run with --yes to edit {path}")
                return
            self.note(msg + (" (--yes given)" if self.args.yes else " (dry run, nothing written)"))
        self.backup(path, path.name, self.install_backup_dir)
        self.write_text(path, out, "MCP config dev.mcp env")

    def write_snippet(self) -> None:
        path = self.profile / "CLAUDE.md"
        snippet = (SRC / "CLAUDE.snippet.md").read_text(encoding="utf-8")
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if self.dry and self.args.remove_harness:
            text = strip_harness_sections(text)
        new = upsert_block(text, snippet)
        if new == text:
            self.note(f"CLAUDE.md block already current: {path}")
            return
        self.write_text(path, new, "CLAUDE.md dev.factory block")

    def write_shims(self) -> None:
        bin_dir = self.profile / "bin"
        fh_sh = f"FACTORY_HOME='{fwd(self.factory_home)}'\nexport FACTORY_HOME\n" if self.explicit_home else ""
        fh_cmd = f'set "FACTORY_HOME={self.factory_home}"\r\n' if self.explicit_home else ""
        sh = (
            "#!/bin/sh\n"
            "# dev.factory CLI shim (generated by install.py)\n"
            f"REPO='{fwd(REPO)}'\n"
            'case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) SEP=";" ;; *) SEP=":" ;; esac\n'
            'PYTHONPATH="$REPO${PYTHONPATH:+$SEP$PYTHONPATH}"\n'
            "export PYTHONPATH\n"
            f"{fh_sh}"
            "if command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then\n"
            '  exec python -m factory "$@"\n'
            "fi\n"
            'exec python3 -m factory "$@"\n'
        )
        cmd = (
            "@echo off\r\n"
            "rem dev.factory CLI shim (generated by install.py)\r\n"
            "setlocal\r\n"
            f'set "PYTHONPATH={REPO};%PYTHONPATH%"\r\n'
            f"{fh_cmd}"
            "python -m factory %*\r\n"
            "exit /b %ERRORLEVEL%\r\n"
        )
        self.write_text(bin_dir / "factory", sh, "shim (sh)")
        if not self.dry:
            mode = (bin_dir / "factory").stat().st_mode
            (bin_dir / "factory").chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        def go():
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "factory.cmd").write_bytes(cmd.encode("utf-8"))
        self.act(f"write shim (cmd): {bin_dir / 'factory.cmd'}", go)
        on_path = any(_same(p, bin_dir) for p in os.environ.get("PATH", "").split(os.pathsep) if p)
        if not on_path:
            self.note(f"{bin_dir} is not on PATH; to make `factory` resolve, run this yourself (not executed here):")
            for ln in path_hint(bin_dir):
                print("     " + ln)


def _same(a: str, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(str(b))
    except (OSError, ValueError):
        return False


def strip_harness_sections(text: str) -> str:
    """Remove the dev.harness '## Delegation to workers' and '## dev.harness flows' sections."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped in HARNESS_SECTIONS:
            skipping = True
            continue
        if skipping and (re.match(r"^#{1,2}\s", stripped) or stripped in (MARK_START, MARK_END)):
            skipping = False
        if not skipping:
            out.append(line)
    result = re.sub(r"\n{3,}", "\n\n", "".join(out))
    return result.rstrip("\n") + "\n" if result.strip() else ""


def upsert_block(text: str, snippet: str) -> str:
    block = f"{MARK_START}\n{snippet.strip()}\n{MARK_END}\n"
    start, end = text.find(MARK_START), text.find(MARK_END)
    if start >= 0 and end > start:
        tail = text[end + len(MARK_END):]
        return text[:start] + block + tail.lstrip("\n")
    if not text.strip():
        return block
    return text.rstrip("\n") + "\n\n" + block


def merge_settings(data: dict, command: str, factory_home: str | None) -> dict:
    """Idempotent: exactly one PreToolUse entry runs protect.js; other keys are kept."""
    new = json.loads(json.dumps(data))
    hooks = new.setdefault("hooks", {})
    pre = [e for e in hooks.get("PreToolUse", []) if not _is_protect_entry(e)]
    pre.append({"matcher": PROTECT_MATCHER, "hooks": [{"type": "command", "command": command}]})
    hooks["PreToolUse"] = pre
    if factory_home:
        new.setdefault("env", {})["FACTORY_HOME"] = factory_home
    return new


def is_dev_mcp_name(name: str) -> bool:
    low = name.lower()
    return "dev.mcp" in low or "dev_mcp" in low


def merge_mcp_env(data: dict, env: dict[str, str]) -> tuple[dict, list[str] | None]:
    """Add missing `env` keys to every dev.mcp server entry (top level and per project).

    Returns (new data, names of added keys); added is None when there is no dev.mcp entry.
    Existing values and all other keys stay untouched.
    """
    new = json.loads(json.dumps(data))
    holders = [new] + [p for p in (new.get("projects") or {}).values() if isinstance(p, dict)] if isinstance(new, dict) else []
    found, added = False, []
    for holder in holders:
        servers = holder.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, conf in servers.items():
            if not (is_dev_mcp_name(name) and isinstance(conf, dict)):
                continue
            found = True
            target = conf.setdefault("env", {})
            if not isinstance(target, dict):
                continue
            for key, val in env.items():
                if key not in target:
                    target[key] = val
                    if key not in added:
                        added.append(key)
    return new, (added if found else None)


def _is_protect_entry(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and re.search(r"[\\/]protect\.js\b", str(h.get("command", "")))
               for h in entry.get("hooks", []))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Install the dev.factory Claude Code layer into a profile.")
    p.add_argument("--profile", required=True, help="Claude profile dir, e.g. ~/.claude")
    p.add_argument("--factory-home", help="FACTORY_HOME (default: env FACTORY_HOME or ~/.factory)")
    p.add_argument("--mcp-config", help="Claude MCP config to add the dev.mcp save_to env to "
                   "(default: ~/.claude.json for ~/.claude, else <profile>/.claude.json when present)")
    p.add_argument("--remove-harness", action="store_true", help="back up and remove the dev.harness layer first")
    p.add_argument("--yes", action="store_true", help="allow changes inside shared (linked) profile folders")
    p.add_argument("--dry-run", action="store_true", help="print actions only")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    inst = Installer(args)
    try:
        if args.remove_harness:
            inst.remove_harness()
        inst.install()
    except OSError as err:
        print(f"ERROR {err}", file=sys.stderr)
        return 2
    if inst.skipped:
        print(f"done with {len(inst.skipped)} skipped item(s)")
        return 1
    print("done" + (" (dry run, nothing changed)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
