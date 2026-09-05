"""Selecting a barter good: aim below the lock banner, and never mistake a dead tap for an
answer about the village.

Live 2026-08-26 at Svear Village. The panel offered four tiles — Birch Tree (timber, Wares),
Näverslöjd (basket, Crafts), and two LOCKED ones carrying a red banner across the middle of
the thumbnail. The bot tapped them in the order 559, 829, 423, 693:

    559  selected Näverslöjd
    829  hit a locked tile's BANNER      -> an info tip appeared
    423  WAS Birch Tree                  -> the tip swallowed the tap
    693  hit the other banner

and the panel read `good=Naverslojd` all four times. Each identical reading was taken as
"this tile is not Birch Tree", so the mission concluded the good was not on offer and aborted
with 445 Iron, 146 Matchlock Gun and 438 Candle aboard — with the tile plainly on screen.

Two rules follow (user, 2026-08-26):

  * A locked tile IS still selectable. Only its banner is not — tapping the banner raises the
    tip instead of selecting. So aim low in the thumbnail rather than skipping the tile.
    (Bartering FOR a locked good needs the village gifted first; a later feature.)
  * Four different tiles cannot all read back as the same good. An unchanged panel means the
    tap did not land, and says nothing whatever about what the village offers.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import brain.barter_mission_live as bml
import actions.barter_panel as bp


class _Icon:
    element_type = "icon"
    def __init__(self, cx, y1=360, y2=487):
        self.cx, self.y1, self.y2 = cx, y1, y2
        self.cy = (y1 + y2) // 2
        self.label = ""


class _Label:
    element_type = "text"
    def __init__(self, cx, label, cy=552):
        self.cx, self.label, self.cy = cx, label, cy
        self.y1, self.y2 = cy - 12, cy + 12


class _Status(_Label):
    """The stock chip, one row above the category — 'Abundant', 'Excessive', anything."""
    def __init__(self, cx, label):
        super().__init__(cx, label, cy=509)


def _panel_elements():
    """The Svear strip: four columns at cx 424/560/693/828."""
    out = []
    for cx, cat in ((424, "Wares"), (560, "Crafts"), (693, "Spices"), (828, "Wares")):
        out.append(_Icon(cx))
        out.append(_Label(cx, cat))
        out.append(_Label(cx, "Abundant", cy=509))
    return out


class TheTapAvoidsTheBanner(unittest.TestCase):

    def setUp(self):
        self.tiles = bp._tradable_tiles(_panel_elements())

    def test_all_four_tiles_are_found(self):
        self.assertEqual(len(self.tiles), 4)

    def test_the_tap_point_clears_the_banner(self):
        """Measured at Svear: the banner spans y≈408-440 on an icon spanning y≈360-487. The
        icon's CENTRE (424) is inside it — which is where the taps were landing."""
        for t in self.tiles:
            with self.subTest(cx=t["cx"]):
                self.assertGreater(t["tap_y"], 440, "tap must clear the banner's lower edge")
                self.assertLess(t["tap_y"], 487, "and stay inside the thumbnail")

    def test_the_old_centre_point_would_have_hit_it(self):
        """Documents the regression rather than asserting an arbitrary number."""
        for t in self.tiles:
            self.assertTrue(408 <= t["cy"] <= 440, "the icon centre sits within the banner")


