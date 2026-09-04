"""What the fleet holds, kept straight across a market visit.

Two numbers per material, with two different owners (user, 2026-08-26):

    FLEET   what is already in cargo, read from the SELL grid — authoritative, and the
            only place a quantity per good is legible. Owned by the fleet: it sails.
    VISIT   what THIS market session has bought since that reading — pending, because
            nothing has confirmed it against the ship yet. Owned by the building: it
            means nothing once you leave.

    believed = fleet + pending

The cycle the ledger exists to support:

    read the Sell grid          -> fleet = 400, pending = 0        believed 400
    buy 200 (result dialog)     -> fleet = 400, pending = 200      believed 600
    check the right panel       -> it reads 600?  carry on.
                                   it disagrees, or will not read? the READING is
                                   suspect, not the ledger — go and look properly:
    re-read the Sell grid       -> fleet = 600, pending = 0        believed 600

The reset is the point. Once an authoritative read has ABSORBED the pending purchases,
keeping them would double-count. And a disagreement is never evidence the buy failed —
the result dialog already proved it happened (CLAUDE.md); it is evidence the cheap read
is wrong, which is what sends the bot to the panel that can answer.

WHY NOT JUST RE-READ EVERY ROUND: the Sell grid is a tab switch away and must be scrolled
to find a good whose tile is below the fold. The pending count is what makes the expensive
read occasional rather than per-round — and what lets the loop stop on "I have bought
enough" even when no reading of the hold succeeds at all.

MULTIPLE MATERIALS PER MARKET: one port can supply several (Barcelona sells both Iron and
Matchlock Gun), so every operation is per-good and a reconcile absorbs all of them at once —
one trip to the Sell grid answers for every material bought there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from loguru import logger


@dataclass
class MarketLedger:
    """Per-good `fleet` (confirmed) and `pending` (bought this visit, unconfirmed)."""

    fleet: Dict[str, int] = field(default_factory=dict)
    pending: Dict[str, int] = field(default_factory=dict)
    # Goods bought this visit whose AMOUNT could not be read. `believed()` cannot count them,
    # which is the point: it must not invent a number, only report that one is outstanding.
    unknown: set = field(default_factory=set)

    # ── the authoritative read ───────────────────────────────────────────────
    def seed(self, counts: Mapping[str, int]) -> None:
        """Take a Sell-grid reading as the fleet's holdings. Does NOT clear pending —
        seeding is for the FIRST read of a visit, before anything has been bought."""
        self.fleet = {_key(g): int(q) for g, q in (counts or {}).items()}
        logger.info(f"[ledger] seeded from the sell grid: {self.fleet}")

    def reconcile(self, counts: Mapping[str, int]) -> None:
        """A fresh Sell-grid reading, taken AFTER buying: it already includes the purchases,
        so the pending counts are absorbed and reset. Keeping them would double-count."""
        absorbed = dict(self.pending)
        unknown = sorted(self.unknown)
        self.fleet = {_key(g): int(q) for g, q in (counts or {}).items()}
        self.pending = {}
        self.unknown = set()
        logger.info(f"[ledger] reconciled against the sell grid: {self.fleet} "
                    f"(absorbed pending {absorbed}"
                    + (f", and {unknown} whose amount was never read)" if unknown else ")"))

    # ── the cheap side ───────────────────────────────────────────────────────
    def bought(self, good: str, qty: Optional[int] = None) -> None:
        """A CONFIRMED purchase — the result dialog said so. Pending, until a Sell-grid
        reading absorbs it.

        `qty=None` (or 0) means the AMOUNT could not be read, and that is not the same as
        nothing being bought — it is the case this ledger exists for. The purchase is still
        recorded as pending so `has_pending()` sends the caller to the sell grid, which can
        say what it added up to. Refusing to record it is how the loop came to stop blind
        exactly when it had most reason to go and look (2026-08-26).
        """
        k = _key(good)
        # A NEGATIVE PURCHASE IS IMPOSSIBLE, SO IT IS A BAD READING (user, 2026-08-31: "it is
        # impossible that a quantity is negative... the screen's number is correct, it is not
        # negative"). Buying never removes goods, so a delta below zero says the two readings
        # were of different things — not that the hold shrank.
        #
        # This is the rule already stated in CLAUDE.md — "the count is a separate reading, and
        # disagreement means the READING is wrong" — applied to the one case that can be
        # proved wrong by arithmetic alone.
        #
        # Live 2026-08-31 at Bordeaux: `bought -543 Raisin — believed 161 (fleet 704 +
        # pending -543)`. The loop then read its own accounting as going backwards, stopped
        # with "no further progress", and walked out of the market leaving a staged cart —
        # which raised the "moving to another menu will empty the cart" dialog and wedged the
        # run. One impossible number, three failures downstream.
        #
        # UNREADABLE, not zero and not negative: fall through to the pending/unknown path,
        # which is exactly what it is for — it sends the caller to the sell grid to find out.
        if qty is not None and int(qty) < 0:
            logger.warning(f"[ledger] a purchase of {qty} {good} is impossible — buying does "
                           "not remove goods, so the READING is wrong. Recording it as "
                           "unreadable and letting the sell grid settle it.")
            qty = None
        if not qty:
            self.pending.setdefault(k, 0)
            self.unknown.add(k)
            logger.info(f"[ledger] bought {good}, amount unreadable — pending, and the sell "
                        "grid is what can settle it")
            return
        self.pending[k] = self.pending.get(k, 0) + int(qty)
        logger.info(f"[ledger] bought {qty} {good} — believed {self.believed(good)} "
                    f"(fleet {self.fleet.get(k, 0)} + pending {self.pending[k]})")

    def believed(self, good: str) -> int:
        k = _key(good)
        return self.fleet.get(k, 0) + self.pending.get(k, 0)

    def has_pending(self) -> bool:
        """True when anything bought this visit is unconfirmed — INCLUDING a purchase whose
        amount could not be read, which is the case most worth going to look at."""
        return bool(self.unknown) or any(v > 0 for v in self.pending.values())

    def amount_unknown(self, good: str) -> bool:
        return _key(good) in self.unknown

    def pending_goods(self) -> list:   # noqa: D401
        """Every good bought this visit and not yet confirmed — a reconcile answers for all
        of them in one trip, which is why the caller asks rather than going good by good."""
        return sorted({g for g, v in self.pending.items() if v > 0} | self.unknown)

    # ── the check that decides whether to go and look ────────────────────────
    def disagrees(self, good: str, observed: Optional[int], *, tolerance: int = 0) -> bool:
        """True when a cheap reading contradicts the ledger and the Sell grid should be
        re-read.

        `observed is None` — the read FAILED, which is not a disagreement and not a zero. It
        is no answer, and the ledger stands. Treating an unreadable panel as `0` is how a
        loop came to report `0/470` while carrying 1,300 (2026-08-26).
        """
        if observed is None:
            return False
        gap = abs(int(observed) - self.believed(good))
        if gap > tolerance:
            logger.info(f"[ledger] the panel says {observed} {good} and the ledger believes "
                        f"{self.believed(good)} — the READING is what to doubt; re-reading "
                        "the sell grid")
            return True
        return False


def _key(good: str) -> str:
    return (good or "").strip().lower()
