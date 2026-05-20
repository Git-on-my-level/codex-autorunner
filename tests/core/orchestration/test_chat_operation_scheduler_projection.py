from __future__ import annotations

import pytest

from codex_autorunner.core.orchestration.chat_operation_scheduler_projection import (
    discord_execution_status_to_chat_operation_state,
    discord_interaction_has_pending_delivery,
    discord_scheduler_state_to_chat_operation_state,
    discord_scheduler_terminal_outcome,
)
from codex_autorunner.core.orchestration.chat_operation_state import (
    ChatOperationState,
)
from codex_autorunner.core.orchestration.discord_interaction_lifecycle import (
    DiscordInteractionExecutionStatus,
    DiscordInteractionSchedulerState,
    is_discord_interaction_execution_terminal,
    is_discord_interaction_scheduler_terminal,
    is_valid_discord_interaction_execution_transition,
    is_valid_discord_interaction_scheduler_transition,
    normalize_discord_interaction_execution_status,
    normalize_discord_interaction_scheduler_state,
    validate_discord_interaction_execution_transition,
    validate_discord_interaction_scheduler_transition,
)


class TestDiscordSchedulerStateToChatOperationState:
    @pytest.mark.parametrize(
        ("scheduler_state", "expected"),
        [
            ("received", ChatOperationState.RECEIVED),
            ("dispatch_ready", ChatOperationState.RECEIVED),
            ("dispatch_ack_pending", ChatOperationState.RECEIVED),
            ("queue_wait_ack_pending", ChatOperationState.RECEIVED),
            ("acknowledged", ChatOperationState.ACKNOWLEDGED),
            ("scheduled", ChatOperationState.QUEUED),
            ("waiting_on_resources", ChatOperationState.QUEUED),
            ("recovery_scheduled", ChatOperationState.QUEUED),
            ("executing", ChatOperationState.RUNNING),
            ("delivery_pending", ChatOperationState.DELIVERING),
            ("delivery_replaying", ChatOperationState.DELIVERING),
            ("completed", ChatOperationState.COMPLETED),
            ("abandoned", ChatOperationState.FAILED),
            ("delivery_expired", ChatOperationState.CANCELLED),
        ],
    )
    def test_exact_mapping(
        self, scheduler_state: str, expected: ChatOperationState
    ) -> None:
        assert (
            discord_scheduler_state_to_chat_operation_state(scheduler_state) == expected
        )

    def test_interrupt_keyword_match(self) -> None:
        assert (
            discord_scheduler_state_to_chat_operation_state("interrupting")
            == ChatOperationState.INTERRUPTING
        )

    def test_unknown_returns_none(self) -> None:
        assert discord_scheduler_state_to_chat_operation_state("unknown_state") is None

    def test_empty_returns_none(self) -> None:
        assert discord_scheduler_state_to_chat_operation_state("") is None

    def test_whitespace_handling(self) -> None:
        assert (
            discord_scheduler_state_to_chat_operation_state("  executing  ")
            == ChatOperationState.RUNNING
        )

    def test_case_insensitive(self) -> None:
        assert (
            discord_scheduler_state_to_chat_operation_state("EXECUTING")
            == ChatOperationState.RUNNING
        )


class TestDiscordExecutionStatusToChatOperationState:
    @pytest.mark.parametrize(
        ("status", "expected_state", "expected_outcome"),
        [
            ("completed", ChatOperationState.COMPLETED, None),
            ("cancelled", ChatOperationState.CANCELLED, None),
            ("timeout", ChatOperationState.FAILED, "timeout"),
            ("failed", ChatOperationState.FAILED, None),
            ("running", ChatOperationState.RUNNING, None),
            ("acknowledged", ChatOperationState.ACKNOWLEDGED, None),
            ("received", ChatOperationState.RECEIVED, None),
        ],
    )
    def test_exact_mapping(
        self,
        status: str,
        expected_state: ChatOperationState,
        expected_outcome: str | None,
    ) -> None:
        state, outcome = discord_execution_status_to_chat_operation_state(status)
        assert state == expected_state
        assert outcome == expected_outcome

    def test_completed_with_pending_delivery(self) -> None:
        state, outcome = discord_execution_status_to_chat_operation_state(
            "completed", has_pending_delivery=True
        )
        assert state == ChatOperationState.DELIVERING
        assert outcome is None

    def test_completed_without_pending_delivery(self) -> None:
        state, outcome = discord_execution_status_to_chat_operation_state(
            "completed", has_pending_delivery=False
        )
        assert state == ChatOperationState.COMPLETED
        assert outcome is None

    def test_unknown_returns_none_tuple(self) -> None:
        state, outcome = discord_execution_status_to_chat_operation_state("unknown")
        assert state is None
        assert outcome is None

    def test_empty_returns_none_tuple(self) -> None:
        state, outcome = discord_execution_status_to_chat_operation_state("")
        assert state is None
        assert outcome is None


