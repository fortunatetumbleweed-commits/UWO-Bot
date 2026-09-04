# tests/test_the_search_box_is_not_the_port.py
#
# "IT IS SWIPING THE MAP TO FIND FARO WHEN IT IS RIGHT IN THE LIST" (user, 2026-09-03).
#
# Two independent defects put the destination out of reach while it sat on screen, and each
# one alone was enough. Both are the same rule read from different sides: NEVER MATCH YOUR OWN
# TYPING, and LOOK AT THE LIST WHEN THE KEYBOARD IS GONE.
#
#   1. `_find_port_on_world_map` matched the SEARCH BOX. The box holds the prefix the bot
#      typed, so it matches at conf 1.00 every single time and outscores the real row:
#
#          'faro'(1.00) @ (303,144)   the search box   <- what it returned
#          'Faro'(0.62) @ (209,200)   the list row     <- what was wanted
#
#      `_on_list` will not tap the box, so nothing was found at all. The rule was already
#      written down — the search box is a guaranteed false positive — and this path had never
#      applied it. Narrowing the scan to the rail does not help: the box IS in the rail.
#
#   2. The keyboard guard in `work()` was conditioned on the TYPING BUDGET, so it switched
#      itself off in the exact state it exists for — typing succeeded, `_typed` hit its max,
#      and the IME still covered the list it had just filtered. `_on_list` then looked at rows
#      it could not see and fell through to scrolling the map.
#
# Live cost: 'gather:Faro' failed after two attempts with Faro visible in a one-row list.
import unittest
from unittest.mock import patch


class TheSearchBoxIsNeverTheDestination(unittest.TestCase):
    """Geometry, not wording: the box and the row read the SAME TEXT, so only position tells
    them apart. Both are compared in frame coordinates — the OCR runs on a crop 40px off the
    top and `+40` puts it back, which is the bug's other half (the first fix compared a
    crop-relative y against a frame-space box and silently never fired)."""

    _BOX = (110, 111, 497, 172)          # the search field, holding 'faro'
    _ROW = (176, 184, 242, 216)          # the list row, 'Faro'

    def _run(self, tokens, box=_BOX):
        """Drive the real matcher over fabricated OCR. `raw` is crop-relative, as easyocr
        returns it; the box comes from the frame."""
        from PIL import Image
        import actions.sail_actions as sa

        raw = []
        for (x1, y1, x2, y2), text, conf in tokens:
            raw.append(([[x1, y1 - 40], [x2, y1 - 40], [x2, y2 - 40], [x1, y2 - 40]],
                        text, conf))

        class _Reader:
            def readtext(self, _arr, detail=1):
                return raw

        class _Box:
            def __init__(self, r):
                self.x1, self.y1, self.x2, self.y2 = r

        with patch("vision.ocr._get_reader", return_value=_Reader()), \
             patch.object(sa, "search_box_element",
                          return_value=(_Box(box) if box else None)), \
             patch("actions.world_map.ports_from_ocr_tokens", return_value=[]):
            pos, _ = sa._find_port_on_world_map(Image.new("RGB", (2400, 1080)), "Faro")
        return pos

    def test_the_row_wins_even_though_the_box_scores_higher(self):
        pos = self._run([(self._BOX, "faro", 1.00), (self._ROW, "Faro", 0.62)])
        self.assertEqual(pos, (209, 200), "returned the search box we typed into")

    def test_the_box_alone_is_not_found_at_all(self):
        """The filter matched nothing, so the box is the ONLY 'Faro' on screen. A refusal is
        the right answer — tapping the box would clear the query."""
        self.assertIsNone(self._run([(self._BOX, "faro", 1.00)]))

    def test_a_port_label_on_the_map_is_still_matched(self):
        """The exclusion is the box's rectangle, not a ban on the name."""
        self.assertEqual(self._run([((1500, 600, 1580, 640), "Faro", 0.90)]), (1540, 620))

    def test_no_search_box_on_screen_excludes_nothing(self):
        """A bare map has no box; the reader must not go blind because it could not find one."""
        self.assertEqual(self._run([(self._ROW, "Faro", 0.62)], box=None), (209, 200))


class TheListIsReadWithTheKeyboardDown(unittest.TestCase):
    def test_a_spent_typing_budget_does_not_switch_the_guard_off(self):
        """THE HOLE, pinned: typing landed (`_typed` at its max) and the IME is still up. The
        guard must still put it away — otherwise the look happens through the keyboard."""
        import brain.activities.world_map as wm
        src = wm.WorldMapActivity.work.__doc__ or ""
        del src
        import inspect
        body = inspect.getsource(wm.WorldMapActivity.work)
        guard = body.split("_kb_hygiene < _MAX_KB_HYGIENE")[0].rsplit("if ", 1)[-1]
        self.assertNotIn("_typed <", guard,
                         "the keyboard guard is conditioned on the typing budget again")
        self.assertNotIn("_type_attempts <", guard,
                         "the keyboard guard is conditioned on the typing budget again")

    def test_the_guard_is_still_bounded(self):
        import inspect
        import brain.activities.world_map as wm
        body = inspect.getsource(wm.WorldMapActivity.work)
        self.assertIn("_kb_hygiene < _MAX_KB_HYGIENE", body,
                      "an unbounded dismisser can never reach the poisoned-query recovery")


if __name__ == "__main__":
    unittest.main()
