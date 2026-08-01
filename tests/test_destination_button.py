"""Tests for _find_destination_button / _has_destination_button_text.

These helpers locate the action button on a world-map destination panel.
On the Port tab it's labelled "Go to City"; on the Explore tab (village
selected) it's "Move to Village".  Both forms must be matched, both as
single merged OCR tokens and as split verb+noun pairs on the same row.
"""
import unittest

from actions.sail_actions import (
    _find_destination_button,
    _has_destination_button_text,
)


def _tok(text, conf, cx, cy):
    """Token tuple in the (text, conf, cx, cy) shape _ocr_frame emits."""
    return (text, conf, cx, cy)


class FindDestinationButtonTests(unittest.TestCase):

    def test_merged_go_to_city(self):
        toks = [_tok("Go to City", 0.95, 1280, 1010)]
        pos = _find_destination_button(toks)
        self.assertEqual(pos, (1280, 1010))

    def test_merged_move_to_village(self):
        toks = [_tok("Move to Village", 0.95, 1290, 1010)]
        pos = _find_destination_button(toks)
        self.assertEqual(pos, (1290, 1010))

    def test_merged_move_to_city(self):
        """Some city panels also use the 'Move to' verb."""
        toks = [_tok("Move to City", 0.95, 1280, 1010)]
        pos = _find_destination_button(toks)
        self.assertEqual(pos, (1280, 1010))

    def test_split_go_to_plus_city(self):
        # The live world-map case: OCR returns the verb and noun as
        # separate tokens on the same row.
        toks = [
            _tok("Go to", 1.00, 1228, 1014),
            _tok("City",  0.80, 1300, 1015),
        ]
        pos = _find_destination_button(toks)
        # Centroid of the two tokens.
        self.assertEqual(pos, ((1228 + 1300) // 2, (1014 + 1015) // 2))

    def test_split_move_to_plus_village(self):
        toks = [
            _tok("Move to", 1.00, 1228, 1014),
            _tok("Village", 0.85, 1340, 1015),
        ]
        pos = _find_destination_button(toks)
        self.assertEqual(pos, ((1228 + 1340) // 2, (1014 + 1015) // 2))

    def test_stray_noun_elsewhere_does_not_match(self):
        # A "City" token in the top toolbar shouldn't pair with the
        # bottom-row "Go to" verb when they're > 100 px apart in y.
        toks = [
            _tok("Go to", 1.00, 1228, 1014),
            _tok("City",  0.50, 2060, 141),   # far away — toolbar
        ]
        pos = _find_destination_button(toks)
        self.assertIsNone(pos)

    def test_split_picks_nearest_noun_on_row(self):
        # Two 'city' tokens on the same row → pick the one closest in x.
        toks = [
            _tok("Go to", 1.00, 1228, 1014),
            _tok("city",  0.50, 2060, 1015),   # far right
            _tok("City",  0.80, 1300, 1015),   # adjacent
        ]
        pos = _find_destination_button(toks)
        # Adjacent token at (1300, 1015) wins.
        self.assertEqual(pos, ((1228 + 1300) // 2, (1014 + 1015) // 2))

    def test_no_button_returns_none(self):
        toks = [
            _tok("World Map", 0.95, 200, 50),
            _tok("Lisbon",    0.95, 1500, 600),
        ]
        self.assertIsNone(_find_destination_button(toks))

    def test_panel_text_above_button_band_is_rejected(self):
        """Regression: the bot read 'Move to Village' from inside the
        right-side Village Info panel at (2084, 596) and tapped there,
        which did nothing.  The real button sits at the bottom centre
        (y ≈ 970+).  Tokens with y < min_y must not match.
        """
        toks = [
            _tok("Move to Village", 0.85, 2084, 596),  # in info panel
        ]
        self.assertIsNone(_find_destination_button(toks))

    def test_real_button_below_panel_text_still_matched(self):
        """If both a panel-text mention AND the real bottom button
        appear in OCR, the bottom button wins."""
        toks = [
            _tok("Move to Village", 0.70, 2084, 596),   # panel text
            _tok("Move to Village", 0.95, 1280, 975),   # real button
        ]
        pos = _find_destination_button(toks)
        self.assertEqual(pos, (1280, 975))

    def test_split_pair_above_button_band_is_rejected(self):
        # Verb + noun on same row but well above the action-button band.
        toks = [
            _tok("Move to", 1.00, 1700, 400),
            _tok("Village", 0.85, 1800, 410),
        ]
        self.assertIsNone(_find_destination_button(toks))

    def test_verb_on_button_does_not_pair_with_label_noun_above(self):
        """Regression: live OCR returned 'Move to' at (1210, 1014) — the
        actual button — and 'Village' at (1313, 950) — the 'Berber
        Village' label above the button.  At the old 100-px row
        tolerance these paired into an averaged centroid (1261, 982)
        which missed the button.  At the tightened 25-px tolerance,
        Δy=64 no longer pairs and the function returns None (caller
        polls again on the next frame, where OCR usually returns the
        merged 'Move to Village' token).
        """
        toks = [
            _tok("Move to",        0.99, 1210, 1014),  # on the button
            _tok("Berber Village", 0.97, 1200,  950),  # label above
            _tok("Village",        1.00, 1313,  950),  # split of the label
        ]
        self.assertIsNone(_find_destination_button(toks))


class HasDestinationButtonTextTests(unittest.TestCase):

    def test_go_to_city_detected(self):
        toks = [_tok("Go to City", 0.95, 1280, 1010)]
        self.assertTrue(_has_destination_button_text(toks))

    def test_move_to_village_detected(self):
        toks = [_tok("Move to Village", 0.95, 1290, 1010)]
        self.assertTrue(_has_destination_button_text(toks))

    def test_split_tokens_form_full_phrase(self):
        # Tokens joined for the lightweight presence check.
        toks = [
            _tok("Move to", 1.00, 1228, 1014),
            _tok("Village", 0.85, 1340, 1015),
        ]
        self.assertTrue(_has_destination_button_text(toks))

    def test_bare_move_marker_not_detected(self):
        """Sea-waypoint shows just 'Move' with no destination noun —
        this MUST NOT be classified as a destination button."""
        toks = [_tok("Move", 0.95, 1200, 500)]
        self.assertFalse(_has_destination_button_text(toks))


class SelectWorldMapTabSmokeTest(unittest.TestCase):
    """High-level: select_world_map_tab is importable and rejects bad
    inputs without crashing.  The OmniParser-driven happy path requires
    a real model; we cover it via mocking in an integration test below.
    """

    def test_unknown_tab_returns_false(self):
        from actions.sail_actions import select_world_map_tab
        self.assertFalse(select_world_map_tab("not-a-real-tab"))


if __name__ == "__main__":
    unittest.main()
