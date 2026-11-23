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
        for patcher in (mock.patch.object(install, "REPO", self.src), mock.patch.object(install, "claude_running", lambda: False)):
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


if __name__ == "__main__":
    unittest.main()