class AnUnchangedPanelIsNotAnAnswer(unittest.TestCase):

    def _select(self, readings, want="Birch Tree"):
        """`readings[0]` is the baseline taken before any tap; the rest answer each tap."""
        seen = []
        reads = iter(readings)

        class _R:
            def __init__(self, good):
                self.selected_good = good

        with patch.object(bp, "_tradable_tiles", return_value=[
                {"cx": c, "cy": 424, "tap_y": 468, "category": k, "status": "Abundant"}
                for c, k in ((424, "Wares"), (560, "Crafts"), (693, "Spices"), (828, "Wares"))]), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("actions.ui.tap_at", side_effect=lambda x, y, why="": seen.append((x, y))), \
             patch("actions.barter_reader.read_barter_panel",
                   side_effect=lambda _f: _R(next(reads, None))), \
             patch.object(bp, "_panel_matches",
                          side_effect=lambda r, g, _rec: r.selected_good == g):
            ok = bp._select_trade_good(want)
        return ok, seen

    def test_the_svear_sequence_no_longer_reads_as_absence(self):
        """The exact failure: the panel never changes, whatever is tapped."""
        ok, seen = self._select(["Naverslojd"] * 9)
        # NOT FALSE — None. A pass that leaves tiles UNTESTED cannot say the good is absent,
        # so the answer is "the row is unread" (2026-08-30, Hutu Village: three tiles, the
        # first swallowed both its taps, and "none of the tiles read back as it" was reported
        # as "not on offer today" with the good sitting on the panel).
        self.assertIsNone(ok, "untested tiles forbid a verdict about the village")
        # Two attempts on each of the four tiles, and then — because the panel is warm by now,
        # other tiles having selected on it — a second pass over the ones that never answered.
        self.assertGreater(len(seen), 8, "it goes back for the tiles that never answered")

    def test_it_finds_the_good_when_a_tap_does_land(self):
        ok, _seen = self._select(["Naverslojd", "Meteorite", "Birch Tree"])
        self.assertTrue(ok)

    def test_a_dead_tap_does_not_consume_the_verdict(self):
        """A swallowed tap must not be recorded as 'that tile is not the good' — otherwise a
        tile that was never really tested is crossed off. Here tile 1's first tap is eaten and
        its retry reveals the wanted good."""
        ok, _seen = self._select(["Naverslojd", "Naverslojd", "Birch Tree"])
        self.assertTrue(ok, "the tile got its retry")


class TilesAreWalkedLeftToRight(unittest.TestCase):
    """"Tap from the first one, or the ones in the same category" (user, 2026-08-26).

    Built from OmniParser's element order the tiles came out arbitrary: at Svear the strip was
    walked 2nd, 4th, 1st, 3rd, so Birch Tree — the leftmost tile — was tried THIRD, after two
    banner taps had already raised the tip that ate it. Starting at the left would have found
    it immediately.
    """

    def test_the_strip_is_ordered_as_drawn(self):
        tiles = bp._tradable_tiles(list(reversed(_panel_elements())))
        self.assertEqual([t["cx"] for t in tiles], [424, 560, 693, 828])

    def test_a_matching_category_is_tried_first_without_losing_that_order(self):
        """The hint only re-orders; among equals the left-to-right order survives, because the
        sort is stable."""
        with patch.object(bp, "_category_hint", return_value="Wares"):
            tiles = bp._tradable_tiles(_panel_elements())
            want = bp._category_hint("Birch Tree")
            tiles.sort(key=lambda t: 0 if (want and t["category"] == want) else 1)
        self.assertEqual([t["cx"] for t in tiles], [424, 828, 560, 693])

    def test_a_good_with_no_hint_still_starts_at_the_left(self):
        """The Svear case: `_category_hint` returned None, the sort key collapsed to a
        constant, and the order was left entirely to chance."""
        tiles = bp._tradable_tiles(_panel_elements())
        want = None
        tiles.sort(key=lambda t: 0 if (want and t["category"] == want) else 1)
        self.assertEqual(tiles[0]["cx"], 424)


