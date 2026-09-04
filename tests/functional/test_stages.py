"""The stage suite: what the bot DECIDES on a real captured frame.

Six thousand frames of real runs sit under data/sessions/. This turns the ones that matter
into a check that can be run BEFORE a live run rather than a post-mortem after one.

RUN ONE STAGE, OR ALL OF THEM (user, 2026-08-26):

    pytest tests/test_stages.py -k market          # every market stage
    pytest tests/test_stages.py -k barter_panel    # one stage
    pytest tests/test_stages.py                    # all of them — for a refactor like today's
    pytest tests/test_stages.py --stage-perception # ALSO re-perceive every frame (slow)

TWO LAYERS, AND THE SPLIT IS THE WHOLE DESIGN.

  * The FAST layer asserts the decision: given the state this frame is known to show, what
    does the activity do? Milliseconds, so it runs on every change.
  * The SLOW layer asserts the reading: does perception still see this frame the way it did
    when the stage was recorded? ~60s a frame, because a frame no fingerprint settles falls
    through to Qwen. Opt-in with --stage-perception.

Splitting them is the lesson of docs/simulation_tests.md, which is the post-mortem of a
simulated test that took 1h05m and whose assertion could not fail on its merits. TEST THE
DECISION, NOT THE VOYAGE. What made that possible here is the dispatcher refactor: an activity
is now a function of (goal, state), so a frame can be turned into a decision without executing
one tap.

NOTHING HERE TOUCHES THE DEVICE — `brain.replay.no_device` makes every route to the phone
raise, because the difference between a simulation and a live run is exactly that.

ADDING A STAGE:

    python -m tools.add_stage <name> data/sessions/trace_.../frame_0060.png "what should happen"

then write its decision test below. The frame is COPIED into tests/stage_suite/frames/ — the
suite owns its evidence, so pruning data/sessions/ cannot quietly empty it.
"""

from __future__ import annotations

import json
import types
import unittest
from pathlib import Path

import pytest

# `parents[1]` because this file moved into tests/functional/ on 2026-08-31 while the
# stage suite stayed at tests/stage_suite/ — the frames are shared with other tests and
# are not part of this package. Derived from __file__, never hardcoded (CLAUDE.md #6).
SUITE = Path(__file__).resolve().parents[1] / "stage_suite"
STAGES = json.loads((SUITE / "stages.json").read_text())


def state_for(stage: str):
    """The reading recorded when the stage was captured — the fast layer's input."""
    d = json.loads((SUITE / "perceived" / f"{stage}.json").read_text())
    return types.SimpleNamespace(state=d["state"], port=d.get("port"),
                                 detail=d.get("detail", ""), frame=None)


def frame_for(stage: str):
    from PIL import Image
    return Image.open(SUITE / STAGES[stage]["frame"])


# ── The catalogue itself ─────────────────────────────────────────────────────

class TheSuiteIsIntact(unittest.TestCase):
    """A stage suite that has quietly lost its frames is worse than none — it reports green."""

    def test_every_stage_has_its_frame_and_its_reading(self):
        for stage, meta in STAGES.items():
            with self.subTest(stage=stage):
                self.assertTrue((SUITE / meta["frame"]).exists(),
                                f"{stage}: frame missing — was data/sessions pruned?")
                self.assertTrue((SUITE / "perceived" / f"{stage}.json").exists(),
                                f"{stage}: no recorded reading")

    def test_every_stage_says_what_it_is(self):
        for stage, meta in STAGES.items():
            with self.subTest(stage=stage):
                self.assertTrue(meta.get("expect"), f"{stage}: no description of the stage")
                self.assertTrue(meta.get("from"), f"{stage}: no provenance")


# ── The fast layer: decisions ────────────────────────────────────────────────

class MarketStages(unittest.TestCase):

    def test_market_goods_info_dialog_is_inside_the_market(self):
        """A dialog over the Purchase grid is still the market — the activity must work here,
        not hand back. `sub_menu:purchase` is in MarketActivity.SERVES for this reason."""
        from brain.activities.market import MarketActivity
        self.assertIn(state_for("market_goods_info_dialog").state, MarketActivity.SERVES)

    def test_a_sell_goal_does_not_re_enter_the_market_from_here(self):
        """The bug this pins: `to_intent` once dispatched ENTER_BUILDING on every tick while
        the bot stood inside the market."""
        from brain.activities.market import SellHold
        from brain.intents import to_intent
        self.assertIsNone(to_intent(SellHold(()), state_for("market_goods_info_dialog")))


