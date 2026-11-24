"""Contract validation: ticket, change, pipeline."""
import unittest

from tests.helpers import lesson, valid_ticket
from dev_factory import contracts
from dev_factory.util import FactoryError

SHA = "a" * 40
CHANGE = {"id": "42", "url": "https://x.example/42", "repo": "web", "title": "T", "description_md": "", "source_branch": "feature/a",
          "target_branch": "main", "head_sha": SHA, "state": "open", "draft": True}
PIPELINE = {"id": "9", "sha": SHA, "status": "success", "created_at": "2026-11-24T14:03:11Z"}


def errors(kind, data, repos=None) -> str:
    with unittest.TestCase().assertRaises(FactoryError) as cm:
        contracts.validate(kind, data, repos)
    return str(cm.exception)


class TicketTests(unittest.TestCase):
    def test_a_valid_ticket_passes_and_optional_nulls_are_dropped(self):
        out = contracts.validate("ticket", valid_ticket(url=None, labels=["a"], acceptance=["x"]))
        self.assertNotIn("url", out)
        self.assertEqual(out["labels"], ["a"])

    @lesson("L-156")
    def test_null_required_field_and_unmapped_enum_are_errors_listing_every_problem(self):
        """L-156: a null required field or an unmapped enum is an error, never inferred."""
        msg = errors("ticket", valid_ticket(title=None, type="story", status=""))
        self.assertIn("data.title: required, and null is not accepted", msg)
        self.assertIn("data.type: 'story' is not one of feature, bug", msg)
        self.assertIn("map provider values", msg)
        self.assertIn("data.status: must be a non-empty string", msg)

    @lesson("L-156")
    def test_missing_unknown_and_mistyped_fields(self):
        """L-156: missing required fields, unknown keys and wrong types are all reported."""
        data = valid_ticket()
        del data["attachments"]
        data["extra"] = 1
        data["labels"] = "x"
        msg = errors("ticket", data)
        for needle in ("data.attachments: required", "data.extra: unknown key", "data.labels: must be a list of strings"):
            self.assertIn(needle, msg)
        self.assertIn("must be a JSON object", errors("ticket", []))

    def test_description_may_be_empty_but_not_missing(self):
        contracts.validate("ticket", valid_ticket(description_md=""))
        self.assertIn("description_md: required", errors("ticket", {k: v for k, v in valid_ticket().items() if k != "description_md"}))

    @lesson("L-29")
    def test_mime_is_case_normalized_and_the_extension_fills_a_missing_or_generic_mime(self):
        """L-29: `Image/PNG` and `application/octet-stream` do not hide media."""
        atts = [{"id": "1", "name": "a.PNG", "mime": "Image/PNG; charset=x"}, {"id": "2", "name": "clip.MP4", "mime": "application/octet-stream"},
                {"id": "3", "name": "shot.jpg"}, {"id": "4", "name": "doc.pdf", "mime": "application/pdf"}, {"id": "5", "name": "notes", "mime": ""}]
        out = contracts.validate("ticket", valid_ticket(attachments=atts))["attachments"]
        self.assertEqual([a["mime"] for a in out], ["image/png", "video/mp4", "image/jpeg", "application/pdf", ""])
        self.assertEqual([contracts.is_media(a) for a in out], [True, True, True, False, False])

    def test_duplicate_attachment_ids_are_rejected(self):
        atts = [{"id": "1", "name": "a.png"}, {"id": "1", "name": "b.png"}]
        self.assertIn("duplicate id", errors("ticket", valid_ticket(attachments=atts)))

    def test_comments_links_and_attachment_sizes_are_typed(self):
        msg = errors("ticket", valid_ticket(comments=[{"id": "1", "author": "a", "created_at": "yesterday", "body_md": ""}],
                                            links=[{"rel": "design"}], attachments=[{"id": "1", "name": "a", "size": "big"}]))
        self.assertIn("created_at: must be a UTC timestamp", msg)
        self.assertIn("links[0].target: required", msg)
        self.assertIn("size: must be an integer", msg)


class ChangeAndPipelineTests(unittest.TestCase):
    def test_valid_change_and_pipeline(self):
        contracts.validate("change", CHANGE, ["web"])
        contracts.validate("pipeline", PIPELINE)

    @lesson("L-156")
    def test_change_needs_a_boolean_draft_a_full_sha_and_a_known_repo(self):
        """L-156: draft is one boolean; SHAs are full; repo must be in factory.toml."""
        msg = errors("change", dict(CHANGE, draft="yes", head_sha="abc1234", state="WIP", repo="api"), ["web"])
        self.assertIn("data.draft: must be true or false", msg)
        self.assertIn("data.head_sha: must be a full lowercase SHA", msg)
        self.assertIn("data.state: 'WIP' is not one of open, merged, closed", msg)
        self.assertIn("data.repo: 'api' is not a repo", msg)

    def test_change_threads_have_a_fixed_shape(self):
        msg = errors("change", dict(CHANGE, threads=[{"id": "1", "author": "a", "resolved": None, "body_md": "x"}]), ["web"])
        self.assertIn("threads[0].resolved", msg)

    @lesson("L-156")
    def test_pipeline_status_enum_and_sha(self):
        """L-156: pipeline status is a closed enum and the SHA is full, so it can be matched to a pushed head."""
        msg = errors("pipeline", dict(PIPELINE, status="passed", sha="deadbeef"))
        self.assertIn("data.status: 'passed' is not one of pending, running, success, failed, canceled, skipped", msg)
        self.assertIn("data.sha", msg)

    def test_change_and_pipeline_are_proof_grade_kinds(self):
        """(Gate use arrives with ship, M3.) Change and pipeline contracts are the ones that must come from an adapter or a user confirmation."""
        self.assertEqual(set(contracts.PROOF_GRADE), {"change", "pipeline"})
        self.assertNotIn("ticket", contracts.PROOF_GRADE)

    def test_unsupported_kinds_are_refused(self):
        self.assertIn("not supported in this build", errors("change_list", {}))


if __name__ == "__main__":
    unittest.main()
