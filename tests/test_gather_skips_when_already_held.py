"""A gather leg must not be sailed when the hold already satisfies its orders.

`buy_to_goal` already refuses to buy what we own — but it only discovers that at the
market, AFTER the fleet has sailed there. Live 2026-08-22: after trimming 981 surplus
Ebony, the re-plan needed {Ebony 175, Coral 263, Textiles 263} for one round and the fleet
already held {700, 797, 920}. `gather:Jakarta` correctly bought nothing... and the mission
then set course for Kolkata to learn the same thing there.

Asking before sailing turns three port calls into none. The check is deliberately
conservative: an unreadable hold means "unknown", never "nothing", because a leg skipped on
a failed read arrives at the village empty-handed.
"""
from unittest import mock

import pytest


from brain.barter_mission_live import orders_already_held


def _held(orders, owned, at_market=True):
    import brain.barter_mission_live as bml
    with mock.patch.object(bml, "_at_a_market_port", return_value=at_market), \
         mock.patch.object(bml, "_owned_here", return_value=owned):
        return orders_already_held(orders, port="Kolkata")


class TestSkipsTheLeg:
    def test_a_fully_satisfied_port_is_skipped(self):
        assert _held({"Coral": 263}, {"coral": 797}) is True

    def test_every_material_must_be_satisfied(self):
        assert _held({"Coral": 263, "Textiles": 900},
                     {"coral": 797, "textiles": 100}) is False

    def test_an_unreadable_hold_does_not_skip(self):
        """Unknown must never mean satisfied — the village has no market."""
        assert _held({"Coral": 263}, None) is False

    def test_away_from_a_market_it_does_not_skip(self):
        assert _held({"Coral": 263}, {"coral": 797}, at_market=False) is False

    def test_an_exact_match_counts_as_satisfied(self):
        assert _held({"Coral": 263}, {"coral": 263}) is True

    def test_empty_orders_need_no_visit(self):
        assert _held({}, None) is True

    def test_material_names_are_matched_case_insensitively(self):
        assert _held({"Textiles": 100}, {"textiles": 900}) is True
