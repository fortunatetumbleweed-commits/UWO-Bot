"""A typing is spent when the QUERY IS IN THE BOX, not when the tap was sent.

Live 2026-09-01, the failure this closes. The bare world map was mistaken for an open list,
and `_on_list` typed the prefix into it — twice:

    100  16:36:35  tap        (420,141)      the search box... on a screen with no search box
    101  16:36:38  input_text                'Barc', into the map
    103  16:36:58  tap        (420,141)
    104  16:37:02  input_text                'Barc', again
    108  16:37:38  tap        (68,176)       the RAIL icon — the list finally opens
    110-118         swipe x5                 and now it can only scroll

`self._typed += 1` recorded that a type had been PERFORMED, which is a stored conclusion, and
nothing asked whether the text arrived. So when the real list opened at frame 108 the budget
was already spent and the search could only swipe. Six swipes did not reach Barcelona and the
leg failed: "'Barcelona' is not in the port list — looked, typed and scrolled".

Typing is the RECOVERY. Burning it on a fumbled tap disables the remedy exactly when
something has already gone wrong.

The box shows the query, so this is a question the screen answers. Two counters now: what
LANDED bounds the search, and what was SENT is the backstop against a keyboard that never
accepts anything.
"""
import types
import unittest
from unittest import mock

from brain.activities.world_map import (_MAX_SCROLLS, _MAX_TYPE_ATTEMPTS, _MAX_TYPED,
                                        WorldMapActivity)


def _box(label, w=390):
    return types.SimpleNamespace(element_type="button", label=label,
                                 cx=302, cy=150, x1=302 - w // 2, x2=302 + w // 2,
                                 y1=120, y2=180)


class TheBoxSaysWhetherItLanded(unittest.TestCase):
    def _holds(self, label, prefix="Barc"):
        from actions.sail_actions import search_box_holds
        return search_box_holds(prefix, None, [_box(label)] if label is not None else [])

    def test_a_box_holding_the_query_landed(self):
        self.assertTrue(self._holds("Barc"))

    def test_the_empty_box_did_not(self):
        self.assertFalse(self._holds("Search"))

    def test_no_box_at_all_did_not(self):
        self.assertFalse(self._holds(None))

    def test_accents_fold_as_everywhere_else(self):
        from actions.sail_actions import search_box_holds
        self.assertTrue(search_box_holds("Male", None, [_box("Malé")]))


class OnlyALandedTypingIsCounted(unittest.TestCase):
    def _activity(self, landed):
        a = WorldMapActivity()
        a._pending_query = "Barc"
        with mock.patch("actions.sail_actions.search_box_holds", return_value=landed), \
             mock.patch.object(a, "_frame", return_value=object()):
            a._settle_last_typing()
        return a

    def test_a_typing_that_reached_the_box_is_spent(self):
        self.assertEqual(self._activity(True)._typed, 1)

    def test_a_typing_that_went_nowhere_is_not(self):
        self.assertEqual(self._activity(False)._typed, 0,
                         "this is what left the real list unsearchable")

    def test_an_unreadable_box_does_not_spend_it_either(self):
        a = WorldMapActivity()
        a._pending_query = "Barc"
        with mock.patch("actions.sail_actions.search_box_holds",
                        side_effect=RuntimeError("no parse")), \
             mock.patch.object(a, "_frame", return_value=object()):
            a._settle_last_typing()
        self.assertEqual(a._typed, 0, "unknown is not landed")

    def test_the_pending_query_is_cleared_either_way(self):
        for landed in (True, False):
            self.assertIsNone(self._activity(landed)._pending_query)


class ButAKeyboardThatNeverTakesAnythingStillStops(unittest.TestCase):
    def test_sent_attempts_are_bounded_separately(self):
        self.assertGreater(_MAX_TYPE_ATTEMPTS, _MAX_TYPED,
                           "there must be room to retry a typing that did not land")
        self.assertLessEqual(_MAX_TYPE_ATTEMPTS, 6, "and not room to do it forever")


class TheResetsMoveTogether(unittest.TestCase):
    """A retype after clearing the keyboard must actually be able to run."""

    def test_putting_the_keyboard_away_frees_both_counters(self):
        import inspect
        src = inspect.getsource(WorldMapActivity._on_list)
        self.assertIn("self._typed = self._type_attempts = 0", src)

    def test_a_new_goal_frees_both(self):
        import inspect
        src = inspect.getsource(WorldMapActivity)
        self.assertIn("self._type_attempts, self._pending_query = 0, None", src)


class ScrollingHasRoomToFinish(unittest.TestCase):
    def test_the_cap_is_raised(self):
        """Six did not reach Barcelona from Tripoli even with the list open (user)."""
        self.assertGreaterEqual(_MAX_SCROLLS, 20)


if __name__ == "__main__":
    unittest.main()


class AQueryAlreadyInTheBoxIsNotTypedAgain(unittest.TestCase):
    """RETYPING CLEARS THE FILTER IT WAS TRYING TO CREATE (user, 2026-09-03).

    `_type_prefix` taps the search field and types with clear_first, so a second typing WIPES
    the box and un-filters the list — undoing the very filter that was about to show the
    destination. Live hunting Faro:

        11:54:27  'Faro' not found          -> typing (1/4, 0/2 landed)
        11:54:53  typing (2/4, 1/2 landed)     the query had LANDED; this cleared it
        11:56:54  'Faro' is in the open list @ (209,200) — tapping

    It got there two minutes and a wasted filter later. `_MAX_TYPED` allowed a second typing
    whether or not the first had worked, and "did it work?" is a question the box answers."""

    def _act(self, *, holds):
        from brain.activities.world_map import WorldMapActivity
        act = WorldMapActivity(kb_up_fn=lambda: False, kb_dismiss_fn=lambda: None)
        act._typed, act._type_attempts = 1, 1        # one landed typing already
        return act

    def _run(self, holds):
        from unittest.mock import patch
        from brain.activities.world_map import ChooseDestination
        act = self._act(holds=holds)
        goal = ChooseDestination(where="Faro", kind="port")
        typed = []
        with patch.object(act, "_list_is_open_for", return_value=True), \
             patch.object(act, "_find_on_screen", return_value=None), \
             patch.object(act, "_settle_last_typing"), \
             patch.object(act, "_frame", return_value=None), \
             patch.object(act, "_type_prefix", side_effect=lambda p: typed.append(p)), \
             patch.object(act, "_scroll_list"), \
             patch.object(act, "_rail_signature", return_value=("x",)), \
             patch("actions.sail_actions.search_box_holds", return_value=holds):
            act._on_list(goal)
        return typed

    def test_it_does_not_retype_when_the_box_already_holds_the_query(self):
        self.assertEqual(self._run(holds=True), [],
                         "retyping clears the box and un-filters the list")

    def test_it_still_types_when_the_box_is_empty(self):
        self.assertEqual(self._run(holds=False), ["Faro"])
