"""Regression test for the no-right-panel sea-HUD override in the classifier.

The 2026-05-19 live-run bug: after departure, OCR read 'Safe' as port
name (from 'Safe Waters'), the right-panel template missed, and the
classifier committed to port_overworld with port='Safe'.  The bot then
looked for 'harbor' in the nearby-ports list and gave up.

The fix runs sea-HUD-token check FIRST in the no-right-panel branch.
This test verifies the helper-level logic that the fix relies on:

  - When OCR tokens contain sea-HUD strings ('days of sailing',
    'tail wind', 'wind speed', etc.) AND the port_name looks like a
    sea-zone misread, the classifier must return sea.
  - When tokens don't contain sea-HUD strings, normal port_overworld
    classification continues.

Full _classify_nav_state has too many dependencies to mock cleanly;
this test exercises the sea-HUD detection block directly.
"""
import unittest


class SeaHudTokenDetectionTests(unittest.TestCase):

    def test_safe_waters_phrase_matches_sea_hud(self):
        """'Safe Waters' in joined OCR text matches the sea_hud_tokens."""
        from actions.sail_actions import _SEA_HUD_TOKENS
        from utils.fuzzy import fuzzy_contains
        full = "safe waters day 3 of sailing tail wind 12 kt"
        matched = [kw for kw in _SEA_HUD_TOKENS if fuzzy_contains(full, kw)]
        self.assertGreater(len(matched), 0)
        # Should match at least one of the canonical sea phrases
        self.assertTrue(
            any(kw in {"safe waters", "days of sailing", "tail wind"}
                for kw in matched),
            f"expected ≥1 canonical sea-HUD match in {matched}",
        )

    def test_port_overworld_text_does_not_match_sea_hud(self):
        """A real port-overworld frame's OCR (port name, building
        labels, NPC text) should NOT match any sea-HUD token."""
        from actions.sail_actions import _SEA_HUD_TOKENS
        from utils.fuzzy import fuzzy_contains
        full = "amsterdam market harbor inn shipyard bank cathedral guild"
        matched = [kw for kw in _SEA_HUD_TOKENS if fuzzy_contains(full, kw)]
        self.assertEqual(matched, [],
                         f"unexpected sea-HUD matches on port text: {matched}")


if __name__ == "__main__":
    unittest.main()
