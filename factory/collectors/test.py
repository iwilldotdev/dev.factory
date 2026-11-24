"""`test` collector: run a shell command in the repo worktree (DESIGN §4)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT_S = 900
TAIL = 4000


# Stable English tool output (the "no tests matched" and setup markers are matched on it); a value the
# user already set is never overridden.
LOCALE_ENV = {"DOTNET_CLI_UI_LANGUAGE": "en", "DOTNET_NOLOGO": "1", "VSLANG": "1033"}


def run_shell(cmd: str, cwd: Path, timeout: int = DEFAULT_TIMEOUT_S, env_vars: dict | None = None) -> tuple[int, str]:
    """Run `cmd` through the shell. Returns (exit_code, combined output). 124 on timeout."""
    env = dict(LOCALE_ENV)
    env.update(os.environ)
    env.update({k: str(v) for k, v in (env_vars or {}).items()})
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as err:
        out = err.stdout if isinstance(err.stdout, str) else ""
        return 124, f"{out}\n[timeout after {timeout}s]"
    return proc.returncode, (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")


def _rm_error(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def link_deps(src: Path, dst: Path) -> None:
    """Best effort: expose src/node_modules inside a temp worktree."""
    nm = Path(src) / "node_modules"
    target = Path(dst) / "node_modules"
    if not nm.is_dir() or target.exists():
        return
    try:
        os.symlink(nm, target, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(nm)],
                           capture_output=True, text=True)


def unlink_deps(dst: Path) -> None:
    target = Path(dst) / "node_modules"
    if target.is_symlink():
        target.unlink()
    elif target.exists() and os.name == "nt" and target.is_dir():
        try:
            os.rmdir(target)  # junction: removes the link only
        except OSError:
            pass


def setup_command(workspace: dict | None, alias: str | None) -> str | None:
    """`quality.setup` of the repo: the restore command every worktree gets before checks run."""
    for repo in (workspace or {}).get("repos", []):
        if alias and alias in (repo.get("alias"), repo.get("name")):
            return (repo.get("quality") or {}).get("setup") or None
    return None


def files_from_main_of(workspace: dict | None, alias: str | None) -> list[str]:
    """`env.files_from_main` of the repo: gitignored files (relative paths) a new worktree copies from the main checkout."""
    for repo in (workspace or {}).get("repos", []):
        if alias and alias in (repo.get("alias"), repo.get("name")):
            files = (repo.get("env") or {}).get("files_from_main")
            return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []
    return []


def safe_rel(rel) -> bool:
    """A non-empty relative path that stays inside its root: not absolute (POSIX, drive letter, UNC), no `..` segment."""
    text = str(rel).strip() if isinstance(rel, str) else ""
    if not text or text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", text):
        return False
    return ".." not in re.split(r"[\\/]+", text)


def copy_main_files(src: Path, dst: Path, files: list[str] | None) -> list[str]:
    """Copy (never link) each `files` entry from the main checkout `src` into the worktree `dst` when it exists in `src`
    and is absent in `dst`; an existing file is never overwritten. Returns the relative paths copied."""
    copied: list[str] = []
    for rel in files or []:
        if not safe_rel(rel):
            continue
        source, target = Path(src) / rel, Path(dst) / rel
        if not source.is_file() or target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError:
            continue
        copied.append(rel)
    return copied


def prepare_worktree(src: Path, dst: Path, setup: str | None = None, env_vars: dict | None = None) -> None:
    """Identical preparation for every worktree (run, temp base, red, green): `setup` when the repo
    declares one (no node_modules link then: a restore through a junction would rewrite the main repo),
    else link src/node_modules. Raises RuntimeError when setup fails."""
    if setup:
        code, out = run_shell(setup, Path(dst), DEFAULT_TIMEOUT_S, env_vars)
        if code != 0:
            raise RuntimeError(f"setup `{setup}` failed (exit {code}): {out.strip()[-500:]}")
        return
    link_deps(src, dst)


def temp_worktree(repo: Path, ref: str, setup: str | None = None, env_vars: dict | None = None,
                  files: list[str] | None = None) -> Path:
    """Detached worktree of `repo` at `ref` in a fresh temp dir, prepared like the run worktree (`files`: the repo's
    `env.files_from_main`, copied from `repo`)."""
    base = Path(tempfile.mkdtemp(prefix="factory-wt-"))
    target = base / "wt"
    proc = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(target), ref],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")
    try:
        prepare_worktree(repo, target, setup, env_vars)
    except RuntimeError:
        drop_worktree(repo, target)
        raise
    copy_main_files(repo, target, files)
    return target


def drop_worktree(repo: Path, target: Path) -> None:
    unlink_deps(target)
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(target)],
                   capture_output=True, text=True)
    shutil.rmtree(target.parent, ignore_errors=True, onerror=_rm_error)
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)


def collect(check: dict, ctx) -> dict:
    if ctx.repo_path is None:
        return {"status": "error", "summary": "test check needs a repo"}
    cmd = check.get("cmd")
    if not cmd:
        return {"status": "error", "summary": "test check has no cmd"}
    cwd = Path(ctx.repo_path) / (check.get("cwd") or ".")
    timeout = int(check.get("timeout_s") or DEFAULT_TIMEOUT_S)
    code, output = run_shell(cmd, cwd, timeout, (ctx.env or {}).get("env_vars"))
    expect = check.get("expect") or "exit0"
    if expect == "exit0":
        ok = code == 0
    elif expect == "nonzero":
        ok = code != 0
    elif isinstance(expect, str) and expect.startswith("exit"):
        ok = code == int(expect[4:])
    else:
        return {"status": "error", "summary": f"unknown expect {expect!r}"}
    artifacts = []
    if ctx.out_dir is not None:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / "output.log").write_text(output, encoding="utf-8")
        artifacts.append(f"evidence/{check['id']}/output.log")
    details = {"cmd": cmd, "exit_code": code, "output_tail": output[-TAIL:]}
    empty = no_tests_marker(output)
    if empty:
        details["kind"] = "setup"
        return {"status": "fail", "summary": f"no tests matched ({empty!r}): a filter matching nothing never passes",
                "artifacts": artifacts, "details": details}
    failure = fail_pattern_error(check, code, output)
    if failure:
        return {"status": "error", "summary": failure, "artifacts": artifacts, "details": details}
    return {
        "status": "pass" if ok else "fail",
        "summary": f"exit {code} (expected {expect})",
        "artifacts": artifacts,
        "details": details,
    }


# a run that executed zero tests (a filter matching nothing) is never a pass
NO_TESTS_RE = re.compile("|".join((
    r"No test matches the given testcase filter", r"Nenhum teste corresponde", r"No test is available in",
    r"No test files? found", r"No tests found", r"no tests ran", r"collected 0 items",
    )), re.IGNORECASE)
GO_NO_TESTS_RE = re.compile(r"\[no tests to run\]|testing: warning: no tests to run", re.IGNORECASE)
GO_RAN_RE = re.compile(r"^ok\s+\S+\s+[\d.]+s\s*(?:\(cached\))?\s*$|^\s*--- (?:PASS|FAIL)", re.MULTILINE)
CARGO_RUNNING_RE = re.compile(r"^\s*running (\d+) tests?\b", re.MULTILINE)
MOCHA_ZERO_RE = re.compile(r"^\s*0 passing\b", re.MULTILINE)
MOCHA_FAILING_RE = re.compile(r"^\s*[1-9]\d* failing\b", re.MULTILINE)
DOTNET_TOTAL_RE = re.compile(r"Total tests:\s*(\d+)", re.IGNORECASE)
DOTNET_LINE_RE = re.compile(r"(?:Passed|Failed)!\s+-\s+Failed:\s*(\d+),\s*Passed:\s*(\d+)", re.IGNORECASE)


def _dotnet_executed(output: str) -> tuple[int, str] | None:
    """(tests reported by the dotnet summaries, text of the zero-marker) or None when no summary is printed."""
    totals = [int(n) for n in DOTNET_TOTAL_RE.findall(output)]
    lines = [(int(f), int(p)) for f, p in DOTNET_LINE_RE.findall(output)]
    if not totals and not lines:
        return None
    return sum(totals) + sum(f + p for f, p in lines), "Total tests: 0"


def no_tests_marker(output: str) -> str | None:
    """Text proving that nothing was executed, else None. Parsed totals win over message markers (a multi-project
    dotnet run where one project matches no test but another passed some is a real run)."""
    output = output or ""
    dotnet = _dotnet_executed(output)
    if dotnet is not None:
        return dotnet[1] if dotnet[0] == 0 else None
    if GO_NO_TESTS_RE.search(output) and not GO_RAN_RE.search(output):
        return "no tests to run"
    runs = [int(n) for n in CARGO_RUNNING_RE.findall(output)]
    if runs and sum(runs) == 0:
        return "running 0 tests"
    if MOCHA_ZERO_RE.search(output) and not MOCHA_FAILING_RE.search(output):
        return "0 passing"
    m = NO_TESTS_RE.search(output)
    return m.group(0) if m else None


# exit codes of a shell that could not run the command (126 not executable, 127 / cmd 9009 not found)
SETUP_EXIT_CODES = frozenset({126, 127, 9009})
SETUP_FAILURE_RE = re.compile("|".join(re.escape(m) for m in (
    "command not found", "is not recognized as an internal or external command", "ENOENT",
    "No such file or directory", "Cannot find module", "could not find a part of the path",
    "MSBUILD : error MSB1009", "ModuleNotFoundError")), re.IGNORECASE)
MIN_PATTERN_LEN = 6
MIN_PATTERN_WORD_CHARS = 4


def weak_pattern_reason(pattern) -> str | None:
    r"""None when `expect_fail_pattern` is specific enough to name a test or assertion: at least
    MIN_PATTERN_LEN chars and MIN_PATTERN_WORD_CHARS literal word characters (`.`, `.*`, `\w+` are rejected)."""
    text = str(pattern or "")
    if len(text) < MIN_PATTERN_LEN:
        return f"expect_fail_pattern {text!r} is shorter than {MIN_PATTERN_LEN} characters"
    lit = re.sub(r"\[(?:\\.|[^\]\\])*\]", "", text)  # character classes
    lit = re.sub(r"\(\?(?:P?<\w+>|P=\w+\)|[aiLmsux-]+\)|[:=!]|<[=!])", "", lit)  # group names, inline flags
    lit = re.sub(r"\{\d*(?:,\d*)?\}", "", lit)  # {n,m} quantifiers
    lit = re.sub(r"\\[A-Za-z0-9]", "", lit)  # \w \d \b \1 ... are not literal characters
    words = len(re.findall(r"\w", lit))
    if words < MIN_PATTERN_WORD_CHARS:
        return (f"expect_fail_pattern {text!r} has {words} literal word character(s); it needs at least "
                f"{MIN_PATTERN_WORD_CHARS} (name the failing test or assertion text)")
    return None


def _first_word(cmd: str | None) -> str | None:
    m = re.match(r'\s*(?:"([^"]+)"|(\S+))', cmd or "")
    return (m.group(1) or m.group(2)) if m else None


def setup_failure_reason(code: int, output: str, cmd: str | None = None) -> str | None:
    """A failing exit that is the shell or toolchain not finding the command, file or module."""
    if code in SETUP_EXIT_CODES:
        return f"exit {code} (command not found / not executable)"
    m = SETUP_FAILURE_RE.search(output or "")
    if m:
        return f"output shows a setup failure marker {m.group(0)!r}"
    word = _first_word(cmd)
    # Windows cmd.exe prints `'<cmd>' <localized "is not recognized ...">` (exit 1) in the user's language.
    if os.name == "nt" and word and re.search(rf"^'{re.escape(word)}'\s", output or "", re.MULTILINE):
        return f"cmd.exe could not run {word!r} (command not found)"
    return None


def fail_pattern_error(check: dict, code: int, output: str) -> str | None:
    """A failing exit must show `expect_fail_pattern` (the test name or assertion) and no setup-failure
    marker (command/file/module not found); otherwise it is a setup failure, not a reproduction."""
    pattern = check.get("expect_fail_pattern")
    if not pattern or code == 0:
        return None
    setup = setup_failure_reason(code, output, check.get("cmd"))
    if setup:
        return f"could not reproduce: setup failure ({setup})"
    try:
        found = re.search(pattern, output, re.MULTILINE)
    except re.error as err:
        return f"invalid expect_fail_pattern {pattern!r}: {err}"
    if found:
        return None
    return f"could not reproduce: setup failure (exit {code} without expect_fail_pattern {pattern!r} in the output)"
