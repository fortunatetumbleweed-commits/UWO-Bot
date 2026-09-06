"""What the bot knows about how THE GAME works, as opposed to how its UI is operated.

The third layer in `docs/one_loop_task_drives_state.md`. It is consulted and never acts.

The distinction it exists for: a dialog asking "Continue?" cannot be answered from UI
mechanics (the state machine does not know a voyage exists) *or* from task context alone —
for the departure Notice, task context argues for the WRONG answer. Only a rule about the
game settles it:

    "Moving to <destination> after Auto Supply. Continue?
     Fleet will immediately set sail if Auto Supply is not possible.
     Sailing can be dangerous with a lack of Food and Water."   [Cancel] [Ok]

Reading that as a supply hazard says Cancel. The rule says otherwise: **OK runs Auto Supply,
which resupplies the fleet**; the warning applies only when Auto Supply is impossible (user,
2026-08-23). See docs/game_mechanics.md → "Departure & Auto Supply".

Rules are matched on the dialog's own words, so they transfer across ports and screens —
unlike a learned tap position, which is worthless the moment the layout shifts.

**Where this is going** (user, 2026-08-23): an unknown dialog will be answered by an LLM given
BOTH the accumulated game knowledge AND the dialog's text, and what it answers becomes a rule
recorded here. `vision/obstruction_consult.py` already has the consult plumbing — it caches by
structural hash and goal — so the seam is: replace the default below with a consult, keep the
red-gem refusal in front of it, and write the answer back as a `DialogRule`.

Until then the default is deliberate and simple: take the positive option unless the dialog
spends red gems. That is a stated interim policy, not an oversight — the cases it gets wrong
are the ones the LLM is meant to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from loguru import logger


@dataclass(frozen=True)
class DialogRule:
    """A known dialog and the answer the game's rules imply."""
    name: str
    phrases: Tuple[str, ...]        # ALL must appear (lower-cased substring match)
    answer: str                     # the option label to choose
    because: str


# Known dialogs. Keep the phrase sets narrow enough that a different dialog cannot match.
DIALOG_RULES: Tuple[DialogRule, ...] = (
    DialogRule(
        name="departure_auto_supply",
        phrases=("auto supply",),
        answer="ok",
        because=("OK runs Auto Supply, which resupplies the fleet before it sails; the "
                 "'immediately set sail' warning applies only when Auto Supply is not "
                 "possible"),
    ),
    DialogRule(
        name="attempt_negotiation",
        phrases=("negotiat",),
        answer="no",
        because=("a haggle GAMBLES the transaction the bot has already committed to, and "
                 "every flow this game has ever run declines it (`_react_after_purchase`: "
                 "'negotiation popup — No'). It is also the only card here whose right "
                 "answer is not the positive one, so the default would say YES to it"),
    ),
)


# Words that mark the option which ADVANCES a flow, when the detector has not said which.
_POSITIVE_WORDS = ("ok", "okay", "confirm", "yes", "continue", "accept", "proceed", "close")

# RED GEMS are premium currency — spending them is real money. `brain/action_executor.py`
# enforces the same discipline on commit buttons via the cost-icon colour; here the only
# evidence available is the dialog's own words, so callers with detector evidence should
# pass `spends_red_gem` explicitly rather than relying on the text match.
_RED_GEM_PHRASES = ("red gem", "red gems", "redgem")


def answer_dialog(options: Sequence[str], text: Sequence[str], *,
                  positive: Optional[str] = None,
                  spends_red_gem: bool = False) -> Optional[str]:
    """The option to choose for this dialog, or None to leave it alone.

    Order: a known game rule first, then the DEFAULT — take the positive option (user,
    2026-08-23: "the default is OK unless it is spending red gem").

    The default lives HERE, in the layer that knows the game, and nowhere else. The state
    machine still refuses to answer a dialog with no decider (`brain.unexpected.resolve`) —
    the moment IT defaults, this layer is decorative and the decision has silently moved
    back down to UI mechanics.

    The one thing the default will not do is spend premium currency: red gems are real
    money, and no flow is worth auto-confirming that.
    """
    if not options:
        return None
    blob = " ".join(str(t).lower() for t in text)

    for rule in DIALOG_RULES:
        if all(p in blob for p in rule.phrases):
            match = next((o for o in options if o.strip().lower() == rule.answer), None)
            if match is None:
                logger.warning(f"[game_rules] {rule.name!r} applies but {rule.answer!r} is "
                               f"not offered (options={list(options)}) — not guessing")
                return None
            logger.info(f"[game_rules] {rule.name}: choosing {match!r} — {rule.because}")
            return match

    if spends_red_gem or any(p in blob for p in _RED_GEM_PHRASES):
        logger.warning("[game_rules] this dialog spends RED GEMS (real money) — refusing to "
                       "answer it automatically; escalate to a human")
        return None

    choice = positive if positive in options else _default_positive(options)
    if choice is None:
        logger.info(f"[game_rules] no rule and no positive option among {list(options)} — "
                    "leaving it alone")
        return None
    logger.info(f"[game_rules] no specific rule — taking the positive option {choice!r}")
    return choice


def _default_positive(options: Sequence[str]) -> Optional[str]:
    """The option that advances the flow, by word. Ordered by how affirmative it is."""
    lowered = {o.strip().lower(): o for o in options}
    for word in _POSITIVE_WORDS:
        if word in lowered:
            return lowered[word]
    return None
