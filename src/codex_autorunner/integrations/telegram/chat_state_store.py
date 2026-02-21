"""Telegram implementation of chat-core state-store contracts.

Backed by the existing Telegram sqlite store without schema changes.
"""

from __future__ import annotations

from typing import Any, Optional

from ..chat.state_store import (
    ChatOutboxRecord,
    ChatPendingApprovalRecord,
    ChatStateStore,
)
from .state import OutboxRecord, PendingApprovalRecord, TelegramStateStore, topic_key


class TelegramChatStateStore(ChatStateStore):
    """Thin adapter from chat-core state contracts to TelegramStateStore."""

    def __init__(self, store: TelegramStateStore) -> None:
        self._store = store
        self._pending_questions: dict[str, dict[str, dict[str, Any]]] = {}
        self._pending_selections: dict[str, dict[str, dict[str, Any]]] = {}

    async def close(self) -> None:
        # TelegramBotService owns the underlying store lifecycle.
        return None

    def resolve_conversation_key(
        self,
        *,
        chat_id: str,
        thread_id: Optional[str],
        scope: Optional[str] = None,
    ) -> str:
        parsed_chat_id = _parse_int(chat_id, kind="chat_id")
        parsed_thread_id = _parse_optional_int(thread_id)
        return topic_key(parsed_chat_id, parsed_thread_id, scope=scope)

    async def get_last_processed_event_id(self, conversation_key: str) -> Optional[str]:
        record = await self._store.get_topic(conversation_key)
        if record is None:
            return None
        update_id = record.last_update_id
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            return None
        return str(update_id)

    async def set_last_processed_event_id(
        self, conversation_key: str, event_id: str
    ) -> Optional[str]:
        update_id = _parse_int(event_id, kind="event_id")

        def apply(record: Any) -> None:
            record.last_update_id = update_id

        updated = await self._store.update_topic(conversation_key, apply)
        last = updated.last_update_id
        if isinstance(last, int) and not isinstance(last, bool):
            return str(last)
        return None

    async def upsert_pending_approval(
        self, record: ChatPendingApprovalRecord
    ) -> ChatPendingApprovalRecord:
        stored = await self._store.upsert_pending_approval(
            PendingApprovalRecord(
                request_id=record.request_id,
                turn_id=record.turn_id,
                chat_id=_parse_int(record.chat_id, kind="chat_id"),
                thread_id=_parse_optional_int(record.thread_id),
                message_id=_parse_optional_int(record.message_id),
                prompt=record.prompt,
                created_at=record.created_at,
                topic_key=record.conversation_key,
            )
        )
        return _approval_to_chat(stored)

    async def clear_pending_approval(self, request_id: str) -> None:
        await self._store.clear_pending_approval(request_id)

    async def pending_approvals_for_conversation(
        self, conversation_key: str
    ) -> list[ChatPendingApprovalRecord]:
        records = await self._store.pending_approvals_for_key(conversation_key)
        return [_approval_to_chat(record) for record in records]

    async def put_pending_question(
        self, *, conversation_key: str, request_id: str, payload: dict[str, Any]
    ) -> None:
        bucket = self._pending_questions.setdefault(conversation_key, {})
        bucket[request_id] = dict(payload)

    async def get_pending_question(
        self, *, conversation_key: str, request_id: str
    ) -> Optional[dict[str, Any]]:
        bucket = self._pending_questions.get(conversation_key)
        if bucket is None:
            return None
        payload = bucket.get(request_id)
        return dict(payload) if isinstance(payload, dict) else None

    async def clear_pending_question(
        self, *, conversation_key: str, request_id: str
    ) -> None:
        bucket = self._pending_questions.get(conversation_key)
        if not bucket:
            return
        bucket.pop(request_id, None)
        if not bucket:
            self._pending_questions.pop(conversation_key, None)

    async def put_pending_selection(
        self, *, conversation_key: str, selection_id: str, payload: dict[str, Any]
    ) -> None:
        bucket = self._pending_selections.setdefault(conversation_key, {})
        bucket[selection_id] = dict(payload)

    async def get_pending_selection(
        self, *, conversation_key: str, selection_id: str
    ) -> Optional[dict[str, Any]]:
        bucket = self._pending_selections.get(conversation_key)
        if bucket is None:
            return None
        payload = bucket.get(selection_id)
        return dict(payload) if isinstance(payload, dict) else None

    async def clear_pending_selection(
        self, *, conversation_key: str, selection_id: str
    ) -> None:
        bucket = self._pending_selections.get(conversation_key)
        if not bucket:
            return
        bucket.pop(selection_id, None)
        if not bucket:
            self._pending_selections.pop(conversation_key, None)

    async def enqueue_outbox(self, record: ChatOutboxRecord) -> ChatOutboxRecord:
        stored = await self._store.enqueue_outbox(_outbox_from_chat(record))
        return _outbox_to_chat(stored)

    async def update_outbox(self, record: ChatOutboxRecord) -> ChatOutboxRecord:
        stored = await self._store.update_outbox(_outbox_from_chat(record))
        return _outbox_to_chat(stored)

    async def delete_outbox(self, record_id: str) -> None:
        await self._store.delete_outbox(record_id)

    async def get_outbox(self, record_id: str) -> Optional[ChatOutboxRecord]:
        stored = await self._store.get_outbox(record_id)
        if stored is None:
            return None
        return _outbox_to_chat(stored)

    async def list_outbox(self) -> list[ChatOutboxRecord]:
        records = await self._store.list_outbox()
        return [_outbox_to_chat(record) for record in records]


