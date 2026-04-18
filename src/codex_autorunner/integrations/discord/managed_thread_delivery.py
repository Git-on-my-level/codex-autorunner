"""Shared Discord durable delivery adapter logic for managed-thread records."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ...core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
)
from ...integrations.chat.managed_thread_turns import (
    render_managed_thread_delivery_record_text,
)
from .constants import DISCORD_MAX_MESSAGE_LENGTH
from .rendering import chunk_discord_message, format_discord_message


async def deliver_discord_managed_thread_record(
    service: Any,
    record: Any,
    *,
    claim: Any,
    channel_id_fallback: Optional[str],
    base_record_label: str,
    error_record_label: str,
    default_execution_error: str,
) -> ManagedThreadDeliveryAttemptResult:
    """Deliver a managed-thread delivery record to Discord (chunks ok-path; errors → message)."""
    _ = claim
    target_channel_id = _resolve_delivery_channel_id(record, fallback=channel_id_fallback)
    if not target_channel_id:
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.ABANDONED,
            error="missing_discord_channel_id",
        )
    if record.envelope.final_status == "ok":
        assistant_text = render_managed_thread_delivery_record_text(record)
        formatted = (
            format_discord_message(assistant_text)
            if assistant_text
            else "(No response text returned.)"
        )
        chunks = chunk_discord_message(
            formatted,
            max_len=DISCORD_MAX_MESSAGE_LENGTH,
            with_numbering=False,
        )
        if not chunks:
            chunks = [formatted]
        base_record_id = (
            f"{base_record_label}:{record.managed_thread_id}:{record.managed_turn_id}"
        )
        try:
            for chunk_index, chunk in enumerate(chunks, start=1):
                record_id = (
                    f"{base_record_id}:chunk:{chunk_index}"
                    if len(chunks) > 1
                    else base_record_id
                )
                await service._send_channel_message_safe(
                    target_channel_id,
                    {"content": chunk},
                    record_id=record_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.FAILED,
                error=str(exc) or exc.__class__.__name__,
            )
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.DELIVERED,
            adapter_cursor={"chunk_count": len(chunks)},
        )
    if record.envelope.final_status == "interrupted":
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.ABANDONED,
            error="interrupted_turn_has_no_terminal_delivery",
        )
    try:
        await service._send_channel_message_safe(
            target_channel_id,
            {
                "content": (
                    f"Turn failed: {record.envelope.error_text or default_execution_error}"
                )
            },
            record_id=(
                f"{error_record_label}:{record.managed_thread_id}:{record.managed_turn_id}"
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.FAILED,
            error=str(exc) or exc.__class__.__name__,
        )
    return ManagedThreadDeliveryAttemptResult(outcome=ManagedThreadDeliveryOutcome.DELIVERED)


def _resolve_delivery_channel_id(record: Any, *, fallback: Optional[str]) -> str:
    tt = record.target.transport_target
    if fallback is not None:
        return str(tt.get("channel_id") or fallback).strip()
    return str(tt.get("channel_id", "")).strip()
