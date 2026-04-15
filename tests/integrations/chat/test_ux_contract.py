from __future__ import annotations

from codex_autorunner.core.orchestration.chat_operation_state import (
    CHAT_OPERATION_TERMINAL_STATES,
    ChatOperationSnapshot,
    ChatOperationState,
    ChatOperationStore,
    is_valid_chat_operation_transition,
)
from codex_autorunner.integrations.chat.ux_contract import (
    CHAT_UX_CONTRACT_VERSION,
    CHAT_UX_STATE_DESCRIPTORS,
    get_chat_ux_state_descriptor,
    is_terminal_chat_ux_state,
)


def test_chat_operation_state_machine_foundation_contract() -> None:
    assert CHAT_UX_CONTRACT_VERSION == "chat-ux-foundation-v1"
    assert is_valid_chat_operation_transition(
        ChatOperationState.RECEIVED,
        ChatOperationState.ACKNOWLEDGED,
    )
    assert is_valid_chat_operation_transition(
        ChatOperationState.RECEIVED,
        ChatOperationState.ROUTING,
    )
    assert is_valid_chat_operation_transition(
        ChatOperationState.RUNNING,
        ChatOperationState.DELIVERING,
    )
    assert not is_valid_chat_operation_transition(
        ChatOperationState.COMPLETED,
        ChatOperationState.RUNNING,
    )
    assert CHAT_OPERATION_TERMINAL_STATES == {
        ChatOperationState.COMPLETED,
        ChatOperationState.INTERRUPTED,
        ChatOperationState.FAILED,
        ChatOperationState.CANCELLED,
    }


def test_chat_ux_descriptors_cover_every_shared_state() -> None:
    assert set(CHAT_UX_STATE_DESCRIPTORS) == set(ChatOperationState)
    for state in ChatOperationState:
        descriptor = get_chat_ux_state_descriptor(state)
        assert descriptor.state is state
        assert descriptor.terminal == is_terminal_chat_ux_state(state)
    assert get_chat_ux_state_descriptor(ChatOperationState.ACKNOWLEDGED).phase == (
        "pending"
    )
    assert get_chat_ux_state_descriptor(ChatOperationState.VISIBLE).phase == "active"
    assert get_chat_ux_state_descriptor(ChatOperationState.INTERRUPTING).title == (
        "Interrupting"
    )


def test_chat_operation_snapshot_and_store_protocol_surface_smoke() -> None:
    snapshot = ChatOperationSnapshot(
        operation_id="op-1",
        surface_kind="telegram",
        surface_operation_key="telegram:update:1",
        thread_target_id="thread-1",
        state=ChatOperationState.QUEUED,
        execution_id="exec-1",
        status_message="Queued behind an active turn",
        metadata={"surface": "telegram"},
    )

    assert snapshot.state is ChatOperationState.QUEUED
    assert snapshot.execution_id == "exec-1"
    assert snapshot.metadata["surface"] == "telegram"
    assert hasattr(ChatOperationStore, "get_operation")
    assert hasattr(ChatOperationStore, "get_operation_by_surface")
    assert hasattr(ChatOperationStore, "upsert_operation")
    assert hasattr(ChatOperationStore, "list_operations_for_thread")
    assert hasattr(ChatOperationStore, "list_recoverable_operations")
    assert hasattr(ChatOperationStore, "delete_operation")
