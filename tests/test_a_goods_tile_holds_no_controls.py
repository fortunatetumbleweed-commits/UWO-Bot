"""Goods cards are MEASURED from the frame, and nothing inside one is a control.

User, 2026-09-10: *"there are no buttons in the good tiles"* — said positionally here, which
is the durable form. A list of banner words catches `Specialties` and misses the next banner;
a card's own boundary catches every gold thing painted on one, because they are all LABELS
and a tap on any of them is a tap on the tile.

The geometry and colours below are MEASURED off the live frames of 2026-09-10 (Lisboa sell,
San sell, Madeira purchase); the frames themselves are not in the repo (`data/sessions/` is
gitignored), so the fixtures reproduce them.

    card body      rgb(216, 206, 196)     identical at Lisboa and San
    background     rgb(38, 36, 38)
    card           431 x 234, pitch ~437 across and ~242 down

Why cream-absence rather than darkness is the gap test — both were tried on the real frames:

    a true gap, between cards or rows     cream fraction  EXACTLY 0.000
    worst row INSIDE a card                               0.044
    a thumbnail column                                    0.165 - 0.318

Darkness cannot separate those: an inter-card gap is a ~4px shadow reaching only 54 in some
rows, while a card's own furniture reaches 88. Every single luminance cut either split a card
in two or merged two cards into one, and both were observed while building this.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

import numpy as np
from PIL import Image

from vision.region_detectors.goods_tiles import detect_goods_tiles, tile_containing

CREAM = (216, 206, 196)
DARK = (38, 36, 38)
W, H = 2400, 1080
CARD_W, CARD_H = 431, 234
X0, Y0, PITCH_X, PITCH_Y = 358, 198, 437, 242


# A SOLD-OUT tile is the same cream at half the light — measured rgb(109,104,98) at Ambon,
# the game's ~1.98 dim factor. Matched by hue, since dimming scales the channels together.
DIM = (109, 104, 98)


def _page(n_cards: int, *, rows: int = 1, furniture: bool = False,
          side_panel: bool = False, dim_last: bool = False) -> Image.Image:
    """A market page: dark ground, cream cards on the measured pitch."""
    a = np.full((H, W, 3), DARK, dtype=np.uint8)
    placed = 0
    for r in range(rows):
        for c in range(3):
            if placed >= n_cards:
                break
            x, y = X0 + c * PITCH_X, Y0 + r * PITCH_Y
            a[y:y + CARD_H, x:x + CARD_W] = (DIM if (dim_last and placed == n_cards - 1)
                                             else CREAM)
            # A thumbnail: dark-ish, but an IMAGE, and only part of the card's height.
            a[y + 10:y + 140, x + 10:x + 140] = (120, 110, 100)
            if furniture and placed == n_cards - 1:
                # The conditional card's own dark band, measured at luminance 88 — the thing
                # a darkness test mistook for a gap and split the card on. INSET, as the real
                # one is: it leaves a cream margin, which is why its worst row still reads
                # 0.044 cream and not zero. A band spanning the card edge to edge WOULD split
                # it, and that limit is recorded in the module rather than papered over.
                a[y + 180:y + 200, x + 8:x + CARD_W - 8] = (88, 88, 88)
            placed += 1
    if side_panel:
        # The cream right-hand cargo panel: TALL, so the landscape test excludes it.
        a[171:1038, 1682:2239] = CREAM
    return Image.fromarray(a)


class TheCardsAreMeasuredFromTheFrame(unittest.TestCase):

    def test_one_card(self):
        tiles = detect_goods_tiles(_page(1))
        self.assertEqual(len(tiles), 1)
        self.assertEqual((tiles[0].w, tiles[0].h), (CARD_W, CARD_H))
        self.assertEqual((tiles[0].x1, tiles[0].y1), (X0, Y0))

    def test_A_ROW_OF_TOUCHING_CARDS_IS_SEPARATED(self):
        tiles = detect_goods_tiles(_page(3))
        self.assertEqual(len(tiles), 3)
        self.assertEqual([t.w for t in tiles], [CARD_W] * 3)

    def test_a_full_grid(self):
        tiles = detect_goods_tiles(_page(6, rows=2))
        self.assertEqual(len(tiles), 6)

    def test_A_RAGGED_LAST_ROW(self):
        """The Madeira case: 7 goods, so the final row holds one card and the rest of its
        band is background. A profile taken across the whole page reads that as a gap."""
        tiles = detect_goods_tiles(_page(7, rows=3))
        self.assertEqual(len(tiles), 7)
        self.assertEqual([t.h for t in tiles], [CARD_H] * 7)

    def test_A_CARDS_OWN_DARK_BAND_DOES_NOT_SPLIT_IT(self):
        """The conditional Shea Butter card. Split in two by every luminance threshold that
        was also low enough to see the inter-card shadow."""
        tiles = detect_goods_tiles(_page(7, rows=3, furniture=True))
        self.assertEqual(len(tiles), 7)
        self.assertEqual([t.h for t in tiles], [CARD_H] * 7)

    def test_the_tall_side_panel_is_not_a_card(self):
        tiles = detect_goods_tiles(_page(3, side_panel=True))
        self.assertEqual(len(tiles), 3)
        self.assertTrue(all(t.w > t.h for t in tiles))

    def test_A_SOLD_OUT_TILE_IS_STILL_A_TILE(self):
        """The game dims a sold-out card. A fixed colour missed it and the page came back a
        good short — `Ebony` was simply absent at Ambon, so nothing could call it sold out."""
        tiles = detect_goods_tiles(_page(3, dim_last=True))
        self.assertEqual(len(tiles), 3)
        self.assertEqual([t.w for t in tiles], [CARD_W] * 3)

    def test_the_background_is_not_a_dim_card(self):
        """Dim cream is WARM (b < r); the ground is neutral (b == r). That is what the ratio
        test separates, and why brightness alone could not."""
        a = np.full((H, W, 3), (56, 54, 56), dtype=np.uint8)   # the lighter background
        self.assertEqual(detect_goods_tiles(Image.fromarray(a)), [])

    def test_an_empty_page_yields_nothing(self):
        self.assertEqual(detect_goods_tiles(Image.new("RGB", (W, H), DARK)), [])

    def test_A_DIALOG_IS_NOT_A_CARD(self):
        """A dialog is cream, landscape and centred in the content area — everything a card
        is. Only its SCALE tells them apart, and the grid being three columns is what makes
        that a structural statement. Caught by the existing suite: calling the overflow card
        a tile made the detector refuse its own `Receive` button."""
        a = np.full((H, W, 3), DARK, dtype=np.uint8)
        a[161:972, 543:1857] = CREAM              # the measured overflow card, 1314x811
        self.assertEqual(detect_goods_tiles(Image.fromarray(a)), [])


class NothingInsideACardIsAControl(unittest.TestCase):

    def test_a_point_on_the_card_is_inside_it(self):
        tiles = detect_goods_tiles(_page(1))
        # Where the Specialties banner sat on the live Lisboa card.
        self.assertIsNotNone(tile_containing(tiles, 571, 355))

    def test_the_commit_strip_is_not(self):
        tiles = detect_goods_tiles(_page(3))
        self.assertIsNone(tile_containing(tiles, 2100, 995))

    def test_THE_DETECTOR_DROPS_A_BANNER_WITHOUT_THE_WORD_LIST(self):
        """The positional rule must stand ALONE — that is the whole point of building it.

        The word list is disabled here, so only the card's boundary can reject the banner."""
        import vision.region_detectors.commit_button as cb
        banner = types.SimpleNamespace(label="Specialties", element_type="button",
                                       x1=432, y1=280, x2=780, y2=330, cx=606, cy=305)
        page = _page(1)
        with mock.patch.object(cb, "_TILE_BANNERS", frozenset()), \
             mock.patch.object(cb, "read_text", return_value="Specialties"), \
             mock.patch.object(cb, "looks_like_commit_button", return_value=True), \
             mock.patch.object(cb, "yellow_fraction", return_value=0.9):
            self.assertEqual(cb.detect_commit_buttons([banner], page), [])

    def test_a_real_commit_button_outside_the_cards_survives(self):
        import vision.region_detectors.commit_button as cb
        sell = types.SimpleNamespace(label="Sell", element_type="button",
                                     x1=1900, y1=960, x2=2300, y2=1035, cx=2100, cy=997)
        page = _page(3)
        with mock.patch.object(cb, "read_text", return_value="Sell"), \
             mock.patch.object(cb, "looks_like_commit_button", return_value=True), \
             mock.patch.object(cb, "yellow_fraction", return_value=0.9):
            self.assertEqual([c.verb for c in cb.detect_commit_buttons([sell], page)], ["Sell"])


if __name__ == "__main__":
    unittest.main()
