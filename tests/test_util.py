"""Shared helpers: canonical JSON, safe ids, atomic writes, redaction, versions, process control."""
import json
import os
import sys
import time
import unittest
from unittest import mock

import subprocess
from tests.helpers import ROOT, lesson, tmpdir
from dev_factory import proc, util


class UtilTests(unittest.TestCase):
    @lesson("L-167")
    def test_canonical_json_ignores_key_order_and_whitespace(self):
        """L-167: hashes are computed over canonical JSON."""
        a = {"b": 1, "a": {"y": [1, 2], "x": "é"}}
        b = json.loads('{ "a": {"x": "é", "y": [1,2]}, "b": 1 }')
        self.assertEqual(util.canonical(a), util.canonical(b))
        self.assertEqual(util.canonical(a), '{"a":{"x":"é","y":[1,2]},"b":1}')
        self.assertEqual(util.sha256_bytes(util.canonical(a).encode()), util.sha256_bytes(util.canonical(b).encode()))

    @lesson("L-157")
    def test_safe_id_maps_unsafe_characters_and_rejects_dot_names(self):
        """L-157: provider ids such as 3544:5710 or group/proj#42 become safe file names."""
        self.assertEqual(util.safe_id("3544:5710"), "3544-5710")
        self.assertEqual(util.safe_id("group/proj#42"), "group-proj-42")
        self.assertEqual(util.safe_id("ABC-1.x_y"), "ABC-1.x_y")
        for bad in ("", ".", ".."):
            with self.assertRaises(util.FactoryError):
                util.safe_id(bad)

    @lesson("L-163")
    def test_atomic_write_retries_sharing_violations(self):
        """L-163: atomic replace retries PermissionError (Windows sharing violation) and leaves no temp file."""
        d = tmpdir()
        target = d / "state.json"
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("sharing violation")
            return real(src, dst)
        with mock.patch("os.replace", flaky):
            util.write_atomic(target, '{"ok": true}')
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(target.read_text()), {"ok": True})
        self.assertEqual([p.name for p in d.iterdir()], ["state.json"])

    @lesson("L-163")
    def test_atomic_write_never_leaves_a_torn_file_when_replace_keeps_failing(self):
        """L-163: a permanent failure raises, keeps the old content and removes the temp file."""
        d = tmpdir()
        target = d / "state.json"
        target.write_text("old")
        with mock.patch("os.replace", side_effect=PermissionError("locked")), mock.patch("time.sleep"):
            with self.assertRaises(PermissionError):
                util.write_atomic(target, "new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual([p.name for p in d.iterdir()], ["state.json"])

    @lesson("L-80")
    def test_redact_replaces_every_secret_value(self):
        """L-80: stored tails never keep a secret value."""
        self.assertEqual(util.redact("token=abcd1234 and abcd1234", ["abcd1234"]), "token=*** and ***")
        self.assertEqual(util.redact("x", ["", "ab"]), "x")

    def test_version_specifier(self):
        """L-149: a project can pin the CLI version; pre-releases order below the release."""
        self.assertTrue(util.version_ok(">=1.0.0a1,<1.1", "1.0.0a1"))
        self.assertTrue(util.version_ok(">=1.0.0a1,<1.1", "1.0.2"))
        self.assertFalse(util.version_ok(">=1.0.0a2", "1.0.0a1"))
        self.assertTrue(util.version_ok("<1.0.0", "1.0.0a1"))
        self.assertFalse(util.version_ok(">=1.1", "1.0.9"))
        with self.assertRaises(util.FactoryError):
            util.version_ok("~=1.0", "1.0.0")

    def test_fwd_is_absolute_with_forward_slashes(self):
        self.assertNotIn("\\", util.fwd("a/b"))
        self.assertTrue(os.path.isabs(util.fwd("a/b")))


class ProcTests(unittest.TestCase):
    def py(self, code: str, timeout: float = 20, env=None):
        return proc.run([sys.executable, "-c", code], cwd=tmpdir(), env=proc.child_env(env), timeout_s=timeout)

    @lesson("L-44")
    def test_stdin_is_closed(self):
        """L-44: a child that reads stdin sees end of file at once instead of hanging."""
        script = ("import json, sys\nfrom pathlib import Path\nfrom dev_factory import proc\n"
                  "r = proc.run([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'], cwd=Path('.'),\n"
                  "             env=proc.child_env(), timeout_s=8)\n"
                  "print(json.dumps([r.out.strip(), r.timed_out]))\n")
        # The parent's own stdin is an open pipe that never delivers data: only a closed child stdin finishes at once.
        p = subprocess.Popen([sys.executable, "-P", "-c", script], cwd=tmpdir(), env=dict(os.environ, PYTHONPATH=str(ROOT)),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = p.communicate(timeout=60)
        finally:
            p.kill()
        self.assertEqual(json.loads(out), ["''", False], err)

    @lesson("L-45")
    def test_children_get_an_english_locale(self):
        """L-45: children run with a forced English locale."""
        res = self.py("import os; print(os.environ['LC_ALL'], os.environ['LANGUAGE'])", env={"LC_ALL": "pt_BR.UTF-8"})
        self.assertEqual(res.out.split(), ["C", "en"])

    @lesson("L-42", "L-43")
    def test_timeout_kills_the_whole_process_tree(self):
        """L-42, L-43: after the wall-clock cap the child and its grandchild are gone, on Windows too."""
        d = tmpdir()
        pid_file = d / "pid"
        code = ("import subprocess, sys, time\n"
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                f"open({pid_file.as_posix()!r}, 'w').write(str(c.pid))\n"
                "time.sleep(120)\n")
        res = self.py(code, timeout=3)
        self.assertTrue(res.timed_out)
        self.assertIsNone(res.exit)
        grand = int(pid_file.read_text())
        for _ in range(50):
            if not proc.pid_alive(grand):
                break
            time.sleep(0.1)
        self.assertFalse(proc.pid_alive(grand), "grandchild survived the timeout")

    def test_template_is_split_before_substitution_and_never_reparsed(self):
        """L-130: shell metacharacters in a value stay one literal argument; no shell is involved."""
        argv = proc.split_template('tool "a b" {id} --x={id}')
        self.assertEqual(argv, ["tool", "a b", "{id}", "--x={id}"])
        out = proc.substitute(argv, {"id": 'A&B | %PATH% "q" $(x)'})
        self.assertEqual(out[2], 'A&B | %PATH% "q" $(x)')
        self.assertEqual(out[3], '--x=A&B | %PATH% "q" $(x)')
        with self.assertRaises(util.FactoryError):
            proc.substitute(["{nope}"], {"id": "1"})

    def test_missing_executable_is_a_clean_error(self):
        with self.assertRaises(util.FactoryError):
            proc.run(["definitely-not-a-command-xyz"], cwd=tmpdir(), env=proc.child_env(), timeout_s=5)


if __name__ == "__main__":
    unittest.main()
