from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

from ...core.logging_utils import log_event
from ...core.orchestration import (
    ManagedThreadDeliveryAttemptResult,
    ManagedThreadDeliveryOutcome,
)


@dataclass(frozen=True)
class ManagedThreadDeliveryCleanupContext:
    delivery_id: str
    idempotency_key: str
    managed_thread_id: str
    managed_turn_id: str
    final_status: str
    assistant_text: str
    error_text: Optional[str] = None
    transport_target: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: Any) -> "ManagedThreadDeliveryCleanupContext":
        return cls(
            delivery_id=str(getattr(record, "delivery_id", "") or ""),
            idempotency_key=str(getattr(record, "idempotency_key", "") or ""),
            managed_thread_id=str(getattr(record, "managed_thread_id", "") or ""),
            managed_turn_id=str(getattr(record, "managed_turn_id", "") or ""),
            final_status=_normalize_terminal_status(
                getattr(getattr(record, "envelope", None), "final_status", "")
            ),
            assistant_text=managed_thread_delivery_success_text(record),
            error_text=(
                str(getattr(getattr(record, "envelope", None), "error_text", "") or "")
                or None
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


def managed_thread_terminal_delivery_send_key(
    record: Any,
    *,
    suffix: Optional[str] = None,
) -> str:
    """Return the shared transport idempotency key for one terminal send."""

    base = str(getattr(record, "idempotency_key", "") or "").strip()
    if not base:
        base = str(getattr(record, "delivery_id", "") or "").strip()
    if not base:
        managed_thread_id = str(getattr(record, "managed_thread_id", "") or "").strip()
        managed_turn_id = str(getattr(record, "managed_turn_id", "") or "").strip()
        base = f"managed-delivery:{managed_thread_id}:{managed_turn_id}"
    normalized_suffix = str(suffix or "").strip()
    return f"{base}:{normalized_suffix}" if normalized_suffix else base


def managed_thread_delivery_success_text(record: Any) -> str:
    output = getattr(getattr(record, "envelope", None), "assistant_output", None)
    if output is None:
        return ""
    return str(getattr(output, "text", "") or "")


def managed_thread_delivery_output_metadata(record: Any) -> dict[str, Any]:
    output = getattr(getattr(record, "envelope", None), "assistant_output", None)
    if output is None:
        return {
            "turn_output_present": False,
            "turn_output_chars": 0,
        }
    metadata = {
        "turn_output_present": True,
        "turn_output_ownership": str(getattr(output, "ownership", "") or ""),
        "turn_output_source": str(getattr(output, "source", "") or ""),
        "turn_output_chars": len(str(getattr(output, "text", "") or "")),
        "turn_output_backend_thread_id": (
            str(getattr(output, "backend_thread_id", "") or "") or None
        ),
        "turn_output_backend_turn_id": (
            str(getattr(output, "backend_turn_id", "") or "") or None
        ),
    }
    provenance = getattr(output, "provenance", None)
    if isinstance(provenance, Mapping) and provenance:
        safe_provenance = dict(provenance)
        matched_prior = str(safe_provenance.pop("matched_prior_text", "") or "")
        if matched_prior:
            safe_provenance["matched_prior_chars"] = len(matched_prior)
        if safe_provenance:
            metadata["turn_output_provenance"] = safe_provenance
    return metadata


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
            log_event(
                _LOGGER,
                logging.INFO,
                "chat.managed_thread.delivery.terminal_send_failed",
                delivery_id=context.delivery_id,
                managed_thread_id=context.managed_thread_id,
                managed_turn_id=context.managed_turn_id,
                final_status=status,
                error=send_result.error,
                **managed_thread_delivery_output_metadata(record),
            )
            return ManagedThreadDeliveryAttemptResult(
                outcome=ManagedThreadDeliveryOutcome.FAILED,
                error=send_result.error,
            )
        log_event(
            _LOGGER,
            logging.INFO,
            "chat.managed_thread.delivery.terminal_send_succeeded",
            delivery_id=context.delivery_id,
            managed_thread_id=context.managed_thread_id,
            managed_turn_id=context.managed_turn_id,
            final_status=status,
            **managed_thread_delivery_output_metadata(record),
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


def _normalize_terminal_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "ok":
        return "ok"
    if normalized == "interrupted":
        return "interrupted"
    return "error"


__all__ = [
    "ManagedThreadDeliveryCleanupContext",
    "ManagedThreadDeliveryCleanupFn",
    "ManagedThreadDeliverySendFn",
    "ManagedThreadDeliverySendResult",
    "deliver_managed_thread_terminal_record",
    "managed_thread_delivery_output_metadata",
    "managed_thread_delivery_success_text",
    "managed_thread_terminal_delivery_send_key",
]
