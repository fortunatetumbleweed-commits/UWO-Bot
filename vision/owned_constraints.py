"""The hold cannot hold more than it holds — so arithmetic can veto a misreading.

Two readers disagree about how many of a good are aboard. Rather than pick by
confidence (which is not comparable across readers: the badge specialist scored 0.6519
on a WRONG value and 0.6539 on a correct one), check each candidate against what the
ship can physically contain. The Cargo bar sits on the same screen as the goods grid,
so this costs nothing — no extra capture, no extra read.

Live 2026-08-27 at Barcelona, the sell page read:

    Iron 2,099  +  Candle 2,148            = 4,247  >  3,040 held   -> impossible
    Iron 2,099  +  Candle   148  (+793 sup) = 3,040  =  3,040 held   -> exact

The per-good bound alone does NOT catch it — 2,148 is less than the 3,040 held. It is
the SUM that is impossible, which is why the set is checked as a whole and not each
reading in isolation.

Note what this does not do: it never decides WHICH reader is right in general, only
which combination of their answers the ship could actually be carrying. When the
constraint does not bind, it says so and leaves the choice to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Dict, Mapping, Optional, Sequence

from loguru import logger


@dataclass(frozen=True)
class Verdict:
    """`values` is the chosen quantity per good, `exact` when the set lands on the cargo
    total to the unit, and `unresolved` names goods the constraint could not settle."""
    values: Dict[str, int]
    exact: bool
    unresolved: tuple
    reason: str


def reconcile_owned(candidates: Mapping[str, Sequence[int]],
                    cargo_used: int,
                    cargo_capacity: int,
                    supplies: Optional[int] = None) -> Verdict:
    """Choose each good's quantity from its candidate readings using the hold's arithmetic.

    `candidates` maps good -> the distinct values its readers proposed, best-supported
    first (ties are broken by that order, so the caller's preference survives when the
    constraint does not bind). `supplies` is water+food when known: the sell grid does not
    list them, so without it only the INEQUALITY can be checked, not the exact total.
    """
    if cargo_used <= 0 or cargo_capacity <= 0:
        return Verdict({g: v[0] for g, v in candidates.items() if v}, False, (),
                       "cargo bar unknown — no constraint applied")

    # Per-good bound first: no single good can exceed the whole hold. Cheap, and it
    # shrinks the combinations before the set check.
    viable, dropped = {}, []
    for good, vals in candidates.items():
        keep = [v for v in vals if 0 <= v <= cargo_used]
        for v in vals:
            if v not in keep:
                dropped.append(f"{good}={v}")
        viable[good] = keep or list(vals)      # never leave a good with no candidate at all

    goods = [g for g in viable if viable[g]]
    combos = [dict(zip(goods, pick)) for pick in product(*(viable[g] for g in goods))]
    fits = [c for c in combos if sum(c.values()) <= cargo_used]

    if supplies is not None:
        exact = [c for c in fits if sum(c.values()) + int(supplies) == cargo_used]
        if len(exact) == 1:
            _log_drop(dropped)
            return Verdict(exact[0], True, (),
                           f"the set sums to the cargo total exactly ({cargo_used})")
        if len(exact) > 1:
            logger.info(f"[owned] {len(exact)} candidate sets hit {cargo_used} exactly — "
                        "the total cannot separate them")

    if not fits:
        _log_drop(dropped)
        return Verdict({g: viable[g][0] for g in goods}, False, tuple(goods),
                       f"no combination fits {cargo_used} — every reading is suspect")

    if len(fits) == 1:
        _log_drop(dropped)
        return Verdict(fits[0], False, (),
                       f"only one combination fits within {cargo_used}")

    # More than one combination is possible: the constraint does not bind. Take each
    # good's best-supported candidate and report which ones it could not settle.
    chosen = {g: viable[g][0] for g in goods}
    unresolved = tuple(g for g in goods if len({c[g] for c in fits}) > 1)
    _log_drop(dropped)
    return Verdict(chosen, False, unresolved,
                   f"{len(fits)} combinations fit {cargo_used} — constraint does not bind")


def _log_drop(dropped) -> None:
    if dropped:
        logger.info(f"[owned] impossible readings dropped (exceed the hold): "
                    f"{', '.join(dropped)}")
