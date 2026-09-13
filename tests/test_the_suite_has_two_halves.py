"""The suite splits into navigation and business, and the split is read off the imports.

WHY THERE IS A SPLIT (user, 2026-09-13): *"most of them do not overlap, and changing one does
not affect the other. The only difference is upstream, at the dispatcher level, and the sea
gauges, those are shared. If those are not touched, generally we only need to run the tests
for one group."*

WHY IT IS DERIVED RATHER THAN DECLARED: 517 files would each need a marker, and the one that
mattered would be the one nobody remembered to add. Classifying by the modules a file NAMES
means a test becomes a business test the day it starts touching the market.

WHY "BOTH" AND "NEITHER" ARE THE SAME ANSWER: a file that spans the halves must run in both,
and so must a file that names nothing — the vision primitives and the hygiene checks. Being
wrong in that direction costs a few seconds; being wrong the other way skips the test that
would have caught the regression.
"""
from __future__ import annotations

import unittest

from tests import groups


class AFileIsClassifiedByWhatItNames(unittest.TestCase):

    def test_the_navigation_loop(self):
        self.assertEqual(groups.classify("from brain.ai_nav.pipeline import AiNavPipeline"),
                         "navigation")

    def test_the_dispatcher_path(self):
        self.assertEqual(groups.classify("from brain.activities.market import MarketActivity"),
                         "business")

    def test_A_FILE_THAT_NAMES_BOTH_IS_SHARED(self):
        """It runs in every group. `-m \"not business\"` must not deselect it."""
        self.assertIsNone(groups.classify(
            "from brain.ai_nav.state import NavState\nfrom brain.dispatcher import Dispatcher"))

    def test_a_file_that_names_neither_is_shared(self):
        """The vision primitives and the hygiene checks have no side."""
        self.assertIsNone(groups.classify("from vision.ocr import read_text"))

    def test_a_patch_target_counts_as_naming_it(self):
        """A test that only ever reaches a module through `mock.patch` belongs to it just as
        much as one that imports it."""
        self.assertEqual(groups.classify('mock.patch("brain.market_ledger.MarketLedger")'),
                         "business")

    def test_SAIL_TO_BELONGS_TO_NEITHER(self):
        """Every goal in `brain/goals/` is sea control except this one, which is the sailing
        leg of a business mission — and `test_every_import_resolves` names it while being a
        hygiene check that must run in both groups."""
        self.assertIsNone(groups.classify("from brain.goals.sail_to import SailToGoal"))


class TheMarkersAreActuallyApplied(unittest.TestCase):
    """Read off a collected item rather than off `classify`, so a broken conftest hook fails
    here rather than silently running the whole suite under every filter."""

    def test_this_file_is_shared(self):
        import pathlib
        self.assertIsNone(groups.classify(pathlib.Path(__file__).read_text()))


class TheSharedModulesAreNamed(unittest.TestCase):

    def test_the_gauge_reader_is_shared(self):
        """`vision/sea_hud.py` is the file the 2026-09-12 regression happened in: made more
        careful for the business run, it doubled the navigation tick."""
        self.assertIn("vision/sea_hud.py", groups.SHARED_MODULES)

    def test_perceive_and_the_parser_are_shared(self):
        self.assertIn("brain/perceive.py", groups.SHARED_MODULES)
        self.assertIn("vision/omniparser.py", groups.SHARED_MODULES)


if __name__ == "__main__":
    unittest.main()
