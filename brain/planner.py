# brain/planner.py
#
# FSM Planner — replaces "recovery" as a separate concern.
#
# Per docs/fsm_planning_and_edge_learning.md, the bot does not have a "main
# path" and a "recovery path".  It has a state graph, a goal, and a planner.
# The planner asks "given current state and target state, what edge fires
# next?" and executes it.  When no edge is found, the learning layer is
# invoked to produce a new edge.
#
# This module is the central orchestrator for that loop:
#
#     while not at_target:
#         result = perceive()
#         edge   = planner.next_edge_from(result.state, target)
#         if edge is None:
#             edge = planner.learn_edge(result.state, target)
#         planner.execute_edge(edge)
#
# Phase 1 deliverable (current): foundation — Planner class with arbitrary-
# target BFS, Edge metadata, and execution dispatch that delegates to the
# existing recovery primitives.  recover_to_port_overworld continues to
# exist; the planner is built alongside it, not replacing it yet.  Phase 2+
# migrates consumers (sail_to first) to call the planner directly.

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger


# ── Edge metadata ─────────────────────────────────────────────────────────────

@dataclass
class EdgeMetadata:
    """
    Operational metadata attached to every edge in the FSM.  Drives the
    exploration policy (Rule 1/2/3 in the architecture doc).  Hand-authored
    edges get sensible defaults; learned edges fill these in as they are
    persisted.
    """
    provenance:        str = "hand_authored"   # 'hand_authored' | 'claude_guided' | 'human_taught' | 'distilled'
    confidence:        str = "high"            # 'unverified' | 'low' | 'medium' | 'high'

    travel_count:      int = 0
    success_count:     int = 0
    failure_count:     int = 0
    avg_duration_secs: float = 0.0

    first_traveled_at: Optional[str] = None    # ISO timestamp
    last_traveled_at:  Optional[str] = None

    supersedes:        list[str] = field(default_factory=list)
    notes:             str = ""

    def update_success(self, duration_secs: float) -> None:
        """Record a successful traversal."""
        self.travel_count    += 1
        self.success_count   += 1
        # Running average
        if self.travel_count == 1:
            self.avg_duration_secs = duration_secs
        else:
            n = self.travel_count
            self.avg_duration_secs = ((n - 1) * self.avg_duration_secs + duration_secs) / n
        now = datetime.now(timezone.utc).isoformat()
        if self.first_traveled_at is None:
            self.first_traveled_at = now
        self.last_traveled_at = now

    def update_failure(self) -> None:
        """Record a failed traversal (action executed but did not reach to_state)."""
        self.travel_count    += 1
        self.failure_count   += 1
        self.last_traveled_at = datetime.now(timezone.utc).isoformat()

    @property
    def success_rate(self) -> float:
        if self.travel_count == 0:
            return 0.0
        return self.success_count / self.travel_count


# ── Edge ──────────────────────────────────────────────────────────────────────

@dataclass
class Edge:
    """
    Generalised edge in the FSM.  Wraps both static state-to-state transitions
    (from states.json:exits) and atomic-flow steps (from flows.json:steps).

    Self-edges (from_state == to_state) are first-class — they carry the same
    metadata and can have their own predicates.  See dynamic-state model in
    the architecture doc.
    """
    id:         str
    from_state: str
    to_state:   str
    action:     str                    # 'tap_home', 'press_back', 'tap_button', 'wait', etc.
    note:       str = ""
    actions:    list[dict] = field(default_factory=list)   # full action payload for complex edges
    metadata:   EdgeMetadata = field(default_factory=EdgeMetadata)

    @property
    def is_self_edge(self) -> bool:
        return self.from_state == self.to_state

    def __repr__(self) -> str:
        return f"Edge({self.from_state!r} --{self.action}--> {self.to_state!r})"


# ── Planner ───────────────────────────────────────────────────────────────────