class TheBotTapsInsideTheStripAndNowhereElse(unittest.TestCase):
    """The detail panel carries labels at the same height as the category row — "Negotiate"
    among them. At Svear on 2026-08-26 one was paired with a nearby icon, accepted as a fifth
    goods tile, and tapped at (1959, 486).

    The strip identifies itself by its own SPACING: measured there the columns sat at cx 423 /
    558 / 693 / 829, gaps of 135, 135, 136, while "Negotiate" was 1130px further on. No
    absolute coordinate is involved (CLAUDE.md: find the element, do not write down where it
    is).
    """

    def test_a_detail_panel_label_is_not_a_tile(self):
        els = _panel_elements() + [_Icon(1962), _Label(1962, "Negotiate")]
        tiles = bp._tradable_tiles(els)
        self.assertEqual([t["cx"] for t in tiles], [424, 560, 693, 828])

    def test_several_strays_are_all_dropped(self):
        els = _panel_elements() + [_Icon(1906), _Label(1906, "Price Negotiation"),
                                   _Icon(1962), _Label(1962, "Negotiate")]
        tiles = bp._tradable_tiles(els)
        self.assertTrue(all(t["cx"] < 900 for t in tiles), [t["cx"] for t in tiles])

    def test_a_stray_on_the_LEFT_is_dropped_too(self):
        """The left menu ('Recruit Crew' at cx 177) sits at the same height as well."""
        els = _panel_elements() + [_Icon(177), _Label(177, "Recruit Crew")]
        tiles = bp._tradable_tiles(els)
        self.assertEqual([t["cx"] for t in tiles], [424, 560, 693, 828])

    def test_a_genuine_strip_of_any_width_survives(self):
        """Villages differ in how many goods they offer; the rule is regularity, not count."""
        els = []
        for i, cx in enumerate(range(424, 424 + 136 * 6, 136)):
            els += [_Icon(cx), _Label(cx, f"Cat{i}")]
        self.assertEqual(len(bp._tradable_tiles(els)), 6)

    def test_two_tiles_are_left_alone(self):
        """With fewer than three columns there is no pitch to measure, so nothing is dropped
        on the strength of a guess."""
        els = [_Icon(424), _Label(424, "Wares"), _Icon(560), _Label(560, "Crafts")]
        self.assertEqual(len(bp._tradable_tiles(els)), 2)


class ASwallowedTapDoesNotCrossOffTheTile(unittest.TestCase):
    """An info tip left over from a locked good's banner sits over the screen, and the next tap
    anywhere DISMISSES THE TIP rather than selecting (user, 2026-08-26).

    Live that day the tip was up, the bot tapped the Birch Tree tile, the tip vanished, and the
    selection did not change — and with no baseline reading the unchanged panel was recorded as
    "the Wares tile is Juniper Berry, not Birch Tree". The one tile that WAS the wanted good got
    crossed off without ever being tested.
    """

    def _select(self, panel_sequence, want="Birch Tree"):
        """`panel_sequence[0]` is the baseline read, before any tap."""
        taps = []
        reads = iter(panel_sequence)

        class _R:
            def __init__(self, good):
                self.selected_good = good

        with patch.object(bp, "_tradable_tiles", return_value=[
                {"cx": c, "cy": 424, "tap_y": 474, "category": k, "status": "Abundant"}
                for c, k in ((424, "Wares"), (560, "Crafts"))]), \
             patch("capture.adb_capture.capture_screen", return_value=object()), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("actions.ui.tap_at", side_effect=lambda x, y, why="": taps.append(x)), \
             patch("actions.barter_reader.read_barter_panel",
                   side_effect=lambda _f: _R(next(reads, None))), \
             patch.object(bp, "_panel_matches",
                          side_effect=lambda r, g, _rec: r.selected_good == g):
            ok = bp._select_trade_good(want)
        return ok, taps

    def test_the_svear_case_retries_the_tile_the_tip_ate(self):
        """baseline Juniper Berry; the first tap is eaten (still Juniper Berry); the retry
        lands and the tile turns out to BE Birch Tree."""
        ok, taps = self._select(["Juniper Berry", "Juniper Berry", "Birch Tree"])
        self.assertTrue(ok, "the tile must not be crossed off on a swallowed tap")
        self.assertEqual(taps, [424, 424], "it retries the SAME tile")

    def test_an_already_selected_good_needs_no_tapping_at_all(self):
        ok, taps = self._select(["Birch Tree"])
        self.assertTrue(ok)
        self.assertEqual(taps, [])

    def test_a_tile_that_never_selects_is_reported_as_untested(self):
        ok, taps = self._select(["Juniper Berry"] + ["Juniper Berry"] * 6)
        self.assertIsNone(ok, "both tiles went untested — that is unread, not absent")
        self.assertEqual(taps[:4], [424, 424, 560, 560],
                         "first pass: two attempts per tile, then move on")
        self.assertGreater(len(taps), 4,
                           "then a second pass over the untested tiles, on a warm panel")
        self.assertEqual(sorted(set(taps)), [424, 560], "and nowhere but those two tiles")


