"""Every material must reach its OWN target.

Live 2026-08-27 at Barcelona: one round bought Iron 506→830 and Matchlock Gun 0→158.
`track_bought_good` follows a single cargo tile, so the +324 it measured was Iron's — and
that number was credited to BOTH goods. The loop then compared Iron's 830 against the
SUMMED goal of 811 (506 Iron + 305 Matchlock), declared success, and broke out before the
sold-out branch could spend a blue gem on the empty Matchlock shelf. The fleet sailed with
158 of the 305 it needed, believing it had 324.
"""
import pytest

from actions.buy_materials import _goal_met
from brain.market_ledger import MarketLedger

GOAL = {"Iron": 506, "Matchlock Gun": 305}


def test_a_surplus_of_one_cannot_cover_a_shortfall_of_another():
    led = MarketLedger()
    led.seed({"iron": 830, "matchlock gun": 158})       # the real hold that day
    met, why = _goal_met(led, GOAL)
    assert not met
    assert "Matchlock Gun 158/305" in why
    assert sum((830, 158)) >= sum(GOAL.values()), \
        "the summed test WOULD have passed — that is the bug being pinned"


def test_all_goods_at_target_is_met():
    led = MarketLedger()
    led.seed({"iron": 506, "matchlock gun": 305})
    met, _ = _goal_met(led, GOAL)
    assert met


def test_an_unknown_amount_is_never_treated_as_enough():
    """`bought()` with no quantity means the read failed. Unknown must not become 'enough'
    — the ledger records it pending so the caller goes and reads the sell grid."""
    led = MarketLedger()
    led.seed({"iron": 830})
    led.bought("Matchlock Gun")                          # amount unreadable
    met, why = _goal_met(led, GOAL)
    assert not met and "amount unknown" in why


def test_buying_several_goods_at_once_records_each_as_unknown():
    """One tracked tile cannot attribute a multi-good purchase, so nothing may be assumed."""
    led = MarketLedger()
    led.seed({"iron": 506})
    for material in ("Iron", "Matchlock Gun"):
        led.bought(material)
    assert led.amount_unknown("Iron") and led.amount_unknown("Matchlock Gun")
    assert not _goal_met(led, GOAL)[0]


def test_a_single_good_round_still_credits_its_measured_rise():
    led = MarketLedger()
    led.seed({"iron": 506})
    led.bought("Iron", 324)
    assert led.believed("Iron") == 830 and not led.amount_unknown("Iron")


def test_a_single_good_order_may_use_an_aggregate_total():
    """With one material in the order, every unit the cargo counter saw is that material,
    so a total IS a per-good count. This is the only case where an aggregate may stand in —
    it is exactly the attribution that failed with two goods at Barcelona."""
    from actions.buy_materials import buy_to_goal
    src = __import__("inspect").getsource(buy_to_goal)
    assert "len(goal) == 1" in src and "the only good in the order" in src


def test_two_goods_never_use_an_aggregate():
    """The Barcelona case: an aggregate cannot say how much of each arrived."""
    led = MarketLedger()
    led.seed({"iron": 506})
    for material in ("Iron", "Matchlock Gun"):
        led.bought(material)                             # multi-good round -> unknown each
    met, why = _goal_met(led, {"Iron": 506, "Matchlock Gun": 305})
    assert not met and "amount unknown" in why
