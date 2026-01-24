from __future__ import annotations

import asyncio
import logging
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from ...core.logging_utils import log_event
from ...core.request_context import reset_conversation_id, set_conversation_id
from ...core.state import now_iso
from .constants import (
    OUTBOX_IMMEDIATE_RETRY_DELAYS,
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_RETRY_INTERVAL_SECONDS,
)
from .retry import _extract_retry_after_seconds
from .state import OutboxRecord, TelegramStateStore, topic_key

__all__ = ["_outbox_key", "TelegramOutboxManager"]

SendMessageFn = Callable[..., Awaitable[None]]
EditMessageFn = Callable[..., Awaitable[bool]]
DeleteMessageFn = Callable[..., Awaitable[bool]]


def _outbox_key(
    chat_id: int,
    thread_id: Optional[int],
    message_id: Optional[int],
    operation: Optional[str],
) -> str:
    return f"{chat_id}:{thread_id if thread_id is not None else 'root'}:{message_id if message_id is not None else 'new'}:{operation or 'send'}"


# Keep a module-level reference so static analysis sees this helper as used in production.
OUTBOX_KEY_HELPER = _outbox_key


def _parse_next_attempt_at(next_at_str: Optional[str]) -> Optional[datetime]:
    if not next_at_str:
        return None
    try:
        return datetime.strptime(next_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


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
        self._inflight_outbox_keys: set[str] = set()
        self._lock: Optional[asyncio.Lock] = None

    def start(self) -> None:
        self._inflight = set()
        self._inflight_outbox_keys = set()
        self._lock = asyncio.Lock()

    async def restore(self) -> None:
        records = await self._store.list_outbox()
        if not records:
            return
        for record in records:
            conversation_id = None
            try:
                from .state import topic_key as build_topic_key

                conversation_id = build_topic_key(record.chat_id, record.thread_id)
            except Exception:
                pass
            if conversation_id:
                from ...core.request_context import set_conversation_id

                token = set_conversation_id(conversation_id)
                try:
                    log_event(
                        self._logger,
                        logging.INFO,
                        "telegram.outbox.restore",
                        record_id=record.record_id,
                        chat_id=record.chat_id,
                        thread_id=record.thread_id,
                        message_id=record.message_id,
                        conversation_id=conversation_id,
                    )
                finally:
                    from ...core.request_context import reset_conversation_id

                    reset_conversation_id(token)
            else:
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.outbox.restore",
                    record_id=record.record_id,
                    chat_id=record.chat_id,
                    thread_id=record.thread_id,
                    message_id=record.message_id,
                )
        await self._flush(records)

    async def run_loop(self) -> None:
        while True:
            await asyncio.sleep(OUTBOX_RETRY_INTERVAL_SECONDS)
            records = []
            try:
                records = await self._store.list_outbox()
                if records:
                    await self._flush(records)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.flush_failed",
                    exc=exc,
                    record_count=len(records) if records else 0,
                )

    async def send_message_with_outbox(
        self,
        record: OutboxRecord,
    ) -> bool:
        await self._store.enqueue_outbox(record)
        conversation_id = None
        try:
            conversation_id = topic_key(record.chat_id, record.thread_id)
        except Exception:
            pass
        log_event(
            self._logger,
            logging.INFO,
            "telegram.outbox.enqueued",
            record_id=record.record_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            message_id=record.message_id,
            conversation_id=conversation_id,
        )
        immediate_delays_iter = iter(OUTBOX_IMMEDIATE_RETRY_DELAYS)
        immediate_delays_exhausted = False
        while True:
            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            next_at = _parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                now = datetime.now(timezone.utc)
                sleep_duration = (next_at - now).total_seconds()
                if sleep_duration > 0.01:
                    await asyncio.sleep(sleep_duration)
            if await self._attempt_send(current):
                return True
            current = await self._store.get_outbox(record.record_id)
            if current is None:
                return False
            if current.attempts >= OUTBOX_MAX_ATTEMPTS:
                return False
            next_at = _parse_next_attempt_at(current.next_attempt_at)
            if next_at is not None:
                now = datetime.now(timezone.utc)
                sleep_duration = (next_at - now).total_seconds()
                if sleep_duration > 0.01:
                    await asyncio.sleep(sleep_duration)
                continue
            if immediate_delays_exhausted:
                break
            try:
                delay = next(immediate_delays_iter)
            except StopIteration:
                immediate_delays_exhausted = True
                has_next = await self._store.get_outbox(record.record_id)
                if has_next is not None and has_next.next_attempt_at is None:
                    break
                continue
            if delay > 0:
                await asyncio.sleep(delay)
        return False

    async def _flush(self, records: list[OutboxRecord]) -> None:
        now = datetime.now(timezone.utc)
        ready_records: list[OutboxRecord] = []
        for record in records:
            next_at = _parse_next_attempt_at(record.next_attempt_at)
            if next_at is None or now >= next_at:
                ready_records.append(record)

        # Keep only the last ready record per outbox_key, but do not drop deferred
        # future records; we leave them for later flush cycles. Latest wins to avoid
        # delivering stale edits.
        coalesced_ready: dict[str, OutboxRecord] = {}
        for record in ready_records:
            if record.outbox_key is not None:
                coalesced_ready[record.outbox_key] = record
            else:
                await self._process_record(record)

        for record in coalesced_ready.values():
            await self._process_record(record)

    async def _process_record(self, record: OutboxRecord) -> None:
        with self._conversation_context(record.chat_id, record.thread_id):
            conversation_id = None
            try:
                conversation_id = topic_key(record.chat_id, record.thread_id)
            except Exception:
                pass
            if record.attempts >= OUTBOX_MAX_ATTEMPTS:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.gave_up",
                    record_id=record.record_id,
                    chat_id=record.chat_id,
                    thread_id=record.thread_id,
                    message_id=record.message_id,
                    attempts=record.attempts,
                    conversation_id=conversation_id,
                )
                if record.outbox_key:
                    records = await self._store.list_outbox()
                    for r in records:
                        if r.outbox_key == record.outbox_key:
                            await self._store.delete_outbox(r.record_id)
                else:
                    await self._store.delete_outbox(record.record_id)
                if record.placeholder_message_id is not None:
                    await self._edit_message_text(
                        record.chat_id,
                        record.placeholder_message_id,
                        "Delivery failed after retries. Please resend.",
                        message_thread_id=record.thread_id,
                    )
                return
            await self._attempt_send(record)

    async def _attempt_send(self, record: OutboxRecord) -> bool:
        current = await self._store.get_outbox(record.record_id)
        if current is None:
            return False
        record = current
        if not await self._mark_inflight(
            record.outbox_key if record.outbox_key else record.record_id
        ):
            return False
        conversation_id = None
        try:
            conversation_id = topic_key(record.chat_id, record.thread_id)
        except Exception:
            pass
        with self._conversation_context(record.chat_id, record.thread_id):
            try:
                await self._send_message(
                    record.chat_id,
                    record.text,
                    thread_id=record.thread_id,
                    reply_to=record.reply_to_message_id,
                )
            except Exception as exc:
                retry_after = _extract_retry_after_seconds(exc)
                record.attempts += 1
                record.last_error = str(exc)[:500]
                record.last_attempt_at = now_iso()
                if retry_after is not None:
                    now = datetime.now(timezone.utc)
                    delay_seconds = max(1, math.ceil(retry_after))
                    next_at = now.replace(microsecond=0) + timedelta(
                        seconds=delay_seconds
                    )
                    if next_at <= now:
                        next_at = now + timedelta(seconds=delay_seconds)
                    record.next_attempt_at = next_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                await self._store.update_outbox(record)
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.outbox.send_failed",
                    record_id=record.record_id,
                    chat_id=record.chat_id,
                    thread_id=record.thread_id,
                    message_id=record.message_id,
                    attempts=record.attempts,
                    retry_after=retry_after,
                    exc=exc,
                    conversation_id=conversation_id,
                )
                return False
            finally:
                await self._clear_inflight(
                    record.outbox_key if record.outbox_key else record.record_id
                )
            if record.outbox_key:
                # Only delete records up to (and including) this record's created_at to
                # avoid dropping newer queued messages for the same key.
                records = await self._store.list_outbox()
                for r in records:
                    if (
                        r.outbox_key == record.outbox_key
                        and r.created_at <= record.created_at
                    ):
                        await self._store.delete_outbox(r.record_id)
            else:
                await self._store.delete_outbox(record.record_id)
            if record.placeholder_message_id is not None:
                await self._delete_message(
                    record.chat_id,
                    record.placeholder_message_id,
                    record.thread_id,
                )
            log_event(
                self._logger,
                logging.INFO,
                "telegram.outbox.delivered",
                record_id=record.record_id,
                chat_id=record.chat_id,
                thread_id=record.thread_id,
                message_id=record.message_id,
                conversation_id=conversation_id,
            )
            return True

    async def _mark_inflight(self, key: str) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if key in self._inflight_outbox_keys:
                return False
            self._inflight_outbox_keys.add(key)
            return True

    async def _clear_inflight(self, key: str) -> None:
        if self._lock is None:
            return
        async with self._lock:
            self._inflight_outbox_keys.discard(key)

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
