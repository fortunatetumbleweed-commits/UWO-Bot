# tests/test_one_list_scroller_serves_them_all.py
#
# ONE PLACE THAT KNOWS HOW TO PAGE A LIST (user, 2026-09-03).
#
# Three implementations of "swipe, re-read, stop when it stops moving" had grown, and the LIVE
# path was the one without it: `world_map._on_list` swiped and returned the fixed string
# "scrolled the list", so it could not tell a list that was paging from one that was not
# moving. Live 2026-09-03 it swiped five times at Faro and the stall guard stopped the mission
# for reporting the same thing three ticks running.
#
# What differed between the three was what they did with each page, not how to page — so the
# shared thing is the pager, and finding/collecting sit on top.

import unittest

from actions.ui.lists import DEFAULT_MAX_PAGES, find_in_list, pages, signature


class _Panel:
    """A fake scrollable list: `view` rows visible at a time out of `entries`."""

    def __init__(self, entries, view=4, start=0, moves=True):
        self.entries, self.view, self.top, self.moves = entries, view, start, moves
        self.swipes = []

    def rows(self, _frame=None):
        window = self.entries[self.top:self.top + self.view]
        # (label, x, y) — x is the list column, y descends the panel
        return [(name, 1900, 400 + 70 * i) for i, name in enumerate(window)]

    def swipe(self, x1, y1, x2, y2):
        self.swipes.append((x1, y1, x2, y2))
        if not self.moves:
            return
        down = y2 < y1                      # content moves up == paging down
        self.top = (min(self.top + self.view, max(0, len(self.entries) - self.view))
                    if down else max(0, self.top - self.view))

    def capture(self):
        return object()


def _match(rows, target):
    return next(((x, y) for name, x, y in rows if name == target), None)


def _find(panel, target, **kw):
    kw.setdefault("fallback_x", 1200)
    kw.setdefault("y", 700)
    return find_in_list(target, match=_match, read_rows=panel.rows,
                        capture=panel.capture, swipe=panel.swipe, **kw)


_TWELVE = [f"Port{i:02d}" for i in range(12)]


class TheSignatureTellsMovementFromStillness(unittest.TestCase):
    def test_the_same_page_has_the_same_signature(self):
        p = _Panel(_TWELVE)
        self.assertEqual(signature(p.rows()), signature(p.rows()))

    def test_a_different_page_does_not(self):
        p = _Panel(_TWELVE)
        before = signature(p.rows())
        p.swipe(1900, 850, 1900, 550)
        self.assertNotEqual(signature(p.rows()), before)

    def test_a_nudge_that_moves_nothing_reads_as_unchanged(self):
        """Labels alone would say "same"; y alone would say "different". Both, bucketed."""
        p = _Panel(_TWELVE)
        before = signature(p.rows())
        p.moves = False
        p.swipe(1900, 850, 1900, 550)
        self.assertEqual(signature(p.rows()), before)


class FindingSomethingBelowTheFold(unittest.TestCase):
    def test_an_entry_already_on_screen_is_found_without_swiping(self):
        p = _Panel(_TWELVE)
        self.assertIsNotNone(_find(p, "Port01"))
        self.assertEqual(p.swipes, [], "no swipe needed for what is already visible")

    def test_an_entry_far_down_is_paged_to(self):
        p = _Panel(_TWELVE)
        self.assertIsNotNone(_find(p, "Port10"))

    def test_an_entry_ABOVE_the_start_is_found_because_it_rewinds_first(self):
        """A list left part-scrolled hides entries above the viewport, and paging down would
        never reach them. This is why the rewind exists (live 2026-08-19, Jakarta)."""
        p = _Panel(_TWELVE, start=8)
        self.assertIsNotNone(_find(p, "Port00"))

    def test_something_absent_is_a_refusal_not_a_guess(self):
        p = _Panel(_TWELVE)
        self.assertIsNone(_find(p, "Nowhere"),
                          "None means paged the whole list; the caller must handle it")