class PortStages(unittest.TestCase):

    def test_a_market_goal_from_the_port_asks_to_enter_the_market(self):
        from brain.activities.market import Hold
        from brain.intents import to_intent
        i = to_intent(Hold({"Iron": 242}), state_for("port_overworld_amsterdam"))
        self.assertIsNotNone(i, "a gather goal must walk into the market")
        self.assertEqual(i.name, "ENTER_BUILDING")

    def test_the_port_is_read_from_the_frame(self):
        self.assertEqual(state_for("port_overworld_amsterdam").port, "Amsterdam")

    def test_no_activity_serves_the_bare_port_overworld(self):
        """Standing in a port is not a place work happens — it is where transitions start.

        The port is now REGISTERED, for `AshoreActivity` — but only for an `ArriveAshore`
        goal, which is how a voyage ends. What must still hold is that an ordinary goal
        resolves to NOTHING there, so the dispatcher asks the task runner and dispatches an
        intent (`no activity for state 'port_overworld' — asking for a goal`, 24 times a day
        and correct every time).
        """
        from brain.activities.village import Barter
        from brain.dispatcher import Dispatcher
        from brain.run_goal import default_activities
        d = Dispatcher(perceive=lambda: None, activities=default_activities(),
                       next_goal=lambda a, b: None, to_intent=lambda a, b: None,
                       dispatch=lambda i: None)
        where = state_for("port_overworld_amsterdam").state
        self.assertIsNone(d._pick(where, Barter("Birch Tree", "Svear Village")),
                          "an ordinary goal must find no activity at a bare port")


class WorldMapStages(unittest.TestCase):

    def test_the_world_map_is_not_a_market(self):
        from brain.activities.market import MarketActivity
        self.assertNotIn(state_for("world_map_village_list").state, MarketActivity.SERVES)

    def test_a_market_goal_from_the_world_map_still_routes_to_the_market(self):
        from brain.activities.market import SellHold
        from brain.intents import to_intent
        # THE MAP CLOSES FIRST. This expected ENTER_BUILDING straight from the map, which
        # predates `to_intent` deciding CLOSE_WORLD_MAP before the goal branches: the map is
        # a full-screen overlay and a market goal cannot be served through it — `Depart`
        # would have tapped a harbour entrance drawn underneath it. Closing is one tap and
        # the next perceive routes onward, so the market is still reached, one step later.
        i = to_intent(SellHold(()), state_for("world_map_village_list"))
        self.assertEqual(i.name, "CLOSE_WORLD_MAP")


class MarketRestockStages(unittest.TestCase):
    """Refreshing a sold-out shelf costs gems, and WHICH gem is the whole question.

    Blue gems are earned through investment and ordinary play. Red gems cost real money, and
    the standing rule is that nothing spends them without the user (2026-08-25). The detector
    reads the currency off the control, so this stage pins that reading against a real frame
    rather than against a mock that would agree with whatever the code did.
    """

    def test_the_restock_control_is_found_and_priced_in_blue_gems(self):
        from vision.region_detectors.market_restock import find_restock_button
        btn = find_restock_button(frame_for("market_restock_blue_gem"))
        self.assertIsNotNone(btn, "the restock control must be found on the Purchase grid")
        self.assertEqual(btn.currency, "blue_gem")

    def test_a_refresh_is_refused_when_the_currency_is_not_clearly_blue(self):
        """No red-gem frame has been captured yet, so this drives the refusal directly. An
        UNKNOWN colour must be refused too — unsafe is not the same as red, and treating it
        as spendable is how real money gets spent by accident."""
        from unittest.mock import patch
        from actions.buy_materials import refresh_market
        for currency in ("red_gem", "unknown", None):
            with self.subTest(currency=currency):
                btn = types.SimpleNamespace(currency=currency, cx=1, cy=2, timer="00.10.00")
                with patch("vision.region_detectors.market_restock.find_restock_button",
                           return_value=btn), \
                     patch("capture.adb_capture.capture_screen", return_value=object()):
                    res = refresh_market(capture_fn=lambda: object(),
                                         tap_fn=lambda *a: self.fail("must not tap"))
                self.assertFalse(res.get("ok"))
                self.assertTrue(res.get("refused"))


