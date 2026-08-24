"""The buy loop's "own N" stop condition depends on reading the RIGHT cargo tile.

Live 2026-08-21 at Jakarta: the goal was 350 Ebony and the bot bought 1,681 before being
stopped by hand — ~4.8x over, at 3.56M ducats, filling the hold to 93% with one material.

Two independent defects, both reproduced here from the real frames of that run
(data/sessions/trace_barter_cmd_2026-08-21T22-04-46):

  1. `_cargo_tiles` required x1 > 1780. The strip fills RIGHT-TO-LEFT and the good being
     bought always takes the leftmost owned slot, so which pixel that is depends on how
     many distinct goods are in the hold:

         Malé,    2 owned goods → Coral at x≈1960  (visible, tracked 56→170→284→455)
         Jakarta, 4 owned goods → Ebony at x≈1680  (HIDDEN, 1→141→281→421 never seen)

     Coral was therefore not handled correctly, only LUCKILY — the cutoff happened to fall
     between the 3-slot and 4-slot layouts. Jakarta reached four goods because Ebony was
     already in the hold before the first purchase there. With the tile invisible, before
     and after multisets matched → no leftover → owned=None → bought_total never rose →
     the stop condition could not fire.

  2. When several tiles change at once, the tie-break was NEAREST-TO-TRACKED. But the strip
     REFLOWS: a newly acquired good is inserted at the left and pushes the rest right. Ebony
     moved 1688 → 1825 while a different tile (140) took its old slot, so position picked
     the wrong good and the owned count went BACKWARDS.
"""
from actions.buy_materials import _cargo_tiles, track_bought_good


class _El:
    def __init__(self, x1, n, y1=264):
        self.element_type = "button"
        self.label = str(n)
        self.x1, self.y1 = x1, y1
        self.x2, self.y2 = x1 + 120, y1 + 100


# Real element layouts, transcribed from the frames named in each constant.
# Goods-grid numbers sit far left; the cargo strip is the evenly-pitched right-hand run.
F0020 = [_El(450, 96), _El(1687, 141), _El(1824, 797), _El(1963, 920), _El(2100, 217)]
F0030 = [_El(450, 96), _El(874, 140), _El(1312, 248), _El(1688, 281), _El(1826, 797),
         _El(1964, 920), _El(2101, 217)]
F0037 = [_El(450, 96), _El(1312, 248), _El(1688, 140), _El(1825, 421), _El(1964, 797),
         _El(2100, 920)]


def _counts(elements):
    return sorted(n for _pos, n in _cargo_tiles(None, elements))


class TestCargoTilesIncludesLeftmostSlot:
    def test_leftmost_slot_is_read(self):
        """x1≈1688 — below the old x1>1780 cutoff, and where the bought good lives."""
        assert 141 in _counts(F0020)
        assert 281 in _counts(F0030)
        assert 421 in _counts(F0037)

    def test_goods_grid_numbers_are_excluded(self):
        """The grid sits far left with a large gap; only the pitched run is cargo."""
        c = _counts(F0030)
        assert 96 not in c and 248 not in c
        assert 140 not in c          # grid entry at x=874, not the strip

    def test_no_numbers_yields_no_tiles(self):
        assert _cargo_tiles(None, []) == []


class TestTrackerFollowsTheGoodThroughReflow:
    def test_reads_the_growing_count(self):
        owned, _pos = track_bought_good(_cargo_tiles(None, F0020),
                                        _cargo_tiles(None, F0030), None)
        assert owned == 281

    def test_follows_the_good_when_the_strip_reflows(self):
        """Ebony 281 → 421 moved 1688 → 1825 while tile 140 took its old slot.
        Nearest-to-tracked picks 140 and the count goes backwards; growth picks 421."""
        before = _cargo_tiles(None, F0030)
        after = _cargo_tiles(None, F0037)
        tracked = next(pos for pos, n in before if n == 281)
        owned, _pos = track_bought_good(before, after, tracked, last_owned=281)
        assert owned == 421

    def test_without_the_growth_hint_position_still_misleads(self):
        """Documents WHY last_owned exists — this is the old behaviour."""
        before = _cargo_tiles(None, F0030)
        after = _cargo_tiles(None, F0037)
        tracked = next(pos for pos, n in before if n == 281)
        owned, _pos = track_bought_good(before, after, tracked)
        assert owned == 140          # the wrong good, i.e. a count that went backwards

    def test_the_goal_is_reached_on_real_frames(self):
        """End to end: with both fixes, 350 Ebony is met at 421 instead of running to the
        round cap and buying 1,681."""
        tracked, bought = None, 0
        for a, b in ((F0020, F0030), (F0030, F0037)):
            owned, tracked = track_bought_good(_cargo_tiles(None, a), _cargo_tiles(None, b),
                                               tracked, last_owned=bought or None)
            if owned is not None:
                bought = max(bought, owned)
        assert bought >= 350