class ADeadListIsNotPagedForEver(unittest.TestCase):
    def test_a_list_that_does_not_move_stops_early(self):
        """The failure the world map had no defence against: swiping at a list that is not
        scrolling. It must stop, not spend the whole budget."""
        p = _Panel(_TWELVE, moves=False)
        self.assertIsNone(_find(p, "Port10"))
        self.assertLess(len(p.swipes), 2 + DEFAULT_MAX_PAGES,
                        "an unchanged page means the end — stop asking")

    def test_the_budget_is_bounded_even_when_the_list_keeps_changing(self):
        """A list that never repeats must not be paged indefinitely."""
        p = _Panel([f"P{i:03d}" for i in range(500)])
        self.assertIsNone(_find(p, "absent", max_pages=3, rewind_pages=1))
        self.assertLessEqual(len(p.swipes), 1 + 3)


class TheSwipeGoesDownTheListsOwnColumn(unittest.TestCase):
    def test_it_uses_the_median_entry_x_not_the_fallback(self):
        """A region-centre swipe can miss the list entirely; the rewind then never scrolls,
        the signature is unchanged, "at the top" is assumed, and a clipped entry never comes
        back (live 2026-08-19, Jakarta)."""
        p = _Panel(_TWELVE)
        _find(p, "Port10")
        self.assertTrue(p.swipes)
        self.assertEqual({x for x, _, _, _ in p.swipes}, {1900},
                         "swiped at the fallback x instead of the list column")

    def test_the_fallback_is_used_only_when_there_are_no_rows(self):
        p = _Panel([])
        _find(p, "anything")
        self.assertTrue(all(x == 1200 for x, _, _, _ in p.swipes) or not p.swipes)


class PagesIsTheSharedThing(unittest.TestCase):
    def test_it_yields_the_page_already_on_screen_first(self):
        """`explore_port` collects every entry rather than finding one, so it needs the pages
        themselves — including the one visible before any swipe."""
        p = _Panel(_TWELVE)
        seen = []
        for rows in pages(read_rows=p.rows, capture=p.capture, swipe=p.swipe,
                          fallback_x=1200, y=700):
            seen.extend(name for name, _, _ in rows)
        self.assertIn("Port00", seen)
        self.assertIn("Port11", seen, "collecting must reach the bottom")


class AnyListInTheGameIsThisList(unittest.TestCase):
    """EVERY LIST FOLLOWS THE SAME PATTERN (user, 2026-09-03).

    The village list, and the fleet and task lists under the other tabs, are not wired up yet.
    Adding one must need no scrolling logic at all — only a row reader and a matcher. These
    drive the pager with panels of deliberately different shape and position to prove nothing
    in it is specific to the building list it grew up in."""

    def test_a_narrow_left_column_panel_pages_the_same(self):
        """The world map's rail sits on the LEFT (x~300); the building list on the right."""
        class _Left(_Panel):
            def rows(self, _frame=None):
                window = self.entries[self.top:self.top + self.view]
                return [(n, 300, 200 + 58 * i) for i, n in enumerate(window)]
        p = _Left([f"Village{i:02d}" for i in range(10)], view=3)
        self.assertIsNotNone(_find(p, "Village09", y=500))
        self.assertEqual({x for x, _, _, _ in p.swipes}, {300},
                         "swiped somewhere other than this panel's own column")

    def test_a_short_panel_uses_a_shorter_swipe(self):
        """A swipe longer than the panel overshoots whole pages. `reach_px` is per-call for
        exactly that reason, so a fleet or task panel is not stuck with the rail's travel."""
        p = _Panel([f"Ship{i}" for i in range(6)], view=2)
        _find(p, "Ship5", reach_px=40)
        travels = {abs(y2 - y1) for _, y1, _, y2 in p.swipes}
        self.assertEqual(travels, {80}, "reach_px was ignored")

    def test_two_rows_only_still_works(self):
        p = _Panel(["Task A", "Task B"], view=2)
        self.assertIsNotNone(_find(p, "Task B"))

    def test_an_empty_panel_is_a_refusal_not_a_crash(self):
        p = _Panel([], view=4)
        self.assertIsNone(_find(p, "anything"))
