from __future__ import annotations

from codex_autorunner.core.orchestration.chat_operation_duplicates import (
    ChatOperationDuplicateAction,
    chat_operation_is_terminal_duplicate,
    plan_chat_operation_duplicate,
)
from codex_autorunner.core.orchestration.chat_operation_state import (
    ChatOperationSnapshot,
    ChatOperationState,
)


def _snap(
    *,
    state: ChatOperationState = ChatOperationState.RECEIVED,
    delivery_state: str | None = None,
    terminal_outcome: str | None = None,
) -> ChatOperationSnapshot:
    return ChatOperationSnapshot(
        operation_id="dup-op-1",
        surface_kind="discord",
        surface_operation_key="interaction-dup-1",
        state=state,
        delivery_state=delivery_state,
        terminal_outcome=terminal_outcome,
        created_at="2026-04-15T11:00:00Z",
        updated_at="2026-04-15T11:05:00Z",
    )


def test_duplicate_plan_accepts_missing_snapshot() -> None:
    decision = plan_chat_operation_duplicate(None)
    assert decision.action is ChatOperationDuplicateAction.ACCEPT_FRESH
    assert decision.reason == "no_existing_snapshot"


def test_duplicate_plan_rejects_terminal_snapshot() -> None:
    decision = plan_chat_operation_duplicate(
        _snap(
            state=ChatOperationState.COMPLETED,
            terminal_outcome="completed",
        )
    )
    assert decision.action is ChatOperationDuplicateAction.REJECT_TERMINAL
    assert decision.reason == "terminal_duplicate_rejected"


def test_duplicate_plan_suppresses_delivery_pending_hint_without_snapshot() -> None:
    decision = plan_chat_operation_duplicate(None, delivery_pending=True)
    assert decision.action is ChatOperationDuplicateAction.SUPPRESS_PENDING_DELIVERY
    assert decision.reason == "delivery_pending_duplicate_suppressed"
    assert decision.previous_state is None


def test_duplicate_plan_suppresses_delivering_snapshot() -> None:
    decision = plan_chat_operation_duplicate(
        _snap(
            state=ChatOperationState.DELIVERING,
            delivery_state="pending",
        )
    )
    assert decision.action is ChatOperationDuplicateAction.SUPPRESS_PENDING_DELIVERY
    assert decision.reason == "delivering_duplicate_suppressed"


def test_duplicate_plan_suppresses_non_terminal_non_delivery_state() -> None:
    decision = plan_chat_operation_duplicate(
        _snap(state=ChatOperationState.ACKNOWLEDGED)
    )
    assert decision.action is ChatOperationDuplicateAction.SUPPRESS_IN_FLIGHT
    assert decision.reason == "non_terminal_duplicate_suppressed"


def test_duplicate_plan_suppresses_existing_record_without_snapshot() -> None:
    decision = plan_chat_operation_duplicate(
        None,
        operation_already_registered=True,
    )
    assert decision.action is ChatOperationDuplicateAction.SUPPRESS_IN_FLIGHT
    assert decision.reason == "existing_operation_duplicate_suppressed"


def test_terminal_duplicate_helper_accepts_abandoned_terminal_outcome() -> None:
    assert (
        chat_operation_is_terminal_duplicate(
            _snap(
                state=ChatOperationState.RECEIVED,
                terminal_outcome="abandoned",
            )
        )
        is True
    )
