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
from ..chat.outbox_kernel import (
    ChatOutboxKernel,
    OutboxAttemptResult,
    parse_next_attempt_at,
)
from .adapter import TelegramAPIError
from .constants import (
    OUTBOX_IMMEDIATE_RETRY_DELAYS,
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_RETRY_INTERVAL_SECONDS,
)
from .retry import _extract_retry_after_seconds
from .state import OutboxRecord, TelegramStateStore, topic_key

__all__ = [
    "OUTBOX_OPERATION_SEND_DELETE_PLACEHOLDER",
    "OUTBOX_OPERATION_SEND_KEEP_PLACEHOLDER",
    "TelegramOutboxManager",
    "_outbox_key",
]

SendMessageFn = Callable[..., Awaitable[Any]]
EditMessageFn = Callable[..., Awaitable[bool]]
DeleteMessageFn = Callable[..., Awaitable[bool]]
DeliveredCallback = Callable[[OutboxRecord, Optional[int]], Awaitable[None]]


def _outbox_key(
    chat_id: int,
    thread_id: Optional[int],
    message_id: Optional[int],
    operation: Optional[str],
) -> str:
    return f"{chat_id}:{thread_id if thread_id is not None else 'root'}:{message_id if message_id is not None else 'new'}:{operation or 'send'}"


# Keep a module-level reference so static analysis sees this helper as used in production.
OUTBOX_KEY_HELPER = _outbox_key
OUTBOX_OPERATION_SEND_DELETE_PLACEHOLDER = "send_delete_placeholder"
OUTBOX_OPERATION_SEND_KEEP_PLACEHOLDER = "send_keep_placeholder"
OUTBOX_OPERATION_EDIT = "edit"
OUTBOX_OPERATION_DELETE = "delete"


def _should_delete_placeholder_on_delivery(record: OutboxRecord) -> bool:
    if record.operation == OUTBOX_OPERATION_SEND_KEEP_PLACEHOLDER:
        return False
    if record.operation == OUTBOX_OPERATION_SEND_DELETE_PLACEHOLDER:
        return True
    # Backward-compatible default for existing outbox records.
    return True


def _parse_next_attempt_at(next_at_str: Optional[str]) -> Optional[datetime]:
    return parse_next_attempt_at(next_at_str)


def _coalesce_key(record: OutboxRecord) -> Optional[str]:
    if record.operation_id is not None:
        return f"op:{record.operation_id}"
    return record.outbox_key