def _parse_int(value: str, *, kind: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{kind} must be numeric: {value!r}") from exc
    return parsed


def _parse_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _approval_to_chat(record: PendingApprovalRecord) -> ChatPendingApprovalRecord:
    return ChatPendingApprovalRecord(
        request_id=record.request_id,
        turn_id=record.turn_id,
        conversation_key=record.topic_key
        or topic_key(record.chat_id, record.thread_id),
        chat_id=str(record.chat_id),
        thread_id=str(record.thread_id) if record.thread_id is not None else None,
        message_id=str(record.message_id) if record.message_id is not None else None,
        prompt=record.prompt,
        created_at=record.created_at,
    )


def _outbox_to_chat(record: OutboxRecord) -> ChatOutboxRecord:
    return ChatOutboxRecord(
        record_id=record.record_id,
        chat_id=str(record.chat_id),
        thread_id=str(record.thread_id) if record.thread_id is not None else None,
        reply_to_message_id=(
            str(record.reply_to_message_id)
            if record.reply_to_message_id is not None
            else None
        ),
        placeholder_message_id=(
            str(record.placeholder_message_id)
            if record.placeholder_message_id is not None
            else None
        ),
        text=record.text,
        created_at=record.created_at,
        attempts=record.attempts,
        last_error=record.last_error,
        last_attempt_at=record.last_attempt_at,
        next_attempt_at=record.next_attempt_at,
        operation=record.operation,
        message_id=str(record.message_id) if record.message_id is not None else None,
        outbox_key=record.outbox_key,
    )


def _outbox_from_chat(record: ChatOutboxRecord) -> OutboxRecord:
    return OutboxRecord(
        record_id=record.record_id,
        chat_id=_parse_int(record.chat_id, kind="chat_id"),
        thread_id=_parse_optional_int(record.thread_id),
        reply_to_message_id=_parse_optional_int(record.reply_to_message_id),
        placeholder_message_id=_parse_optional_int(record.placeholder_message_id),
        text=record.text,
        created_at=record.created_at,
        attempts=record.attempts,
        last_error=record.last_error,
        last_attempt_at=record.last_attempt_at,
        next_attempt_at=record.next_attempt_at,
        operation=record.operation,
        message_id=_parse_optional_int(record.message_id),
        outbox_key=record.outbox_key,
    )
