"""Shared immediate-feedback primitives for chat adapter implementations.

This module provides platform-agnostic primitives for acknowledging user
actions, creating visible anchors, publishing queue/working states, and
requesting interrupts. Transport adapters (Telegram, Discord) delegate to
these primitives so that the user sees a stable visible signal quickly even
when actual execution remains queued.

All primitives operate through the shared ``ChatTransport`` protocol and
``ChatOperationState`` control-plane types so that no transport-local code
owns durable lifecycle authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from ...core.logging_utils import log_event
from ...core.orchestration.chat_operation_state import ChatOperationState
from ...core.state import now_iso
from .action_ux_contract import (
    AnchorMessageReuse,
    ChatActionAckClass,
    ChatActionUxContractEntry,
)
from .models import (
    ChatAction,
    ChatInteractionRef,
    ChatMessageRef,
    ChatThreadRef,
)
from .transport import ChatTransport


@dataclass(frozen=True)
class ImmediateAckResult:
    ack_class: ChatActionAckClass
    acknowledged: bool
    anchor_ref: Optional[str] = None
    first_visible_feedback_at: Optional[str] = None


@dataclass(frozen=True)
class WorkingAnchorResult:
    anchor_ref: Optional[str]
    created: bool
    reused: bool
    message_ref: Optional[ChatMessageRef] = None


@dataclass(frozen=True)
class QueuedNoticeResult:
    anchor_ref: Optional[str]
    published: bool
    message_ref: Optional[ChatMessageRef] = None


@dataclass(frozen=True)
class InterruptNoticeResult:
    published: bool
    anchor_ref: Optional[str] = None


@dataclass(frozen=True)
class OptimisticButtonUpdateResult:
    updated: bool
    anchor_ref: Optional[str] = None


QUEUED_NOTICE_TEXT = "Queued (waiting for available worker...)"
WORKING_PLACEHOLDER_TEXT = "Working..."
INTERRUPT_REQUESTED_TEXT = "Interrupt requested..."


@runtime_checkable
class ChatOperationStateWriter(Protocol):
    async def __call__(
        self,
        operation_id: Optional[str],
        *,
        state: ChatOperationState,
        **changes: Any,
    ) -> None: ...


@runtime_checkable
class ChatBusyChecker(Protocol):
    def __call__(self, conversation_id: str) -> bool: ...


async def immediate_ack(
    transport: ChatTransport,
    interaction: Optional[ChatInteractionRef],
    *,
    ack_class: ChatActionAckClass,
    operation_id: Optional[str] = None,
    state_writer: Optional[ChatOperationStateWriter] = None,
    anchor_message_reuse: AnchorMessageReuse = "never",
    existing_anchor: Optional[ChatMessageRef] = None,
    logger: Optional[logging.Logger] = None,
) -> ImmediateAckResult:
    if interaction is None:
        return ImmediateAckResult(
            ack_class=ack_class,
            acknowledged=False,
        )

    anchor_ref: Optional[str] = None
    if existing_anchor is not None:
        anchor_ref = existing_anchor.message_id

    try:
        await transport.ack_interaction(interaction)
    except Exception as exc:
        _log_ack_failure(logger, exc, interaction)
        return ImmediateAckResult(
            ack_class=ack_class,
            acknowledged=False,
            anchor_ref=anchor_ref,
        )

    timestamp = now_iso()
    if anchor_ref is None and interaction is not None:
        anchor_ref = interaction.interaction_id

    if state_writer is not None and operation_id is not None:
        await state_writer(
            operation_id,
            state=ChatOperationState.ACKNOWLEDGED,
            ack_completed_at=timestamp,
            first_visible_feedback_at=timestamp,
            anchor_ref=anchor_ref,
        )

    return ImmediateAckResult(
        ack_class=ack_class,
        acknowledged=True,
        anchor_ref=anchor_ref,
        first_visible_feedback_at=timestamp,
    )


async def create_or_reuse_working_anchor(
    transport: ChatTransport,
    thread: ChatThreadRef,
    *,
    reply_to: Optional[ChatMessageRef] = None,
    text: str = WORKING_PLACEHOLDER_TEXT,
    actions: Sequence[ChatAction] = (),
    anchor_reuse: AnchorMessageReuse = "never",
    existing_anchor: Optional[ChatMessageRef] = None,
    operation_id: Optional[str] = None,
    state_writer: Optional[ChatOperationStateWriter] = None,
    logger: Optional[logging.Logger] = None,
) -> WorkingAnchorResult:
    if existing_anchor is not None and anchor_reuse in ("prefer", "require"):
        if state_writer is not None and operation_id is not None:
            await state_writer(
                operation_id,
                state=ChatOperationState.VISIBLE,
                first_visible_feedback_at=now_iso(),
                anchor_ref=existing_anchor.message_id,
            )
        return WorkingAnchorResult(
            anchor_ref=existing_anchor.message_id,
            created=False,
            reused=True,
            message_ref=existing_anchor,
        )

    if anchor_reuse == "require" and existing_anchor is None:
        if logger:
            log_event(
                logger,
                logging.WARNING,
                "chat.feedback.anchor_required_missing",
                operation_id=operation_id,
                thread_chat_id=thread.chat_id,
            )
        return WorkingAnchorResult(
            anchor_ref=None,
            created=False,
            reused=False,
        )

    try:
        if actions:
            message_ref = await transport.present_actions(
                thread,
                text,
                actions=actions,
                reply_to=reply_to,
            )
        else:
            message_ref = await transport.send_text(
                thread,
                text,
                reply_to=reply_to,
            )
    except Exception as exc:
        _log_transport_failure(logger, "create_working_anchor", exc, thread)
        return WorkingAnchorResult(
            anchor_ref=None,
            created=False,
            reused=False,
        )

    timestamp = now_iso()
    if state_writer is not None and operation_id is not None:
        await state_writer(
            operation_id,
            state=ChatOperationState.VISIBLE,
            first_visible_feedback_at=timestamp,
            anchor_ref=message_ref.message_id,
        )

    return WorkingAnchorResult(
        anchor_ref=message_ref.message_id,
        created=True,
        reused=False,
        message_ref=message_ref,
    )


async def publish_queued_notice(
    transport: ChatTransport,
    thread: ChatThreadRef,
    *,
    reply_to: Optional[ChatMessageRef] = None,
    text: str = QUEUED_NOTICE_TEXT,
    actions: Sequence[ChatAction] = (),
    cancel_action_id: str = "cancel:queue_cancel",
    cancel_label: str = "Cancel",
    operation_id: Optional[str] = None,
    state_writer: Optional[ChatOperationStateWriter] = None,
    logger: Optional[logging.Logger] = None,
) -> QueuedNoticeResult:
    effective_actions = tuple(actions)
    if not effective_actions:
        effective_actions = (
            ChatAction(label=cancel_label, action_id=cancel_action_id),
        )

    try:
        message_ref = await transport.present_actions(
            thread,
            text,
            actions=effective_actions,
            reply_to=reply_to,
        )
    except Exception as exc:
        _log_transport_failure(logger, "publish_queued_notice", exc, thread)
        return QueuedNoticeResult(anchor_ref=None, published=False)

    if state_writer is not None and operation_id is not None:
        await state_writer(
            operation_id,
            state=ChatOperationState.VISIBLE,
            first_visible_feedback_at=now_iso(),
            anchor_ref=message_ref.message_id,
        )

    return QueuedNoticeResult(
        anchor_ref=message_ref.message_id,
        published=True,
        message_ref=message_ref,
    )


async def publish_interrupt_notice(
    transport: ChatTransport,
    thread: ChatThreadRef,
    *,
    anchor_ref: Optional[str] = None,
    text: str = INTERRUPT_REQUESTED_TEXT,
    operation_id: Optional[str] = None,
    state_writer: Optional[ChatOperationStateWriter] = None,
    logger: Optional[logging.Logger] = None,
) -> InterruptNoticeResult:
    if state_writer is not None and operation_id is not None:
        await state_writer(
            operation_id,
            state=ChatOperationState.INTERRUPTING,
            interrupt_ref=anchor_ref,
        )

    return InterruptNoticeResult(
        published=True,
        anchor_ref=anchor_ref,
    )


async def optimistic_button_update(
    transport: ChatTransport,
    anchor: ChatMessageRef,
    *,
    text: Optional[str] = None,
    actions: Sequence[ChatAction] = (),
    operation_id: Optional[str] = None,
    state_writer: Optional[ChatOperationStateWriter] = None,
    logger: Optional[logging.Logger] = None,
) -> OptimisticButtonUpdateResult:
    if text is None and not actions:
        return OptimisticButtonUpdateResult(
            updated=False,
            anchor_ref=anchor.message_id,
        )

    try:
        await transport.edit_text(
            anchor,
            text or "",
            actions=actions,
        )
    except Exception as exc:
        _log_transport_failure(logger, "optimistic_button_update", exc, anchor.thread)
        return OptimisticButtonUpdateResult(
            updated=False,
            anchor_ref=anchor.message_id,
        )

    return OptimisticButtonUpdateResult(
        updated=True,
        anchor_ref=anchor.message_id,
    )


async def ack_and_enqueue(
    transport: ChatTransport,
    thread: ChatThreadRef,
    *,
    interaction: Optional[ChatInteractionRef] = None,
    reply_to: Optional[ChatMessageRef] = None,
    ux_entry: Optional[ChatActionUxContractEntry] = None,
    operation_id: Optional[str] = None,
    state_writer: Optional[ChatOperationStateWriter] = None,
    is_busy: bool = False,
    cancel_action_id: str = "cancel:queue_cancel",
    logger: Optional[logging.Logger] = None,
) -> tuple[Optional[ImmediateAckResult], Optional[QueuedNoticeResult]]:
    ack_result: Optional[ImmediateAckResult] = None

    if interaction is not None:
        ack_class = ux_entry.ack_class if ux_entry else "callback_answer"
        ack_result = await immediate_ack(
            transport,
            interaction,
            ack_class=ack_class,
            operation_id=operation_id,
            state_writer=state_writer,
            logger=logger,
        )

    if not is_busy:
        return ack_result, None

    queued_result = await publish_queued_notice(
        transport,
        thread,
        reply_to=reply_to,
        operation_id=operation_id,
        state_writer=state_writer,
        cancel_action_id=cancel_action_id,
        logger=logger,
    )

    if state_writer is not None and operation_id is not None:
        await state_writer(
            operation_id,
            state=ChatOperationState.QUEUED,
        )

    return ack_result, queued_result


def _log_ack_failure(
    logger: Optional[logging.Logger],
    exc: Exception,
    interaction: Optional[ChatInteractionRef],
) -> None:
    if logger is None:
        return
    log_event(
        logger,
        logging.WARNING,
        "chat.feedback.ack_failed",
        interaction_id=(interaction.interaction_id if interaction else None),
        exc=exc,
    )


def _log_transport_failure(
    logger: Optional[logging.Logger],
    operation: str,
    exc: Exception,
    thread: ChatThreadRef,
) -> None:
    if logger is None:
        return
    log_event(
        logger,
        logging.WARNING,
        f"chat.feedback.{operation}.failed",
        platform=thread.platform,
        chat_id=thread.chat_id,
        thread_id=thread.thread_id,
        exc=exc,
    )


__all__ = [
    "INTERRUPT_REQUESTED_TEXT",
    "QUEUED_NOTICE_TEXT",
    "WORKING_PLACEHOLDER_TEXT",
    "ChatBusyChecker",
    "ChatOperationStateWriter",
    "ImmediateAckResult",
    "InterruptNoticeResult",
    "OptimisticButtonUpdateResult",
    "QueuedNoticeResult",
    "WorkingAnchorResult",
    "ack_and_enqueue",
    "create_or_reuse_working_anchor",
    "immediate_ack",
    "optimistic_button_update",
    "publish_interrupt_notice",
    "publish_queued_notice",
]
