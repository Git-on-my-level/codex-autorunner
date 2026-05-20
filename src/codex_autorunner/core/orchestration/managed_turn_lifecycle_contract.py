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
ManagedTurnPhaseTransitionAction = Literal["advance", "duplicate", "reject"]
ManagedTurnRecoveryAction = Literal[
    "none",
    "recover_from_harness",
    "record_error",
    "recover_delivery",
    "recover_side_effects",
]

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

_LIVE_PHASE_RECOVERY_ACTIONS: dict[
    ManagedTurnLifecyclePhase, ManagedTurnRecoveryAction
] = {
    "accepted": "record_error",
    "queued": "record_error",
    "runtime_starting": "record_error",
    "runtime_running": "recover_from_harness",
    "runtime_terminal_observed": "recover_from_harness",
    "terminal_recording": "recover_from_harness",
}
_POST_TERMINAL_PHASE_RECOVERY_ACTIONS: dict[
    ManagedTurnLifecyclePhase, ManagedTurnRecoveryAction
] = {
    "terminal_recorded": "none",
    "delivery_enqueued": "recover_delivery",
    "side_effects_pending": "recover_side_effects",
    "side_effects_complete": "none",
}


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


@dataclass(frozen=True)
class ManagedTurnPhaseTransitionDecision:
    """Pure decision for a durable managed-turn phase metadata update."""

    managed_thread_id: str
    execution_id: str
    from_phase: Optional[str]
    to_phase: ManagedTurnLifecyclePhase
    action: ManagedTurnPhaseTransitionAction
    reason: str
    terminal_status: Optional[ManagedTurnTerminalStatus] = None

    @property
    def should_persist(self) -> bool:
        return self.action == "advance"

    @property
    def unblocks_queue(self) -> bool:
        return (
            self.action in {"advance", "duplicate"}
            and self.to_phase == MANAGED_TURN_TERMINAL_PHASE
        )


@dataclass(frozen=True)
class ManagedTurnRecoveryActionDecision:
    phase: str
    selected_action: ManagedTurnRecoveryAction
    reason: str


def normalize_managed_turn_lifecycle_phase(
    value: object,
) -> Optional[ManagedTurnLifecyclePhase]:
    phase = str(value or "").strip().lower()
    if phase in MANAGED_TURN_LIFECYCLE_PHASES:
        return phase
    return None


def is_legal_managed_turn_phase_transition(
    current: ManagedTurnLifecyclePhase,
    next_phase: ManagedTurnLifecyclePhase,
) -> bool:
    return next_phase in MANAGED_TURN_LEGAL_TRANSITIONS[current]


def managed_turn_phase_unblocks_queue(phase: ManagedTurnLifecyclePhase) -> bool:
    return phase == MANAGED_TURN_TERMINAL_PHASE


def plan_managed_turn_phase_transition(
    *,
    managed_thread_id: str,
    execution_id: str,
    current_phase: Optional[str],
    next_phase: ManagedTurnLifecyclePhase,
    terminal_status: Optional[ManagedTurnTerminalStatus] = None,
) -> ManagedTurnPhaseTransitionDecision:
    normalized_current = normalize_managed_turn_lifecycle_phase(current_phase)
    if normalized_current is None:
        return ManagedTurnPhaseTransitionDecision(
            managed_thread_id=managed_thread_id,
            execution_id=execution_id,
            from_phase=current_phase,
            to_phase=next_phase,
            action="advance",
            reason="initial_phase_recorded",
            terminal_status=terminal_status,
        )
    if normalized_current == next_phase:
        return ManagedTurnPhaseTransitionDecision(
            managed_thread_id=managed_thread_id,
            execution_id=execution_id,
            from_phase=normalized_current,
            to_phase=next_phase,
            action="duplicate",
            reason="phase_already_recorded",
            terminal_status=terminal_status,
        )
    if is_legal_managed_turn_phase_transition(normalized_current, next_phase):
        return ManagedTurnPhaseTransitionDecision(
            managed_thread_id=managed_thread_id,
            execution_id=execution_id,
            from_phase=normalized_current,
            to_phase=next_phase,
            action="advance",
            reason="legal_phase_transition",
            terminal_status=terminal_status,
        )
    return ManagedTurnPhaseTransitionDecision(
        managed_thread_id=managed_thread_id,
        execution_id=execution_id,
        from_phase=normalized_current,
        to_phase=next_phase,
        action="reject",
        reason="illegal_phase_transition",
        terminal_status=terminal_status,
    )


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


def classify_managed_turn_recovery_action(
    *,
    phase: str,
    status: str,
    terminal_statuses: frozenset[str],
) -> ManagedTurnRecoveryActionDecision:
    normalized_phase = normalize_managed_turn_lifecycle_phase(phase)
    if normalized_phase is None:
        return ManagedTurnRecoveryActionDecision(
            phase=phase,
            selected_action="none" if status in terminal_statuses else "record_error",
            reason="unknown_lifecycle_phase",
        )
    if status in terminal_statuses:
        selected_action = _POST_TERMINAL_PHASE_RECOVERY_ACTIONS.get(
            normalized_phase, "none"
        )
        return ManagedTurnRecoveryActionDecision(
            phase=normalized_phase,
            selected_action=selected_action,
            reason=(
                "post_terminal_phase_recovery"
                if selected_action != "none"
                else "already_terminal"
            ),
        )
    selected_action = _LIVE_PHASE_RECOVERY_ACTIONS.get(normalized_phase, "record_error")
    return ManagedTurnRecoveryActionDecision(
        phase=normalized_phase,
        selected_action=selected_action,
        reason=(
            "runtime_outcome_may_be_recoverable"
            if selected_action == "recover_from_harness"
            else "live_phase_stale_without_terminal_record"
        ),
    )


__all__ = [
    "MANAGED_TURN_LEGAL_TRANSITIONS",
    "MANAGED_TURN_LIFECYCLE_PHASES",
    "MANAGED_TURN_OPTIONAL_SIDE_EFFECTS",
    "MANAGED_TURN_TERMINAL_PHASE",
    "ManagedTurnLifecyclePhase",
    "ManagedTurnPhaseTransitionAction",
    "ManagedTurnPhaseTransitionDecision",
    "ManagedTurnRecoveryAction",
    "ManagedTurnRecoveryActionDecision",
    "ManagedTurnTerminalOutcome",
    "ManagedTurnTerminalStatus",
    "TerminalRecordAction",
    "TerminalRecordingDecision",
    "classify_managed_turn_recovery_action",
    "classify_terminal_recording",
    "is_legal_managed_turn_phase_transition",
    "managed_turn_phase_unblocks_queue",
    "normalize_managed_turn_lifecycle_phase",
    "plan_managed_turn_phase_transition",
]
