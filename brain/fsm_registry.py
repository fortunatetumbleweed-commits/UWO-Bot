# brain/fsm_registry.py
#
# FSM Registry — loads state/flow/interruptor definitions from the KB and provides:
#   - BFS recovery path from any state to port_overworld
#   - Flow and interruptor lookups
#   - add_state / add_flow / add_interruptor for in-session learning
#
# The registry is the authoritative source for the state graph.
# Detection logic lives in brain/perceive.py; this module provides the graph structure.

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from brain.flow_completeness import (
    STATUS_COMPLETE, STATUS_INCOMPLETE, STATUS_CANCELLED, STATUS_UNKNOWN,
    VALID_STATUSES,
)

_KB_DIR = Path("memory/knowledge/fsm")

# Confidence levels — drive selection / skip decisions in flow detection.
CONFIDENCE_HIGH       = "high"
CONFIDENCE_MEDIUM     = "medium"
CONFIDENCE_LOW        = "low"
CONFIDENCE_UNVERIFIED = "unverified"

# Failure threshold above which a flow is considered broken and demoted.
# Architecture doc Q2: "If failure_count exceeds a threshold, demote
# confidence and trigger Rule 1 (no longer a viable edge to this goal)."
FAILURE_DEMOTION_THRESHOLD = 3

# ── Data classes ──────────────────────────────────────────────────────────────

class Transition:
    def __init__(self, action: str, to: Optional[str], note: str = ""):
        self.action = action
        self.to     = to        # None means "resolves automatically" (e.g. loading)
        self.note   = note

    def __repr__(self):
        return f"Transition({self.action!r} → {self.to!r})"


class FSMState:
    def __init__(self, data: dict):
        self.id          = data["id"]
        self.description = data.get("description", "")
        self.exits       = [Transition(e["action"], e.get("to"), e.get("note", ""))
                            for e in data.get("exits", [])]
        self.flow_ids    = data.get("flows", [])

    def __repr__(self):
        return f"FSMState({self.id!r})"


class FSMFlow:
    def __init__(self, data: dict):
        self.id             = data["id"]
        self.description    = data.get("description", "")
        self.parent_state   = data.get("parent_state", "building")
        self.parent_detail  = data.get("parent_state_detail_contains", [])
        self.atomic         = data.get("atomic", True)
        self.trigger        = data.get("trigger_detection", "")
        self.steps          = data.get("steps", [])
        self.terminal_state          = data.get("terminal_state", "building")
        self.step_detection_keywords = data.get("step_detection_keywords", {})

        # Provenance + history (per docs/fsm_planning_and_edge_learning.md).
        # Hand-authored flows default to high confidence; learned ones live in
        # the KB with whatever confidence the learning layer wrote.  Counts
        # accumulate across runs as the bot replays the flow.
        meta = data.get("metadata", {})
        self.provenance        = data.get("provenance",        meta.get("provenance",        "hand_authored"))
        self.confidence        = data.get("confidence",        meta.get("confidence",        CONFIDENCE_HIGH))
        self.travel_count      = int(data.get("travel_count",      meta.get("travel_count",      0)))
        self.success_count     = int(data.get("success_count",     meta.get("success_count",     0)))
        self.failure_count     = int(data.get("failure_count",     meta.get("failure_count",     0)))
        self.avg_duration_secs = float(data.get("avg_duration_secs", meta.get("avg_duration_secs", 0.0)))
        self.first_traveled_at = data.get("first_traveled_at", meta.get("first_traveled_at"))
        self.last_traveled_at  = data.get("last_traveled_at",  meta.get("last_traveled_at"))
        self.supersedes        = list(data.get("supersedes",   meta.get("supersedes", [])))

        # Flow-completeness fields (project guideline — see CLAUDE.md
        # "Flow Completeness & Self-Correction").  A flow is complete only
        # when it terminates at a recognized state AND contains at least one
        # positive transaction.  Existing flows default to STATUS_UNKNOWN
        # until the audit pass tags them; the runtime treats UNKNOWN as
        # provisionally usable but the perceive guard refuses to attach any
        # flow whose status is INCOMPLETE.
        status_raw = data.get("status", meta.get("status", STATUS_UNKNOWN))
        self.status = status_raw if status_raw in VALID_STATUSES else STATUS_UNKNOWN
        self.positive_transaction_count = int(
            data.get("positive_transaction_count",
                     meta.get("positive_transaction_count", 0))
        )
        self.terminal_state_recognized = data.get(
            "terminal_state_recognized",
            meta.get("terminal_state_recognized"),
        )
        self.last_attempt_outcome = data.get(
            "last_attempt_outcome",
            meta.get("last_attempt_outcome"),
        )

        self._raw = data

    def __repr__(self):
        return f"FSMFlow({self.id!r})"

    @property
    def success_rate(self) -> float:
        if self.travel_count == 0:
            return 0.0
        return self.success_count / self.travel_count

    def should_skip(self) -> bool:
        """
        Heuristic: a flow is skipped during detection if its history says
        it is unreliable.  Used by callers to ignore demoted edges so the
        next not-enough-crew-style encounter exercises alternatives instead
        of replaying a known-broken recipe.
        """
        return self.confidence in (CONFIDENCE_LOW, CONFIDENCE_UNVERIFIED) and \
               self.failure_count >= FAILURE_DEMOTION_THRESHOLD

    def record_success(self, duration_secs: float) -> None:
        """Record a successful flow completion (post-state matched terminal_state)."""
        self.travel_count  += 1
        self.success_count += 1
        n = self.travel_count
        if n == 1:
            self.avg_duration_secs = duration_secs
        else:
            self.avg_duration_secs = ((n - 1) * self.avg_duration_secs + duration_secs) / n
        now = datetime.now(timezone.utc).isoformat()
        if self.first_traveled_at is None:
            self.first_traveled_at = now
        self.last_traveled_at = now
        # Learning a flow once is not enough to call it "high confidence".
        # Promote unverified → high only after a couple of confirmed successes.
        if self.confidence == CONFIDENCE_UNVERIFIED and self.success_count >= 2:
            self.confidence = CONFIDENCE_HIGH

    def record_failure(self) -> None:
        """
        Record a failed flow completion (post-state did not match terminal_state).
        Demotes confidence once the failure threshold is reached, so future
        detection passes can skip the flow (Rule 1 — no longer a viable edge).
        """
        self.travel_count  += 1
        self.failure_count += 1
        self.last_traveled_at = datetime.now(timezone.utc).isoformat()
        if self.failure_count >= FAILURE_DEMOTION_THRESHOLD and self.success_count == 0:
            # Repeated failure with zero successes → not a viable flow.
            self.confidence = CONFIDENCE_LOW


