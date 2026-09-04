"""A swipe over the soft keyboard TYPES. It does not scroll.

LIVE 2026-08-30, looking for the port Gijon. The list ladder is look -> type -> scroll, and
the scroll branch logged "the keyboard may be in the way" and then swiped anyway, at (300,700).
On this phone the keyboard covers the lower half, so the swipe landed on the keys and
glide-typed into the search box: 'gijo' became 'gijo aww awwawwaww', one 'aww' per scroll.

Each retry made the query LESS able to match, so the port could never surface, and the leg
ended "looked, typed and scrolled" — having destroyed its own search to get there.

Whether the keyboard is up is not a thing to infer from pixels: the OS reports it
(`dumpsys input_method`, mInputShown). Put it away, then retype — `_type_search_prefix`
clears the box, which is what removes the junk already in it.
"""
import unittest
import unittest.mock

from brain.activities.world_map import WorldMapActivity, ChooseDestination
from brain.dispatcher import WORKING


class _Frame:
    width, height = 2400, 1080


class TheKeyboardIsPutAwayNotSwipedOver(unittest.TestCase):
    def _act(self, kb_up, **kw):
        self.scrolled, self.dismissed, self.typed = [], [], []
        return WorldMapActivity(
            capture_fn=lambda *a, **k: _Frame(),
            kb_up_fn=lambda: kb_up,
            kb_dismiss_fn=lambda: self.dismissed.append(True),
            scroll_fn=lambda: self.scrolled.append(True),
            type_fn=lambda p: self.typed.append(p),
            **kw)

    def _exhaust_typing(self, act, goal):
        """Drive the ladder past its typing attempts, as the live run had."""
        from brain.activities import world_map as wm
        act._goal_key = (goal.where, goal.kind)
        # BOTH counters, since 2026-09-01: a typing is spent when the query reaches the box,
        # and `_type_attempts` is the separate backstop for a keyboard that never accepts
        # anything — which is exactly the situation these tests describe.
        act._typed = wm._MAX_TYPED
        act._type_attempts = wm._MAX_TYPE_ATTEMPTS
        act._pending_query = None
        act._scrolls = 0
        act._kb_clears = 0

    def test_the_keyboard_is_dismissed_instead_of_swiped(self):
        goal = ChooseDestination(where="Gijon", kind="port")
        act = self._act(kb_up=True)
        self._exhaust_typing(act, goal)
        with unittest.mock.patch.object(act, "_find_on_screen", return_value=None), \
             unittest.mock.patch.object(act, "_list_is_open_for", return_value=True):
            res = act._on_list(goal)
        self.assertEqual(res.status, WORKING)
        self.assertTrue(self.dismissed, "the keyboard is put away")
        self.assertFalse(self.scrolled, "and NOT swiped over — that would type")

    def test_after_dismissing_it_retypes_which_clears_the_box(self):
        goal = ChooseDestination(where="Gijon", kind="port")
        act = self._act(kb_up=True)
        self._exhaust_typing(act, goal)
        with unittest.mock.patch.object(act, "_find_on_screen", return_value=None), \
             unittest.mock.patch.object(act, "_list_is_open_for", return_value=True):
            act._on_list(goal)
        self.assertEqual(act._typed, 0,
                         "the query may be poisoned; a retype clears the box")

    def test_with_the_keyboard_down_it_scrolls_as_before(self):
        goal = ChooseDestination(where="Gijon", kind="port")
        act = self._act(kb_up=False)
        self._exhaust_typing(act, goal)
        with unittest.mock.patch.object(act, "_find_on_screen", return_value=None), \
             unittest.mock.patch.object(act, "_list_is_open_for", return_value=True):
            res = act._on_list(goal)
        self.assertEqual(res.status, WORKING)
        self.assertTrue(self.scrolled, "nothing in the way, so scrolling is fine")
        self.assertFalse(self.dismissed)


if __name__ == "__main__":
    unittest.main()


