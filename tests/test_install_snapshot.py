"""The installer's runtime snapshot (`<profile>/factory-dist`), the shims that run it, and `--version`."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install  # noqa: E402
from factory import dist_info  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def _find_bash() -> str | None:
    """A POSIX bash (Git Bash on Windows). The bare name would resolve to the WSL launcher in System32 there."""
    found = shutil.which("bash")
    if found and "system32" in found.lower():
        found = None
    return found


BASH = _find_bash()


def git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._cleanup)
        # a fake source checkout: the real package, a stub browser build, a node_modules, committed in git
        self.src = self.tmp / "src"
        shutil.copytree(ROOT / "factory", self.src / "factory", ignore=shutil.ignore_patterns("__pycache__"))
        (self.src / "browser" / "dist").mkdir(parents=True)
        (self.src / "browser" / "dist" / "cli.js").write_text("// stub\n", encoding="utf-8")
        (self.src / "browser" / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")
        (self.src / "browser" / "node_modules" / "dep").mkdir(parents=True)
        (self.src / "browser" / "node_modules" / "dep" / "index.js").write_text("//\n", encoding="utf-8")
        (self.src / ".gitignore").write_text("node_modules\n__pycache__\n", encoding="utf-8")
        git(self.src, "init", "-q")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "init")
        self.profile = self.tmp / "profile"
        self.fh = self.tmp / "fh"
        self.profile.mkdir()
        for patcher in (mock.patch.object(install, "REPO", self.src),):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.install()

    def _cleanup(self):
        for root, dirs, _ in os.walk(self.tmp):
            for d in list(dirs):
                if install.is_link(Path(root) / d):
                    install.remove_link(Path(root) / d)
                    dirs.remove(d)
        self._tmp.cleanup()

    def install(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            install.main(["--profile", str(self.profile), "--factory-home", str(self.fh), "--mcp-config", str(self.tmp / "none")])
        return out.getvalue()

    def shim(self, *args: str, src: Path | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
        exe = self.profile / "bin" / ("factory.cmd" if os.name == "nt" else "factory")
        env = {k: v for k, v in os.environ.items() if k not in ("FACTORY_SRC", "PYTHONPATH")}
        if src:
            env["FACTORY_SRC"] = str(src)
        return subprocess.run([str(exe), *args], capture_output=True, text=True, encoding="utf-8", env=env,
                              cwd=cwd or self.tmp, timeout=60)

    def set_version(self, root: Path, version: str) -> None:
        core = root / "factory" / "core.py"
        core.write_text(core.read_text(encoding="utf-8").replace('VERSION = "0.1.0"', f'VERSION = "{version}"'), encoding="utf-8")

    def test_layout_and_provenance(self):
        dist = self.profile / "factory-dist"
        self.assertTrue((dist / "factory" / "cli.py").is_file())
        self.assertFalse(list(dist.rglob("__pycache__")))
        self.assertTrue((dist / "browser" / "dist" / "cli.js").is_file())
        self.assertTrue((dist / "browser" / "package.json").is_file())
        self.assertTrue((dist / "browser" / "node_modules" / "dep" / "index.js").exists())
        prov = json.loads((dist / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(prov["source"], install.fwd(self.src))
        self.assertEqual(prov["sha"], git(self.src, "rev-parse", "HEAD"))
        self.assertIs(prov["dirty"], False)
        self.assertRegex(prov["installed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        self.assertFalse((self.profile / "factory-dist.new").exists())

    def test_version_reports_snapshot(self):
        proc = self.shim("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("snapshot", proc.stdout)
        self.assertIn(git(self.src, "rev-parse", "HEAD")[:9], proc.stdout)
        self.assertIn("factory-dist", proc.stdout)
        self.assertEqual(proc.stderr.strip(), "")  # source still at the installed HEAD

    def test_snapshot_ignores_later_source_edits(self):
        self.set_version(self.src, "9.9.9")
        out = self.shim("--version").stdout
        self.assertIn("factory 0.1.0", out)
        self.assertNotIn("9.9.9", out)

    def test_snapshot_wins_over_cwd_checkout(self):
        self.set_version(self.src, "9.9.9")
        proc = self.shim("--version", cwd=self.src)  # `python -m factory` would pick the cwd tree
        self.assertIn("snapshot", proc.stdout)
        self.assertIn("factory 0.1.0", proc.stdout)

    def test_factory_src_runs_source(self):
        self.set_version(self.src, "9.9.9")
        proc = self.shim("--version", src=self.src)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("factory 9.9.9 (source", proc.stdout)
        self.assertIn(str(self.src.resolve()).lower(), proc.stdout.lower())

    def test_behind_source_notice(self):
        self.assertIsNone(dist_info.behind_source(self.profile / "factory-dist"))
        self.set_version(self.src, "9.9.9")  # dirty runtime file
        proc = self.shim("--version")
        self.assertIn("installed snapshot is behind source (run install.py)", proc.stderr)
        self.assertNotIn("behind", proc.stdout)
        git(self.src, "commit", "-q", "-am", "bump")  # new HEAD, clean tree
        self.assertIn("behind source", self.shim("--version").stderr)
        self.install()
        self.assertEqual(self.shim("--version").stderr.strip(), "")
        self.assertIn("factory 9.9.9", self.shim("--version").stdout)

    def test_notice_on_run_status_only_and_never_in_json_stdout(self):
        self.set_version(self.src, "9.9.9")
        status = self.shim("--json", "run", "status", "no-such-run")
        self.assertIn("behind source", status.stderr)
        self.assertNotIn("behind", status.stdout)
        other = self.shim("run", "list")  # other commands stay free of git calls
        self.assertNotIn("behind source", other.stderr)

    def test_untracked_non_runtime_file_is_not_behind(self):
        (self.src / "notes.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(dist_info.behind_source(self.profile / "factory-dist"))

    def test_reinstall_replaces_snapshot_and_backs_up_the_old_one(self):
        self.set_version(self.src, "9.9.9")
        out = self.install()
        self.assertIn("factory 9.9.9", self.shim("--version").stdout)
        backups = list((self.profile / "backups").glob("factory-*/factory-dist/provenance.json"))
        self.assertEqual(len(backups), 1, out)
        self.assertFalse((backups[0].parent / "browser" / "node_modules").exists())
        self.assertTrue((self.src / "browser" / "node_modules" / "dep" / "index.js").is_file())  # target untouched

    def test_failed_install_keeps_the_old_snapshot(self):
        before = (self.profile / "factory-dist" / "provenance.json").read_text(encoding="utf-8")
        with mock.patch.object(install.Installer, "build_dist", side_effect=OSError("disk full")):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = install.main(["--profile", str(self.profile), "--factory-home", str(self.fh), "--mcp-config", str(self.tmp / "none")])
        self.assertEqual(code, 2)
        self.assertEqual((self.profile / "factory-dist" / "provenance.json").read_text(encoding="utf-8"), before)
        self.assertFalse((self.profile / "factory-dist.new").exists())
        self.assertIn("snapshot", self.shim("--version").stdout)

    def test_node_modules_copy_fallback(self):
        def copy(src, dest):
            shutil.copytree(src, dest)
            return "copied"
        with mock.patch.object(install, "link_or_copy_dir", side_effect=copy):
            self.install()
        nm = self.profile / "factory-dist" / "browser" / "node_modules"
        self.assertTrue((nm / "dep" / "index.js").is_file())
        self.assertFalse(install.is_link(nm))

    def test_shims_text(self):
        sh = (self.profile / "bin" / "factory").read_text(encoding="utf-8")
        cmd = (self.profile / "bin" / "factory.cmd").read_text(encoding="utf-8")
        self.assertIn(install.fwd(self.profile / "factory-dist"), sh)
        self.assertNotIn(install.fwd(self.src), sh)
        self.assertIn('[ -n "$FACTORY_SRC" ]', sh)
        self.assertIn("if defined FACTORY_SRC", cmd)
        self.assertNotIn(str(self.src), cmd)


    # ---------- cwd checkout never wins ----------

    def shim_env(self, pythonpath: str | None, src: str | None = None) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("FACTORY_SRC", "PYTHONPATH")}
        if pythonpath is not None:
            env["PYTHONPATH"] = pythonpath
        if src is not None:
            env["FACTORY_SRC"] = src
        return env

    def run_shim_in(self, kind: str, args, *, env: dict, cwd: Path, stdin: str | None = None):
        bin_dir = self.profile / "bin"
        cmdline = [str(bin_dir / "factory.cmd")] if kind == "cmd" else [BASH, install.fwd(bin_dir / "factory")]
        return subprocess.run([*cmdline, *args], capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd,
                              input=stdin, timeout=60)

    def kinds(self) -> list[str]:
        """Shims runnable here: factory.cmd on Windows, the sh shim through bash when there is one."""
        return (["cmd"] if os.name == "nt" else []) + (["sh"] if BASH else [])

    def test_cwd_checkout_never_wins_pythonpath_unset_and_set(self):
        self.set_version(self.src, "9.9.9")
        other = self.tmp / "other"
        other.mkdir()
        for kind in self.kinds():
            for pp in (None, str(other)):
                with self.subTest(kind=kind, pythonpath=pp):
                    proc = self.run_shim_in(kind, ["--version"], env=self.shim_env(pp), cwd=self.src)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("snapshot", proc.stdout)
                    self.assertIn("factory 0.1.0", proc.stdout)

    def test_missing_root_fails_clearly_instead_of_using_the_cwd_checkout(self):
        for kind in self.kinds():
            for pp in (None, str(self.tmp / "other")):
                with self.subTest(kind=kind, pythonpath=pp, case="FACTORY_SRC"):
                    env = self.shim_env(pp, src=str(self.tmp / "no" / "such"))
                    proc = self.run_shim_in(kind, ["--version"], env=env, cwd=self.src)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("no factory", proc.stderr)
                    self.assertNotIn("factory 0.1.0", proc.stdout)
        install.drop_dir(self.profile / "factory-dist")
        for kind in self.kinds():
            for pp in (None, str(self.tmp / "other")):
                with self.subTest(kind=kind, pythonpath=pp, case="missing snapshot"):
                    proc = self.run_shim_in(kind, ["--version"], env=self.shim_env(pp), cwd=self.src)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("no factory", proc.stderr)
                    self.assertIn("factory-dist", proc.stderr)
                    self.assertNotIn("factory 0.1.0", proc.stdout)

    def test_shims_text_has_no_empty_pythonpath_entry(self):
        cmd = (self.profile / "bin" / "factory.cmd").read_text(encoding="utf-8")
        self.assertNotIn('set "PYTHONPATH=%FACTORY_ROOT%;%PYTHONPATH%"', cmd)
        self.assertIn("if defined PYTHONPATH", cmd)

    # ---------- argument, stdin and exit code passthrough ----------

    def probe_root(self) -> Path:
        root = self.tmp / "probe"
        (root / "factory").mkdir(parents=True)
        (root / "factory" / "__init__.py").write_text("", encoding="utf-8")
        (root / "factory" / "cli.py").write_text(
            "import json, sys\n"
            "def main():\n"
            "    data = sys.stdin.read()\n"
            "    sys.stdout.write(json.dumps({'argv': sys.argv[1:], 'stdin': data}))\n"
            "    return int(sys.argv[2]) if sys.argv[1:2] == ['exit'] else 0\n", encoding="utf-8")
        return root

    def test_shims_pass_arguments_stdin_and_exit_code(self):
        root = self.probe_root()
        cases = [["plain"], ["has space", "b"], ['say "hi" there'], ["a=b", "--flag=x y"], ["it's"]]
        for kind in self.kinds():
            for args in cases:
                if kind == "sh" and "'" in args[0]:
                    continue  # Windows -> MSYS argument conversion eats the apostrophe before the shim runs
                with self.subTest(kind=kind, args=args):
                    proc = self.run_shim_in(kind, args, env=self.shim_env(None, src=str(root)), cwd=self.tmp, stdin="in\nput")
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(json.loads(proc.stdout), {"argv": args, "stdin": "in\nput"})
            with self.subTest(kind=kind, case="exit code"):
                proc = self.run_shim_in(kind, ["exit", "7"], env=self.shim_env(None, src=str(root)), cwd=self.tmp)
                self.assertEqual(proc.returncode, 7)

    # ---------- swap robustness ----------

    def test_locked_snapshot_stops_the_install_before_anything_else_changes(self):
        dist = self.profile / "factory-dist"
        shim_before = (self.profile / "bin" / "factory.cmd").read_bytes()
        prov_before = (dist / "provenance.json").read_text(encoding="utf-8")
        (self.profile / "hooks" / "protect.js").unlink()  # a later install step would put it back
        self.set_version(self.src, "9.9.9")
        real = os.replace

        def locked(src, dst, *a, **k):
            if Path(src) == dist:
                raise PermissionError(5, "Access is denied", str(src))
            return real(src, dst, *a, **k)
        err = io.StringIO()
        with mock.patch.object(install.os, "replace", locked), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = install.main(["--profile", str(self.profile), "--factory-home", str(self.fh), "--mcp-config", str(self.tmp / "none")])
        self.assertEqual(code, 2)
        self.assertIn(str(dist), err.getvalue())
        self.assertIn("close", err.getvalue())
        self.assertEqual((dist / "provenance.json").read_text(encoding="utf-8"), prov_before)
        self.assertEqual((self.profile / "bin" / "factory.cmd").read_bytes(), shim_before)
        self.assertFalse((self.profile / "hooks" / "protect.js").exists())
        self.assertFalse((self.profile / "factory-dist.new").exists())
        self.assertIn("factory 0.1.0", self.shim("--version").stdout)

    def test_failed_second_replace_restores_the_old_snapshot_with_its_node_modules(self):
        dist = self.profile / "factory-dist"
        real = os.replace

        def fail_swap_in(src, dst, *a, **k):
            if Path(dst) == dist and Path(src).name == "factory-dist.new":
                raise PermissionError(5, "Access is denied", str(dst))
            return real(src, dst, *a, **k)
        with mock.patch.object(install.os, "replace", fail_swap_in), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = install.main(["--profile", str(self.profile), "--factory-home", str(self.fh), "--mcp-config", str(self.tmp / "none")])
        self.assertEqual(code, 2)
        self.assertTrue((dist / "browser" / "node_modules" / "dep" / "index.js").is_file())
        self.assertIn("snapshot", self.shim("--version").stdout)
        self.assertTrue((self.src / "browser" / "node_modules" / "dep" / "index.js").is_file())

    def test_backup_prune_failure_keeps_the_new_snapshot(self):
        self.set_version(self.src, "9.9.9")
        real = install.drop_dir

        def flaky(path):
            if Path(path).name == "node_modules":
                raise PermissionError(5, "Access is denied", str(path))
            return real(path)
        with mock.patch.object(install, "drop_dir", flaky):
            out = self.install()
        self.assertIn("could not prune", out)
        self.assertIn("factory 9.9.9", self.shim("--version").stdout)
        self.assertTrue((self.src / "browser" / "node_modules" / "dep" / "index.js").is_file())

    # ---------- provenance state and stale browser build ----------

    def test_install_from_a_dirty_tree_is_not_behind_until_it_changes_again(self):
        self.set_version(self.src, "9.9.9")
        self.install()
        dist = self.profile / "factory-dist"
        self.assertIsNone(dist_info.behind_source(dist))
        self.assertEqual(self.shim("--version").stderr.strip(), "")
        self.set_version(self.src, "9.9.9")  # same content again
        self.assertIsNone(dist_info.behind_source(dist))
        (self.src / "factory" / "new_module.py").write_text("x = 1\n", encoding="utf-8")  # untracked runtime file
        self.assertIn("behind source", dist_info.behind_source(dist))
        (self.src / "factory" / "new_module.py").unlink()
        self.assertIsNone(dist_info.behind_source(dist))
        core = self.src / "factory" / "core.py"
        core.write_text(core.read_text(encoding="utf-8").replace("9.9.9", "8.8.8"), encoding="utf-8")
        self.assertIn("behind source", self.shim("--version").stderr)

    def test_stale_browser_dist_is_flagged(self):
        src_ts = self.src / "browser" / "src" / "cli.ts"
        src_ts.parent.mkdir(parents=True)
        src_ts.write_text("export {}\n", encoding="utf-8")
        js = self.src / "browser" / "dist" / "cli.js"
        os.utime(js, (1_000_000_000, 1_000_000_000))
        out = self.install()
        self.assertIn("npm run build", out)
        prov = json.loads((self.profile / "factory-dist" / "provenance.json").read_text(encoding="utf-8"))
        self.assertIs(prov["dist_stale"], True)
        self.assertIn("npm run build", dist_info.behind_source(self.profile / "factory-dist"))
        os.utime(js, None)  # rebuilt
        out = self.install()
        self.assertNotIn("npm run build", out)
        prov = json.loads((self.profile / "factory-dist" / "provenance.json").read_text(encoding="utf-8"))
        self.assertIs(prov["dist_stale"], False)


if __name__ == "__main__":
    unittest.main()
