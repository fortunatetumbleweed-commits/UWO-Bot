"""Centerline-based WorldMap — Piece A.

A SLAM-style accumulator that turns per-tick M6f local trees into a
growing planar graph in world (lat, lon) coordinates.  The structure
is mode-agnostic: rivers, coasts, lakes, and bays all populate the
same `nodes + edges` shape.  What varies per mode is the upstream
corridor mask (out of scope here — see docs/centerline_world_map.md).

Piece A scope:
  - Accept a per-tick local tree + ship pose (lat, lon, ship_xy).
  - Convert tree's STABLE node kinds (junction, dead_end) into world
    coords.  Frame-edge anchors are observer-relative, NOT stored.
  - Merge against existing world nodes by nearest-neighbour-within-
    threshold (MERGE_THRESHOLD_KM).
  - Track an EWMA position update per re-observation.
  - Maintain edges between consecutive bot positions plus edges
    implied by the local tree's topology.

NOT in scope here:
  - Using the map for navigation decisions (Piece B).
  - Loop closure / bot-position correction from observations.
  - Multi-edge / cycle support (islands).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# ── Tuneables ──────────────────────────────────────────────────────────

PX_PER_KM = 1.5            # Minimap scale.  Approximate; see
                           # docs/centerline_world_map.md for the
                           # calibration discussion.  Merge threshold
                           # is generous enough to tolerate ±50% error.
MERGE_THRESHOLD_KM = 3.0
EWMA_ALPHA = 0.3           # weight for new observation in position update

KM_PER_DEG_LAT = 111.0


def _km_between(lat1: float, lon1: float,
               lat2: float, lon2: float) -> float:
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * KM_PER_DEG_LAT
    dx = (lon2 - lon1) * KM_PER_DEG_LAT * cos_lat
    return math.hypot(dy, dx)


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """World bearing from point 1 to point 2 in degrees (0=N, 90=E, CW)."""
    cos_lat = math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * KM_PER_DEG_LAT
    dx = (lon2 - lon1) * KM_PER_DEG_LAT * cos_lat
    return math.degrees(math.atan2(dx, dy)) % 360.0


# ── Pixel → world transform ────────────────────────────────────────────


def pixel_to_world(py: int, px: int,
                  ship_lat: float, ship_lon: float,
                  ship_px: int, ship_py: int,
                  px_per_km: float = PX_PER_KM
                  ) -> tuple[float, float]:
    """Convert a minimap pixel (py, px) to world (lat, lon).

    The minimap is centered on the ship.  Positive y in image is south
    (decreasing lat); positive x is east.
    """
    cos_lat = math.cos(math.radians(ship_lat))
    km_per_px = 1.0 / px_per_km
    dlat = -(py - ship_py) * km_per_px / KM_PER_DEG_LAT
    dlon = (px - ship_px) * km_per_px / (KM_PER_DEG_LAT * cos_lat)
    return ship_lat + dlat, ship_lon + dlon


# ── Data structures ────────────────────────────────────────────────────


@dataclass
class WorldNode:
    id: str
    kind: str             # "junction" | "dead_end" | other mode-specific
    lat: float
    lon: float
    mode: str             # "river" | "coast" | "lake" | "open"
    n_observations: int = 1
    first_seen_tick: int = 0
    last_seen_tick: int = 0


@dataclass
class WorldEdge:
    a: str                # WorldNode id
    b: str                # WorldNode id
    mode: str
    n_observations: int = 1


@dataclass
class BotState:
    """The bot's persistent navigation commitment."""
    target_node_id: Optional[str] = None    # which WorldNode are we heading to
    last_target_id: Optional[str] = None    # node we just departed (don't backtrack)


