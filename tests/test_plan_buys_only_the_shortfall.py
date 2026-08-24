"""After a surplus clear, the mission buys only the SHORTFALL — no re-checking each port.

The hold is measured during the clear, so the mission already knows what it carries. Gather
legs for materials it already has are unnecessary: on 2026-08-22, holding
{Ebony 700, Coral 797, Textiles 920} against a need of {175, 263, 263}, it still set course
for Kolkata to rediscover that it needed nothing.

Worse, the check it ran on arrival read the wrong tab. `_read_owned_via_sell` tapped a
hardcoded Sell-tab coordinate and trusted whatever came back; when that tap missed it read
the PURCHASE grid, which lists the SHOP'S stock. At Kolkata that answered "textiles: 3"
while the hold carried 920, so it decided to buy — one unit at a time, because the trim had
left Put In Bulk off and the quantity dialog defaults to 1.

User: "after sell surplus, we need to go back to the task executor, and it should just go
to the village without checking anymore, because we already checked before selling the
surplus or after selling the surplus."
"""
from unittest import mock

import pytest

from brain.barter_mission_live import plan_barter_task


def _plan(needs, held):
    recipe = mock.MagicMock()
    recipe.good = "Box of Nutmeg"
    recipe.output_per_round = {"Neutral": 679}
    with mock.patch("brain.barter_mission_live.material_sources_from_recipe",
                    return_value={m: ["Jakarta", "Kolkata"] for m in needs}), \
         mock.patch("brain.barter_mission_live.plan_gathering") as pg, \
         mock.patch("brain.barter_mission_live.assign_purchases") as ap:
        pg.return_value = mock.MagicMock(route=["Jakarta"], unsourced=set())
        ap.return_value = {"Jakarta": {"Ebony": 1}}
        plan_barter_task(recipe, "Melanesian Village", "", 1,
                         {}, (0, 0), needs=needs, output_per_round=679,
                         already_held=held)
        # the quantities actually handed to the gather planner
        return pg.call_args.kwargs.get("quantities", pg.call_args[0][-1])


class TestShortfallOnly:
    def test_a_fully_covered_need_asks_for_nothing(self):
        """Every material already aboard -> no gather legs at all."""
        got = _plan({"Ebony": 175, "Coral": 263, "Textiles": 263},
                    {"ebony": 700, "coral": 797, "textiles": 920})
        assert got == {}, f"still planning to buy {got}"

    def test_only_the_missing_amount_is_bought(self):
        got = _plan({"Ebony": 175, "Coral": 263},
                    {"ebony": 700, "coral": 100})
        assert got == {"Coral": 163}

    def test_nothing_held_means_the_full_need(self):
        needs = {"Ebony": 175, "Coral": 263}
        assert _plan(needs, {}) == needs

    def test_no_measurement_means_the_full_need(self):
        """An unmeasured hold must not be read as 'we have everything'."""
        needs = {"Ebony": 175, "Coral": 263}
        assert _plan(needs, None) == needs

    def test_an_exact_match_needs_no_purchase(self):
        assert _plan({"Coral": 263}, {"coral": 263}) == {}

    def test_material_names_match_case_insensitively(self):
        assert _plan({"Textiles": 100}, {"textiles": 900}) == {}
