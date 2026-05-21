from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import Request

from .....core.lifecycle_events import LifecycleEvent, LifecycleEventType
from .....core.time_utils import now_iso
from ...services.pma import get_pma_request_context
from ...services.pma.common import normalize_optional_text

logger = logging.getLogger(__name__)


_LIFECYCLE_EVENT_BY_TO_STATE = {
    "running": LifecycleEventType.FLOW_STARTED,
    "started": LifecycleEventType.FLOW_STARTED,
    "resumed": LifecycleEventType.FLOW_RESUMED,
    "paused": LifecycleEventType.FLOW_PAUSED,
    "blocked": LifecycleEventType.FLOW_PAUSED,
    "completed": LifecycleEventType.FLOW_COMPLETED,
    "succeeded": LifecycleEventType.FLOW_COMPLETED,
    "failed": LifecycleEventType.FLOW_FAILED,
    "error": LifecycleEventType.FLOW_FAILED,
    "stopped": LifecycleEventType.FLOW_STOPPED,
    "cancelled": LifecycleEventType.FLOW_STOPPED,
    "canceled": LifecycleEventType.FLOW_STOPPED,
}


async def await_if_needed(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


async def notify_hub_automation_transition(
    request: Request,
    runtime_state: Any = None,
    *,
    repo_id: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    from_state: str,
    to_state: str,
    reason: Optional[str] = None,
    timestamp: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    payload: dict[str, Any] = {
        "from_state": (from_state or "").strip(),
        "to_state": (to_state or "").strip(),
        "reason": normalize_optional_text(reason) or "",
        "timestamp": normalize_optional_text(timestamp) or now_iso(),
    }
    normalized_repo_id = normalize_optional_text(repo_id)
    normalized_resource_kind = normalize_optional_text(resource_kind)
    normalized_resource_id = normalize_optional_text(resource_id)
    normalized_run_id = normalize_optional_text(run_id)
    normalized_thread_id = normalize_optional_text(thread_id)
    if normalized_repo_id:
        payload["repo_id"] = normalized_repo_id
    if normalized_resource_kind:
        payload["resource_kind"] = normalized_resource_kind
    if normalized_resource_id:
        payload["resource_id"] = normalized_resource_id
    if normalized_run_id:
        payload["run_id"] = normalized_run_id
    if normalized_thread_id:
        payload["thread_id"] = normalized_thread_id
    if isinstance(extra, dict):
        payload.update(extra)

    supervisor = get_pma_request_context(request).hub_supervisor
    if supervisor is None:
        return
    trigger = getattr(supervisor, "trigger_pma_from_lifecycle_event", None)
    if not callable(trigger):
        return
    try:
        await await_if_needed(
            trigger(_lifecycle_event_from_transition_payload(payload))
        )
    except (
        RuntimeError,
        OSError,
        TypeError,
        ValueError,
    ):  # notification must not disrupt caller
        logger.exception("Failed to notify hub automation transition")
        return

    process_now = getattr(supervisor, "process_automation_now", None)
    if not callable(process_now):
        return
    try:
        await await_if_needed(process_now(include_timers=False))
    except TypeError:
        try:
            await await_if_needed(process_now())
        except (RuntimeError, OSError, ValueError):
            logger.exception("Failed immediate automation processing")
    except (RuntimeError, OSError, ValueError):
        logger.exception("Failed immediate automation processing")


def _lifecycle_event_from_transition_payload(payload: dict[str, Any]) -> LifecycleEvent:
    to_state = str(payload.get("to_state") or "").strip().lower()
    event_type = _LIFECYCLE_EVENT_BY_TO_STATE.get(
        to_state, LifecycleEventType.FLOW_FAILED
    )
    event_id = normalize_optional_text(
        payload.get("transition_id")
    ) or normalize_optional_text(payload.get("idempotency_key"))
    return LifecycleEvent(
        event_type=event_type,
        repo_id=normalize_optional_text(payload.get("repo_id")) or "",
        run_id=normalize_optional_text(payload.get("run_id")) or "",
        data=dict(payload),
        origin="web_pma_route",
        timestamp=normalize_optional_text(payload.get("timestamp")) or now_iso(),
        event_id=event_id or "",
    )


async def notify_managed_thread_terminal_transition(
    request: Request,
    runtime_state: Any = None,
    *,
    thread: dict[str, Any],
    managed_thread_id: str,
    managed_turn_id: str,
    to_state: str,
    reason: str,
) -> None:
    normalized_to_state = (to_state or "").strip().lower() or "failed"
    await notify_hub_automation_transition(
        request,
        runtime_state,
        repo_id=normalize_optional_text(thread.get("repo_id")),
        resource_kind=normalize_optional_text(thread.get("resource_kind")),
        resource_id=normalize_optional_text(thread.get("resource_id")),
        run_id=None,
        thread_id=managed_thread_id,
        from_state="running",
        to_state=normalized_to_state,
        reason=reason,
        extra={
            "event_type": f"managed_thread_{normalized_to_state}",
            "transition_id": f"managed_turn:{managed_turn_id}:{normalized_to_state}",
            "idempotency_key": (
                f"managed_turn:{managed_turn_id}:{normalized_to_state}"
            ),
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "agent": normalize_optional_text(thread.get("agent")) or "",
        },
    )
