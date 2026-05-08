"""Shared turn lifecycle helpers for Telegram execution paths.

These helpers extract common patterns from the managed-thread and legacy
execution paths so that turn registration, progress tracking, interrupt
handling, and runtime cleanup follow the same contract.

All helpers stay Telegram-owned; they do not belong in the shared chat/runtime
layer because they operate on Telegram-specific UX primitives (placeholder IDs,
Telegram message routing, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ...constants import TurnKey
from ...helpers import _compose_interrupt_response, is_interrupt_status

if TYPE_CHECKING:
    from ...adapter import TelegramMessage


def resolve_interrupt_status_fallback(
    *,
    turn_status: str,
    runtime: Any,
    turn_id: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Determine interrupt status fallback text for a completed turn.

    Returns (was_interrupted, fallback_text).
    """
    was_interrupted = is_interrupt_status(turn_status)
    fallback_text: Optional[str] = None
    if was_interrupted:
        if (
            runtime.interrupt_message_id is not None
            and runtime.interrupt_turn_id == turn_id
        ):
            fallback_text = "Interrupted."
    elif runtime.interrupt_turn_id == turn_id:
        fallback_text = "Interrupt requested; turn completed."
    return was_interrupted, fallback_text


def compose_interrupt_aware_response(
    response: str,
    *,
    turn_status: str,
) -> str:
    if is_interrupt_status(turn_status):
        return _compose_interrupt_response(response)
    return response


def clear_turn_runtime_state(runtime: Any) -> None:
    runtime.current_turn_id = None
    runtime.current_turn_key = None
    runtime.interrupt_requested = False


async def try_register_turn_and_start_progress(
    handlers: Any,
    *,
    thread_id: str,
    turn_id: str,
    topic_key: str,
    message: TelegramMessage,
    placeholder_id: Optional[int],
    runtime: Any,
    agent: str,
    model: Optional[str],
    label: str = "working",
    placeholder_reused: bool = False,
) -> Optional[TurnKey]:
    """Register turn context and start progress tracking.

    Returns the turn key on success, or None if registration failed
    (collision). On failure, caller is responsible for sending a failure
    response and cleaning up runtime state.
    """
    turn_key = handlers._turn_key(thread_id, turn_id)
    if turn_key is None:
        return None

    from ...types import TurnContext

    ctx = TurnContext(
        topic_key=topic_key,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        codex_thread_id=thread_id,
        reply_to_message_id=message.message_id,
        placeholder_message_id=placeholder_id,
        placeholder_reused=placeholder_reused,
    )
    if not handlers._register_turn_context(turn_key, turn_id, ctx):
        return None

    runtime.current_turn_id = turn_id
    runtime.current_turn_key = turn_key
    await handlers._start_turn_progress(
        turn_key,
        ctx=ctx,
        agent=agent,
        model=model,
        label=label,
    )
    return turn_key
