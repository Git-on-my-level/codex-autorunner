"""Discord implementation of chat-core state-store contracts.

Backed by the existing Discord sqlite store without schema changes.
"""

from __future__ import annotations

from typing import Any, Optional

from ..chat.state_store import (
    ChatOutboxRecord,
    ChatPendingApprovalRecord,
    ChatStateStore,
)
from .state import DiscordStateStore, OutboxRecord


def discord_conversation_key(
    channel_id: str, thread_id: Optional[str], *, scope: Optional[str] = None
) -> str:
    if not isinstance(channel_id, str) or not channel_id:
        raise ValueError("channel_id is required")
    suffix = thread_id if thread_id is not None else "root"
    base_key = f"{channel_id}:{suffix}"
    if not isinstance(scope, str) or not scope.strip():
        return base_key
    return f"{base_key}:{scope.strip()}"


def parse_discord_conversation_key(
    key: str,
) -> tuple[str, Optional[str], Optional[str]]:
    if not isinstance(key, str) or not key:
        raise ValueError("invalid conversation key")
    parts = key.split(":", 2)
    if len(parts) < 2:
        raise ValueError("invalid conversation key")
    channel_id, thread_raw = parts[0], parts[1]
    scope = parts[2] if len(parts) == 3 else None
    if not channel_id:
        raise ValueError("invalid channel_id in conversation key")
    thread_id = None if thread_raw == "root" else thread_raw
    return channel_id, thread_id, scope


class DiscordChatStateStore(ChatStateStore):
    """Thin adapter from chat-core state contracts to DiscordStateStore."""

    def __init__(self, store: DiscordStateStore) -> None:
        self._store = store
        self._pending_questions: dict[str, dict[str, dict[str, Any]]] = {}
        self._pending_selections: dict[str, dict[str, dict[str, Any]]] = {}
        self._pending_approvals: dict[str, ChatPendingApprovalRecord] = {}
        self._conversation_approvals: dict[str, set[str]] = {}
        self._last_event_ids: dict[str, str] = {}

    async def close(self) -> None:
        return None

    def resolve_conversation_key(
        self,
        *,
        chat_id: str,
        thread_id: Optional[str],
        scope: Optional[str] = None,
    ) -> str:
        return discord_conversation_key(chat_id, thread_id, scope=scope)

    async def get_last_processed_event_id(self, conversation_key: str) -> Optional[str]:
        return self._last_event_ids.get(conversation_key)

    async def set_last_processed_event_id(
        self, conversation_key: str, event_id: str
    ) -> Optional[str]:
        self._last_event_ids[conversation_key] = event_id
        return event_id

    async def upsert_pending_approval(
        self, record: ChatPendingApprovalRecord
    ) -> ChatPendingApprovalRecord:
        self._pending_approvals[record.request_id] = record
        bucket = self._conversation_approvals.setdefault(record.conversation_key, set())
        bucket.add(record.request_id)
        return record

    async def clear_pending_approval(self, request_id: str) -> None:
        record = self._pending_approvals.pop(request_id, None)
        if record is not None:
            bucket = self._conversation_approvals.get(record.conversation_key)
            if bucket is not None:
                bucket.discard(request_id)
                if not bucket:
                    self._conversation_approvals.pop(record.conversation_key, None)

    async def pending_approvals_for_conversation(
        self, conversation_key: str
    ) -> list[ChatPendingApprovalRecord]:
        bucket = self._conversation_approvals.get(conversation_key)
        if bucket is None:
            return []
        return [
            self._pending_approvals[rid]
            for rid in bucket
            if rid in self._pending_approvals
        ]

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
        discord_record = _outbox_from_chat(record)
        stored = await self._store.enqueue_outbox(discord_record)
        return _outbox_to_chat(stored, record)

    async def update_outbox(self, record: ChatOutboxRecord) -> ChatOutboxRecord:
        discord_record = _outbox_from_chat(record)
        stored = await self._store.enqueue_outbox(discord_record)
        return _outbox_to_chat(stored, record)

    async def delete_outbox(self, record_id: str) -> None:
        await self._store.mark_outbox_delivered(record_id)

    async def get_outbox(self, record_id: str) -> Optional[ChatOutboxRecord]:
        stored = await self._store.get_outbox(record_id)
        if stored is None:
            return None
        return _outbox_to_chat_from_discord(stored)

    async def list_outbox(self) -> list[ChatOutboxRecord]:
        records = await self._store.list_outbox()
        return [_outbox_to_chat_from_discord(r) for r in records]


def _outbox_to_chat_from_discord(record: OutboxRecord) -> ChatOutboxRecord:
    payload = record.payload_json or {}
    text = payload.get("text", "")
    reply_to_message_id = payload.get("reply_to_message_id")
    placeholder_message_id = payload.get("placeholder_message_id")
    outbox_key = payload.get("outbox_key")
    last_attempt_at = payload.get("last_attempt_at")
    thread_id = payload.get("thread_id")
    return ChatOutboxRecord(
        record_id=record.record_id,
        chat_id=record.channel_id,
        thread_id=thread_id,
        reply_to_message_id=reply_to_message_id,
        placeholder_message_id=placeholder_message_id,
        text=text,
        created_at=record.created_at,
        attempts=record.attempts,
        last_error=record.last_error,
        last_attempt_at=last_attempt_at,
        next_attempt_at=record.next_attempt_at,
        operation=record.operation,
        message_id=record.message_id,
        outbox_key=outbox_key,
    )


def _outbox_to_chat(
    record: OutboxRecord, original: ChatOutboxRecord
) -> ChatOutboxRecord:
    return ChatOutboxRecord(
        record_id=record.record_id,
        chat_id=record.channel_id,
        thread_id=original.thread_id,
        reply_to_message_id=original.reply_to_message_id,
        placeholder_message_id=original.placeholder_message_id,
        text=original.text,
        created_at=record.created_at,
        attempts=record.attempts,
        last_error=record.last_error,
        last_attempt_at=original.last_attempt_at,
        next_attempt_at=record.next_attempt_at,
        operation=record.operation,
        message_id=record.message_id,
        outbox_key=original.outbox_key,
    )


def _outbox_from_chat(record: ChatOutboxRecord) -> OutboxRecord:
    payload: dict[str, Any] = {
        "text": record.text,
        "thread_id": record.thread_id,
        "reply_to_message_id": record.reply_to_message_id,
        "placeholder_message_id": record.placeholder_message_id,
        "outbox_key": record.outbox_key,
        "last_attempt_at": record.last_attempt_at,
    }
    return OutboxRecord(
        record_id=record.record_id,
        channel_id=record.chat_id,
        message_id=record.message_id,
        operation=record.operation or "send_message",
        payload_json=payload,
        attempts=record.attempts,
        next_attempt_at=record.next_attempt_at,
        created_at=record.created_at,
        last_error=record.last_error,
    )
