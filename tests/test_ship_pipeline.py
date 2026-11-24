"""require_pipeline: proof grade, the pushed head, the newest pipeline, waivers, and a pipeline never proving an AC."""
import json
import unittest

from tests.helpers import lesson
from tests.m3_support import ShipCase

OTHER_SHA = "a" * 40
CONFIRM = ("confirm", "change:42", "--user-said", "yes, that is the change on the board")


def pipe(ident="p1", sha=None, status="success", at="2026-11-24T10:00:00Z"):
    return {"id": ident, "sha": sha, "status": status, "created_at": at}


class PipelineTests(ShipCase):
    def setUp(self):
        super().setUp()
        self.to_ship(extra_toml="\n[ship]\nrequire_pipeline = true\n")
        self.ship_plan()

    def shipped(self, pipelines=("p1",)):
        self.ingest_change(pipelines=list(pipelines))
        self.ok(*CONFIRM)

    def pipeline(self, ident="p1", confirm=True, **over):
        data = pipe(ident, over.pop("sha", self.head()), **over)
        raw = self.run_dir / "ship" / "readback" / f"web-{ident}.json"
        raw.write_text(json.dumps({"payload": data}), encoding="utf-8")
        self.ok("ingest", "pipeline", "--raw", str(raw), "--stdin", stdin=json.dumps(data))
        if confirm:
            self.ok("confirm", f"pipeline:{ident}", "--user-said", "that is the pipeline of the change")

    def mismatches(self):
        return "\n".join(self.ship_verify(code=1)["mismatches"])

    def test_a_required_pipeline_must_exist_be_proof_grade_and_be_success(self):
        self.shipped(pipelines=())
        self.assertIn("no pipeline contract for the pushed head", self.mismatches())
        self.pipeline(confirm=False)
        self.shipped()
        self.assertIn("pipeline p1 was written by the agent", self.mismatches())
        self.ok("confirm", "pipeline:p1", "--user-said", "yes, green on the board")
        self.assertEqual(self.ship_verify()["result"], "pass")
        self.assertEqual(self.gate("ship")[0], 0)

    @lesson("L-155")
    def test_a_pipeline_of_an_older_head_or_an_older_pipeline_does_not_satisfy_the_gate(self):
        """L-155: the pipeline contract's SHA must equal the pushed head, be the latest, and be `success`."""
        self.shipped(pipelines=("p2", "p1"))
        self.pipeline("p1", sha=OTHER_SHA)
        self.assertIn("pipeline p2 is the newest on the change but has no contract", self.mismatches())
        self.pipeline("p2", sha=OTHER_SHA, at="2026-11-24T11:00:00Z")
        self.assertIn("pipeline p2 ran for aaaaaaa, not the pushed head", self.mismatches())
        self.pipeline("p2", status="failed", at="2026-11-24T11:00:00Z")
        self.assertIn("pipeline p2 is failed", self.mismatches())
        self.pipeline("p3", sha=self.head(), at="2026-11-24T12:00:00Z")
        self.shipped(pipelines=("p2", "p1"))
        self.assertIn("pipeline p2 is failed", self.mismatches(), "p2 is still the newest the change lists")
        self.shipped(pipelines=("p3", "p2"))
        self.assertEqual(self.ship_verify()["result"], "pass")

    @lesson("L-20")
    def test_a_waiver_names_one_pipeline_and_carries_the_users_words(self):
        """L-20: a waiver covers exactly the named pipeline and stops covering after a new one."""
        self.shipped()
        self.pipeline(status="failed")
        self.assertIn("pipeline p1 is failed", self.mismatches())
        self.fails("waive", "pipeline:p1", "--user-said", "")
        self.assertIn("no pipeline contract", self.fails("waive", "pipeline:p9", "--user-said", "ok"))
        self.assertIn("only a pipeline", self.fails("waive", "change:42", "--user-said", "ok"))
        self.ok("waive", "pipeline:p1", "--user-said", "the flaky job is known, ship it")
        self.assertEqual(self.ship_verify()["result"], "pass")
        self.pipeline("p2", status="failed", at="2026-11-24T12:00:00Z")
        self.shipped(pipelines=("p2", "p1"))
        self.assertIn("pipeline p2 is failed", self.mismatches())
        self.assertEqual(self.ship_events("waiver")[0]["pipeline"], "p1")

    @lesson("L-19")
    def test_a_pipeline_proves_no_ac_and_only_gates_ship_when_the_project_requires_it(self):
        """L-19: a green pipeline never satisfies the verify gate, and a red one never fails ship without require_pipeline."""
        self.write("factory.toml", self.TOML)
        self.shipped(pipelines=())
        self.pipeline(status="failed")
        self.assertEqual(self.ship_verify()["result"], "pass")
        self.assertEqual(self.gate("ship")[0], 0)
        self.commit("more.txt", "x\n", "fix: follow up")
        self.pipeline("p2", sha=self.head(), at="2026-11-24T12:00:00Z")
        self.assertIn("no verify record", self.failing("verify"))
        self.assertIn("no verify record", self.failing("ship"))


if __name__ == "__main__":
    unittest.main()