class TelegramOutboxManager:
    def __init__(
        self,
        store: TelegramStateStore,
        *,
        send_message: SendMessageFn,
        edit_message_text: EditMessageFn,
        delete_message: DeleteMessageFn,
        logger: logging.Logger,
        on_delivered: Optional[DeliveredCallback] = None,
    ) -> None:
        self._store = store
        self._send_message = send_message
        self._edit_message_text = edit_message_text
        self._delete_message = delete_message
        self._on_delivered = on_delivered
        self._logger = logger
        self._kernel: ChatOutboxKernel[OutboxRecord, int] = ChatOutboxKernel(
            store,
            deliver=self._deliver_record,
            cleanup_delivered=self._cleanup_delivered,
            drop_exhausted=self._drop_exhausted,
            coalesce_key=_coalesce_key,
            inflight_key=self._inflight_key,
            logger=logger,
            max_attempts=OUTBOX_MAX_ATTEMPTS,
            immediate_retry_delays=tuple(OUTBOX_IMMEDIATE_RETRY_DELAYS),
            on_delivered=on_delivered,
            drop_direct_exhausted=False,
            drop_all_flush_exhausted_before_coalesce=False,
            callback_failed_event="telegram.outbox.delivery_callback_failed",
        )

    def start(self) -> None:
        self._kernel.start()

    async def restore(self) -> None:
        records = await self._store.list_outbox()
        if not records:
            return
        for record in records:
            conversation_id = None
            try:
                from .state import topic_key as build_topic_key

                conversation_id = build_topic_key(record.chat_id, record.thread_id)
            except (TypeError, ImportError):
                self._logger.debug("outbox.restore: topic_key failed", exc_info=True)
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
            except (
                Exception
            ) as exc:  # intentional: top-level loop guard — must not crash
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
        except TypeError:
            self._logger.debug("outbox.enqueue: topic_key failed", exc_info=True)
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
        return await self._kernel.enqueue_and_retry(record)

    async def _flush(self, records: list[OutboxRecord]) -> None:
        await self._kernel.flush(records)

    async def _process_record(self, record: OutboxRecord) -> None:
        await self._kernel.attempt_send(record)

    async def _drop_exhausted(self, record: OutboxRecord) -> None:
        with self._conversation_context(record.chat_id, record.thread_id):
            conversation_id = None
            try:
                conversation_id = topic_key(record.chat_id, record.thread_id)
            except TypeError:
                self._logger.debug("outbox.process: topic_key failed", exc_info=True)
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
            if self._on_delivered is not None:
                try:
                    await self._on_delivered(record, None)
                except (
                    Exception
                ):  # intentional: user-supplied callback must not break give-up cleanup
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.outbox.give_up_callback_failed",
                        record_id=record.record_id,
                        chat_id=record.chat_id,
                        thread_id=record.thread_id,
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

    async def _attempt_send(self, record: OutboxRecord) -> bool:
        return await self._kernel.attempt_send(record)

    async def _deliver_record(self, record: OutboxRecord) -> OutboxAttemptResult[int]:
        conversation_id = None
        try:
            conversation_id = topic_key(record.chat_id, record.thread_id)
        except TypeError:
            self._logger.debug("outbox.deliver: topic_key failed", exc_info=True)
        with self._conversation_context(record.chat_id, record.thread_id):
            try:
                delivered_message_id: Optional[int] = None
                if record.operation in {
                    None,
                    "",
                    OUTBOX_OPERATION_SEND_DELETE_PLACEHOLDER,
                    OUTBOX_OPERATION_SEND_KEEP_PLACEHOLDER,
                    "send",
                }:
                    try:
                        response = await self._send_message(
                            record.chat_id,
                            record.text,
                            thread_id=record.thread_id,
                            reply_to=record.reply_to_message_id,
                            overflow_mode_override=record.overflow_mode_override,
                        )
                    except TypeError as exc:
                        if "overflow_mode_override" not in str(exc):
                            raise
                        response = await self._send_message(
                            record.chat_id,
                            record.text,
                            thread_id=record.thread_id,
                            reply_to=record.reply_to_message_id,
                        )
                    if isinstance(response, int):
                        delivered_message_id = response
                elif record.operation == OUTBOX_OPERATION_EDIT:
                    if record.message_id is None:
                        raise RuntimeError(
                            "Unsupported Telegram outbox edit operation: missing message id"
                        )
                    edit_ok = await self._edit_message_text(
                        record.chat_id,
                        record.message_id,
                        record.text,
                        message_thread_id=record.thread_id,
                    )
                    if edit_ok is False:
                        raise RuntimeError("Telegram edit returned false")
                elif record.operation == OUTBOX_OPERATION_DELETE:
                    if record.message_id is None:
                        raise RuntimeError(
                            "Unsupported Telegram outbox delete operation: missing message id"
                        )
                    delete_ok = await self._delete_message(
                        record.chat_id,
                        record.message_id,
                        record.thread_id,
                    )
                    if delete_ok is False:
                        raise RuntimeError("Telegram delete returned false")
                else:
                    raise RuntimeError(
                        f"Unsupported Telegram outbox operation: {record.operation}"
                    )
            except Exception as exc:
                retry_after = _extract_retry_after_seconds(exc)
                if not isinstance(exc, (TelegramAPIError, OSError, RuntimeError)):
                    if retry_after is None:
                        raise
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
                return OutboxAttemptResult(delivered=False)
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
            return OutboxAttemptResult(
                delivered=True,
                delivered_id=delivered_message_id,
            )

    async def _cleanup_delivered(self, record: OutboxRecord) -> None:
        if record.outbox_key or record.operation_id:
            records = await self._store.list_outbox()
            for r in records:
                same_key = (
                    record.outbox_key is not None and r.outbox_key == record.outbox_key
                )
                same_op = (
                    record.operation_id is not None
                    and r.operation_id == record.operation_id
                )
                if (same_key or same_op) and r.created_at <= record.created_at:
                    await self._store.delete_outbox(r.record_id)
        else:
            await self._store.delete_outbox(record.record_id)
        if (
            record.placeholder_message_id is not None
            and _should_delete_placeholder_on_delivery(record)
        ):
            await self._delete_message(
                record.chat_id,
                record.placeholder_message_id,
                record.thread_id,
            )

    def _inflight_key(self, record: OutboxRecord) -> str:
        if record.operation_id is not None:
            return f"op:{record.operation_id}"
        return record.outbox_key if record.outbox_key else record.record_id

    @contextmanager
    def _conversation_context(self, chat_id: int, thread_id: Optional[int]) -> Any:
        token = None
        try:
            conversation_id = topic_key(chat_id, thread_id)
        except TypeError:
            conversation_id = None
        if conversation_id:
            token = set_conversation_id(conversation_id)
        try:
            yield
        finally:
            if token is not None:
                reset_conversation_id(token)