class TheRowDecidesTheTapHeight(unittest.TestCase):
    """Tiles share one vertical extent, and an individual icon box can come back short when
    something overlaps it.

    On the real Svear frame the info tip clipped tile 4's icon; deriving the tap point from
    that tile's OWN box put it at y=442 — back inside the banner (y≈420-460), which is the
    thing the whole change exists to avoid.
    """

    def test_a_clipped_icon_does_not_drag_its_tap_into_the_banner(self):
        els = []
        for cx, cat in ((424, "Wares"), (560, "Crafts"), (693, "Spices")):
            els += [_Icon(cx, y1=366, y2=514), _Label(cx, cat)]
        els += [_Icon(828, y1=366, y2=455), _Label(828, "Wares")]   # clipped by the info tip
        tiles = bp._tradable_tiles(els)
        for t in tiles:
            with self.subTest(cx=t["cx"]):
                self.assertGreater(t["tap_y"], 465, "clear of the banner's lower edge")

    def test_every_tile_in_a_row_taps_at_the_same_height(self):
        els = []
        for cx, cat in ((424, "Wares"), (560, "Crafts"), (693, "Spices"), (828, "Wares")):
            els += [_Icon(cx, y1=366, y2=514), _Label(cx, cat)]
        heights = {t["tap_y"] for t in bp._tradable_tiles(els)}
        self.assertEqual(len(heights), 1, f"the row should agree on one height: {heights}")


class TheBerberPanelOfTwoGoods(unittest.TestCase):
    """Two goods and a 'Negotiate' button — the panel that cost a three-port gather.

    Live 2026-09-05, both tiles present and correctly read:

        button cy=509 cx=423 'Excessive'     button cy=509 cx=557 'Abundant'
        button cy=552 cx=424 'Seasoning'     button cy=552 cx=558 'Spices'

    `_tradable_tiles` returned [('Spices','Abundant'), ('Negotiate','')] — it dropped Argan
    Oil and admitted a button 1,400px away in the detail panel, which the mission then tapped
    twice. Two independent faults, one per test class below.
    """

    def _els(self):
        return [_Icon(424), _Status(424, "Excessive"), _Label(424, "Seasoning"),
                _Icon(558), _Status(558, "Abundant"), _Label(558, "Spices"),
                _Icon(1960), _Label(1960, "Negotiate")]

    def test_both_goods_are_offered(self):
        self.assertEqual([t["category"] for t in bp._tradable_tiles(self._els())],
                         ["Seasoning", "Spices"])

    def test_the_negotiate_button_is_not_a_good(self):
        """It costs amity per attempt; it was tapped twice before the run was stopped."""
        self.assertNotIn("Negotiate",
                         [t["category"] for t in bp._tradable_tiles(self._els())])

    def test_a_pair_is_still_a_row(self):
        """`_strip_row` bailed below three tiles, so on a two-good panel the guard that
        exists to reject 'Negotiate' never ran at all."""
        tiles = bp._tradable_tiles(self._els())
        self.assertTrue(all(t["cx"] < 900 for t in tiles), [t["cx"] for t in tiles])