@dataclass
class WorldMap:
    nodes: dict[str, WorldNode] = field(default_factory=dict)
    edges: list[WorldEdge] = field(default_factory=list)
    bot_path: list[tuple[float, float]] = field(default_factory=list)
    bot_state: BotState = field(default_factory=BotState)
    _next_id: int = 0

    # ── Node accessors ────────────────────────────────────────────────

    def _new_id(self) -> str:
        nid = f"W{self._next_id}"
        self._next_id += 1
        return nid

    def find_nearest(self, lat: float, lon: float,
                    max_km: float = MERGE_THRESHOLD_KM
                    ) -> Optional[WorldNode]:
        best: Optional[WorldNode] = None
        best_d = max_km
        for n in self.nodes.values():
            d = _km_between(lat, lon, n.lat, n.lon)
            if d < best_d:
                best_d = d
                best = n
        return best

    def _add_or_merge_node(self, lat: float, lon: float, kind: str,
                          mode: str, tick: int) -> WorldNode:
        existing = self.find_nearest(lat, lon)
        if existing is not None and existing.kind == kind \
                and existing.mode == mode:
            existing.lat = (1 - EWMA_ALPHA) * existing.lat + EWMA_ALPHA * lat
            existing.lon = (1 - EWMA_ALPHA) * existing.lon + EWMA_ALPHA * lon
            existing.n_observations += 1
            existing.last_seen_tick = tick
            return existing
        node = WorldNode(
            id=self._new_id(), kind=kind, lat=lat, lon=lon,
            mode=mode, first_seen_tick=tick, last_seen_tick=tick,
        )
        self.nodes[node.id] = node
        return node

    def _add_edge_if_new(self, a: str, b: str, mode: str):
        if a == b: return
        key = tuple(sorted((a, b)))
        for e in self.edges:
            if tuple(sorted((e.a, e.b))) == key:
                e.n_observations += 1
                return
        self.edges.append(WorldEdge(a=a, b=b, mode=mode))

    def neighbors(self, node_id: str) -> list[WorldNode]:
        out = []
        for e in self.edges:
            if e.a == node_id: out.append(self.nodes[e.b])
            elif e.b == node_id: out.append(self.nodes[e.a])
        return out

    # ── Target selection / arrival ────────────────────────────────────

    def select_target(self, sim_lat: float, sim_lon: float,
                     goal_lat: float, goal_lon: float,
                     min_obs: int = 3) -> Optional[WorldNode]:
        """Pick a stable world node ahead of the bot, toward the goal.

        "Ahead" = the bot-to-node bearing should be within ±90° of the
        bot-to-goal bearing.  Among forward candidates, prefer:
          (a) seen at least `min_obs` times (real, not transient)
          (b) closest to the bot (so we commit to the next nearby landmark,
              not the final goal directly)
          (c) NOT the node we just departed (avoid trivial backtrack)
        """
        goal_bearing = _bearing(sim_lat, sim_lon, goal_lat, goal_lon)
        best: Optional[WorldNode] = None
        best_d = float("inf")
        for n in self.nodes.values():
            if n.id == self.bot_state.last_target_id:
                continue
            if n.n_observations < min_obs:
                continue
            bear = _bearing(sim_lat, sim_lon, n.lat, n.lon)
            diff = abs(((bear - goal_bearing + 180) % 360) - 180)
            if diff > 90:
                continue   # this node is behind / sideways
            d = _km_between(sim_lat, sim_lon, n.lat, n.lon)
            if d < best_d:
                best_d = d
                best = n
        return best

    def arrived_at_target(self, sim_lat: float, sim_lon: float,
                         arrival_km: float = 3.0) -> bool:
        if self.bot_state.target_node_id is None:
            return False
        target = self.nodes.get(self.bot_state.target_node_id)
        if target is None:
            return False
        return _km_between(sim_lat, sim_lon,
                          target.lat, target.lon) < arrival_km

    # ── Per-tick integration ──────────────────────────────────────────

    def integrate(self, local_tree, ship_lat: float, ship_lon: float,
                 ship_xy: tuple[int, int], tick: int,
                 mode: str = "river") -> dict:
        """Merge a per-tick local tree into the world map.

        local_tree: object with .nodes (dict id → TreeNode w/ kind, y, x)
                    and .edges (list of TreeEdge w/ a, b, points).
        ship_xy:    (ship_px_x, ship_px_y) in trimmed minimap coords.
        Returns a summary dict for logging.
        """
        self.bot_path.append((ship_lat, ship_lon))
        ship_px_x, ship_px_y = ship_xy
        added_nodes = 0
        merged_nodes = 0
        local_to_world: dict[str, str] = {}

        for nid, tn in local_tree.nodes.items():
            stable = ("junction" in tn.kind) or ("dead_end" in tn.kind)
            if not stable:
                continue
            lat, lon = pixel_to_world(
                tn.y, tn.x, ship_lat, ship_lon, ship_px_x, ship_px_y,
            )
            kind = "junction" if "junction" in tn.kind else "dead_end"
            existing = self.find_nearest(lat, lon)
            wn = self._add_or_merge_node(lat, lon, kind, mode, tick)
            local_to_world[nid] = wn.id
            if existing is None or existing.id != wn.id:
                added_nodes += 1
            else:
                merged_nodes += 1

        # Edges between two STABLE nodes both observed this tick.
        for e in local_tree.edges:
            if e.a in local_to_world and e.b in local_to_world:
                self._add_edge_if_new(
                    local_to_world[e.a], local_to_world[e.b], mode,
                )

        return {
            "tick": tick,
            "stable_nodes_in_tree": len(local_to_world),
            "added": added_nodes,
            "merged": merged_nodes,
            "total_world_nodes": len(self.nodes),
            "total_world_edges": len(self.edges),
        }
