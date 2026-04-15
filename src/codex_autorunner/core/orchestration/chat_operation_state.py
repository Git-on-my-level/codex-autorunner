"""Control-plane contract for shared chat-surface operation state.

This module is intentionally placed in `core/orchestration` because the shared
chat operation state machine and durable snapshot shape are control-plane
authority. Adapter-layer code may render or mirror these states, but it must
not redefine them or become the long-term source of truth for recovery.

Two distinctions matter for future tickets:

- `ACKNOWLEDGED` and `VISIBLE` are separate because an adapter can accept a
  transport interaction before it has produced a visible placeholder or anchor.
- `DELIVERING` is still control-plane state because delivery retry and recovery
  must be driven by durable truth rather than transport-local message objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


class ChatOperationState(str, Enum):
    """Authoritative lifecycle states for one surface-visible chat operation.

    Future tickets may add finer-grained state, but they must preserve the
    separation of concerns encoded here: control plane owns lifecycle truth,
    adapters own presentation semantics derived from it.
    """

    RECEIVED = "received"
    ACKNOWLEDGED = "acknowledged"
    VISIBLE = "visible"
    ROUTING = "routing"
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTING = "interrupting"
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
# Discord converge on one shared operation model. When new states are added,
# they should extend this table rather than creating transport-specific enums.
CHAT_OPERATION_ALLOWED_TRANSITIONS: dict[
    ChatOperationState, frozenset[ChatOperationState]
] = {
    ChatOperationState.RECEIVED: frozenset(
        {
            ChatOperationState.ACKNOWLEDGED,
            ChatOperationState.VISIBLE,
            ChatOperationState.ROUTING,
            ChatOperationState.QUEUED,
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTING,
            ChatOperationState.CANCELLED,
            ChatOperationState.FAILED,
        }
    ),
    ChatOperationState.ACKNOWLEDGED: frozenset(
        {
            ChatOperationState.VISIBLE,
            ChatOperationState.QUEUED,
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTING,
            ChatOperationState.DELIVERING,
            ChatOperationState.COMPLETED,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }
    ),
    ChatOperationState.VISIBLE: frozenset(
        {
            ChatOperationState.QUEUED,
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTING,
            ChatOperationState.DELIVERING,
            ChatOperationState.COMPLETED,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }
    ),
    ChatOperationState.ROUTING: frozenset(
        {
            ChatOperationState.ACKNOWLEDGED,
            ChatOperationState.VISIBLE,
            ChatOperationState.BLOCKED,
            ChatOperationState.QUEUED,
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTING,
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
            ChatOperationState.VISIBLE,
            ChatOperationState.RUNNING,
            ChatOperationState.INTERRUPTING,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }
    ),
    ChatOperationState.RUNNING: frozenset(
        {
            ChatOperationState.BLOCKED,
            ChatOperationState.VISIBLE,
            ChatOperationState.DELIVERING,
            ChatOperationState.INTERRUPTING,
            ChatOperationState.COMPLETED,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
        }
    ),
    ChatOperationState.INTERRUPTING: frozenset(
        {
            ChatOperationState.DELIVERING,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
        }
    ),
    ChatOperationState.DELIVERING: frozenset(
        {
            ChatOperationState.VISIBLE,
            ChatOperationState.COMPLETED,
            ChatOperationState.INTERRUPTED,
            ChatOperationState.FAILED,
            ChatOperationState.CANCELLED,
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
    surface_kind: str
    surface_operation_key: str
    state: ChatOperationState
    thread_target_id: Optional[str] = None
    conversation_id: Optional[str] = None
    execution_id: Optional[str] = None
    backend_turn_id: Optional[str] = None
    status_message: Optional[str] = None
    blocking_reason: Optional[str] = None
    ack_requested_at: Optional[str] = None
    ack_completed_at: Optional[str] = None
    first_visible_feedback_at: Optional[str] = None
    anchor_ref: Optional[str] = None
    interrupt_ref: Optional[str] = None
    delivery_state: Optional[str] = None
    delivery_cursor: Optional[Mapping[str, Any]] = None
    delivery_attempt_count: int = 0
    delivery_claimed_at: Optional[str] = None
    terminal_outcome: Optional[str] = None
    terminal_detail: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ChatOperationStore(Protocol):
    """Durable store boundary for control-plane chat operation snapshots.

    Future implementations may persist these snapshots in orchestration sqlite
    or another control-plane artifact, but recovery must be possible from this
    store plus existing orchestration records rather than transport-local UI
    state. The store boundary intentionally carries only operation-lifecycle
    data; transcript, full execution history, and transport payload details
    remain in their existing subsystems.
    """

    def get_operation(self, operation_id: str) -> Optional[ChatOperationSnapshot]: ...

    def upsert_operation(
        self, snapshot: ChatOperationSnapshot
    ) -> ChatOperationSnapshot: ...

    def get_operation_by_surface(
        self, surface_kind: str, surface_operation_key: str
    ) -> Optional[ChatOperationSnapshot]: ...

    def list_operations_for_thread(
        self,
        thread_target_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 20,
    ) -> list[ChatOperationSnapshot]: ...

    def list_recoverable_operations(
        self,
        *,
        surface_kind: Optional[str] = None,
        limit: int = 200,
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