class AnUnknownStatusWordKeepsItsTile(unittest.TestCase):
    """The chip's WORD gates nothing (user, 2026-09-05: "we should not care about these words
    at all"). It was matched against a fixed vocabulary, and a tile whose chip said anything
    else was dropped whenever no icon was emitted for it — twice in two days: 'Recommended'
    on 09-04 (San's Medicine tile) and 'Excessive' on 09-05 (ARGAN OIL)."""

    def test_a_status_nobody_has_seen_before_still_yields_a_tile(self):
        els = [_Status(424, "Excessive"), _Label(424, "Seasoning"),
               _Status(558, "Wildly Overstocked"), _Label(558, "Spices")]
        self.assertEqual([t["category"] for t in bp._tradable_tiles(els)],
                         ["Seasoning", "Spices"])

    def test_the_word_still_comes_along_as_a_hint(self):
        els = [_Icon(424), _Status(424, "Excessive"), _Label(424, "Seasoning")]
        self.assertEqual(bp._tradable_tiles(els)[0]["status"], "Excessive")


class ThePitchIsWhicheverSpacingExplainsTheMostTiles(unittest.TestCase):
    """Neither the median nor the minimum gap describes the strip."""

    def test_a_single_far_stray_does_not_become_the_rule(self):
        """gaps [134, 1402] — the MEDIAN is 1402, which kept the stray and dropped a good."""
        els = [_Icon(424), _Label(424, "Seasoning"), _Icon(558), _Label(558, "Spices"),
               _Icon(1960), _Label(1960, "Negotiate")]
        self.assertEqual([t["cx"] for t in bp._tradable_tiles(els)], [424, 558])

    def test_two_close_strays_do_not_become_the_rule(self):
        """gaps include 56 between two strays — the MINIMUM would shatter a real strip."""
        els = []
        for cx in (424, 560, 696, 832):
            els += [_Icon(cx), _Label(cx, f"C{cx}")]
        els += [_Icon(1906), _Label(1906, "Price Negotiation"),
                _Icon(1962), _Label(1962, "Negotiate")]
        self.assertEqual([t["cx"] for t in bp._tradable_tiles(els)], [424, 560, 696, 832])


class TheStripIsBoundedByItsOwnBanner(unittest.TestCase):
    """user, 2026-09-05: "why is Negotiation ever considered? it is far from the tiles, the
    tiles are under the Tradeable Trade Goods banner, the negotiate button is in the right
    panel".

    Everything else here filters by HEIGHT alone, so any element at the right height anywhere
    across a 2,400px screen was a candidate. `_strip_row` rejects a stray by SPACING; this
    asks whether it is in the panel at all.
    """

    BANNER = type("_Banner", (), {"element_type": "text", "label": "Tradable Trade Goods",
                                  "cx": 524, "cy": 321, "x1": 365, "x2": 683,
                                  "y1": 303, "y2": 339})()

    def _strip(self, extra=()):
        els = [self.BANNER]
        for cx in (424, 560, 693, 828):
            els += [_Icon(cx), _Label(cx, f"C{cx}")]
        return bp._tradable_tiles(els + list(extra))

    def test_the_detail_panel_is_outside_the_strip(self):
        tiles = self._strip([_Icon(1960), _Label(1960, "Negotiate")])
        self.assertEqual([t["cx"] for t in tiles], [424, 560, 693, 828])

    def test_the_left_menu_is_outside_it_too(self):
        tiles = self._strip([_Icon(177), _Label(177, "Recruit Crew")])
        self.assertEqual([t["cx"] for t in tiles], [424, 560, 693, 828])

    def test_a_four_good_strip_fits_inside_the_allowance(self):
        """The banner's own box is narrower than the strip it heads — this bounds the PANEL,
        not the heading."""
        self.assertEqual(len(self._strip()), 4)

    def test_no_banner_drops_nothing(self):
        """An unreadable heading is not evidence about where the tiles are."""
        els = []
        for cx in (424, 560):
            els += [_Icon(cx), _Label(cx, f"C{cx}")]
        self.assertEqual(len(bp._tradable_tiles(els)), 2)

    def test_it_never_empties_the_strip(self):
        """If the bound would remove everything, the read is wrong and the tiles stand."""
        els = [self.BANNER, _Icon(1960), _Label(1960, "Negotiate")]
        self.assertEqual(len(bp._tradable_tiles(els)), 1)
