"""Arriving at the village is not the same as having the Barter panel open.

`barter_commit_verified` acts on an ALREADY-OPEN panel — it does not navigate. Live
2026-08-22, run 26, the fleet reached Melanesian Village and perceive read the interior's
left menu verbatim:

    [classify] -> village (left-menu vocab match ['barter', 'explore', 'gifting', 'loot',
                                                  'recruit crew'])
    [mission.barter] barter panel unreadable on arrival — running VERIFIED commits
    [commit] iter 0: no positive button found — settled after 0 tap(s)
    [barter_commit] NO PROGRESS: tapped [], no amity/cargo change — escalate, don't re-tap
    FAILED at step mission: barter failed: barter commit stalled after 0

The item to tap was named in the bot's own log line. It was never tapped.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from brain import barter_mission_live as bml

# A frame with real dimensions: the left-menu bound is a FRACTION of the width, so a bare
# object() stub cannot exercise it.
_FRAME = Image.new("RGB", (2400, 1080))


class OpensTheBarterPanel(unittest.TestCase):

    def _run(self, *, already_on, tap_ok=True, after_tap):
        """Returns (result, taps) for a scripted screen."""
        import types
        taps = []
        seq = [already_on] + list(after_tap)
        item = {"label": "Barter", "cx": 183, "cy": 616, "is_locked": False}
        menu = types.SimpleNamespace(items=[item], labels=lambda: ["Barter"],
                                     find=lambda lab: item)
        with patch("actions.ui.on_submenu", side_effect=lambda _n, _f=None: seq.pop(0)), \
             patch("vision.region_detectors.left_menu.detect_left_menu",
                   return_value=menu if tap_ok else None), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("actions.ui.tap_element",
                   side_effect=lambda el, **k: taps.append(k.get("why")) or True), \
             patch("capture.adb_capture.capture_screen", return_value=_FRAME):
            return bml._open_barter_panel(), taps

    def test_it_taps_barter_from_the_village_interior(self):
        ok, taps = self._run(already_on=False, after_tap=[True])
        self.assertTrue(ok)
        self.assertEqual(len(taps), 1)

    def test_it_does_not_tap_when_the_panel_is_already_open(self):
        """The title already says Barter — tapping again would navigate away."""
        ok, taps = self._run(already_on=True, after_tap=[])
        self.assertTrue(ok)
        self.assertEqual(taps, [])

    def test_a_tap_that_did_not_open_the_panel_reports_failure(self):
        """Confirmed by reading the title, not by assuming the tap worked."""
        ok, _taps = self._run(already_on=False, after_tap=[False])
        self.assertFalse(ok)

    def test_a_missing_barter_item_reports_failure(self):
        ok, _taps = self._run(already_on=False, tap_ok=False, after_tap=[False])
        self.assertFalse(ok)

    def test_a_perceive_failure_is_not_a_success(self):
        with patch("actions.ui.on_submenu", side_effect=RuntimeError("boom")), \
             patch("capture.adb_capture.capture_screen", return_value=_FRAME):
            self.assertFalse(bml._open_barter_panel())


if __name__ == "__main__":
    unittest.main()


class RefusesToCommitFromAnUnknownScreen(unittest.TestCase):
    """Not knowing where we are is a reason to STOP, never a reason to start tapping.

    Previously an unreadable panel logged "running VERIFIED commits" and committed anyway.
    `barter_commit_verified` assumes an open panel, so on the village interior that meant
    hunting for a positive button on a screen that has none — twice — before aborting.

    The refusal reports what the screen ACTUALLY shows, because the caller's state is the
    thing that is wrong and it can only correct itself if told what is really there.
    """

    def _failure(self, *, detail, submenu=None, raises=False):
        where = (RuntimeError("no frame") if raises
                 else None)
        with patch("actions.sail_actions.where_am_i",
                   **({"side_effect": where} if raises
                      else {"return_value": {"location": "village", "detail": detail}})), \
             patch("actions.ui.active_submenu", return_value=submenu):
            return bml._no_panel_failure()

    def test_it_is_not_ok(self):
        self.assertFalse(self._failure(detail="Village interior")["ok"])

    def test_it_names_the_screen_it_saw(self):
        res = self._failure(detail="Village interior (menu: barter, explore, gifting)")
        self.assertIn("Village interior", res["reason"])
        self.assertIn("Village interior", res["screen"])

    def test_it_reports_the_submenu_when_there_is_one(self):
        self.assertEqual(self._failure(detail="Market", submenu="Purchase")["submenu"],
                         "Purchase")

    def test_an_unreadable_screen_still_refuses_rather_than_committing(self):
        res = self._failure(detail=None, raises=True)
        self.assertFalse(res["ok"])
        self.assertIn("unreadable", res["screen"])


class TheMenuItemNotTheProse(unittest.TestCase):
    """"Barter" names a menu item AND appears as prose on the same screen.

    Live 2026-08-23 on the Melanesian Village landing page: the left menu carries "Barter"
    at cx=183, and the Amity Effect panel in the centre reads "Increase Barter Count by 3"
    at cx=1052. A frame-wide label search returned the PROSE, so the tap did nothing:

        [ui] tap 'barter' @ (1052, 297) — village → Barter
        [mission.barter] Barter panel opened: False

    A chromed screen has a known layout (user, 2026-08-23): title top-left, the MENU ITEM
    LIST directly below it on the left, a centre panel, a right panel, and a top menu bar at
    the top right — except a VILLAGE, which has no top menu bar. The menu is read by the
    canonical region detector rather than by scanning the whole frame for a word.
    """

    MENU_ITEMS = ["Explore", "Gifting", "Loot", "Recruit Crew", "Barter"]

    def _menu(self, items=None, locked=False, selected=False):
        import types
        # Measured on the Melanesian Village landing page: items ~114px apart, Barter last
        # at cy=616.
        rows = [{"label": l, "cx": 183, "cy": 160 + 114 * i, "bbox": (0, 555, 367, 678),
                 "is_locked": locked and l == "Barter", "is_selected": selected}
                for i, l in enumerate(items if items is not None else self.MENU_ITEMS)]
        return types.SimpleNamespace(
            items=rows, labels=lambda: [r["label"] for r in rows],
            find=lambda lab: next((r for r in rows
                                   if r["label"].lower() == lab.lower()), None))

    def _open(self, menu, *, opened_after=True):
        taps = []
        with patch("actions.ui.on_submenu", side_effect=[False, opened_after]), \
             patch("vision.region_detectors.left_menu.detect_left_menu", return_value=menu), \
             patch("vision.omniparser.parse_fast_cached", return_value=[]), \
             patch("actions.ui.tap_element",
                   side_effect=lambda el, **k: taps.append((el["cx"], el["cy"])) or True), \
             patch("capture.adb_capture.capture_screen", return_value=_FRAME):
            ok = bml._open_barter_panel()
        return ok, taps

    def test_it_taps_the_menu_item_position(self):
        ok, taps = self._open(self._menu())
        self.assertTrue(ok)
        self.assertEqual(taps, [(183, 616)], "the menu item, not the prose at x=1052")

    def test_a_missing_menu_item_is_reported(self):
        ok, taps = self._open(self._menu(items=["Explore", "Loot"]))
        self.assertFalse(ok)
        self.assertEqual(taps, [])

    def test_an_unavailable_item_reports_completion_and_is_not_tapped(self):
        """A locked Barter item is the day's rounds being USED UP, not an error.

        After all 7 are spent the panel returns to the village top menu on its own and the
        Barter item goes dark under a red "Unavailable" ribbon (user, 2026-08-23). That means
        the ship should leave — success, not failure — so the opener reports it distinctly
        rather than as a plain False.
        """
        ok, taps = self._open(self._menu(locked=True))
        self.assertEqual(ok, "unavailable")
        self.assertEqual(taps, [], "a disabled item must never be tapped")

    def test_no_menu_at_all_is_reported(self):
        ok, taps = self._open(None)
        self.assertFalse(ok)
        self.assertEqual(taps, [])


if __name__ == "__main__":
    unittest.main()
