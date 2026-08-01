# brain/route_planner.py
#
# Finds the best trade route from A to B using Dijkstra on the port graph.
#
# Edge weight = estimated sailing time / expected profit score
#   lower weight = better route (faster AND more profitable)
#
# Expected profit uses a two-layer estimate:
#   Layer 1 (prior): trade_priors.leg_profit_score — specialties, distance,
#                    port preferences.  Always available, no data needed.
#   Layer 2 (observed): real ducats/hour from voyage_log, averaged per leg.
#                    Overrides the prior once enough observations exist.
#
# Exploration: unvisited ports get a 15% sailing-time discount, making
# Dijkstra route through them occasionally without needing a separate UCB loop.
# The discount fades once the port has been visited (≥2 times).
#
# Usage:
#   from brain.route_planner import plan_route, score_leg
#
#   waypoints = plan_route("London", "Port Royal")
#   # → ["london", "las palmas", "port royal"]  (or a longer path if needed)

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from memory.port_graph import PortGraph, _normalise
from memory.trade_priors import leg_profit_score
from memory.voyage_log import load_legs, load_rounds


# ── Constants ──────────────────────────────────────────────────────────────────

# Discount applied to unvisited nodes (makes Dijkstra prefer routing through them)
_EXPLORE_DISCOUNT = 0.85          # 15% time discount for unvisited port
_EXPLORE_MIN_VISITS = 2           # discount fades once port seen this many times
_FALLBACK_SAILING_S = 2_400.0     # 40-min default for edges not yet in graph
_MIN_PROFIT_SCORE = 0.01          # prevent divide-by-zero


# ── Public API ─────────────────────────────────────────────────────────────────

def plan_route(start: str, end: str) -> list[str]:
    """
    Return the recommended sequence of ports from start to end (inclusive),
    as normalised lowercase strings.

    Uses Dijkstra on the port graph with exploration discounts for unvisited ports.
    Falls back to [start, end] if no graph path exists and they are directly
    sailable (relies on sail_to_port's world-map search).
    """
    graph   = PortGraph.build()
    visited = _visit_counts(graph)

    path = _dijkstra(graph, _normalise(start), _normalise(end), visited)

    if path:
        logger.info(
            f"  [planner] {start}→{end}: "
            + " → ".join(path)
            + f"  ({len(path)-1} hop(s))"
        )
        return path

    # Graph has no path yet (empty or disconnected) — direct sail
    logger.debug(f"  [planner] No graph path {start}→{end} — direct sail")
    return [_normalise(start), _normalise(end)]


def score_leg(from_port: str, to_port: str) -> dict:
    """
    Return a breakdown of the expected score for one leg.
    Useful for debugging and the supervisor UI.
    """
    graph       = PortGraph.build()
    prior       = leg_profit_score(from_port, to_port)
    edge        = graph.get_edge(from_port, to_port)
    obs_dph     = _observed_dph(from_port, to_port)
    sailing_s   = edge["median_s"] if edge else _FALLBACK_SAILING_S
    weight      = _edge_weight(sailing_s, prior, obs_dph)
    return {
        "from":          from_port,
        "to":            to_port,
        "prior_score":   round(prior, 3),
        "obs_dph":       round(obs_dph, 0) if obs_dph else None,
        "edge_count":    edge["count"] if edge else 0,
        "sailing_s":     sailing_s,
        "weight":        round(weight, 2),
    }


# ── Dijkstra ───────────────────────────────────────────────────────────────────

def _dijkstra(
    graph: PortGraph,
    start: str,
    end: str,
    visited: dict[str, int],
) -> list[str]:
    """
    Standard Dijkstra with edge weight = sailing_time / profit_score.
    Unvisited nodes get a sailing-time discount to encourage exploration.
    Returns ordered port list [start, ..., end] or [] if unreachable.
    """
    dist: dict[str, float] = {start: 0.0}
    prev: dict[str, Optional[str]] = {start: None}
    heap: list[tuple[float, str]] = [(0.0, start)]

    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        if u == end:
            return _reconstruct(prev, end)

        for v in graph.neighbors(u):
            edge    = graph.get_edge(u, v)
            sail_s  = edge["median_s"] if edge and edge["median_s"] else _FALLBACK_SAILING_S
            prior   = leg_profit_score(u, v)
            obs_dph = _observed_dph(u, v)
            w       = _edge_weight(sail_s, prior, obs_dph)

            # Exploration discount for under-visited destination nodes
            if visited.get(v, 0) < _EXPLORE_MIN_VISITS:
                w *= _EXPLORE_DISCOUNT

            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))

    return []   # unreachable


def _edge_weight(sailing_s: float, prior: float, obs_dph: Optional[float]) -> float:
    """
    Convert sailing time and profit estimate to a Dijkstra cost (lower = better).

    weight = sailing_time_s / effective_profit_score

    effective_profit_score blends prior and observed data:
      - No observations: use prior only
      - Some observations: weighted blend (more observations → less prior weight)
      - Many observations (≥5): use observed ducats/hour directly, normalised
    """
    if obs_dph is not None and obs_dph > 0:
        # Normalise observed dph to the same scale as prior (prior ≈ 1–4)
        # 10_000 duc/hr → score ≈ 1.0 (rough calibration)
        obs_score = obs_dph / 10_000.0
        profit_score = max(obs_score, _MIN_PROFIT_SCORE)
    else:
        profit_score = max(prior, _MIN_PROFIT_SCORE)

    return sailing_s / profit_score


def _reconstruct(prev: dict[str, Optional[str]], end: str) -> list[str]:
    path = []
    node: Optional[str] = end
    while node is not None:
        path.append(node)
        node = prev.get(node)
    path.reverse()
    return path


# ── Helpers ────────────────────────────────────────────────────────────────────

def _visit_counts(graph: PortGraph) -> dict[str, int]:
    """Count how many times each port has been a departure point in the voyage log."""
    counts: dict[str, int] = {}
    for leg in load_legs():
        if leg.get("ok"):
            p = _normalise(leg["from"])
            counts[p] = counts.get(p, 0) + 1
    return counts


def _observed_dph(from_port: str, to_port: str) -> Optional[float]:
    """
    Mean ducats/hour observed for rounds that used this specific leg.
    Returns None if no data.
    """
    fp = _normalise(from_port)
    tp = _normalise(to_port)
    values = []
    for r in load_rounds():
        ports = [_normalise(p) for p in r.get("ports", [])]
        dph   = r.get("ducats_per_hour")
        if dph and dph > 0:
            # Check if this leg appears consecutively in the round
            for i in range(len(ports) - 1):
                if ports[i] == fp and ports[i + 1] == tp:
                    values.append(dph)
                    break
    return sum(values) / len(values) if values else None
