from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

ManagedTurnLifecyclePhase = Literal[
    "accepted",
    "queued",
    "runtime_starting",
    "runtime_running",
    "runtime_terminal_observed",
    "terminal_recording",
    "terminal_recorded",
    "delivery_enqueued",
    "side_effects_pending",
    "side_effects_complete",
]
ManagedTurnTerminalStatus = Literal["ok", "error", "interrupted"]
TerminalRecordAction = Literal["record", "duplicate", "conflict"]

MANAGED_TURN_LIFECYCLE_PHASES: tuple[ManagedTurnLifecyclePhase, ...] = (
    "accepted",
    "queued",
    "runtime_starting",
    "runtime_running",
    "runtime_terminal_observed",
    "terminal_recording",
    "terminal_recorded",
    "delivery_enqueued",
    "side_effects_pending",
    "side_effects_complete",
)

MANAGED_TURN_TERMINAL_PHASE: ManagedTurnLifecyclePhase = "terminal_recorded"

MANAGED_TURN_LEGAL_TRANSITIONS: dict[
    ManagedTurnLifecyclePhase, frozenset[ManagedTurnLifecyclePhase]
] = {
    "accepted": frozenset({"queued", "runtime_starting"}),
    "queued": frozenset({"runtime_starting", "runtime_terminal_observed"}),
    "runtime_starting": frozenset({"runtime_running", "runtime_terminal_observed"}),
    "runtime_running": frozenset({"runtime_terminal_observed"}),
    "runtime_terminal_observed": frozenset({"terminal_recording"}),
    "terminal_recording": frozenset({"terminal_recorded"}),
    "terminal_recorded": frozenset(
        {"delivery_enqueued", "side_effects_pending", "side_effects_complete"}
    ),
    "delivery_enqueued": frozenset({"side_effects_pending", "side_effects_complete"}),
    "side_effects_pending": frozenset({"side_effects_complete"}),
    "side_effects_complete": frozenset(),
}

MANAGED_TURN_OPTIONAL_SIDE_EFFECTS: frozenset[str] = frozenset(
    {
        "activity_update",
        "archive_cleanup",
        "cold_trace",
        "delivery",
        "final_timeline",
        "live_timeline",
        "pr_binding",
        "transcript_write",
    }
)


@dataclass(frozen=True)
class ManagedTurnTerminalOutcome:
    """Durable terminal orchestration outcome for one managed-thread turn."""

    status: ManagedTurnTerminalStatus
    error: Optional[str] = None


@dataclass(frozen=True)
class TerminalRecordingDecision:
    """Pure idempotency decision before a terminal outcome write."""

    action: TerminalRecordAction
    outcome: ManagedTurnTerminalOutcome
    existing: Optional[ManagedTurnTerminalOutcome] = None

    @property
    def should_write(self) -> bool:
        return self.action == "record"

    @property
    def unblocks_queue(self) -> bool:
        return True


def is_legal_managed_turn_phase_transition(
    current: ManagedTurnLifecyclePhase,
    next_phase: ManagedTurnLifecyclePhase,
) -> bool:
    return next_phase in MANAGED_TURN_LEGAL_TRANSITIONS[current]


def managed_turn_phase_unblocks_queue(phase: ManagedTurnLifecyclePhase) -> bool:
    return phase == MANAGED_TURN_TERMINAL_PHASE


def classify_terminal_recording(
    *,
    existing: Optional[ManagedTurnTerminalOutcome],
    proposed: ManagedTurnTerminalOutcome,
) -> TerminalRecordingDecision:
    if existing is None:
        return TerminalRecordingDecision(action="record", outcome=proposed)
    if existing == proposed:
        return TerminalRecordingDecision(
            action="duplicate",
            outcome=existing,
            existing=existing,
        )
    return TerminalRecordingDecision(
        action="conflict",
        outcome=existing,
        existing=existing,
    )


__all__ = [
    "MANAGED_TURN_LEGAL_TRANSITIONS",
    "MANAGED_TURN_LIFECYCLE_PHASES",
    "MANAGED_TURN_OPTIONAL_SIDE_EFFECTS",
    "MANAGED_TURN_TERMINAL_PHASE",
    "ManagedTurnLifecyclePhase",
    "ManagedTurnTerminalOutcome",
    "ManagedTurnTerminalStatus",
    "TerminalRecordAction",
    "TerminalRecordingDecision",
    "classify_terminal_recording",
    "is_legal_managed_turn_phase_transition",
    "managed_turn_phase_unblocks_queue",
]
