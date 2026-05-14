from __future__ import annotations

import pytest

from codex_autorunner.core.orchestration.managed_turn_lifecycle_contract import (
    MANAGED_TURN_LIFECYCLE_PHASES,
    MANAGED_TURN_OPTIONAL_SIDE_EFFECTS,
    ManagedTurnTerminalOutcome,
    classify_terminal_recording,
    is_legal_managed_turn_phase_transition,
    managed_turn_phase_unblocks_queue,
)


def test_only_terminal_recorded_unblocks_managed_thread_queue() -> None:
    for phase in MANAGED_TURN_LIFECYCLE_PHASES:
        assert managed_turn_phase_unblocks_queue(phase) is (
            phase == "terminal_recorded"
        )


@pytest.mark.parametrize(
    ("current", "next_phase"),
    [
        ("accepted", "queued"),
        ("queued", "runtime_starting"),
        ("runtime_starting", "runtime_running"),
        ("runtime_running", "runtime_terminal_observed"),
        ("runtime_terminal_observed", "terminal_recording"),
        ("terminal_recording", "terminal_recorded"),
        ("terminal_recorded", "delivery_enqueued"),
        ("terminal_recorded", "side_effects_pending"),
        ("side_effects_pending", "side_effects_complete"),
    ],
)
def test_contract_allows_expected_phase_progression(
    current: str, next_phase: str
) -> None:
    assert is_legal_managed_turn_phase_transition(current, next_phase)


def test_contract_rejects_optional_side_effects_before_terminal_recorded() -> None:
    assert not is_legal_managed_turn_phase_transition(
        "runtime_running", "delivery_enqueued"
    )
    assert not is_legal_managed_turn_phase_transition(
        "terminal_recording", "side_effects_pending"
    )
    assert "live_timeline" in MANAGED_TURN_OPTIONAL_SIDE_EFFECTS
    assert "transcript_write" in MANAGED_TURN_OPTIONAL_SIDE_EFFECTS
    assert "delivery" in MANAGED_TURN_OPTIONAL_SIDE_EFFECTS
    assert "archive_cleanup" in MANAGED_TURN_OPTIONAL_SIDE_EFFECTS


def test_duplicate_terminal_recording_is_idempotent_and_unblocks_queue() -> None:
    outcome = ManagedTurnTerminalOutcome(status="ok")

    decision = classify_terminal_recording(existing=outcome, proposed=outcome)

    assert decision.action == "duplicate"
    assert decision.outcome == outcome
    assert decision.should_write is False
    assert decision.unblocks_queue is True


def test_conflicting_terminal_outcome_preserves_first_durable_outcome() -> None:
    existing = ManagedTurnTerminalOutcome(status="ok")
    proposed = ManagedTurnTerminalOutcome(status="error", error="late failure")

    decision = classify_terminal_recording(existing=existing, proposed=proposed)

    assert decision.action == "conflict"
    assert decision.outcome == existing
    assert decision.existing == existing
    assert decision.should_write is False
    assert decision.unblocks_queue is True
