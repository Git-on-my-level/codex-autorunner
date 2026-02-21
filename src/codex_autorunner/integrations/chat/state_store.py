"""Platform-agnostic chat state-store contracts.

This module defines the minimal state interface consumed by chat-core, while
allowing platform adapters to back it with their own storage implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class ChatPendingApprovalRecord:
    """Normalized pending-approval record for chat-core orchestration."""

    request_id: str
    turn_id: str
    conversation_key: str
    chat_id: str
    thread_id: Optional[str]
    message_id: Optional[str]
    prompt: str
    created_at: str


@dataclass(frozen=True)
class ChatOutboxRecord:
    """Normalized outbox record used by chat-core transports."""

    record_id: str
    chat_id: str
    thread_id: Optional[str]
    reply_to_message_id: Optional[str]
    placeholder_message_id: Optional[str]
    text: str
    created_at: str
    attempts: int = 0
    last_error: Optional[str] = None
    last_attempt_at: Optional[str] = None
    next_attempt_at: Optional[str] = None
    operation: Optional[str] = None
    message_id: Optional[str] = None
    outbox_key: Optional[str] = None


class ChatStateStore(Protocol):
    """Minimal state contract for chat-core service and dispatcher logic."""

    async def close(self) -> None: ...

    def resolve_conversation_key(
        self,
        *,
        chat_id: str,
        thread_id: Optional[str],
        scope: Optional[str] = None,
    ) -> str: ...

    async def get_last_processed_event_id(
        self, conversation_key: str
    ) -> Optional[str]: ...

    async def set_last_processed_event_id(
        self, conversation_key: str, event_id: str
    ) -> Optional[str]: ...

    async def upsert_pending_approval(
        self, record: ChatPendingApprovalRecord
    ) -> ChatPendingApprovalRecord: ...

    async def clear_pending_approval(self, request_id: str) -> None: ...

    async def pending_approvals_for_conversation(
        self, conversation_key: str
    ) -> list[ChatPendingApprovalRecord]: ...

    async def put_pending_question(
        self, *, conversation_key: str, request_id: str, payload: dict[str, Any]
    ) -> None: ...

    async def get_pending_question(
        self, *, conversation_key: str, request_id: str
    ) -> Optional[dict[str, Any]]: ...

    async def clear_pending_question(
        self, *, conversation_key: str, request_id: str
    ) -> None: ...

    async def put_pending_selection(
        self, *, conversation_key: str, selection_id: str, payload: dict[str, Any]
    ) -> None: ...

    async def get_pending_selection(
        self, *, conversation_key: str, selection_id: str
    ) -> Optional[dict[str, Any]]: ...

    async def clear_pending_selection(
        self, *, conversation_key: str, selection_id: str
    ) -> None: ...

    async def enqueue_outbox(self, record: ChatOutboxRecord) -> ChatOutboxRecord: ...

    async def update_outbox(self, record: ChatOutboxRecord) -> ChatOutboxRecord: ...

    async def delete_outbox(self, record_id: str) -> None: ...

    async def get_outbox(self, record_id: str) -> Optional[ChatOutboxRecord]: ...

    async def list_outbox(self) -> list[ChatOutboxRecord]: ...
