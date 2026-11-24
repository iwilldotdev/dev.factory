"""`escaped_findings`: major findings raised after the prove gate passed, not every finding."""

import sys as _sys
import unittest
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from ops_helpers import FactoryCase

from factory.commands import metrics

PASS_PROVE = {"at": "2026-01-01T01:00:00+00:00", "kind": "gate_pass", "data": {"phase": "prove"}}


class EscapedFindingsTests(FactoryCase):
    def review(self, name, at, findings):
        self.write(f"runs/ws-dev-1/reviews/{name}.json", {"recorded": at, "findings": findings})

    def f(self, fid, sev, text="t", file="a.ts"):
        return {"id": fid, "severity": sev, "text": text, "file": file}

    def test_only_major_after_prove_counts(self):
        self.make_run("ws-dev-1", events=[PASS_PROVE])
        self.review("code-1", "2026-01-01T02:00:00+00:00",
                    [self.f("F1", "major", "x"), self.f("F2", "minor", "y"), self.f("F3", "nit", "z")])
        self.review("code-0", "2026-01-01T00:30:00+00:00", [self.f("F9", "major", "before prove")])
        self.review("feedback-1", "2026-01-01T03:00:00+00:00", [self.f("F1", "major", "pipeline failed", "")])
        e = metrics.compute("ws-dev-1")["escaped_findings"]
        self.assertEqual((e["count"], e["by_severity"], e["all_findings"]), (2, {"major": 2}, 4))

    def test_a_finding_carried_to_the_next_round_counts_once(self):
        self.make_run("ws-dev-1", events=[PASS_PROVE])
        self.review("code-1", "2026-01-01T02:00:00+00:00", [self.f("F1", "major", "same")])
        self.review("code-2", "2026-01-01T04:00:00+00:00", [self.f("F1", "major", "same")])
        self.assertEqual(metrics.compute("ws-dev-1")["escaped_findings"]["count"], 1)

    def test_no_prove_pass_means_zero(self):
        self.make_run("ws-dev-1", events=[])
        self.review("code-1", "2026-01-01T02:00:00+00:00", [self.f("F1", "major")])
        e = metrics.compute("ws-dev-1")["escaped_findings"]
        self.assertEqual((e["count"], e["by_severity"]), (0, {}))


if __name__ == "__main__":
    unittest.main()