class FSMInterruptor:
    def __init__(self, data: dict):
        self.id                 = data["id"]
        self.description        = data.get("description", "")
        self.atomic             = data.get("atomic", True)
        self.detection          = data.get("detection", "")
        self.detection_keywords = data.get("detection_keywords", [])
        self.dismissal          = data.get("dismissal", "tap_ok")
        self.resumes            = data.get("resumes", "current_state_unchanged")
        self._raw               = data

    def __repr__(self):
        return f"FSMInterruptor({self.id!r})"


# ── Registry ──────────────────────────────────────────────────────────────────

class FSMRegistry:
    """
    Loads the FSM knowledge base and provides graph-based recovery path lookup.

    Recovery paths are BFS-computed at load time so lookups are O(1).
    Target is always port_overworld — the floor state.
    """

    TARGET = "port_overworld"

    def __init__(self):
        self.states:       dict[str, FSMState]       = {}
        self.flows:        dict[str, FSMFlow]         = {}
        self.interruptors: dict[str, FSMInterruptor]  = {}
        self._recovery_paths: dict[str, list[Transition]] = {}  # state_id → path to TARGET

    def load(self) -> "FSMRegistry":
        """Load all KB files. Safe to call multiple times (reloads)."""
        self._load_states()
        self._load_flows()
        self._load_interruptors()
        self._compute_recovery_paths()
        logger.info(
            f"  [FSM] Loaded {len(self.states)} states, "
            f"{len(self.flows)} flows, "
            f"{len(self.interruptors)} interruptors"
        )
        return self

    def _load_states(self):
        path = _KB_DIR / "states.json"
        if not path.exists():
            logger.warning(f"  [FSM] states.json not found at {path}")
            return
        for entry in json.loads(path.read_text()):
            s = FSMState(entry)
            self.states[s.id] = s

    def _load_flows(self):
        path = _KB_DIR / "flows.json"
        if not path.exists():
            return
        for entry in json.loads(path.read_text()):
            f = FSMFlow(entry)
            self.flows[f.id] = f

    def _load_interruptors(self):
        path = _KB_DIR / "interruptors.json"
        if not path.exists():
            return
        for entry in json.loads(path.read_text()):
            i = FSMInterruptor(entry)
            self.interruptors[i.id] = i

    def _compute_recovery_paths(self):
        """
        BFS from TARGET backwards to compute the shortest path from every
        reachable state to port_overworld.

        Stored as forward paths: self._recovery_paths[state_id] = [t1, t2, …]
        where executing t1, t2, … in order leads to port_overworld.
        """
        self._recovery_paths = {}

        # Build reverse graph: to_state → [(from_state, transition)]
        reverse: dict[str, list[tuple[str, Transition]]] = {}
        for state in self.states.values():
            for t in state.exits:
                if t.to:
                    reverse.setdefault(t.to, []).append((state.id, t))

        # BFS from TARGET
        visited: dict[str, Optional[tuple[str, Transition]]] = {self.TARGET: None}
        queue: deque[str] = deque([self.TARGET])

        while queue:
            current = queue.popleft()
            for (from_state, transition) in reverse.get(current, []):
                if from_state not in visited:
                    visited[from_state] = (current, transition)
                    queue.append(from_state)

        # Reconstruct forward paths
        for state_id, parent in visited.items():
            if state_id == self.TARGET:
                self._recovery_paths[state_id] = []
                continue
            path: list[Transition] = []
            node = state_id
            while visited[node] is not None:
                next_node, trans = visited[node]
                path.append(trans)
                node = next_node
            self._recovery_paths[state_id] = path

        reachable = len(self._recovery_paths)
        total     = len(self.states)
        if reachable < total:
            unreachable = [s for s in self.states if s not in self._recovery_paths]
            logger.warning(
                f"  [FSM] {total - reachable} state(s) have no recovery path to "
                f"port_overworld: {unreachable}"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def recovery_path(self, from_state: str) -> list[Transition]:
        """
        Return the shortest sequence of transitions from from_state to port_overworld.
        Returns [] if already at target, or if no path exists.
        """
        return list(self._recovery_paths.get(from_state, []))

    def flows_for(self, state_id: str) -> list[FSMFlow]:
        """Return all flows that can be active in the given state."""
        state = self.states.get(state_id)
        if not state:
            return []
        return [self.flows[fid] for fid in state.flow_ids if fid in self.flows]

    def add_state(self, data: dict) -> FSMState:
        """Add a newly discovered state (e.g. from Claude Vision) and recompute paths."""
        s = FSMState(data)
        self.states[s.id] = s
        self._save_states()
        self._compute_recovery_paths()
        logger.info(f"  [FSM] New state added: {s.id!r}")
        return s

    def add_flow(self, data: dict) -> FSMFlow:
        """Add a newly discovered flow (e.g. from Claude Vision)."""
        f = FSMFlow(data)
        self.flows[f.id] = f
        self._save_flows()
        logger.info(f"  [FSM] New flow added: {f.id!r}")
        return f

    def add_interruptor(self, data: dict) -> FSMInterruptor:
        """Add a newly discovered interruptor."""
        i = FSMInterruptor(data)
        self.interruptors[i.id] = i
        self._save_interruptors()
        logger.info(f"  [FSM] New interruptor added: {i.id!r}")
        return i

    def _save_states(self):
        _KB_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "id": s.id, "description": s.description,
                "exits": [{"action": t.action, "to": t.to, "note": t.note}
                          for t in s.exits],
                "flows": s.flow_ids,
            }
            for s in self.states.values()
        ]
        (_KB_DIR / "states.json").write_text(json.dumps(entries, indent=2))

    def _save_flows(self):
        """
        Persist flows.  Start from each flow's raw dict so non-listed
        fields (step_detection_keywords, learned_at, learned_via, notes,
        etc.) survive a save cycle, then overlay the live structural and
        metadata fields.  Earlier versions enumerated a fixed set of keys
        and silently dropped anything else — that's how the close_position
        commit accidentally erased detection_keywords.
        """
        _KB_DIR.mkdir(parents=True, exist_ok=True)
        entries = []
        for f in self.flows.values():
            entry = dict(f._raw or {})
            entry.update({
                "id":           f.id,
                "description":  f.description,
                "parent_state": f.parent_state,
                "parent_state_detail_contains": f.parent_detail,
                "atomic":       f.atomic,
                "trigger_detection": f.trigger,
                "steps":        f.steps,
                "terminal_state":             f.terminal_state,
                "step_detection_keywords":    f.step_detection_keywords,
                # Provenance / history — flat fields per architecture doc.
                "provenance":        f.provenance,
                "confidence":        f.confidence,
                "travel_count":      f.travel_count,
                "success_count":     f.success_count,
                "failure_count":     f.failure_count,
                "avg_duration_secs": f.avg_duration_secs,
                "first_traveled_at": f.first_traveled_at,
                "last_traveled_at":  f.last_traveled_at,
                "supersedes":        f.supersedes,
                # Flow-completeness fields (CLAUDE.md guideline).
                "status":                     f.status,
                "positive_transaction_count": f.positive_transaction_count,
                "terminal_state_recognized":  f.terminal_state_recognized,
                "last_attempt_outcome":       f.last_attempt_outcome,
            })
            # Drop the older nested 'metadata' dict if present so we don't
            # carry two sources of truth for the same fields.
            entry.pop("metadata", None)
            entries.append(entry)
        (_KB_DIR / "flows.json").write_text(
            json.dumps(entries, indent=2, ensure_ascii=False)
        )

    def _save_interruptors(self):
        """
        Persist interruptors.  Same _raw-overlay pattern as _save_flows so
        fields like detection_keywords and close_position survive saves.
        """
        _KB_DIR.mkdir(parents=True, exist_ok=True)
        entries = []
        for i in self.interruptors.values():
            entry = dict(i._raw or {})
            entry.update({
                "id":          i.id,
                "description": i.description,
                "atomic":      i.atomic,
                "detection":   i.detection,
                "dismissal":   i.dismissal,
                "resumes":     i.resumes,
            })
            entries.append(entry)
        (_KB_DIR / "interruptors.json").write_text(
            json.dumps(entries, indent=2, ensure_ascii=False)
        )

    # ── Flow outcome recording (Phase 4 verification) ─────────────────────────

    def record_flow_outcome(
        self,
        flow_id: str,
        success: bool,
        duration_secs: float = 0.0,
        positive_transaction_count: int = 0,
        terminal_state_recognized: Optional[bool] = None,
        cancelled: bool = False,
    ) -> None:
        """
        Record the outcome of a flow traversal and persist immediately.

        Called by the planner / recovery layer after a flow completes —
        post-execute perceive checks whether the new state matches
        flow.terminal_state.  On match: success.  On mismatch: failure
        (and the flow's confidence is demoted once the failure threshold
        is reached, so future detection skips it).

        Beyond confidence demotion, this method also evaluates the
        flow-completeness rule (CLAUDE.md → "Flow Completeness &
        Self-Correction") using the supplied positive_transaction_count and
        terminal_state_recognized signals.  The result is persisted in the
        flow's `status` field so the perceive guard can refuse to attach
        flows that are known-incomplete.

        This is the mechanism that lets the bot autonomously shed bad
        learned recipes such as the broken
        learned_building__sea_cinematic__... flow without human edits.
        """
        flow = self.flows.get(flow_id)
        if flow is None:
            logger.warning(f"  [FSM] record_flow_outcome: unknown flow {flow_id!r}")
            return
        before_conf   = flow.confidence
        before_status = flow.status
        if success:
            flow.record_success(duration_secs)
        else:
            flow.record_failure()
        if flow.confidence != before_conf:
            logger.warning(
                f"  [FSM] flow {flow_id!r} confidence demoted: "
                f"{before_conf} → {flow.confidence}  "
                f"(success={flow.success_count}, fail={flow.failure_count})"
            )

        # ── Flow-completeness evaluation ──────────────────────────────────
        # Only update status when the caller supplied runtime signals.
        # Legacy callers (no positive_transaction_count, no cancelled flag,
        # terminal_state_recognized=None) are unaffected.
        #
        # Runtime rule (CLAUDE.md): a single attempt counts as COMPLETE only
        # if it both (a) committed a positive transaction during execution
        # AND (b) terminated at a recognized state.  Looking at the flow
        # definition's static step list is wrong here because a flow can
        # define a transaction step that the runtime never reaches (the
        # recruit-crew loop: step_2 exists in the definition but the bot
        # exits after step_1 every time).
        runtime_signals_supplied = (
            positive_transaction_count > 0 or cancelled or terminal_state_recognized is not None
        )
        if runtime_signals_supplied:
            from brain.flow_completeness import (
                STATUS_COMPLETE, STATUS_INCOMPLETE, STATUS_CANCELLED,
            )
            flow.positive_transaction_count = max(
                flow.positive_transaction_count, int(positive_transaction_count)
            )
            if terminal_state_recognized is not None:
                flow.terminal_state_recognized = bool(terminal_state_recognized)

            if cancelled:
                new_status = STATUS_CANCELLED
            elif (positive_transaction_count >= 1
                  and bool(flow.terminal_state_recognized)):
                new_status = STATUS_COMPLETE
            else:
                new_status = STATUS_INCOMPLETE
            flow.status = new_status

            if cancelled:
                flow.last_attempt_outcome = "cancelled"
            elif success:
                flow.last_attempt_outcome = "succeeded"
            else:
                flow.last_attempt_outcome = "failed"

            if new_status != before_status:
                logger.warning(
                    f"  [FSM] flow {flow_id!r} status: {before_status} → {new_status}  "
                    f"(transactions={flow.positive_transaction_count}, "
                    f"terminal_recognized={flow.terminal_state_recognized})"
                )

        self._save_flows()


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[FSMRegistry] = None


def get_fsm_registry() -> FSMRegistry:
    global _instance
    if _instance is None:
        _instance = FSMRegistry().load()
    return _instance
