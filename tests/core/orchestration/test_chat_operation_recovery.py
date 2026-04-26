from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from codex_autorunner.core.orchestration.chat_operation_recovery import (
    ChatOperationRecoveryAction,
    ChatOperationRecoveryDecision,
    _delivery_is_pending,
    _execution_is_resumeable,
    plan_chat_operation_recovery,
)
from codex_autorunner.core.orchestration.chat_operation_state import (
    CHAT_OPERATION_TERMINAL_STATES,
    ChatOperationSnapshot,
    ChatOperationState,
)

_NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)


def _snap(
    *,
    state: ChatOperationState = ChatOperationState.RECEIVED,
    delivery_state: str | None = None,
    delivery_attempt_count: int = 0,
    terminal_outcome: str | None = None,
    created_at: str = "2026-04-15T11:00:00Z",
    updated_at: str = "2026-04-15T11:55:00Z",
) -> ChatOperationSnapshot:
    return ChatOperationSnapshot(
        operation_id="test-op-1",
        surface_kind="discord",
        surface_operation_key="interaction-test",
        state=state,
        delivery_state=delivery_state,
        delivery_attempt_count=delivery_attempt_count,
        terminal_outcome=terminal_outcome,
        created_at=created_at,
        updated_at=updated_at,
    )


class TestChatOperationRecoveryActionEnum:
    def test_members_are_str_subclasses(self) -> None:
        for member in ChatOperationRecoveryAction:
            assert isinstance(member, str)
            assert member == member.value

    def test_backward_compat_string_equality(self) -> None:
        assert ChatOperationRecoveryAction.NOOP == "noop"
        assert ChatOperationRecoveryAction.RESUME_EXECUTION == "resume_execution"
        assert ChatOperationRecoveryAction.REPLAY_DELIVERY == "replay_delivery"
        assert ChatOperationRecoveryAction.MARK_ABANDONED == "mark_abandoned"
        assert ChatOperationRecoveryAction.MARK_EXPIRED == "mark_expired"

    def test_all_actions_present(self) -> None:
        expected = {
            "noop",
            "resume_execution",
            "replay_delivery",
            "mark_abandoned",
            "mark_expired",
        }
        actual = {member.value for member in ChatOperationRecoveryAction}
        assert actual == expected


class TestChatOperationRecoveryDecision:
    def test_frozen(self) -> None:
        decision = ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="test",
            previous_state=ChatOperationState.RECEIVED,
            delivery_pending=False,
            execution_replayable=False,
            attempt_count=0,
        )
        with pytest.raises(AttributeError):
            decision.action = ChatOperationRecoveryAction.MARK_ABANDONED  # type: ignore[misc]

    def test_default_rationale_is_empty(self) -> None:
        decision = ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="test",
            previous_state=ChatOperationState.RECEIVED,
            delivery_pending=False,
            execution_replayable=False,
            attempt_count=0,
        )
        assert decision.rationale == {}

    def test_structured_rationale(self) -> None:
        decision = ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.MARK_ABANDONED,
            reason="delivery_attempt_budget_exhausted",
            previous_state=ChatOperationState.DELIVERING,
            delivery_pending=True,
            execution_replayable=False,
            attempt_count=3,
            rationale={"attempt_count": 3, "max_attempts": 3},
        )
        assert decision.rationale["attempt_count"] == 3
        assert decision.rationale["max_attempts"] == 3


class TestDeliveryIsPending:
    @pytest.mark.parametrize("delivery_state", ("pending", "failed"))
    def test_pending_states(self, delivery_state: str) -> None:
        snap = _snap(delivery_state=delivery_state)
        assert _delivery_is_pending(snap) is True

    @pytest.mark.parametrize("delivery_state", (None, "completed", "in_flight", ""))
    def test_non_pending_states(self, delivery_state: str | None) -> None:
        snap = _snap(delivery_state=delivery_state)
        assert _delivery_is_pending(snap) is False


