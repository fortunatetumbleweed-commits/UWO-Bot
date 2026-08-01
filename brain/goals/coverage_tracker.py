"""Coverage tracker — 2D visited-cell grid + STUCK verdict.

See `docs/destination_generator_design.md` § "Phase 3 plan" for the
empirical justification of the current STUCK predicate (net
displacement over a time window — exactly Nav2's `ProgressChecker`).
The earlier "no new cells in N ticks" predicate (Phase 2) conflated
productive slow exploration with stuck oscillation; net displacement
correctly distinguishes them.

In short: the bot's trajectory in lat/lon is recorded into a rolling
window.  STUCK fires when the bot's net displacement across the
window is below a threshold AND no new cell was entered.  Frontier
identification (cells adjacent to visited cells but unvisited
themselves) is exposed for FrontierPicker but not computed every
tick — it's evaluated lazily on `frontiers()`.

The tracker is pure logic — no perception, no I/O.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional, Set, Tuple


CellIdx = Tuple[int, int]    # (i, j) integer cell coordinates


class Verdict(str, Enum):
    """Per-tick coverage verdict.

    `MAKING_PROGRESS`: this tick's position is in a cell never
        before visited.  The visited set just grew — coverage is
        expanding regardless of displacement magnitude.
    `KEEP_FOLLOWING`: we're in an already-visited cell but the net
        displacement over the window is above threshold — the bot
        is in transit (e.g. backtracking through known area to
        reach new ground).
    `STUCK`: in an already-visited cell AND net displacement over
        the window is below threshold.  The bot is oscillating in
        a bounded region.  Trigger for FrontierPicker.
    """
    MAKING_PROGRESS = "making_progress"
    KEEP_FOLLOWING  = "keep_following"
    STUCK           = "stuck"


# ── Tunables ────────────────────────────────────────────────────────────────

# Cell size in degrees.  0.1° ≈ 6 nm at the equator — about the
# distance a ship covers in one game-minute at cruise speed.  Cell
# size only affects MAKING_PROGRESS granularity now (Phase 3a moved
# the STUCK check off cell counts onto net displacement); smaller
# cells produce more MAKING_PROGRESS verdicts but no longer mask
# stuck states.
DEFAULT_CELL_SIZE_DEG = 0.1

# Window size for the displacement check, in ticks.  At the current
# ~1-second capture cadence this is ~30 wall-seconds.
DEFAULT_STUCK_WINDOW_TICKS = 30

# Minimum net displacement (lat/lon delta euclidean) over the window
# for the bot to *not* be considered stuck.  Calibrated against the
# Cairo→endpoint voyage trace: legitimate exploration descended ~1.5°
# over 30 ticks; backtracking covered ~0.9°; oscillation stayed
# under 0.3°.  See docs/destination_generator_design.md § 3a.
DEFAULT_MIN_DISPLACEMENT_DEG = 0.3


# ── Tracker ─────────────────────────────────────────────────────────────────

@dataclass
class CoverageTracker:
    """Pure 2D coverage tracker.

    Construction:
        CoverageTracker()  # defaults
        CoverageTracker(cell_size_deg=0.05, stuck_window_ticks=20)

    Per tick:
        verdict = tracker.record(tick, lat, lon)

    Frontier query (for FrontierPicker):
        cells = tracker.frontiers()
    """
    cell_size_deg:        float = DEFAULT_CELL_SIZE_DEG
    stuck_window_ticks:   int   = DEFAULT_STUCK_WINDOW_TICKS
    min_displacement_deg: float = DEFAULT_MIN_DISPLACEMENT_DEG

    visited:               Set[CellIdx] = field(default_factory=set)
    last_recorded_tick:    Optional[int] = field(default=None)
    # Rolling window of recent (tick, lat, lon) samples for the
    # net-displacement check.  Trimmed each tick so the oldest
    # sample is at most `stuck_window_ticks` behind the newest.
    _recent_window: Deque[Tuple[int, float, float]] = field(
        default_factory=deque)

    def record(self, tick: int, lat: float, lon: float) -> Verdict:
        """Push this tick's position and return the verdict."""
        cell = self._cell_of(lat, lon)
        self.last_recorded_tick = tick

        # Maintain the rolling sample window.  Trim from the left
        # while the oldest sample is outside the window.
        self._recent_window.append((tick, lat, lon))
        while (self._recent_window
                and tick - self._recent_window[0][0] > self.stuck_window_ticks):
            self._recent_window.popleft()

        # Cell tracking — orthogonal to STUCK; drives MAKING_PROGRESS
        # and feeds frontier identification.
        was_new_cell = cell not in self.visited
        if was_new_cell:
            self.visited.add(cell)
            return Verdict.MAKING_PROGRESS

        # In an already-visited cell.  STUCK iff the bot's net
        # displacement across the window is below threshold AND we
        # have a full window of samples.  Partial window = not enough
        # evidence; defer STUCK.
        if len(self._recent_window) >= self.stuck_window_ticks:
            _t0, lat0, lon0 = self._recent_window[0]
            disp = math.hypot(lat - lat0, lon - lon0)
            if disp < self.min_displacement_deg:
                return Verdict.STUCK
        return Verdict.KEEP_FOLLOWING

    def frontiers(self) -> Set[CellIdx]:
        """Cells adjacent (4-neighbour) to a visited cell but not
        themselves visited.  Computed lazily; not cached.

        Used by FrontierPicker to score candidate exploration targets.
        Returns an empty set when no cells are visited yet.
        """
        out: Set[CellIdx] = set()
        for (i, j) in self.visited:
            for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                neighbour = (i + di, j + dj)
                if neighbour not in self.visited:
                    out.add(neighbour)
        return out

    @property
    def visited_count(self) -> int:
        return len(self.visited)

    @property
    def frontier_count(self) -> int:
        return len(self.frontiers())

    def cell_center(self, cell: CellIdx) -> Tuple[float, float]:
        """Lat/lon at the center of a grid cell.  Used by callers
        (FrontierPicker) that need a steerable target point from a
        cell index."""
        i, j = cell
        return ((i + 0.5) * self.cell_size_deg,
                (j + 0.5) * self.cell_size_deg)

    def _cell_of(self, lat: float, lon: float) -> CellIdx:
        # Integer floor gives us the grid index regardless of sign.
        return (math.floor(lat / self.cell_size_deg),
                math.floor(lon / self.cell_size_deg))