class BarterPanelStages(unittest.TestCase):
    """The panel where every bug of 2026-08-26 lived."""

    def test_the_village_panel_is_a_village(self):
        from brain.activities.village import VillageActivity
        self.assertIn(state_for("barter_panel_birch_tree_selected").state,
                      VillageActivity.SERVES)

    def test_the_selected_good_is_read_back_from_the_panel(self):
        """The tiles carry no names — only a thumbnail, a stock status and a category — so
        the ONLY way to know what is selected is to read the right-hand panel."""
        from actions.barter_reader import read_barter_panel
        panel = read_barter_panel(frame_for("barter_panel_birch_tree_selected"))
        self.assertEqual(panel.selected_good, "Birch Tree")

    def test_the_wrong_good_is_visible_as_the_wrong_good(self):
        """The live bug: the bot had Naverslojd selected while it wanted Birch Tree, and
        proceeded. Nothing can catch that unless the read-back is believed."""
        from actions.barter_reader import read_barter_panel
        panel = read_barter_panel(frame_for("barter_panel_wrong_good_selected"))
        self.assertEqual(panel.selected_good, "Naverslojd")
        self.assertNotEqual(panel.selected_good, "Birch Tree")

    def test_an_open_panel_does_not_convince_the_activity_the_good_is_right(self):
        """A panel full of Naverslojd's materials reads exactly like a panel full of Birch
        Tree's: populated. So selection must run regardless of how full the panel looks."""
        from unittest.mock import patch
        from brain.activities.village import Barter, VillageActivity
        from actions.barter_reader import read_barter_panel
        asked = []
        panel = read_barter_panel(frame_for("barter_panel_wrong_good_selected"))
        a = VillageActivity(open_panel_fn=lambda: True,
                            select_fn=lambda g, r: asked.append(g) or True,
                            read_panel_fn=lambda: types.SimpleNamespace(
                                good="Naverslojd",
                                rounds_remaining=0, materials={"Wares": 445},
                                amity_points=panel.amity_points, partial_fraction=0.0,
                                binding=None, shortfall=0),
                            commit_fn=lambda: {"ok": True},
                            context_fn=lambda _s: __import__("brain.village_context",
                                                             fromlist=["x"]).BARTER_PANEL_BLOCKED,
                            overflow_fn=lambda: 0, saw_fn=lambda: {},
                            exchange_live_fn=lambda: False, recipe_fn=lambda g: None)
        a.work(Barter("Birch Tree", "Svear Village"),
               state_for("barter_panel_wrong_good_selected"))
        self.assertEqual(asked, ["Birch Tree"],
                         "the wanted good must be selected, not assumed")


