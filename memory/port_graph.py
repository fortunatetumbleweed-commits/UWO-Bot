# memory/port_graph.py
#
# Port graph derived from the voyage log.
#
# Each directed edge (from_port → to_port) stores:
#   - observed sailing times (seconds)
#   - observation count
#   - last observed datetime
#
# The graph is rebuilt on demand from voyage_log.jsonl — it is never stored
# separately.  "Build once and cache" is fine since the file is small.
#
# Usage:
#   from memory.port_graph import PortGraph
#   g = PortGraph.build()
#   edge = g.get_edge("London", "Lisboa")
#   # {"from": "London", "to": "Lisboa", "count": 3, "mean_s": 1234, "median_s": 1200}
#   g.neighbors("London")   # → ["Lisboa", ...]
#   g.all_ports()           # → ["London", "Lisboa", "Las Palmas", ...]

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from memory.voyage_log import load_legs


@dataclass
class Edge:
    from_port: str
    to_port:   str
    times_s:   list[float] = field(default_factory=list)
    last_at:   Optional[str] = None   # ISO datetime string

    @property
    def count(self) -> int:
        return len(self.times_s)

    @property
    def mean_s(self) -> Optional[float]:
        return statistics.mean(self.times_s) if self.times_s else None

    @property
    def median_s(self) -> Optional[float]:
        return statistics.median(self.times_s) if self.times_s else None

    def to_dict(self) -> dict:
        return {
            "from":      self.from_port,
            "to":        self.to_port,
            "count":     self.count,
            "mean_s":    round(self.mean_s, 1) if self.mean_s is not None else None,
            "median_s":  round(self.median_s, 1) if self.median_s is not None else None,
            "last_at":   self.last_at,
        }


class PortGraph:
    """In-memory graph of measured sailing legs."""

    def __init__(self) -> None:
        self._edges: dict[tuple[str, str], Edge] = {}

    # ── Build ──────────────────────────────────────────────────────────────────

    @classmethod
    def build(cls) -> "PortGraph":
        """Rebuild from the full voyage log (fast — just reads JSONL)."""
        g = cls()
        for leg in load_legs():
            if not leg.get("ok"):
                continue   # failed voyages don't count
            frm = _normalise(leg["from"])
            to  = _normalise(leg["to"])
            key = (frm, to)
            if key not in g._edges:
                g._edges[key] = Edge(frm, to)
            e = g._edges[key]
            e.times_s.append(leg["sailing_time_s"])
            if e.last_at is None or leg["arrived_at"] > e.last_at:
                e.last_at = leg["arrived_at"]
        return g

    # ── Query ──────────────────────────────────────────────────────────────────

    def get_edge(self, from_port: str, to_port: str) -> Optional[dict]:
        """Return edge stats dict, or None if this leg has never been sailed."""
        key = (_normalise(from_port), _normalise(to_port))
        e = self._edges.get(key)
        return e.to_dict() if e else None

    def neighbors(self, port: str) -> list[str]:
        """Return all ports directly reachable from *port* (observed in log)."""
        p = _normalise(port)
        return sorted({to for frm, to in self._edges if frm == p})

    def all_ports(self) -> list[str]:
        """Return every port that appears in any leg."""
        ports: set[str] = set()
        for frm, to in self._edges:
            ports.add(frm)
            ports.add(to)
        return sorted(ports)

    def unvisited_neighbors(self, port: str, candidates: list[str]) -> list[str]:
        """
        Given a list of candidate port names, return those that have no outgoing
        edges from them in the graph — i.e. ports we have never departed from.
        These are detour candidates for the exploration trigger.
        """
        visited_departures = {frm for frm, _ in self._edges}
        return [c for c in candidates if _normalise(c) not in visited_departures]

    def __len__(self) -> int:
        return len(self._edges)

    def summary(self) -> str:
        ports = self.all_ports()
        return (
            f"PortGraph: {len(self._edges)} edges, {len(ports)} ports  "
            f"[{', '.join(ports[:5])}{'…' if len(ports) > 5 else ''}]"
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _normalise(name: str) -> str:
    """Lowercase + strip for consistent dict keys."""
    return name.strip().lower()
