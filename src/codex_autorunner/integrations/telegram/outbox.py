from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Optional

from ...core.logging_utils import log_event
from ...core.request_context import reset_conversation_id, set_conversation_id
from ...core.state import now_iso
from .constants import (
    OUTBOX_IMMEDIATE_RETRY_DELAYS,
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_RETRY_INTERVAL_SECONDS,
)
from .state import OutboxRecord, TelegramStateStore, topic_key

SendMessageFn = Callable[..., Awaitable[None]]
EditMessageFn = Callable[..., Awaitable[bool]]
DeleteMessageFn = Callable[..., Awaitable[bool]]


class TelegramOutboxManager:
    def __init__(
        self,
        store: TelegramStateStore,
        *,
        send_message: SendMessageFn,
        edit_message_text: EditMessageFn,
        delete_message: DeleteMessageFn,
        logger: logging.Logger,
    ) -> None:
        self._store = store
        self._send_message = send_message
        self._edit_message_text = edit_message_text
        self._delete_message = delete_message
        self._logger = logger
        self._inflight: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None

    def start(self) -> None:
        self._inflight = set()
        self._lock = asyncio.Lock()

    async def restore(self) -> None:
        records = self._store.list_outbox()
        if not records:
            return
        log_event(
            self._logger,
            logging.INFO,
            "telegram.outbox.restore",
            count=len(records),
        )
        await self._flush(records)

    async def run_loop(self) -> None:
        while True:
            await asyncio.sleep(OUTBOX_RETRY_INTERVAL_SECONDS)
            try:
                records = self._store.list_outbox()
                if records:
                    await self._flush(records)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.flush_failed",
                    exc=exc,
                )

    async def send_message_with_outbox(
        self,
        record: OutboxRecord,
    ) -> bool:
        self._store.enqueue_outbox(record)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.outbox.enqueued",
            record_id=record.record_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
        )
        for delay in OUTBOX_IMMEDIATE_RETRY_DELAYS:
            if await self._attempt_send(record):
                return True
            current = self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= OUTBOX_MAX_ATTEMPTS:
                return False
            await asyncio.sleep(delay)
        return False

    async def _flush(self, records: list[OutboxRecord]) -> None:
        for record in records:
            with self._conversation_context(record.chat_id, record.thread_id):
                if record.attempts >= OUTBOX_MAX_ATTEMPTS:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.outbox.gave_up",
                        record_id=record.record_id,
                        chat_id=record.chat_id,
                        thread_id=record.thread_id,
                        attempts=record.attempts,
                    )
                    self._store.delete_outbox(record.record_id)
                    if record.placeholder_message_id is not None:
                        await self._edit_message_text(
                            record.chat_id,
                            record.placeholder_message_id,
                            "Delivery failed after retries. Please resend.",
                        )
                    continue
                await self._attempt_send(record)

    async def _attempt_send(self, record: OutboxRecord) -> bool:
        current = self._store.get_outbox(record.record_id)
        if current is None:
            return False
        record = current
        if not await self._mark_inflight(record.record_id):
            return False
        with self._conversation_context(record.chat_id, record.thread_id):
            try:
                await self._send_message(
                    record.chat_id,
                    record.text,
                    thread_id=record.thread_id,
                    reply_to=record.reply_to_message_id,
                )
            except Exception as exc:
                record.attempts += 1
                record.last_error = str(exc)[:500]
                record.last_attempt_at = now_iso()
                self._store.update_outbox(record)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.send_failed",
                    record_id=record.record_id,
                    chat_id=record.chat_id,
                    thread_id=record.thread_id,
                    attempts=record.attempts,
                    exc=exc,
                )
                return False
            finally:
                await self._clear_inflight(record.record_id)
            self._store.delete_outbox(record.record_id)
            if record.placeholder_message_id is not None:
                await self._delete_message(
                    record.chat_id, record.placeholder_message_id
                )
            log_event(
                self._logger,
                logging.INFO,
                "telegram.outbox.delivered",
                record_id=record.record_id,
                chat_id=record.chat_id,
                thread_id=record.thread_id,
            )
            return True

    async def _mark_inflight(self, record_id: str) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if record_id in self._inflight:
                return False
            self._inflight.add(record_id)
            return True

    async def _clear_inflight(self, record_id: str) -> None:
        if self._lock is None:
            return
        async with self._lock:
            self._inflight.discard(record_id)

    @contextmanager
    def _conversation_context(self, chat_id: int, thread_id: Optional[int]) -> Any:
        token = None
        try:
            conversation_id = topic_key(chat_id, thread_id)
        except Exception:
            conversation_id = None
        if conversation_id:
            token = set_conversation_id(conversation_id)
        try:
            yield
        finally:
            if token is not None:
                reset_conversation_id(token)