class TransientStages(unittest.TestCase):
    """A full-screen notice is not an unknown screen.

    Measured on this frame 2026-08-26: the family CNN said `transient` at 0.9999 and the
    classifier ignored it, spending 23 SECONDS in the legacy cascade — omniparser, chrome
    flags, Moondream three times, read_port_name — to reach "Unknown blocking screen", where
    the escalation machinery starts guessing. Every question in that cascade asks where the
    fleet is, and a notice covering the whole screen cannot answer it.
    """

    def test_it_is_read_as_a_transient_not_an_unknown(self):
        self.assertEqual(state_for("transient_mate_promotion").state, "transient")

    def test_an_activity_serves_it(self):
        """The point of it being a state: something can act on it without any caller
        learning what a promotion notice is."""
        from brain.run_goal import default_activities
        self.assertIn("transient", default_activities())

    def test_it_taps_once_and_finishes_without_claiming_to_know_where_it_landed(self):
        from brain.activities.transient import TransientActivity
        from brain.dispatcher import FINISHED
        taps = []
        r = TransientActivity(tap=lambda x, y: taps.append((x, y)),
                              settle=lambda _p: None).work(None, state_for("transient_mate_promotion"))
        self.assertEqual(r.status, FINISHED)
        self.assertEqual(len(taps), 1)
        self.assertNotIn("state", r.observed)
        self.assertNotIn("port", r.observed)

    def test_the_tap_avoids_the_controls_on_the_notice(self):
        """The centre holds reward tiles and a portrait; 'Other Mates (1)' sits bottom-right
        and would navigate. Dismissing must not become navigating."""
        from brain.activities.transient import TransientActivity
        taps = []
        TransientActivity(tap=lambda x, y: taps.append((x, y)),
                          settle=lambda _p: None).work(None, state_for("transient_mate_promotion"))
        x, y = taps[0]
        self.assertLess(x, 2400 * 0.35, "keep clear of the centre content")
        self.assertLess(y, 1080 * 0.35, "keep clear of the bottom-right 'Other Mates' button")

    def test_the_IDLE_LOCK_is_not_a_notice(self):
        """It looks exactly like one — full screen, no chrome, no action verbs — and its exit
        is a SWIPE, so calling it a notice makes the bot TAP a screen that only answers to a
        gesture. Live 2026-08-27 the CNN said transient at 0.80 on "Barcelona / Slide up to
        unlock" and only the next tick's fingerprint rescued it.

        The lock says what it is, which is the cheapest discriminator available."""
        from brain.perceive import _is_full_screen_notice
        self.assertFalse(_is_full_screen_notice(frame_for("idle_lock_looks_like_a_notice")))

    def test_the_lock_still_reaches_its_own_activity(self):
        from brain.run_goal import default_activities
        # The registry holds a LIST per state — a state may have several activities.
        self.assertIn("idle_lock",
                      [a.name for a in default_activities()["idle_lock"]])

    def test_a_dialog_is_never_routed_here(self):
        """A dialog offers a CHOICE and its buttons are the answer space."""
        self.assertNotEqual(state_for("market_goods_info_dialog").state, "transient")

    def test_a_screen_offering_a_named_action_is_not_a_notice(self):
        """THE REGRESSION THIS SUITE CAUGHT, 2026-08-26. The first version of the notice
        check asked only whether `detect_dialog` found a card — and it does NOT find the
        market's Trade Goods Info card, so that dialog classified as `transient` and would
        have been TAPPED BLIND at a fixed point instead of having its buttons read.

        Absence of evidence from one detector is not evidence of absence. The sturdier
        question is what the screen OFFERS: the promotion notice offers no named action and
        no chrome; the goods dialog offers cancel / load / purchase / sell."""
        from brain.perceive import _is_full_screen_notice
        self.assertTrue(_is_full_screen_notice(frame_for("transient_mate_promotion")))
        self.assertFalse(_is_full_screen_notice(frame_for("market_goods_info_dialog")))


