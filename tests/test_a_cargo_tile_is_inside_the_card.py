"""A numeric box below the Cargo header is not thereby a cargo tile.

`read_overflow` filtered candidates by Y alone, so anything numeric further down the screen
qualified — including the Barter screen's Trade Count chip showing behind the card, out in
the left gutter. Probing it tapped OUTSIDE the dialog, which DISMISSED it, and every action
after that ran against a screen that no longer had one: the discards read nothing and logged
"cancelling" with no dialog to cancel, and Receive tapped a remembered coordinate on bare
screen.

Live 2026-09-10 at San Village, five overflows in a row, 235 units of Bambara Groundnut lost.

The geometry below is MEASURED off frame_0370.png of
`data/sessions/trace_barter_cmd_2026-09-10T10-10-07` — the card's own title and headers, its
five real tiles, and the chip at x[399,506] that broke it. The dialog spans x[559,1841]; the
chip's centre is at 452, 107px clear of the left edge.

What the dialog COVERS cannot be detected, so everything foreign comes from a gutter beside
it — which is why one horizontal bound is the whole fix. Related: the memories
`dialogs-are-centered-panels-are-not-left`, `tapping-outside-a-dialog`, `a-box-bigger-than-its-thing`.
"""

from __future__ import annotations

import types
import unittest

from actions.overflow_dialog import read_overflow


def _el(label, x1, x2, y1, y2, kind="button"):
    return types.SimpleNamespace(label=label, element_type=kind,
                                 x1=x1, x2=x2, y1=y1, y2=y2,
                                 cx=(x1 + x2) // 2, cy=(y1 + y2) // 2)


# Frame 370, element for element.
TITLE = _el("Insufficient Empty Space", 1011, 1389, 115, 157, kind="text")
PENDING_HDR = _el("Received Trade Goods", 561, 879, 291, 327, kind="text")
CARGO_HDR = _el("Cargo", 559, 650, 485, 527, kind="text")
USED = _el("4,952/4,952 (100%)", 1590, 1836, 487, 525, kind="text")
PENDING_TILE = _el("47", 553, 694, 335, 475)
WATER = _el("195", 557, 688, 524, 650)
FOOD = _el("195", 687, 816, 524, 649)
PIG = _el("883", 813, 944, 522, 650)
RAISIN = _el("762", 942, 1073, 522, 649)
OUTPUT = _el("2,917", 1070, 1203, 522, 649)
RECEIVE = _el("Receive", 1140, 1260, 900, 960, kind="text")

# The Barter screen behind the card: Trade Count slot 1, in the LEFT GUTTER.
TRADE_COUNT_CHIP = _el("741", 399, 506, 894, 1004)

CARD = [TITLE, PENDING_HDR, CARGO_HDR, USED, PENDING_TILE,
        WATER, FOOD, PIG, RAISIN, OUTPUT, RECEIVE]


class OnlyWhatIsInsideTheCardIsCargo(unittest.TestCase):

    def test_THE_CHIP_IN_THE_GUTTER_IS_NOT_A_TILE(self):
        st = read_overflow(CARD + [TRADE_COUNT_CHIP])
        self.assertEqual(sorted(t.qty for t in st.tiles), [195, 195, 762, 883, 2917])

    def test_the_five_real_tiles_all_survive(self):
        """A bound that also drops real tiles trades one silent loss for another."""
        st = read_overflow(CARD)
        self.assertEqual(sorted(t.qty for t in st.tiles), [195, 195, 762, 883, 2917])
        self.assertEqual(st.pending, 47)
        self.assertEqual((st.cargo_used, st.cargo_capacity), (4952, 4952))

    def test_the_right_gutter_is_excluded_too(self):
        """The dialog is CENTRED, so the far edge is the near one mirrored — both or neither."""
        far = _el("1,101", 1900, 2010, 894, 1004)
        st = read_overflow(CARD + [far])
        self.assertNotIn(1101, [t.qty for t in st.tiles])

    def test_a_tile_touching_the_edge_is_still_inside(self):
        """Tiles line up a few pixels outside the header text — measured 553 against 559 —
        so the test is the CENTRE, and no slack is needed to forgive the border."""
        st = read_overflow(CARD)
        self.assertIn(195, [t.qty for t in st.tiles])

    def test_WITHOUT_A_MEASURABLE_TITLE_NOTHING_IS_INVENTED(self):
        """No measurement, no edge — fall back to the old reading rather than guess.

        Asserted as it really behaves: with nothing to mirror about, the bound is not
        applied and the chip comes through exactly as it did before. That is the point.
        A card whose title cannot be located is not a licence to pick an edge, and a wrong
        edge would drop real tiles silently — the failure this bound exists to stop."""
        blind = [e for e in CARD if e is not TITLE]
        blind.append(_el("Insufficient Empty Space", 0, 0, 0, 0, kind="text"))
        st = read_overflow(blind + [TRADE_COUNT_CHIP])
        self.assertIn(741, [t.qty for t in st.tiles])


if __name__ == "__main__":
    unittest.main()