class TestCartStagedTilesAreNotOwned:
    """A tile is OWNED (purchased, drawn grayed) or STAGED IN THE CART (drawn bright).

    While a purchase is pending the panel shows the SAME good twice: the staged quantity on
    a bright tile carrying a red ✕ that REMOVES it, and the already-purchased amount on a
    grayed tile. Live frame_0037: Ebony 140 (cart) sat beside Ebony 421 (owned). Counting
    the cart tile as owned makes the tracker follow it and report a count that goes
    BACKWARDS, 421 → 140.

    The graying is the tile's STATE, so it is the primary test; the ✕ is only a control that
    happens to sit on staged tiles. Brightness measured on frame_0037 (tile body, excluding
    the badge corner and count strip):

        Ebony 140 CART  141        <- same good, twice as bright
        Ebony 421 owned  70
        Textiles  owned  87
        Coral     owned  49
    """

    OWNED_FILL = (70, 62, 31)     # measured: grayed Ebony, both 281 and 421
    CART_FILL = (140, 121, 58)    # measured: staged Ebony 140

    def _tile_image(self, fill, with_badge: bool):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (2400, 1080), (40, 40, 40))
        d = ImageDraw.Draw(img)
        d.rectangle([1688, 264, 1808, 364], fill=fill)
        if with_badge:
            d.rectangle([1775, 268, 1805, 296], fill=(200, 45, 45))  # red ✕ remove control
        return img

    def _el(self):
        return _El(1688, 140)

    def test_a_bright_tile_is_staged_in_the_cart(self):
        from actions.buy_materials import _tile_in_cart
        assert _tile_in_cart(self._tile_image(self.CART_FILL, False), self._el())

    def test_a_grayed_tile_is_owned(self):
        from actions.buy_materials import _tile_in_cart
        assert not _tile_in_cart(self._tile_image(self.OWNED_FILL, False), self._el())

    def test_the_remove_badge_also_marks_a_cart_tile(self):
        """Corroborating signal, in case the graying is ever ambiguous."""
        from actions.buy_materials import _tile_in_cart
        assert _tile_in_cart(self._tile_image(self.OWNED_FILL, True), self._el())

    def test_cart_tiles_are_excluded_from_the_owned_counts(self):
        img = self._tile_image(self.CART_FILL, True)
        counts = sorted(n for _pos, n in _cargo_tiles(img, F0037))
        assert 140 not in counts, "the cart-staged quantity must not count as owned"
        assert 421 in counts

    def test_no_frame_means_no_cart_filtering_rather_than_a_crash(self):
        """Callers without a frame (unit fixtures) still get tiles, just unfiltered."""
        assert _cargo_tiles(None, F0037)


class TestSlotPositionDependsOnHoldContents:
    """The tracked tile's x depends on how many distinct goods are held — so an absolute
    cutoff fails only once the hold is full enough, which is the worst way to fail.

    Both layouts are transcribed from live frames. Under the old `x1 > 1780` rule the
    2-good layout tracked fine and the 4-good layout was blind; the reader must handle both.
    """

    # Malé: Coral being bought, only Coral + water held → leftmost owned slot is x≈1960.
    MALE_2_GOODS = [_El(1687, 305), _El(1963, 455), _El(2101, 305)]
    # Jakarta: Ebony being bought, four goods held → leftmost owned slot is x≈1688.
    JAKARTA_4_GOODS = [_El(1688, 281), _El(1826, 797), _El(1964, 920), _El(2101, 217)]

    def test_two_good_layout_is_read(self):
        assert 455 in [n for _p, n in _cargo_tiles(None, self.MALE_2_GOODS)]

    def test_four_good_layout_is_read_including_the_leftmost(self):
        """The case the old cutoff silently dropped."""
        counts = [n for _p, n in _cargo_tiles(None, self.JAKARTA_4_GOODS)]
        assert 281 in counts, "the good being bought sits leftmost and must be read"
        assert len(counts) == 4

    def test_the_old_cutoff_would_have_missed_it(self):
        """Documents the regression this guards: x1>1780 excludes the x≈1688 slot."""
        old_rule = [e for e in self.JAKARTA_4_GOODS if e.x1 > 1780]
        assert 281 not in [int(e.label) for e in old_rule]
