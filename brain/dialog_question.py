"""Ask about a dialog by DESCRIBING it — and let its buttons be the answer space.

The shape of the ask (user, 2026-08-26):

    I see a dialog, it has this title, this body text, and an OK button, and a Cancel
    button, what should I do.

Enumerating the buttons is what makes the answer safe, and it does it through the SHAPE OF
THE QUESTION rather than by filtering afterwards. A model told "there is [Cancel] and [OK]"
answers with one of them. Nothing here has to parse prose, guess which OK was meant, or
judge whether an instruction is executable — the affordances were the offer.

    I am in the HARBOR at Lisboa. My goal is to set sail for Amsterdam.
    The Depart button was inactive, so I tapped Recruit Crew.

    A dialog has appeared:
      title:   "Notice"
      body:    "Recruit Crew? 181,224 ducats will be spent."
      buttons: [Cancel] [OK]

    What should I do?

NO SCREENSHOT. A structured description is what a model can reason about, it survives a
re-skin, and it can key what was learned. Asking about a picture produces a description of
a picture — which is how `memory/knowledge/fsm/learned_recoveries.json` came to hold
eighteen entries, eleven of them the same crew blocker taught over again, one keyed on the
literal string '181,224' (the cost of one particular recruitment, which can never match a
second time).

Design: docs/architecture_DRAFT.md, "Two kinds of unknown, learned separately".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Tuple

from loguru import logger

from vision.region_detectors.dialog import DialogAction, DialogModel


# The first of the three layers: what the game IS. Without it a model reads "Recruit Crew?
# 181,224 ducats will be spent" as a large unexplained purchase; with it, as the ordinary
# precondition for sailing.
GAME = (
    "This is Uncharted Waters Origin, an Android trading game. A fleet sails between ports "
    "buying and selling trade goods for ducats. A fleet cannot depart without enough crew "
    "or enough supplies; crew are hired at the Harbor or the Inn, supplies are bought at "
    "the Market. Spending ducats to make a voyage possible is normal and expected."
)


@dataclass(frozen=True)
class Situation:
    """The other two layers: where the bot is with what goal, and what it has already done."""
    place: str                              # "the HARBOR at Lisboa"
    goal:  str                              # "set sail for Amsterdam"
    tried: Tuple[str, ...] = ()             # "The Depart button was inactive, so I tapped …"


# THE THREE THINGS A DIALOG IS, and the whole of what gets sent about it. Everything below
# works from this triple, so a dialog read by `vision.region_detectors.dialog` and one read
# by `brain.unexpected` ask the SAME question — there is no second phrasing to keep in sync.

def describe(title: Optional[str], body: Sequence[str], labels: Sequence[str]) -> str:
    """The dialog as three labelled lines."""
    body_text = " ".join(t.strip() for t in body if t and t.strip()) or "(no body text)"
    buttons = " ".join(f"[{l}]" for l in labels) or "(no buttons — only a close X)"
    return (f'  title:   "{title or "(no title)"}"\n'
            f'  body:    "{body_text}"\n'
            f"  buttons: {buttons}")


def question(title: Optional[str], body: Sequence[str], labels: Sequence[str],
             situation: Situation) -> str:
    """The full three-layer ask, ending in the only question worth asking."""
    lines = [GAME, "",
             f"I am in {situation.place}. My goal is to {situation.goal}."]
    lines.extend(situation.tried)
    lines += ["", "A dialog has appeared:", describe(title, body, labels), ""]
    if labels:
        lines.append("Reply with EXACTLY ONE of these button labels and nothing else: "
                     + ", ".join(labels) + ".")
    lines.append("What should I do?")
    return "\n".join(lines)


# A spend the user has ruled out in advance, and the one thing no answer may authorise.
# Blue gems are earned through investment and ordinary play, so they are not special; red
# gems are bought with real money (user, 2026-08-25). This is a POLICY about acting, so it
# lives at the point of choosing, not in the question — a model must not be able to talk
# its way past it, and asking it to respect a rule is not the same as it not being able to
# break one.
_NEVER_SPEND = ("red gem", "red gems")


def _spends_red_gems(title: Optional[str], body: Sequence[str]) -> bool:
    text = " ".join([title or "", *(t or "" for t in body)]).lower()
    return any(term in text for term in _NEVER_SPEND)


def ask_which_button(title: Optional[str], body: Sequence[str], labels: Sequence[str],
                     situation: Situation,
                     ask: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """The core: ask about a dialog, return ONE offered label — or None.

    None means "do not act": either the answer named no button, or named two, or the ask
    failed, or the spend is one no answer may authorise. A dialog is a fork in the world;
    picking the wrong branch confidently is worse than not picking.
    """
    if not labels:
        # Nothing to choose between. A dialog with only a close-X is an OBSTRUCTION — the
        # dispatcher clears it while perceiving and never needs to ask about it.
        return None

    if _spends_red_gems(title, body):
        logger.warning("[ask] dialog mentions red gems — not answering; this spend is the "
                       "user's alone to authorise")
        return None

    if ask is None:
        from brain.llm_client import claude_llm_fn as ask

    prompt = question(title, body, labels, situation)
    logger.info("[ask] asking about a dialog:\n" + prompt)
    try:
        reply = (ask(prompt) or "").strip()
    except Exception as e:
        logger.warning(f"[ask] could not ask: {e}")
        return None

    return match(reply, labels)


def choose(dialog: DialogModel,
           situation: Situation,
           ask: Optional[Callable[[str], str]] = None) -> Optional[DialogAction]:
    """As `ask_which_button`, but for a DialogModel — returns the ACTION, bbox and all, so
    the answer round-trips to a tap without a second lookup."""
    labels = [a.label for a in dialog.actions]
    picked = ask_which_button(dialog.title_bar.text if dialog.title_bar else None,
                              dialog.body_text, labels, situation, ask)
    return next((a for a in dialog.actions if a.label == picked), None)


def decider(situation: Situation, ask: Optional[Callable[[str], str]] = None):
    """The `decide` argument `brain.unexpected.resolve` asks for.

    `unexpected.look` classifies an ACTION_DIALOG and then refuses to answer it — "the whole
    point of case 2 is that the decision is not the state machine's to make". This is who
    makes it. Returns a callable Unexpected -> label or None.
    """
    def decide(u) -> Optional[str]:
        return ask_which_button(getattr(u, "title", None), u.text, u.options, situation, ask)
    return decide


def match(reply: str, labels: Sequence[str]) -> Optional[str]:
    """Resolve a reply to one of the offered buttons, or to nothing.

    Named separately because this is the boundary the whole design rests on: whatever comes
    back, only a button on THIS dialog can leave here.
    """
    said = (reply or "").strip().lower()
    if not said:
        return None

    for l in labels:                          # an exact answer, which is what was asked for
        if said == l.strip().lower():
            return l

    # Lenient second pass: a label quoted inside a sentence still counts, but ONLY if
    # exactly one label appears. "OK" and "Cancel" both present means the reply discussed
    # the choice instead of making it.
    hits = [l for l in labels if l.strip().lower() in said]
    if len(hits) == 1:
        logger.info(f"[ask] read {reply!r} as {hits[0]!r}")
        return hits[0]

    logger.warning(f"[ask] {reply!r} names {'no' if not hits else 'more than one'} button "
                   f"of {list(labels)} — not acting on it")
    return None