class TheKeyboardIsPutAwayBeforeTheContextIsDecided(unittest.TestCase):
    """LIVE 2026-09-03, San Village, twice.

    The guard was first placed inside `_on_list` — too late, because the keyboard defeats the
    CONTEXT CLASSIFIER itself. `_village_list_open` reads the left panel, the keyboard covers
    it, so an OPEN list reads as a bare map:

        11:07:59  destination_list -> _on_list      types 'San ', keyboard comes up
        11:08:19  map_open -> _on_map               "not on screen — opening the list"
        11:08:31  map_open -> _on_map               ...and the rail tap TOGGLES IT SHUT

    San Village was in that list the whole time — the user watched the typing land and the row
    appear — and the bot closed the list instead of tapping it. `_on_list` was never reached,
    so a guard living there could never fire."""

    def _act(self, *, kb_up, typed=0, attempts=0, hygiene=0):
        from brain.activities.world_map import WorldMapActivity
        act = WorldMapActivity(kb_up_fn=lambda: kb_up, kb_dismiss_fn=lambda: None)
        act._typed, act._type_attempts, act._kb_hygiene = typed, attempts, hygiene
        return act

    def _work(self, act, goal_where="Faro"):
        import types
        from unittest.mock import patch
        from brain.activities.world_map import ChooseDestination
        act._goal_key = (goal_where, "port")        # not a fresh goal, so counters stand
        goal = ChooseDestination(where=goal_where, kind="port")
        state = types.SimpleNamespace(state="world_map", location="world_map", frame=None)
        with patch.object(act, "_ensure_tab", return_value=True), \
             patch.object(act, "_classify") as classify:
            res = act.work(goal, state)
        return res, classify

    def test_it_fires_before_the_screen_is_classified(self):
        """The classifier must not even run while the keyboard is covering the panel."""
        import types
        from unittest.mock import patch
        from brain.activities.world_map import ChooseDestination
        act = self._act(kb_up=True)
        act._goal_key = ("Faro", "port")            # not a fresh goal, so counters stand
        goal = ChooseDestination(where="Faro", kind="port")
        state = types.SimpleNamespace(state="world_map", location="world_map", frame=None)
        with patch.object(act, "_ensure_tab", return_value=True), \
             patch.object(act, "_classify") as classify:
            res = act.work(goal, state)
        classify.assert_not_called()
        self.assertEqual(res.observed.get("did"), "put the keyboard away")

    def test_a_spent_TYPING_budget_does_not_switch_the_guard_off(self):
        """LOOK AT THE LIST WHEN THE KEYBOARD IS GONE (user, 2026-09-03).

        This assertion used to run the other way — the guard stood aside as soon as the typing
        budget was spent, to keep `_on_list`'s poisoned-query recovery reachable. That put the
        hole in exactly the state the guard exists for: typing SUCCEEDS, `_typed` reaches its
        max, and the IME still covers the list it just filtered. `_on_list` looks at rows it
        cannot see (line ~341, before its own keyboard branch), finds nothing, and falls
        through to scrolling the MAP for a port sitting in plain view — 'gather:Faro', live
        2026-09-03, with Faro alone in a one-row list.

        Whether we may type AGAIN has no bearing on whether we can SEE. The budget belonged to
        the retype, never to the look."""
        from brain.activities.world_map import _MAX_TYPED, _MAX_TYPE_ATTEMPTS
        act = self._act(kb_up=True, typed=_MAX_TYPED, attempts=_MAX_TYPE_ATTEMPTS)
        res, classify = self._work(act)
        classify.assert_not_called()
        self.assertEqual(res.observed.get("did"), "put the keyboard away")

    def test_it_stands_aside_once_its_OWN_budget_is_spent(self):
        """What keeps the recovery reachable now. `_on_list` dismisses AND resets the
        attempts, and its trigger is "keyboard still up with nothing left to try" — so the
        guard has to run out of its own accord rather than never having fired. Bounded by
        `_MAX_KB_HYGIENE`, which is about looking, not about typing."""
        from brain.activities.world_map import _MAX_KB_HYGIENE
        act = self._act(kb_up=True, hygiene=_MAX_KB_HYGIENE)
        _res, classify = self._work(act)
        classify.assert_called_once()
