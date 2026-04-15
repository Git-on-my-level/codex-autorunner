"""Control-plane contract for shared chat-surface operation state.

This module is intentionally placed in `core/orchestration` because the state
machine and durable snapshot shape are control-plane authority. Adapter-layer
code may render or mirror these states, but it must not redefine them or
become the long-term source of truth for recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


class ChatOperationState(str, Enum):
    """Authoritative lifecycle states for one surface-visible chat operation."""

    RECEIVED = "received"
    ROUTING = "routing"
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    CANCELLED = "cancelled"


CHAT_OPERATION_TERMINAL_STATES = frozenset(
    {
        ChatOperationState.COMPLETED,
        ChatOperationState.INTERRUPTED,
        ChatOperationState.FAILED,
        ChatOperationState.CANCELLED,
    }
)

# Future tickets may fill in finer-grained implementation details, but they
# must preserve the broad lifecycle envelope encoded here so Telegram and
# Discord converge on one shared operation model.
CHAT_OPERATION_ALLOWED_TRANSITIONS: dict[
    ChatOperationState, frozenset[ChatOperationState]
] = {
    ChatOperationState.RECEIVED: frozenset(
        {
            ChatOperationState.ROUTING,
            ChatOperationState.CANCELLED,
            ChatOperationState.FAILED,
        }
    ),
    ChatOperationState.ROUTING: frozenset(
        {
            ChatOperationState.BLOCKED,
            ChatOperationState.QUEUED,
            ChatOperationState.RUNNING,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }
    ),
    ChatOperationState.BLOCKED: frozenset(
        {
            ChatOperationState.ROUTING,
            ChatOperationState.QUEUED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }
    ),
    ChatOperationState.QUEUED: frozenset(
        {
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }
    ),
    ChatOperationState.RUNNING: frozenset(
        {
            ChatOperationState.BLOCKED,
            ChatOperationState.DELIVERING,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
        }
    ),
    ChatOperationState.DELIVERING: frozenset(
        {
            ChatOperationState.COMPLETED,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
        }
    ),
    ChatOperationState.COMPLETED: frozenset(),
    ChatOperationState.INTERRUPTED: frozenset(),
    ChatOperationState.FAILED: frozenset(),
    ChatOperationState.CANCELLED: frozenset(),
}


def is_valid_chat_operation_transition(
    from_state: ChatOperationState,
    to_state: ChatOperationState,
) -> bool:
    """Return whether a transition is allowed by the shared UX state machine."""

    return to_state in CHAT_OPERATION_ALLOWED_TRANSITIONS[from_state]


@dataclass(frozen=True)
class ChatOperationSnapshot:
    """Authoritative control-plane projection of one shared chat operation.

    The snapshot is intentionally small. It bridges the user-visible operation
    lifecycle to existing managed-thread and execution records without becoming
    a second execution-history system.
    """

    operation_id: str
    thread_target_id: str
    state: ChatOperationState
    execution_id: Optional[str] = None
    backend_turn_id: Optional[str] = None
    status_message: Optional[str] = None
    blocking_reason: Optional[str] = None
    updated_at: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ChatOperationStore(Protocol):
    """Durable store boundary for control-plane chat operation snapshots.

    Future implementations may persist these snapshots in orchestration sqlite
    or another control-plane artifact, but recovery must be possible from this
    store plus existing orchestration records rather than transport-local UI
    state.
    """

    def get_operation(self, operation_id: str) -> Optional[ChatOperationSnapshot]: ...

    def upsert_operation(
        self, snapshot: ChatOperationSnapshot
    ) -> ChatOperationSnapshot: ...

    def list_operations_for_thread(
        self,
        thread_target_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 20,
    ) -> list[ChatOperationSnapshot]: ...

    def delete_operation(self, operation_id: str) -> None: ...


__all__ = [
    "CHAT_OPERATION_ALLOWED_TRANSITIONS",
    "CHAT_OPERATION_TERMINAL_STATES",
    "ChatOperationSnapshot",
    "ChatOperationState",
    "ChatOperationStore",
    "is_valid_chat_operation_transition",
]