class Planner:
    """
    Central orchestrator: given current state and target state, compute the
    next edge to execute.  Defers detection to perceive() (passed in or
    captured fresh) and execution to the existing recovery primitives during
    Phase 1; later phases move execution into Planner directly.
    """

    def __init__(self):
        # History buffer for dynamic-state edge predicates and stall detection.
        # Holds the last N PerceiveResult dicts (one per tick).
        self._history: deque = deque(maxlen=20)

    # ── Path computation ──────────────────────────────────────────────────────

    def find_path(self, from_state: str, to_state: str) -> list[Edge]:
        """
        BFS on the FSM graph.  Returns the shortest sequence of Edges leading
        from from_state to to_state.  Empty list if from_state == to_state.
        Returns None if no path exists.

        Generalisation of FSMRegistry._compute_recovery_paths which only
        targets port_overworld.  Computed on demand rather than pre-baked
        because target now varies.
        """
        from brain.fsm_registry import get_fsm_registry
        registry = get_fsm_registry()

        if from_state == to_state:
            return []
        if from_state not in registry.states:
            logger.warning(f"[planner] from_state {from_state!r} not in FSM")
            return None
        if to_state not in registry.states:
            logger.warning(f"[planner] to_state {to_state!r} not in FSM")
            return None

        # BFS forward from from_state
        visited: dict[str, Optional[tuple[str, Any]]] = {from_state: None}
        queue: deque[str] = deque([from_state])
        while queue:
            current = queue.popleft()
            if current == to_state:
                break
            state = registry.states.get(current)
            if state is None:
                continue
            for transition in state.exits:
                nxt = transition.to
                if nxt and nxt not in visited:
                    visited[nxt] = (current, transition)
                    queue.append(nxt)

        if to_state not in visited:
            return None

        # Reconstruct forward path
        path_transitions: list = []
        node = to_state
        while visited[node] is not None:
            parent_state, transition = visited[node]
            path_transitions.append((parent_state, transition))
            node = parent_state
        path_transitions.reverse()

        # Wrap each transition as an Edge with default metadata
        edges = []
        for from_s, t in path_transitions:
            edges.append(Edge(
                id         = f"{from_s}__{t.action}__{t.to}",
                from_state = from_s,
                to_state   = t.to,
                action     = t.action,
                note       = t.note,
            ))
        return edges

    # ── Single-step edge selection (for tick loops) ───────────────────────────

    def next_edge_from(self, current_state: str, target_state: str) -> Optional[Edge]:
        """
        Return the next single edge to traverse from current_state toward
        target_state.  None if no path exists (caller should invoke
        learn_edge).
        """
        path = self.find_path(current_state, target_state)
        if path is None:
            return None
        if not path:
            return None  # already at target
        return path[0]

    # ── History (for dynamic-state edge predicates) ───────────────────────────

    def record(self, perceive_result: Any) -> None:
        """Append a perceive result to the rolling history buffer."""
        self._history.append(perceive_result)

    def history(self) -> list:
        """Return a copy of the rolling history (oldest first)."""
        return list(self._history)

    def is_stalled(self, field_name: str, n_ticks: int = 3) -> bool:
        """
        True when the named field of perceive_result has not changed across
        the last n_ticks consecutive perceives.  Used by dynamic-state self-
        edge predicates ('keep_progressing' = NOT is_stalled).
        """
        if len(self._history) < n_ticks:
            return False
        recent = list(self._history)[-n_ticks:]
        values = [getattr(r, field_name, None) for r in recent]
        if any(v is None for v in values):
            return False
        return all(v == values[0] for v in values)

    # ── Edge execution ────────────────────────────────────────────────────────

    def execute_edge(self, edge: Edge) -> bool:
        """
        Execute a single edge's action and update its metadata.  Returns True
        if the edge appears to have produced a state change, False otherwise.

        Phase 1: delegates to existing recovery primitives.  In later phases
        execution dispatch moves here.
        """
        from brain.recovery import _execute_transition
        from brain.fsm_registry import Transition
        from capture.adb_capture import capture_screen

        start = time.time()
        # Build a Transition from the Edge for the existing executor
        transition = Transition(action=edge.action, to=edge.to_state, note=edge.note)
        try:
            frame = capture_screen()
            _execute_transition(transition.action, frame)
            duration = time.time() - start
            edge.metadata.update_success(duration)
            logger.info(f"[planner] executed {edge!r} ({duration:.1f}s)")
            return True
        except Exception as e:
            edge.metadata.update_failure()
            logger.warning(f"[planner] {edge!r} execution failed: {e}")
            return False

    # ── Learning (Phase 2: delegates to existing infrastructure) ─────────────

    def learn_edge(self, from_state: str, target_state: str) -> Optional[Edge]:
        """
        Invoke the learning layer when no path exists from from_state to
        target_state.  Phase 2: delegates to the existing claude_guided_loop +
        human_escalation chain via recover_to_port_overworld's terminal
        escalation.  Phase 5 unifies these into a single API and returns a
        proper Edge record.

        For now this is only invoked indirectly through plan_to (which falls
        back to recover_to_port_overworld for the port_overworld target).
        Direct calls return None until the learning surface is unified.
        """
        logger.warning(
            f"[planner] learn_edge({from_state!r} → {target_state!r}) — "
            "direct learning is not unified yet; route via plan_to instead"
        )
        return None

    # ── High-level convenience ────────────────────────────────────────────────

    def plan_to(
        self,
        target_state: str,
        current_state: Optional[str] = None,
        home_port: Optional[str] = None,
        timeout: float = 60.0,
    ) -> bool:
        """
        Drive the bot from current_state to target_state.  Returns True iff
        the target was reached.

        Phase 2: when target_state == 'port_overworld' the call delegates to
        the existing recover_to_port_overworld backend, which already
        encapsulates active-flow advancement, sea recovery, stall-driven
        reclassification, and human escalation.  This keeps behaviour
        unchanged while giving the goal layer a planner-shaped API.

        For any other target_state the planner runs naive BFS + execute_edge.
        Phase 4 reimplements the per-tick edge selection loop here so that
        port_overworld no longer needs a special path.
        """
        from capture.adb_capture import capture_screen
        from brain.perceive import perceive

        if target_state == "port_overworld":
            from brain.recovery import recover_to_port_overworld
            result = recover_to_port_overworld(
                home_port=home_port, timeout=timeout,
            )
            return result.state == "port_overworld"

        if current_state is None:
            current_state = perceive(capture_screen()).state

        path = self.find_path(current_state, target_state)
        if path is None:
            logger.warning(
                f"[planner] no path from {current_state!r} to {target_state!r} "
                "— learn_edge integration for non-overworld targets pending"
            )
            return False
        if not path:
            return True  # already at target

        for edge in path:
            ok = self.execute_edge(edge)
            if not ok:
                return False
            time.sleep(2.0)
        return True


# ── Module-level singleton ────────────────────────────────────────────────────

_instance: Optional[Planner] = None


def get_planner() -> Planner:
    """Return the process-wide Planner instance."""
    global _instance
    if _instance is None:
        _instance = Planner()
    return _instance
