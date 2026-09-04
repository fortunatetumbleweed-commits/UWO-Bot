"""A grey Exchange button ends the bartering, whatever the material arithmetic says.

The mission already held the right principle — "THE GAME'S OWN ANSWER OUTRANKS OURS" — but
applied it in one direction only: a LIVE Exchange could raise the round count, a GREY one
could never lower it.

Live at Svear Village on 2026-08-26, after three successful rounds:

    materials  Wares 155 (need 2), Sundries 148 (need 2)   -> "74 rounds fundable"
    stock      out=5
    button     grey; daily slots 6/7/8 locked

    [mission.barter] the commit stalled and Exchange is greyed — treating the
                     bartering as FINISHED rather than failed
    [barter_command] 74 more round(s) fundable — bartering again before the tail
    ... and round again, until the run was killed.

The phase declared itself finished and the outer check sent it straight back in. The two ways
a barter ends look identical from the materials alone (memory: barter-ends-two-ways); only the
button tells them apart, and it has to be believed in both directions.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import brain.activities.village as village


class _Panel:
    def __init__(self, rounds_remaining):
        self.rounds_remaining = rounds_remaining
        self.shortfall = {}
        self.binding = None
        self.partial_fraction = 0.0
        self.materials = {"Wares": (155, 2), "Sundries": (148, 2)}


def _decide(rounds_remaining, exchange_live):
    """Reproduce the advance-to-tail decision in isolation."""
    more = int(rounds_remaining or 0)
    if exchange_live:
        more = max(more, 1)
    elif more:
        more = 0
    return more


class TheButtonDecides(unittest.TestCase):

    def test_a_grey_button_ends_it_even_with_materials_to_spare(self):
        """The Svear loop: 74 rounds of materials, no rounds left in the day."""
        self.assertEqual(_decide(74, exchange_live=False), 0)

    def test_a_live_button_continues_even_when_the_maths_says_none(self):
        """The other direction, already relied on: live 2026-08-23 the bot stopped after 2
        rounds while Exchange was still enabled, and the game was right."""
        self.assertEqual(_decide(0, exchange_live=True), 1)

    def test_they_agree_when_they_agree(self):
        self.assertEqual(_decide(3, exchange_live=True), 3)
        self.assertEqual(_decide(0, exchange_live=False), 0)


class TheDecisionIsWiredIn(unittest.TestCase):
    """Guards the real code path, not just the arithmetic above.

    Moved 2026-08-26 from `barter_mission_live` to `brain.activities.village`, along with the
    bartering itself. It used to grep the mission source for a comment; it now exercises the
    behaviour, which is what it should have done in the first place — a comment can be
    reworded, and a rule that only a string search enforces is not enforced.
    """

    def _stopped_because(self, *, rounds_remaining, exchange_live):
        import types
        panel = types.SimpleNamespace(selected_good="Birch Tree", rounds_remaining=rounds_remaining, materials={},
                                      amity_points=(0, 1), partial_fraction=0.0,
                                      binding=None, shortfall=0)
        # The panel is OPEN in both cases — what differs is the Exchange button. Under the
        # context model that IS the state: BARTER_PANEL_READY vs BARTER_PANEL_BLOCKED.
        import brain.village_context as C
        a = village.VillageActivity(open_panel_fn=lambda: True,
                                    select_fn=lambda g, r: True,
                                    read_panel_fn=lambda: panel,
                                    commit_fn=lambda: {"ok": True},
                                    context_fn=lambda _st: (C.BARTER_PANEL_READY
                                                            if exchange_live
                                                            else C.BARTER_PANEL_BLOCKED),
                                    overflow_fn=lambda: 0,
                                    saw_fn=lambda: {},
                                    exchange_live_fn=lambda: exchange_live,
                                    recipe_fn=lambda g: None)
        res = a.work(village.Barter("Birch Tree", "Svear Village"), None)
        return res.observed.get("stopped_because") or res.observed.get("did") or ""

    def test_a_grey_button_ends_it_even_with_materials_to_spare(self):
        """The Svear case: 74 rounds of materials and a grey button.

        The wording changed with the context model — a grey Exchange now says rounds
        REMAIN and names what is short, rather than 'the village refused'. What is being
        pinned is unchanged: the GAME'S ANSWER OUTRANKS THE ARITHMETIC, in both directions.
        """
        self.assertIn("grey", self._stopped_because(rounds_remaining=74,
                                                    exchange_live=False))

    def test_a_live_button_is_not_reported_as_a_refusal(self):
        """Symmetry, not a swap — the raising direction must survive too."""
        self.assertNotIn("refused", self._stopped_because(rounds_remaining=0,
                                                          exchange_live=True))

    def test_no_caller_is_handed_a_number_to_re_enter_on(self):
        """The 74-round loop needed TWO things: a grey button ignored, and a count for the
        caller to act on. The count is gone as well."""
        from brain.barter_mission_live import make_live_executors
        import types
        from unittest.mock import patch
        from brain.dispatcher import ActivityResult, FINISHED
        task = types.SimpleNamespace(params={"good": "Birch Tree", "village": "Svear Village"})
        with patch("brain.run_goal.run_goal",
                   return_value=ActivityResult(FINISHED, {"rounds_committed": 3}, detail="x")), \
             patch("brain.mission_progress.record_rounds"), \
             patch("brain.mission_progress.advance"):
            res = make_live_executors()["barter"](task)
        self.assertEqual(res["more_rounds_fundable"], 0)
