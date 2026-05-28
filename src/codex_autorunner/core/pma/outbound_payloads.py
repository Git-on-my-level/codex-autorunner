from __future__ import annotations

from typing import Any, Optional

from ..orchestration.runtime_threads import (
    RUNTIME_THREAD_INTERRUPTED_ERROR,
    RUNTIME_THREAD_TIMEOUT_ERROR,
)
from ..text_utils import _normalize_optional_text

MANAGED_THREAD_PUBLIC_EXECUTION_ERROR = "Managed thread execution failed"


def sanitize_managed_thread_result_error(detail: Any) -> str:
    sanitized = _normalize_optional_text(detail)
    if sanitized in {RUNTIME_THREAD_TIMEOUT_ERROR, "Managed thread timed out"}:
        return "Managed thread timed out"
    if sanitized in {RUNTIME_THREAD_INTERRUPTED_ERROR, "Managed thread interrupted"}:
        return "Managed thread interrupted"
    return MANAGED_THREAD_PUBLIC_EXECUTION_ERROR


def build_interrupt_failure_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: Optional[str],
    backend_thread_id: str,
    detail: str,
    delivery_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "rejected",
        "interrupt_state": "failed",
        "execution_state": "running",
        "reason": "interrupt_failed",
        "detail": detail,
        "next_step": (
            "Wait for the active turn to finish, inspect thread status, "
            "or retry the interrupt after checking runtime health."
        ),
        "active_turn_status": "running",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "active_managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": detail,
        **delivery_payload,
    }


def build_archived_thread_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
) -> dict[str, Any]:
    detail = "Managed thread is archived and read-only"
    return {
        "status": "error",
        "send_state": "rejected",
        "reason": "thread_archived",
        "detail": detail,
        "next_step": "Use `car pma thread resume` or spawn a new thread.",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": detail,
    }


def build_not_active_thread_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
    exc: Any,
) -> dict[str, Any]:
    detail = (
        "Managed thread is archived and read-only"
        if getattr(exc, "status", None) == "archived"
        else "Managed thread is not active"
    )
    return {
        "status": "error",
        "send_state": "rejected",
        "reason": "thread_not_active",
        "detail": detail,
        "next_step": "Resume the thread or create a new active thread.",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": detail,
    }


def build_running_turn_exists_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
    running_turn: Optional[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "already_in_flight",
        "reason": "running_turn_exists",
        "detail": f"Managed thread {managed_thread_id} already has a running turn",
        "next_step": (
            "Wait for the running turn to finish or let the default terminal "
            "automation job fire; use --watch only for foreground babysitting."
        ),
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": str((running_turn or {}).get("managed_turn_id") or "")
        or None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": "Managed thread already has a running turn",
    }


def build_execution_setup_error_payload(
    *,
    managed_thread_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "accepted",
        "execution_state": "completed",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": None,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
        **delivery_payload,
    }


def build_started_execution_error_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    error: str,
    delivery_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "error",
        "send_state": "accepted",
        "execution_state": "completed",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": error,
        **delivery_payload,
    }


def build_queued_send_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
    queue_depth: int,
    active_managed_turn_id: Optional[str],
    notification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "send_state": "queued",
        "execution_state": "queued",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": None,
        **delivery_payload,
        "queue_depth": queue_depth,
        "active_managed_turn_id": active_managed_turn_id,
    }
    if notification is not None:
        payload["notification"] = notification
    return payload


def build_accepted_send_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
    notification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "send_state": "accepted",
        "execution_state": "running",
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": None,
        **delivery_payload,
    }
    if notification is not None:
        payload["notification"] = notification
    return payload


def build_enqueued_send_payload(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    delivery_payload: dict[str, Any],
    execution_state: str,
    queue_depth: Optional[int] = None,
    active_managed_turn_id: Optional[str] = None,
    notification: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "send_state": "enqueued",
        "execution_state": execution_state,
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": "",
        "error": None,
        **delivery_payload,
    }
    if queue_depth is not None:
        payload["queue_depth"] = queue_depth
    if active_managed_turn_id is not None:
        payload["active_managed_turn_id"] = active_managed_turn_id
    if notification is not None:
        payload["notification"] = notification
    return payload


def build_execution_result_payload(
    *,
    status: str,
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: str,
    assistant_text: str,
    error: Optional[str],
    response_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": backend_thread_id,
        "assistant_text": assistant_text,
        "error": error,
        **response_payload,
    }


__all__ = [
    "MANAGED_THREAD_PUBLIC_EXECUTION_ERROR",
    "build_accepted_send_payload",
    "build_archived_thread_payload",
    "build_enqueued_send_payload",
    "build_execution_result_payload",
    "build_execution_setup_error_payload",
    "build_interrupt_failure_payload",
    "build_not_active_thread_payload",
    "build_queued_send_payload",
    "build_running_turn_exists_payload",
    "build_started_execution_error_payload",
    "sanitize_managed_thread_result_error",
]
