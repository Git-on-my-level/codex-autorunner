"""Platform-agnostic event dispatcher with per-conversation queueing.

This module lives in the adapter layer (`integrations/chat`) and provides a
generic dispatcher that mirrors Telegram's queue/bypass semantics using
normalized chat events.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from numbers import Real
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Iterable,
    Optional,
    Protocol,
    Union,
)

from ...core.logging_utils import log_event
from ...core.time_utils import now_iso
from .callbacks import (
    CALLBACK_APPROVAL,
    CALLBACK_QUESTION_CANCEL,
    CALLBACK_QUESTION_CUSTOM,
    CALLBACK_QUESTION_DONE,
    CALLBACK_QUESTION_OPTION,
    decode_logical_callback,
)
from .models import ChatEvent, ChatInteractionEvent, ChatMessageEvent
from .queue_control import ChatQueueControlStore

DEFAULT_BYPASS_INTERACTION_PREFIXES = (
    "appr:",
    "qopt:",
    "qdone:",
    "qcustom:",
    "qcancel:",
    "cancel:interrupt",
)
DEFAULT_BYPASS_CALLBACK_IDS = frozenset(
    {
        CALLBACK_APPROVAL,
        CALLBACK_QUESTION_OPTION,
        CALLBACK_QUESTION_DONE,
        CALLBACK_QUESTION_CUSTOM,
        CALLBACK_QUESTION_CANCEL,
        "interrupt",
    }
)
DEFAULT_BYPASS_MESSAGE_TEXTS = frozenset(
    {
        "^c",
        "ctrl-c",
        "ctrl+c",
        "esc",
        "escape",
        "/stop",
        "/interrupt",
    }
)


def _normalize_casefolded_values(
    values: Optional[Iterable[str]],
    *,
    default: Iterable[str],
    parameter_name: str,
) -> tuple[str, ...]:
    source = default if values is None else values
    if isinstance(source, str):
        raise TypeError(
            f"{parameter_name} must be an iterable of strings, not a string"
        )

    normalized: list[str] = []
    for value in source:
        if not isinstance(value, str):
            raise TypeError(f"{parameter_name} values must be strings")
        normalized.append(value.lower())
    return tuple(normalized)


def _normalize_timeout_seconds(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if not isinstance(value, Real):
        raise TypeError("timeout values must be numeric or None")
    normalized = float(value)
    return normalized if normalized > 0 else None


@dataclass(frozen=True)
class DispatchContext:
    """Normalized dispatch metadata derived from an inbound chat event."""

    conversation_id: str
    platform: str
    chat_id: str
    thread_id: Optional[str]
    user_id: Optional[str]
    message_id: Optional[str]
    update_id: str
    is_edited: Optional[bool]
    has_message: bool
    has_interaction: bool


@dataclass(frozen=True)
class DispatchResult:
    """Dispatch attempt result."""

    status: str
    context: DispatchContext
    bypassed: bool = False
    queued_pending: int = 0
    queued_while_busy: bool = False


class DispatchPredicate(Protocol):
    """Hook protocol for allowlist/dedupe/bypass decisions."""

    def __call__(
        self, event: ChatEvent, context: DispatchContext
    ) -> Union[bool, Awaitable[bool]]: ...


DispatchHandler = Callable[[ChatEvent, DispatchContext], Awaitable[None]]


class ChatDispatcher:
    """Dispatches chat events with per-conversation ordering and bypass support."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        queue_control_store: Optional[ChatQueueControlStore] = None,
        allowlist_predicate: Optional[DispatchPredicate] = None,
        dedupe_predicate: Optional[DispatchPredicate] = None,
        bypass_predicate: Optional[DispatchPredicate] = None,
        busy_predicate: Optional[DispatchPredicate] = None,
        bypass_interaction_prefixes: Optional[Iterable[str]] = None,
        bypass_callback_ids: Optional[Iterable[str]] = None,
        bypass_message_texts: Optional[Iterable[str]] = None,
        handler_timeout_seconds: Optional[float] = 120.0,
        handler_stalled_warning_seconds: Optional[float] = 60.0,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._queue_control_store = queue_control_store
        self._allowlist_predicate = allowlist_predicate
        self._dedupe_predicate = dedupe_predicate
        self._bypass_predicate = bypass_predicate
        self._busy_predicate = busy_predicate
        self._bypass_interaction_prefixes = _normalize_casefolded_values(
            bypass_interaction_prefixes,
            default=DEFAULT_BYPASS_INTERACTION_PREFIXES,
            parameter_name="bypass_interaction_prefixes",
        )
        self._bypass_callback_ids = frozenset(
            _normalize_casefolded_values(
                bypass_callback_ids,
                default=DEFAULT_BYPASS_CALLBACK_IDS,
                parameter_name="bypass_callback_ids",
            )
        )
        self._bypass_message_texts = frozenset(
            _normalize_casefolded_values(
                bypass_message_texts,
                default=DEFAULT_BYPASS_MESSAGE_TEXTS,
                parameter_name="bypass_message_texts",
            )
        )
        self._handler_timeout_seconds = _normalize_timeout_seconds(
            handler_timeout_seconds
        )
        self._handler_stalled_warning_seconds = _normalize_timeout_seconds(
            handler_stalled_warning_seconds
        )
        self._lock = asyncio.Lock()
        self._queues: Dict[
            str, Deque[tuple[ChatEvent, DispatchContext, DispatchHandler]]
        ] = {}
        self._workers: Dict[str, asyncio.Task[None]] = {}
        self._resetting_conversations: set[str] = set()
        self._active_contexts: Dict[str, DispatchContext] = {}
        self._active_started_at: Dict[str, str] = {}
        self._active_handlers = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    async def dispatch(
        self, event: ChatEvent, handler: DispatchHandler
    ) -> DispatchResult:
        context = build_dispatch_context(event)
        log_event(
            self._logger,
            logging.INFO,
            "chat.dispatch.received",
            conversation_id=context.conversation_id,
            update_id=context.update_id,
            chat_id=context.chat_id,
            thread_id=context.thread_id,
            user_id=context.user_id,
            message_id=context.message_id,
            has_message=context.has_message,
            has_interaction=context.has_interaction,
            is_edited=context.is_edited,
        )
        if self._dedupe_predicate is not None:
            should_process = await _resolve_predicate(
                self._dedupe_predicate, event, context
            )
            if not should_process:
                log_event(
                    self._logger,
                    logging.INFO,
                    "chat.dispatch.duplicate",
                    conversation_id=context.conversation_id,
                    update_id=context.update_id,
                )
                return DispatchResult(status="duplicate", context=context)

        if self._allowlist_predicate is not None:
            allowed = await _resolve_predicate(
                self._allowlist_predicate, event, context
            )
            if not allowed:
                log_event(
                    self._logger,
                    logging.INFO,
                    "chat.dispatch.allowlist.denied",
                    conversation_id=context.conversation_id,
                    update_id=context.update_id,
                    chat_id=context.chat_id,
                    thread_id=context.thread_id,
                    user_id=context.user_id,
                )
                return DispatchResult(status="denied", context=context)

        bypass = is_bypass_event(
            event,
            interaction_prefixes=self._bypass_interaction_prefixes,
            callback_ids=self._bypass_callback_ids,
            message_texts=self._bypass_message_texts,
        )
        if self._bypass_predicate is not None:
            bypass = bypass or await _resolve_predicate(
                self._bypass_predicate, event, context
            )

        if bypass:
            log_event(
                self._logger,
                logging.INFO,
                "chat.dispatch.bypass",
                conversation_id=context.conversation_id,
                update_id=context.update_id,
            )
            await self._run_handler(event, context, handler)
            return DispatchResult(status="dispatched", context=context, bypassed=True)

        externally_busy = False
        if self._busy_predicate is not None:
            externally_busy = await _resolve_predicate(
                self._busy_predicate, event, context
            )

        queued_pending, queued_while_busy = await self._enqueue(
            context.conversation_id,
            event,
            context,
            handler,
            externally_busy=externally_busy,
        )
        return DispatchResult(
            status="queued",
            context=context,
            queued_pending=queued_pending,
            queued_while_busy=queued_while_busy,
        )

    async def wait_idle(self) -> None:
        """Wait until no queued or active handlers remain."""

        await self._idle_event.wait()

    async def pending(self, conversation_id: str) -> int:
        """Return queued item count for one conversation."""

        async with self._lock:
            queue = self._queues.get(conversation_id)
            return len(queue) if queue is not None else 0

    async def wake_conversation(self, conversation_id: str) -> bool:
        """Start draining a queued conversation after an external busy gate clears."""

        async with self._lock:
            if conversation_id in self._resetting_conversations:
                return False
            queue = self._queues.get(conversation_id)
            if not queue or conversation_id in self._workers:
                return False
            self._workers[conversation_id] = asyncio.create_task(
                self._drain_conversation(conversation_id)
            )
            self._idle_event.clear()
            self._publish_queue_state_locked(conversation_id)
            return True

    async def clear_pending(self, conversation_id: str) -> int:
        """Cancel queued items for one conversation without interrupting active work."""

        async with self._lock:
            queue = self._queues.get(conversation_id)
            if not queue:
                return 0
            cancelled = len(queue)
            queue.clear()
            if conversation_id not in self._workers:
                self._queues.pop(conversation_id, None)
            if not self._workers and self._active_handlers == 0:
                self._idle_event.set()
            self._publish_queue_state_locked(conversation_id)
            return cancelled

    async def cancel_pending_message(
        self, conversation_id: str, message_id: str
    ) -> bool:
        """Cancel one pending message for a conversation."""

        if not isinstance(message_id, str) or not message_id:
            return False
        async with self._lock:
            queue = self._queues.get(conversation_id)
            if not queue:
                return False
            for index, (event, _context, _handler) in enumerate(queue):
                if not isinstance(event, ChatMessageEvent):
                    continue
                if event.message.message_id != message_id:
                    continue
                del queue[index]
                if not queue and conversation_id not in self._workers:
                    self._queues.pop(conversation_id, None)
                if not self._workers and self._active_handlers == 0:
                    self._idle_event.set()
                self._publish_queue_state_locked(conversation_id)
                return True
            return False

    async def promote_pending_message(
        self, conversation_id: str, message_id: str
    ) -> bool:
        """Move one pending message to the front of the queue."""

        if not isinstance(message_id, str) or not message_id:
            return False
        async with self._lock:
            queue = self._queues.get(conversation_id)
            if not queue:
                return False
            for index, (event, context, handler) in enumerate(queue):
                if not isinstance(event, ChatMessageEvent):
                    continue
                if event.message.message_id != message_id:
                    continue
                if index == 0:
                    return True
                del queue[index]
                queue.appendleft((event, context, handler))
                self._publish_queue_state_locked(conversation_id)
                return True
            return False

    async def queue_status(self, conversation_id: str) -> dict[str, Any]:
        """Return in-memory queue status for one conversation."""

        async with self._lock:
            return self._build_queue_snapshot_locked(conversation_id)

    async def force_reset(self, conversation_id: str) -> dict[str, Any]:
        """Cancel the active worker and clear queued work for a conversation."""

        cancelled_pending = 0
        cancelled_active = False
        worker: Optional[asyncio.Task[None]] = None

        async with self._lock:
            queue = self._queues.get(conversation_id)
            cancelled_pending = len(queue) if queue is not None else 0
            if queue is not None:
                queue.clear()
                self._queues.pop(conversation_id, None)
            cancelled_active = conversation_id in self._active_contexts
            worker = self._workers.get(conversation_id)
            self._resetting_conversations.add(conversation_id)
            self._publish_queue_state_locked(conversation_id)

        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        async with self._lock:
            self._resetting_conversations.discard(conversation_id)
            self._active_contexts.pop(conversation_id, None)
            self._active_started_at.pop(conversation_id, None)
            queue = self._queues.get(conversation_id)
            if queue:
                if conversation_id not in self._workers:
                    self._workers[conversation_id] = asyncio.create_task(
                        self._drain_conversation(conversation_id)
                    )
            else:
                self._queues.pop(conversation_id, None)
                self._workers.pop(conversation_id, None)
            if self._active_handlers == 0 and not self._workers and not self._queues:
                self._idle_event.set()
            self._publish_queue_state_locked(conversation_id)

        return {
            "conversation_id": conversation_id,
            "cancelled_pending": cancelled_pending,
            "cancelled_active": cancelled_active,
        }

    async def close(self) -> None:
        """Cancel worker tasks and clear queued work."""

        async with self._lock:
            workers = list(self._workers.values())
            conversation_ids = set(self._queues) | set(self._workers)
            self._queues.clear()
            if not workers and self._active_handlers == 0:
                self._idle_event.set()

        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        async with self._lock:
            self._workers.clear()
            self._queues.clear()
            self._active_contexts.clear()
            self._active_started_at.clear()
            if self._active_handlers == 0:
                self._idle_event.set()
            for conversation_id in conversation_ids:
                self._publish_queue_state_locked(conversation_id)

    async def _enqueue(
        self,
        conversation_id: str,
        event: ChatEvent,
        context: DispatchContext,
        handler: DispatchHandler,
        *,
        externally_busy: bool = False,
    ) -> tuple[int, bool]:
        async with self._lock:
            queue = self._queues.get(conversation_id)
            if queue is None:
                queue = deque()
                self._queues[conversation_id] = queue
            is_resetting = conversation_id in self._resetting_conversations
            had_worker = conversation_id in self._workers or is_resetting
            had_pending = bool(queue)
            queue.append((event, context, handler))
            self._idle_event.clear()
            still_externally_busy = externally_busy
            if (
                externally_busy
                and conversation_id not in self._workers
                and not is_resetting
                and self._busy_predicate is not None
            ):
                still_externally_busy = await _resolve_predicate(
                    self._busy_predicate, event, context
                )
            if (
                conversation_id not in self._workers
                and not is_resetting
                and not still_externally_busy
            ):
                self._workers[conversation_id] = asyncio.create_task(
                    self._drain_conversation(conversation_id)
                )
            pending = len(queue)
            queued_while_busy = had_worker or had_pending or still_externally_busy
            self._publish_queue_state_locked(conversation_id)
        log_event(
            self._logger,
            logging.INFO,
            "chat.dispatch.queued",
            conversation_id=conversation_id,
            update_id=context.update_id,
            pending=pending,
        )
        return pending, queued_while_busy

    async def _drain_conversation(self, conversation_id: str) -> None:
        try:
            while True:
                async with self._lock:
                    queue = self._queues.get(conversation_id)
                    if not queue:
                        self._queues.pop(conversation_id, None)
                        self._workers.pop(conversation_id, None)
                        self._active_contexts.pop(conversation_id, None)
                        self._active_started_at.pop(conversation_id, None)
                        if not self._workers and self._active_handlers == 0:
                            self._idle_event.set()
                        self._publish_queue_state_locked(conversation_id)
                        return
                    event, context, handler = queue.popleft()
                    self._active_contexts[conversation_id] = context
                    self._active_started_at[conversation_id] = now_iso()
                    self._active_handlers += 1
                    self._publish_queue_state_locked(conversation_id)
                try:
                    await self._run_handler(event, context, handler)
                finally:
                    async with self._lock:
                        self._active_handlers -= 1
                        self._active_contexts.pop(conversation_id, None)
                        self._active_started_at.pop(conversation_id, None)
                        if (
                            self._active_handlers == 0
                            and not self._workers
                            and not self._queues
                        ):
                            self._idle_event.set()
                        self._publish_queue_state_locked(conversation_id)
        finally:
            async with self._lock:
                self._workers.pop(conversation_id, None)
                self._active_contexts.pop(conversation_id, None)
                self._active_started_at.pop(conversation_id, None)
                if (
                    self._active_handlers == 0
                    and not self._workers
                    and not self._queues
                ):
                    self._idle_event.set()
                self._publish_queue_state_locked(conversation_id)

    async def _run_handler(
        self,
        event: ChatEvent,
        context: DispatchContext,
        handler: DispatchHandler,
    ) -> None:
        handler_task: Optional[asyncio.Future[None]] = None
        stall_monitor_task: Optional[asyncio.Task[None]] = None
        log_event(
            self._logger,
            logging.INFO,
            "chat.dispatch.handler.start",
            conversation_id=context.conversation_id,
            update_id=context.update_id,
        )
        try:
            handler_task = asyncio.ensure_future(handler(event, context))
            if self._handler_stalled_warning_seconds is not None:
                stall_monitor_task = asyncio.create_task(
                    self._warn_on_stalled_handler(handler_task, context)
                )
            if self._handler_timeout_seconds is None:
                await handler_task
            else:
                done, _ = await asyncio.wait(
                    {handler_task},
                    timeout=self._handler_timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if handler_task not in done:
                    log_event(
                        self._logger,
                        logging.ERROR,
                        "chat.dispatch.handler.timeout",
                        conversation_id=context.conversation_id,
                        update_id=context.update_id,
                        timeout_seconds=self._handler_timeout_seconds,
                    )
                    handler_task.cancel()
                    handler_task.add_done_callback(
                        lambda task: self._log_background_handler_failure(task, context)
                    )
                    return
                await handler_task
        except asyncio.CancelledError:
            if handler_task is not None and not handler_task.done():
                handler_task.cancel()
                handler_task.add_done_callback(
                    lambda task: self._log_background_handler_failure(task, context)
                )
            raise
        except Exception as exc:  # intentional: top-level handler fault barrier
            log_event(
                self._logger,
                logging.ERROR,
                "chat.dispatch.handler.failed",
                conversation_id=context.conversation_id,
                update_id=context.update_id,
                exc=exc,
            )
            self._logger.exception(
                "chat.dispatch.handler.failed conversation_id=%s update_id=%s",
                context.conversation_id,
                context.update_id,
            )
        finally:
            if stall_monitor_task is not None:
                stall_monitor_task.cancel()
                await asyncio.gather(stall_monitor_task, return_exceptions=True)
        log_event(
            self._logger,
            logging.INFO,
            "chat.dispatch.handler.done",
            conversation_id=context.conversation_id,
            update_id=context.update_id,
        )

    def _build_queue_snapshot_locked(self, conversation_id: str) -> dict[str, Any]:
        queue = self._queues.get(conversation_id)
        active_context = self._active_contexts.get(conversation_id)
        queued_context: Optional[DispatchContext] = None
        if queue:
            try:
                queued_context = queue[0][1]
            except IndexError:
                queued_context = None
        context = active_context or queued_context
        return {
            "conversation_id": conversation_id,
            "platform": context.platform if context is not None else None,
            "chat_id": context.chat_id if context is not None else None,
            "thread_id": context.thread_id if context is not None else None,
            "pending_count": len(queue) if queue is not None else 0,
            "active": active_context is not None,
            "active_update_id": (
                active_context.update_id if active_context is not None else None
            ),
            "active_started_at": self._active_started_at.get(conversation_id),
            "updated_at": now_iso(),
        }

    def _publish_queue_state_locked(self, conversation_id: str) -> None:
        if self._queue_control_store is None:
            return
        snapshot = self._build_queue_snapshot_locked(conversation_id)
        if int(snapshot.get("pending_count") or 0) <= 0 and not bool(
            snapshot.get("active")
        ):
            self._queue_control_store.clear_snapshot(conversation_id)
            return
        self._queue_control_store.record_snapshot(snapshot)

    async def _warn_on_stalled_handler(
        self,
        handler_task: asyncio.Future[None],
        context: DispatchContext,
    ) -> None:
        warning_seconds = self._handler_stalled_warning_seconds
        if warning_seconds is None:
            return
        try:
            await asyncio.sleep(warning_seconds)
        except asyncio.CancelledError:
            return
        if handler_task.done():
            return
        log_event(
            self._logger,
            logging.WARNING,
            "chat.dispatch.handler.stalled",
            conversation_id=context.conversation_id,
            update_id=context.update_id,
            warning_seconds=warning_seconds,
        )

    def _log_background_handler_failure(
        self,
        task: asyncio.Future[None],
        context: DispatchContext,
    ) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        if not isinstance(exc, Exception):
            self._logger.error(
                "chat.dispatch.handler.background_failed conversation_id=%s update_id=%s",
                context.conversation_id,
                context.update_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        log_event(
            self._logger,
            logging.ERROR,
            "chat.dispatch.handler.background_failed",
            conversation_id=context.conversation_id,
            update_id=context.update_id,
            exc=exc,
        )
        self._logger.error(
            "chat.dispatch.handler.background_failed conversation_id=%s update_id=%s",
            context.conversation_id,
            context.update_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def build_dispatch_context(event: ChatEvent) -> DispatchContext:
    """Build a normalized dispatch context from a chat event."""

    if isinstance(event, ChatMessageEvent):
        return DispatchContext(
            conversation_id=conversation_id_for(
                event.thread.platform, event.thread.chat_id, event.thread.thread_id
            ),
            platform=event.thread.platform,
            chat_id=event.thread.chat_id,
            thread_id=event.thread.thread_id,
            user_id=event.from_user_id,
            message_id=event.message.message_id,
            update_id=event.update_id,
            is_edited=event.is_edited,
            has_message=True,
            has_interaction=False,
        )
    return DispatchContext(
        conversation_id=conversation_id_for(
            event.thread.platform, event.thread.chat_id, event.thread.thread_id
        ),
        platform=event.thread.platform,
        chat_id=event.thread.chat_id,
        thread_id=event.thread.thread_id,
        user_id=event.from_user_id,
        message_id=event.message.message_id if event.message else None,
        update_id=event.update_id,
        is_edited=None,
        has_message=False,
        has_interaction=True,
    )


def conversation_id_for(platform: str, chat_id: str, thread_id: Optional[str]) -> str:
    """Build a stable conversation id for queue partitioning."""

    return f"{platform}:{chat_id}:{thread_id or '-'}"


def is_bypass_event(
    event: ChatEvent,
    *,
    interaction_prefixes: Optional[Iterable[str]] = None,
    callback_ids: Optional[Iterable[str]] = None,
    message_texts: Optional[Iterable[str]] = None,
) -> bool:
    """Return True for events that should bypass per-conversation queues."""

    interaction_prefixes = _normalize_casefolded_values(
        interaction_prefixes,
        default=DEFAULT_BYPASS_INTERACTION_PREFIXES,
        parameter_name="interaction_prefixes",
    )
    callback_ids = frozenset(
        _normalize_casefolded_values(
            callback_ids,
            default=DEFAULT_BYPASS_CALLBACK_IDS,
            parameter_name="callback_ids",
        )
    )
    message_texts = frozenset(
        _normalize_casefolded_values(
            message_texts,
            default=DEFAULT_BYPASS_MESSAGE_TEXTS,
            parameter_name="message_texts",
        )
    )

    if isinstance(event, ChatInteractionEvent):
        payload = (event.payload or "").strip()
        payload_lower = payload.lower()
        if payload_lower.startswith(interaction_prefixes):
            return True
        logical = decode_logical_callback(payload)
        if logical and logical.callback_id.lower() in callback_ids:
            return True
    elif isinstance(event, ChatMessageEvent):
        text = (event.text or "").strip().lower()
        return text in message_texts
    return False


async def _resolve_predicate(
    predicate: DispatchPredicate,
    event: ChatEvent,
    context: DispatchContext,
) -> bool:
    result = predicate(event, context)
    if asyncio.iscoroutine(result):
        return bool(await result)
    return bool(result)