class TestExecutionIsResumeable:
    @pytest.mark.parametrize(
        "state",
        (
            ChatOperationState.ACKNOWLEDGED,
            ChatOperationState.VISIBLE,
            ChatOperationState.QUEUED,
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTING,
            ChatOperationState.ROUTING,
            ChatOperationState.BLOCKED,
        ),
    )
    def test_resumeable_states(self, state: ChatOperationState) -> None:
        snap = _snap(state=state)
        assert _execution_is_resumeable(snap) is True

    @pytest.mark.parametrize(
        "state",
        (
            ChatOperationState.RECEIVED,
            ChatOperationState.DELIVERING,
            ChatOperationState.COMPLETED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
            ChatOperationState.INTERRUPTED,
        ),
    )
    def test_non_resumeable_states(self, state: ChatOperationState) -> None:
        snap = _snap(state=state)
        assert _execution_is_resumeable(snap) is False


class TestPlanChatOperationRecoveryTerminalStates:
    def test_terminal_outcome_recorded_is_noop(self) -> None:
        snap = _snap(
            state=ChatOperationState.COMPLETED,
            terminal_outcome="completed",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.NOOP
        assert decision.reason == "terminal_outcome_already_recorded"
        assert decision.rationale["terminal_outcome"] == "completed"

    @pytest.mark.parametrize(
        "state",
        sorted(CHAT_OPERATION_TERMINAL_STATES, key=lambda s: s.value),
    )
    def test_terminal_state_is_noop(self, state: ChatOperationState) -> None:
        snap = _snap(state=state)
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.NOOP
        assert decision.reason == "terminal_state"

    def test_terminal_state_with_terminal_outcome_prefers_outcome(self) -> None:
        snap = _snap(
            state=ChatOperationState.FAILED,
            terminal_outcome="error",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.NOOP
        assert decision.reason == "terminal_outcome_already_recorded"


class TestPlanChatOperationRecoveryDeliveryPending:
    def test_delivery_attempts_exhausted_is_abandoned(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
            delivery_attempt_count=3,
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW, max_delivery_attempts=3)
        assert decision.action == ChatOperationRecoveryAction.MARK_ABANDONED
        assert decision.reason == "delivery_attempt_budget_exhausted"
        assert decision.delivery_pending is True
        assert decision.attempt_count == 3
        assert decision.rationale["attempt_count"] == 3
        assert decision.rationale["max_attempts"] == 3

    def test_delivery_stale_is_replay(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
            delivery_attempt_count=1,
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(
            snap, now=_NOW, delivery_stale_window=timedelta(minutes=15)
        )
        assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY
        assert decision.reason == "delivery_replay_required"
        assert decision.delivery_pending is True
        assert decision.previous_state is ChatOperationState.DELIVERING

    def test_delivery_no_updated_at_is_replay(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="failed",
            delivery_attempt_count=1,
            updated_at="",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY
        assert decision.rationale["stale"] is True

    def test_delivery_backoff_active_is_noop(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
            delivery_attempt_count=1,
            updated_at="2026-04-15T11:55:00Z",
        )
        decision = plan_chat_operation_recovery(
            snap,
            now=datetime(2026, 4, 15, 11, 56, 0, tzinfo=timezone.utc),
            delivery_stale_window=timedelta(minutes=15),
        )
        assert decision.action == ChatOperationRecoveryAction.NOOP
        assert decision.reason == "delivery_backoff_active"
        assert decision.delivery_pending is True

    def test_delivery_failed_state_treated_as_pending(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="failed",
            delivery_attempt_count=2,
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY
        assert decision.delivery_pending is True

    def test_delivery_pending_with_execution_state_not_in_flight(self) -> None:
        snap = _snap(
            state=ChatOperationState.RUNNING,
            delivery_state="pending",
            delivery_attempt_count=1,
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY
        assert decision.execution_replayable is True


class TestPlanChatOperationRecoveryExecutionResume:
    @pytest.mark.parametrize(
        "state",
        (
            ChatOperationState.ACKNOWLEDGED,
            ChatOperationState.VISIBLE,
            ChatOperationState.QUEUED,
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTING,
            ChatOperationState.ROUTING,
            ChatOperationState.BLOCKED,
        ),
    )
    def test_execution_states_trigger_resume(self, state: ChatOperationState) -> None:
        snap = _snap(state=state)
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.RESUME_EXECUTION
        assert decision.reason == "execution_resume_required"
        assert decision.execution_replayable is True
        assert decision.delivery_pending is False
        assert decision.rationale["state"] == state.value


class TestPlanChatOperationRecoveryReceivedState:
    def test_received_expired_is_mark_expired(self) -> None:
        snap = _snap(
            state=ChatOperationState.RECEIVED,
            created_at="2026-04-15T10:00:00Z",
            updated_at="2026-04-15T10:00:00Z",
        )
        decision = plan_chat_operation_recovery(
            snap,
            now=datetime(2026, 4, 15, 10, 10, 0, tzinfo=timezone.utc),
            unacked_expiry=timedelta(minutes=5),
        )
        assert decision.action == ChatOperationRecoveryAction.MARK_EXPIRED
        assert decision.reason == "accepted_operation_never_acknowledged"
        assert decision.previous_state is ChatOperationState.RECEIVED
        assert decision.delivery_pending is False
        assert decision.execution_replayable is False
        assert "unacked_expiry_seconds" in decision.rationale

    def test_received_not_expired_is_noop(self) -> None:
        snap = _snap(
            state=ChatOperationState.RECEIVED,
            created_at="2026-04-15T10:58:00Z",
            updated_at="2026-04-15T10:58:00Z",
        )
        decision = plan_chat_operation_recovery(
            snap,
            now=datetime(2026, 4, 15, 11, 0, 0, tzinfo=timezone.utc),
            unacked_expiry=timedelta(minutes=5),
        )
        assert decision.action == ChatOperationRecoveryAction.NOOP
        assert decision.reason == "no_recovery_action"

    def test_received_no_created_at_uses_updated_at(self) -> None:
        snap = _snap(
            state=ChatOperationState.RECEIVED,
            created_at="",
            updated_at="2026-04-15T10:00:00Z",
        )
        decision = plan_chat_operation_recovery(
            snap,
            now=datetime(2026, 4, 15, 10, 10, 0, tzinfo=timezone.utc),
            unacked_expiry=timedelta(minutes=5),
        )
        assert decision.action == ChatOperationRecoveryAction.MARK_EXPIRED

    def test_received_no_timestamps_is_expired(self) -> None:
        snap = _snap(
            state=ChatOperationState.RECEIVED,
            created_at="",
            updated_at="",
        )
        decision = plan_chat_operation_recovery(
            snap, now=_NOW, unacked_expiry=timedelta(minutes=5)
        )
        assert decision.action == ChatOperationRecoveryAction.MARK_EXPIRED


class TestPlanChatOperationRecoveryDecisionFields:
    def test_decision_carries_previous_state(self) -> None:
        snap = _snap(state=ChatOperationState.RUNNING)
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.previous_state is ChatOperationState.RUNNING

    def test_decision_carries_attempt_count(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
            delivery_attempt_count=2,
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.attempt_count == 2

    def test_decision_defaults_attempt_count_to_zero(self) -> None:
        snap = _snap(
            state=ChatOperationState.RUNNING,
            delivery_attempt_count=0,
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.attempt_count == 0


class TestPlanChatOperationRecoveryDeliveringState:
    def test_delivering_without_pending_delivery_is_noop(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state=None,
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.NOOP
        assert decision.reason == "no_recovery_action"

    def test_delivering_with_completed_delivery_is_noop(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="completed",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.NOOP


class TestPlanChatOperationRecoveryEdgeCases:
    def test_delivery_pending_takes_priority_over_execution_resumeable(self) -> None:
        snap = _snap(
            state=ChatOperationState.RUNNING,
            delivery_state="pending",
            delivery_attempt_count=1,
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY
        assert decision.delivery_pending is True
        assert decision.execution_replayable is True

    def test_terminal_outcome_takes_priority_over_delivery(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
            delivery_attempt_count=1,
            terminal_outcome="completed",
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.NOOP
        assert decision.reason == "terminal_outcome_already_recorded"

    def test_delivery_failed_outcome_does_not_block_delivery_recovery(self) -> None:
        """Discord records delivery_failed while still DELIVERING + failed cursor."""
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="failed",
            delivery_attempt_count=1,
            terminal_outcome="delivery_failed",
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY
        assert decision.reason == "delivery_replay_required"

    def test_snapshot_with_zero_attempt_count(self) -> None:
        snap = _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
            delivery_attempt_count=0,
            updated_at="2026-04-15T11:00:00Z",
        )
        decision = plan_chat_operation_recovery(snap, now=_NOW)
        assert decision.action == ChatOperationRecoveryAction.REPLAY_DELIVERY
        assert decision.attempt_count == 0