class ReadAQuantityFromITSOWNTILE(unittest.TestCase):
    """Not from the whole frame, and not from an upscaled whole frame.

    Measured on two real Barcelona sell pages, 2026-08-27, with the truth read off the tiles
    by eye (`Lemon Oil 1`, `Neroli 2`, user-confirmed):

        read                     Lemon Oil (1)   Neroli (2)
        full frame               None (silent)   2  correct
        whole frame x2 LANCZOS   1    correct    8  WRONG
        whole frame x2 bicubic   31   WRONG      2  correct

    Every whole-frame variant is wrong somewhere, AND THE RESAMPLE FILTER CHANGES THE ANSWER.
    So "fill the gaps from an upscaled read" is not safe — it could have filled Lemon Oil
    with 31, and a wrong number is worse than a missing one because nothing downstream can
    tell.

    What works is a tight crop of the ONE tile, upscaled: the number is small and alone in
    the frame instead of being one of forty things in a busy 2400x1080 image.
    """

    def _goods(self, stage):
        from vision.market_reader import read_market_page_omni
        return {g.name: g for g in read_market_page_omni(frame_for(stage), tab="sell")}

    def _tile_number(self, stage, name, scale=4):
        from vision.ocr import read_text
        im = frame_for(stage)
        g = self._goods(stage)[name]
        x, y = g.tap_x, g.tap_y
        tile = im.crop((max(0, x - 215), max(0, y - 120), max(0, x - 40),
                        min(im.height, y + 40)))
        tile = tile.resize((tile.width * scale, tile.height * scale))
        digits = [t for t in read_text(tile).replace(",", "").split() if t.isdigit()]
        return int(digits[-1]) if digits else None

    def test_the_page_read_now_fills_the_gap_itself(self):
        """The full frame IS silent on Lemon Oil — but the page read no longer leaves it that
        way. `fill_missing_quantities` used to be called only by `read_market_all_pages`, so
        the ledger got the tile fallback and `sell_down_to` did not; on 2026-08-29 that split
        the two by eleven seconds and the trim skipped Candle as unreadable while the
        accumulator had just recovered 1182 from its tile. The recovery belongs to the READ,
        so every caller gets it. The gap is still real — the tests below read the tile
        directly to show what the whole frame cannot — it is simply closed before anyone sees
        it."""
        self.assertEqual(self._goods("sell_grid_iron_2099")["Lemon Oil"].owned_qty, 1)

    def test_the_tile_crop_reads_what_the_full_frame_could_not(self):
        self.assertEqual(self._tile_number("sell_grid_iron_2099", "Lemon Oil"), 1)

    def test_the_tile_crop_agrees_where_the_full_frame_did_read(self):
        """It must not disturb the values that were already right."""
        self.assertEqual(self._tile_number("sell_grid_iron_2099", "Iron"), 2099)
        self.assertEqual(self._tile_number("sell_grid_iron_2099", "Gunpowder"), 2)

    def test_the_full_frame_reads_the_big_number(self):
        self.assertEqual(self._goods("sell_grid_iron_2099")["Iron"].owned_qty, 2099)

    def test_a_material_can_sit_on_a_later_page(self):
        """Candle is on page 2, so a reader that never scrolls cannot see it — which is why
        the owned-count read now uses read_market_all_pages."""
        self.assertEqual(self._goods("sell_grid_page2_neroli")["Candle"].owned_qty, 148)
        self.assertNotIn("Candle", self._goods("sell_grid_iron_2099"))


# ── The slow layer: perception ───────────────────────────────────────────────

@pytest.mark.stage_perception
class TestPerceptionStillReadsTheseFrames:
    """Re-perceive every stage frame and compare against what was recorded.

    Run it after touching perceive, the fingerprints, or the family classifier:

        pytest tests/test_stages.py --stage-perception

    ~60s a frame, because a frame no fingerprint settles falls through to Qwen — which is why
    it is opt-in while the decision tests above always run.

    The class name must start with `Test`. It did not, for about a minute on 2026-08-26, and
    pytest simply did not collect it: `--collect-only` found zero of these while the suite
    reported nine passing tests. A slow test that never runs is indistinguishable from a fast
    suite, which is the failure mode this whole file exists to avoid.
    """

    @pytest.mark.parametrize("stage", sorted(STAGES))
    def test_the_reading_still_holds(self, stage):
        from brain.replay import state_of
        got = state_of(SUITE / STAGES[stage]["frame"], use_cache=False)
        assert got.state == STAGES[stage]["state"], (
            f"{stage}: perception now reads {got.state!r}, recorded as "
            f"{STAGES[stage]['state']!r} ({STAGES[stage]['from']})")


if __name__ == "__main__":
    unittest.main()


class NoticesAreNotDescribed(unittest.TestCase):
    """A full-screen notice needs a TAP, not a description.

    Live 2026-08-26: a 'Barcelona is unlocked' notice classified as `transient` at 0.81 —
    just under the ≥0.95 gate that skips the VLM — and the Qwen call it triggered took SEVEN
    MINUTES to return the string 'Barcelona is unlocked'. Correct, cosmetic, and the single
    most expensive thing in the run.

    `TransientActivity` reads none of it. Neither does the idle lock's swipe.
    """

    def test_a_transient_does_not_pay_for_the_vlm(self):
        import inspect
        from brain import perceive as p
        src = inspect.getsource(p._perceive_uncached)
        self.assertIn('if nav_state in ("transient", "idle_lock")', src,
                      "a notice must be exempt from the Qwen call, like sea and world_map")

    def test_the_activity_that_serves_it_reads_no_detail(self):
        """The structural reason it is safe to skip: nothing consumes the description."""
        import inspect
        from brain.activities.transient import TransientActivity
        src = inspect.getsource(TransientActivity.work)
        for consumed in ("detail", "sub_menu", "overlays"):
            self.assertNotIn(f"state.{consumed}", src)
