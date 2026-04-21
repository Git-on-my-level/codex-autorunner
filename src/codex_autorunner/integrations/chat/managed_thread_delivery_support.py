from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

from ...core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
)


@dataclass(frozen=True)
class ManagedThreadDeliveryCleanupContext:
    delivery_id: str
    managed_thread_id: str
    managed_turn_id: str
    final_status: str
    transport_target: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: Any) -> "ManagedThreadDeliveryCleanupContext":
        return cls(
            delivery_id=str(getattr(record, "delivery_id", "") or ""),
            managed_thread_id=str(getattr(record, "managed_thread_id", "") or ""),
            managed_turn_id=str(getattr(record, "managed_turn_id", "") or ""),
            final_status=str(
                getattr(getattr(record, "envelope", None), "final_status", "") or ""
            ),
            transport_target=dict(
                getattr(getattr(record, "target", None), "transport_target", {}) or {}
            ),
            metadata=dict(getattr(record, "metadata", {}) or {}),
        )


@dataclass(frozen=True)
class ManagedThreadDeliverySendResult:
    error: Optional[str] = None
    adapter_cursor: Optional[Mapping[str, Any]] = None


ManagedThreadDeliverySendFn = Callable[
    [ManagedThreadDeliveryCleanupContext],
    Awaitable[Optional[ManagedThreadDeliverySendResult]],
]
ManagedThreadDeliveryCleanupFn = Callable[
    [ManagedThreadDeliveryCleanupContext],
    Awaitable[None],
]

_LOGGER = logging.getLogger(__name__)


async def deliver_managed_thread_terminal_record(
    record: Any,
    *,
    send_success: ManagedThreadDeliverySendFn,
    send_failure: ManagedThreadDeliverySendFn,
    cleanup: Optional[ManagedThreadDeliveryCleanupFn] = None,
    cleanup_statuses: frozenset[str] = frozenset({"ok"}),
) -> ManagedThreadDeliveryAttemptResult:
    context = ManagedThreadDeliveryCleanupContext.from_record(record)
    status = context.final_status
    if status == "interrupted":
        return ManagedThreadDeliveryAttemptResult(
            outcome=ManagedThreadDeliveryOutcome.ABANDONED,
            error="interrupted_turn_has_no_terminal_delivery",
        )
    send = send_success if status == "ok" else send_failure
    try:
        send_result = await send(context)
        if send_result is None:
            send_result = ManagedThreadDeliverySendResult()
        if send_result.error:
            return ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.FAILED,
                error=send_result.error,
            )
        if cleanup is not None and status in cleanup_statuses:
            try:
                await cleanup(context)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning(
                    "Post-delivery cleanup failed after terminal send (best-effort; "
                    "delivery still counted as succeeded): delivery_id=%s thread=%s turn=%s",
                    context.delivery_id,
                    context.managed_thread_id,
                    context.managed_turn_id,
                    exc_info=True,
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
        adapter_cursor=(
            dict(send_result.adapter_cursor)
            if send_result.adapter_cursor is not None
            else None
        ),
    )


__all__ = [
    "ManagedThreadDeliveryCleanupContext",
    "ManagedThreadDeliveryCleanupFn",
    "ManagedThreadDeliverySendFn",
    "ManagedThreadDeliverySendResult",
    "deliver_managed_thread_terminal_record",
]
