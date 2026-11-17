"""test collector: stable English tool output, and a run that executed no test never passes."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factory import core  # noqa: E402
from factory.collectors import test as test_collector  # noqa: E402

PY = f'"{sys.executable}"'


def say(text: str, code: int = 0) -> str:
    return f'{PY} -c "import sys; print({text!r}); sys.exit({code})"'


class Collector(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ctx = core.Ctx(run={}, workspace={}, run_dir=self.dir, repo_path=self.dir, env={}, out_dir=None)

    def collect(self, cmd: str, **extra) -> dict:
        return test_collector.collect(dict({"id": "CHK-1", "type": "test", "cmd": cmd}, **extra), self.ctx)

    def test_locale_variables_are_added_but_never_override(self):
        probe = (f'{PY} -c "import os; print(os.environ.get(\'DOTNET_CLI_UI_LANGUAGE\'), os.environ.get(\'DOTNET_NOLOGO\'), '
                 f'os.environ.get(\'VSLANG\'))"')
        env = {k: v for k, v in os.environ.items() if k not in ("DOTNET_CLI_UI_LANGUAGE", "DOTNET_NOLOGO", "VSLANG")}
        with mock.patch.dict(os.environ, env, clear=True):
            code, out = test_collector.run_shell(probe, self.dir)
        self.assertEqual((code, out.strip()), (0, "en 1 1033"))
        with mock.patch.dict(os.environ, {"DOTNET_CLI_UI_LANGUAGE": "pt-BR"}):
            self.assertTrue(test_collector.run_shell(probe, self.dir)[1].startswith("pt-BR"))

    def test_a_filter_matching_nothing_fails_as_setup(self):
        for text, code in (("No test matches the given testcase filter `Name~Nope` in x.dll", 0),
                           ("Nenhum teste corresponde ao filtro de caso de teste fornecido", 0),
                           ("No test files found, exiting with code 1", 1),
                           ("No tests found, exiting with code 1", 1),
                           ("============ no tests ran in 0.01s ============", 5),
                           ("collected 0 items", 0)):
            res = self.collect(say(text, code))
            self.assertEqual(res["status"], "fail", text)
            self.assertEqual(res["details"]["kind"], "setup", text)
            self.assertIn("no tests matched", res["summary"], text)

    def test_nonzero_expectation_does_not_turn_an_empty_run_into_a_pass(self):
        res = self.collect(say("No test files found", 1), expect="nonzero")
        self.assertEqual(res["status"], "fail")

    def test_a_real_run_still_passes(self):
        res = self.collect(say("Passed! 12 tests"))
        self.assertEqual(res["status"], "pass")
        self.assertNotIn("kind", res["details"])


if __name__ == "__main__":
    unittest.main()