class TestDiscordSchedulerTerminalOutcome:
    @pytest.mark.parametrize(
        ("scheduler_state", "expected"),
        [
            ("abandoned", "abandoned"),
            ("delivery_expired", "expired"),
        ],
    )
    def test_terminal_states(self, scheduler_state: str, expected: str) -> None:
        assert discord_scheduler_terminal_outcome(scheduler_state) == expected

    @pytest.mark.parametrize(
        "scheduler_state",
        ["received", "acknowledged", "executing", "completed", "scheduled"],
    )
    def test_non_terminal_returns_none(self, scheduler_state: str) -> None:
        assert discord_scheduler_terminal_outcome(scheduler_state) is None


class TestDiscordInteractionHasPendingDelivery:
    def test_scheduler_delivery_pending(self) -> None:
        assert discord_interaction_has_pending_delivery(
            scheduler_state="delivery_pending"
        )

    def test_scheduler_delivery_replaying(self) -> None:
        assert discord_interaction_has_pending_delivery(
            scheduler_state="delivery_replaying"
        )

    def test_cursor_pending(self) -> None:
        assert discord_interaction_has_pending_delivery(
            scheduler_state="executing", delivery_cursor_state="pending"
        )

    def test_cursor_failed(self) -> None:
        assert discord_interaction_has_pending_delivery(
            scheduler_state="executing", delivery_cursor_state="failed"
        )

    def test_no_pending_delivery(self) -> None:
        assert not discord_interaction_has_pending_delivery(scheduler_state="completed")

    def test_cursor_completed_not_pending(self) -> None:
        assert not discord_interaction_has_pending_delivery(
            scheduler_state="completed", delivery_cursor_state="completed"
        )

    def test_none_cursor_not_pending(self) -> None:
        assert not discord_interaction_has_pending_delivery(
            scheduler_state="completed", delivery_cursor_state=None
        )


class TestDiscordInteractionLifecyclePolicy:
    def test_scheduler_normalization_is_typed(self) -> None:
        assert (
            normalize_discord_interaction_scheduler_state(" EXECUTING ")
            == DiscordInteractionSchedulerState.EXECUTING
        )

    def test_execution_normalization_is_typed(self) -> None:
        assert (
            normalize_discord_interaction_execution_status(" RUNNING ")
            == DiscordInteractionExecutionStatus.RUNNING
        )

    def test_legal_scheduler_transition(self) -> None:
        assert is_valid_discord_interaction_scheduler_transition(
            DiscordInteractionSchedulerState.ACKNOWLEDGED,
            DiscordInteractionSchedulerState.SCHEDULED,
        )
        assert (
            validate_discord_interaction_scheduler_transition(
                "acknowledged", "scheduled"
            )
            == DiscordInteractionSchedulerState.SCHEDULED
        )

    def test_illegal_scheduler_transition_out_of_terminal_state(self) -> None:
        assert not is_valid_discord_interaction_scheduler_transition(
            DiscordInteractionSchedulerState.ABANDONED,
            DiscordInteractionSchedulerState.EXECUTING,
        )
        with pytest.raises(
            ValueError, match="illegal interaction scheduler transition"
        ):
            validate_discord_interaction_scheduler_transition(
                "delivery_expired", "executing"
            )

    def test_legal_execution_transition(self) -> None:
        assert is_valid_discord_interaction_execution_transition(
            DiscordInteractionExecutionStatus.ACKNOWLEDGED,
            DiscordInteractionExecutionStatus.RUNNING,
        )
        assert (
            validate_discord_interaction_execution_transition("running", "completed")
            == DiscordInteractionExecutionStatus.COMPLETED
        )

    def test_illegal_execution_transition_out_of_terminal_status(self) -> None:
        assert not is_valid_discord_interaction_execution_transition(
            DiscordInteractionExecutionStatus.COMPLETED,
            DiscordInteractionExecutionStatus.RUNNING,
        )
        with pytest.raises(
            ValueError, match="illegal interaction execution transition"
        ):
            validate_discord_interaction_execution_transition("failed", "running")

    def test_terminal_helpers_cover_recovery_states(self) -> None:
        assert is_discord_interaction_scheduler_terminal("delivery_expired")
        assert is_discord_interaction_scheduler_terminal("abandoned")
        assert is_discord_interaction_execution_terminal("timeout")
