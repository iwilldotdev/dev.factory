"""Neutral delivery keys of workspace.json (ship.*, repos[].scm_project, epics[].ticket_transition) are validated."""

import unittest

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from factory.commands.workspace import ship_config_errors


class ShipConfigTests(unittest.TestCase):
    def errs(self, **ws):
        return ship_config_errors(ws)

    def test_neutral_config_is_valid(self):
        ws = {"ship": {"targets": ["pack"], "draft_default": True, "title_format": "[{key}] - {summary}",
                       "ticket_mode": "comment+transition", "ticket_comment": "change-links", "ticket_transition": "In review",
                       "reviewers": [{"scm_user": "ana", "ticket_user": "u-1"}, "carl"], "pipeline_watch": True},
              "scm_user": "me", "repos": [{"alias": "api", "scm_project": "team/api"}],
              "epics": [{"key": "E-1", "ticket_transition": None}]}
        self.assertEqual(ship_config_errors(ws), [])

    def test_unknown_ship_key_names_the_known_ones(self):
        (err,) = self.errs(ship={"nope": 1})
        self.assertIn("ship.nope: unknown key", err)
        self.assertIn("ticket_mode", err)

    def test_modes_and_comment_kind(self):
        self.assertIn("ship.ticket_mode must be one of", self.errs(ship={"ticket_mode": "all"})[0])
        self.assertIn("ship.ticket_comment must be", self.errs(ship={"ticket_comment": "links"})[0])
        self.assertEqual(self.errs(ship={"ticket_mode": "none"}), [])

    def test_reviewer_needs_scm_user(self):
        (err,) = self.errs(ship={"reviewers": [{"ticket_user": "u-1"}]})
        self.assertIn("ship.reviewers[0] needs scm_user", err)
        self.assertIn("unknown key", self.errs(ship={"reviewers": [{"scm_user": "a", "x": 1}]})[0])

    def test_title_format_placeholders(self):
        (err,) = self.errs(ship={"title_format": "{key} {area}"})
        self.assertIn("{area}", err)

    def test_types(self):
        self.assertIn("ship.pipeline_watch must be true or false", self.errs(ship={"pipeline_watch": "yes"})[0])
        self.assertIn("ticket_transition must be a status name", self.errs(epics=[{"ticket_transition": 3}])[0])
        self.assertIn("scm_project must be a string", self.errs(repos=[{"alias": "a", "scm_project": 5}])[0])


if __name__ == "__main__":
    unittest.main()
