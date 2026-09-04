"""A grey Exchange means rounds REMAIN — the opposite of what the code used to conclude.

The two ways a barter ends are told apart by the SCREEN, not inferred from the button
(user, 2026-08-27):

  * rounds USED UP  — the game closes the barter submenu itself and drops the bot back to
    the village top menu. There is no panel left to read.
  * rounds REMAIN, barter blocked — the panel stays open with Exchange GREY, because a
    material is short or amity is too low. The right panel shows the short material as 0
    in RED.

Live 2026-08-27 at Svear: after 4 of 5 rounds Matchlock Gun hit 0 and the code reported
"the day's barter rounds are spent". The 5th round was available, 95 Matchlock away, and
the mission stopped believing the day was over.
"""
import inspect

from actions.barter_executor import _short_materials


def test_the_short_material_is_named():
    panel = {"materials": [("Wares", 140, 2), ("Firearms", 0, 2), ("Sundries", 231, 2)]}
    assert _short_materials(panel) == ["Firearms"]


def test_nothing_is_named_when_all_materials_are_present():
    panel = {"materials": [("Wares", 140, 2), ("Sundries", 231, 2)]}
    assert _short_materials(panel) == []


def test_a_malformed_panel_does_not_crash_the_report():
    """Diagnosis must never be the thing that breaks the path it is diagnosing."""
    assert _short_materials(None) == []
    assert _short_materials({"materials": [("Wares",), None, 7]}) == []


def test_a_grey_exchange_is_not_reported_as_exhausted():
    """`exhausted=True` told the mission the day was over. It is not — a round remains."""
    src = inspect.getsource(__import__("actions.barter_executor",
                                       fromlist=["x"]).barter_commit_verified)
    grey = src.index("not progressed and not tapped")
    branch = src[grey:grey + 700]
    assert '"exhausted": False' in branch
    assert '"blocked": True' in branch
