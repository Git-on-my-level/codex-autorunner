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
from typing import (
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
from .callbacks import (
    CALLBACK_APPROVAL,
    CALLBACK_QUESTION_CANCEL,
    CALLBACK_QUESTION_CUSTOM,
    CALLBACK_QUESTION_DONE,
    CALLBACK_QUESTION_OPTION,
    decode_logical_callback,
)
from .models import ChatEvent, ChatInteractionEvent, ChatMessageEvent

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
        allowlist_predicate: Optional[DispatchPredicate] = None,
        dedupe_predicate: Optional[DispatchPredicate] = None,
        bypass_predicate: Optional[DispatchPredicate] = None,
        bypass_interaction_prefixes: Optional[Iterable[str]] = None,
        bypass_callback_ids: Optional[Iterable[str]] = None,
        bypass_message_texts: Optional[Iterable[str]] = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._allowlist_predicate = allowlist_predicate
        self._dedupe_predicate = dedupe_predicate
        self._bypass_predicate = bypass_predicate
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
        self._lock = asyncio.Lock()
        self._queues: Dict[
            str, Deque[tuple[ChatEvent, DispatchContext, DispatchHandler]]
        ] = {}
        self._workers: Dict[str, asyncio.Task[None]] = {}
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

        await self._enqueue(context.conversation_id, event, context, handler)
        return DispatchResult(status="queued", context=context)

    async def wait_idle(self) -> None:
        """Wait until no queued or active handlers remain."""

        await self._idle_event.wait()

    async def _enqueue(
        self,
        conversation_id: str,
        event: ChatEvent,
        context: DispatchContext,
        handler: DispatchHandler,
    ) -> None:
        async with self._lock:
            queue = self._queues.get(conversation_id)
            if queue is None:
                queue = deque()
                self._queues[conversation_id] = queue
            queue.append((event, context, handler))
            self._idle_event.clear()
            if conversation_id not in self._workers:
                self._workers[conversation_id] = asyncio.create_task(
                    self._drain_conversation(conversation_id)
                )
            pending = len(queue)
        log_event(
            self._logger,
            logging.INFO,
            "chat.dispatch.queued",
            conversation_id=conversation_id,
            update_id=context.update_id,
            pending=pending,
        )

    async def _drain_conversation(self, conversation_id: str) -> None:
        try:
            while True:
                async with self._lock:
                    queue = self._queues.get(conversation_id)
                    if not queue:
                        self._queues.pop(conversation_id, None)
                        self._workers.pop(conversation_id, None)
                        if not self._workers and self._active_handlers == 0:
                            self._idle_event.set()
                        return
                    event, context, handler = queue.popleft()
                    self._active_handlers += 1
                try:
                    await self._run_handler(event, context, handler)
                finally:
                    async with self._lock:
                        self._active_handlers -= 1
                        if (
                            self._active_handlers == 0
                            and not self._workers
                            and not self._queues
                        ):
                            self._idle_event.set()
        finally:
            async with self._lock:
                self._workers.pop(conversation_id, None)
                if (
                    self._active_handlers == 0
                    and not self._workers
                    and not self._queues
                ):
                    self._idle_event.set()

    async def _run_handler(
        self,
        event: ChatEvent,
        context: DispatchContext,
        handler: DispatchHandler,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "chat.dispatch.handler.start",
            conversation_id=context.conversation_id,
            update_id=context.update_id,
        )
        try:
            await handler(event, context)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "chat.dispatch.handler.failed",
                conversation_id=context.conversation_id,
                update_id=context.update_id,
                exc=exc,
            )
        log_event(
            self._logger,
            logging.INFO,
            "chat.dispatch.handler.done",
            conversation_id=context.conversation_id,
            update_id=context.update_id,
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
