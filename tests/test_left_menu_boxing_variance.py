"""The left menu must survive OmniParser boxing the SAME rows differently.

Live at San Village 2026-08-24, two captures seconds apart:

    capture A: Explore(full-row box) + 4 tight text rows      → menu read fine
    capture B: Explore/Gifting/Loot(full-row) + 2 tight rows   → menu read as []

Identical labels, identical positions — only the box widths differed. In B the median
member was full-width, so `_is_reward_cluster` wrote the real menu off as a reward/info
box and the detector returned a stray map label instead. The run reported "no 'Barter'
item in the left menu" while Explore/Gifting/Loot/Recruit Crew/Barter were plainly on
screen, and abandoned the barter with the fleet standing at the village holding both
materials.

The fix is ASSOCIATION, not dimension (user, 2026-08-24): a cluster is the menu because it
NAMES MENU COMMANDS, and a reward widget because it names rewards. What the rows SAY did not
change between those two captures, and never does — sizes did. Geometry was the wrong kind of
evidence for an identity question.
"""

from __future__ import annotations

import types
import unittest

from vision.region_detectors.left_menu import detect_left_menu

W, H = 2400, 1080


def _el(label, x1, cy, width, etype="text", h=40):
    return types.SimpleNamespace(label=label, element_type=etype, cx=x1 + width // 2, cy=cy,
                                 x1=x1, y1=cy - h // 2, x2=x1 + width, y2=cy + h // 2)


# The five San Village rows, at their measured positions.
_ROWS = [("Explore", 160), ("Gifting", 278), ("Loot", 391),
         ("Recruit Crew", 503), ("Barter", 616)]


def _menu(full_row_count):
    """The same menu with the first `full_row_count` rows boxed full-width."""
    out = []
    for i, (label, cy) in enumerate(_ROWS):
        if i < full_row_count:
            out.append(_el(label, 0, cy, 360, etype="button", h=100))
        else:
            out.append(_el(label, 31, cy, len(label) * 17))
    return out


class BoxingVarianceMustNotHideTheMenu(unittest.TestCase):

    def test_every_mix_of_box_widths_reads_all_five_rows(self):
        for n in range(len(_ROWS) + 1):
            with self.subTest(full_row_boxes=n):
                menu = detect_left_menu(_menu(n), W, H)
                self.assertIsNotNone(menu)
                self.assertEqual(menu.labels(),
                                 ["Explore", "Gifting", "Loot", "Recruit Crew", "Barter"])

    def test_the_live_failing_case(self):
        """Capture B verbatim: three full-row boxes, two tight rows, plus a map label."""
        els = _menu(3) + [_el("Sa", 313, 686, 48)]
        menu = detect_left_menu(els, W, H)
        self.assertIn("Barter", menu.labels())

    def test_a_stray_label_alone_is_not_a_menu(self):
        """With no left-anchored column, a lone map label must not become the menu."""
        menu = detect_left_menu([_el("Sa", 313, 686, 48)], W, H)
        self.assertEqual(menu.labels() if menu else [], [])

    def test_reward_widgets_are_rejected_by_what_they_SAY(self):
        """Named reward boxes lose to the menu even when boxed identically to it — the
        Village Info page really does carry 'Achievement Reward' and 'Weekly Reward'."""
        rewards = [_el("Achievement Reward", 31, 300, 360, etype="button", h=90),
                   _el("Weekly Reward", 31, 420, 360, etype="button", h=90),
                   _el("Discoveries", 31, 540, 360, etype="button", h=90)]
        menu = detect_left_menu(_menu(3) + rewards, W, H)
        self.assertIn("Barter", menu.labels())
        self.assertNotIn("Weekly Reward", menu.labels())

    def test_a_market_menu_is_recognised_by_its_own_vocabulary(self):
        """Same rule on a different screen: Purchase/Sell/Trade Info name the market menu."""
        rows = [_el("Purchase", 0, 160, 360, etype="button", h=100),
                _el("Sell", 31, 275, 68),
                _el("Trade Points", 31, 390, 200),
                _el("Trade Info", 31, 505, 180)]
        self.assertIn("Sell", detect_left_menu(rows, W, H).labels())


if __name__ == "__main__":
    unittest.main()
