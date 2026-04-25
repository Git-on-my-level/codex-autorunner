from __future__ import annotations

import uuid
from typing import Any, Mapping, Optional

from .constants import (
    DELIVERY_MODE_AUTO,
    SUBSCRIPTION_STATE_ACTIVE,
    TIMER_STATE_PENDING,
    WAKEUP_STATE_PENDING,
)
from .events import PmaDomainEvent, PmaDomainEventType
from .models import (
    PmaDeliveryAttempt,
    PmaDeliveryIntent,
    PmaDeliveryState,
    PmaDeliveryTarget,
    PmaDispatchAttempt,
    PmaDispatchDecision,
    PmaOriginContext,
    PmaSubscription,
    PmaTimer,
    PmaWakeup,
    _iso_now,
    _normalize_bool,
    _normalize_lane_id,
    _normalize_non_negative_int,
    _normalize_positive_int,
    _normalize_surface_kind,
    _normalize_text,
    _normalize_text_list,
    _normalize_timer_type,
)


def _stamp() -> str:
    return _iso_now()


def _fallback_text(value: Any, *, fallback: str = "") -> str:
    text = _normalize_text(value)
    return text if text is not None else fallback


def _dict_field(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# PmaOriginContext
# ---------------------------------------------------------------------------


def normalize_pma_origin_context(value: Any) -> Optional[PmaOriginContext]:
    if not isinstance(value, dict):
        return None
    origin = PmaOriginContext(
        thread_id=_normalize_text(value.get("thread_id")),
        lane_id=_normalize_text(value.get("lane_id")),
        agent=_normalize_text(value.get("agent")),
        profile=_normalize_text(value.get("profile")),
    )
    return None if origin.is_empty() else origin


def pma_origin_context_to_dict(origin: PmaOriginContext) -> dict[str, str]:
    return origin.to_metadata()


# ---------------------------------------------------------------------------
# PmaSubscription
# ---------------------------------------------------------------------------


def normalize_pma_subscription(data: Any) -> Optional[PmaSubscription]:
    if isinstance(data, PmaSubscription):
        return data
    if not isinstance(data, dict):
        return None
    metadata_raw = data.get("metadata")
    metadata = _dict_field(metadata_raw)
    notify_once = _normalize_bool(data.get("notify_once"), fallback=False)
    if isinstance(metadata_raw, dict):
        metadata = {k: v for k, v in metadata_raw.items() if k != "notify_once"}
    max_matches = _normalize_positive_int(data.get("max_matches"), fallback=None)
    if max_matches is None and notify_once:
        max_matches = 1
    match_count = _normalize_non_negative_int(data.get("match_count"), fallback=0) or 0
    return PmaSubscription(
        subscription_id=_fallback_text(
            data.get("subscription_id"), fallback=str(uuid.uuid4())
        ),
        created_at=_fallback_text(data.get("created_at"), fallback=_stamp()),
        updated_at=_fallback_text(
            data.get("updated_at"),
            fallback=_fallback_text(data.get("created_at"), fallback=_stamp()),
        ),
        state=_fallback_text(
            data.get("state"), fallback=SUBSCRIPTION_STATE_ACTIVE
        ).lower(),
        event_types=tuple(_normalize_text_list(data.get("event_types"))),
        repo_id=_normalize_text(data.get("repo_id")),
        run_id=_normalize_text(data.get("run_id")),
        thread_id=_normalize_text(data.get("thread_id")),
        lane_id=_normalize_lane_id(data.get("lane_id")),
        from_state=_normalize_text(data.get("from_state")),
        to_state=_normalize_text(data.get("to_state")),
        reason=_normalize_text(data.get("reason")),
        idempotency_key=_normalize_text(data.get("idempotency_key")),
        max_matches=max_matches,
        match_count=match_count,
        metadata=metadata,
    )


def pma_subscription_to_dict(sub: PmaSubscription) -> dict[str, Any]:
    return {
        "subscription_id": sub.subscription_id,
        "created_at": sub.created_at,
        "updated_at": sub.updated_at,
        "state": sub.state,
        "event_types": list(sub.event_types),
        "repo_id": sub.repo_id,
        "run_id": sub.run_id,
        "thread_id": sub.thread_id,
        "lane_id": sub.lane_id,
        "from_state": sub.from_state,
        "to_state": sub.to_state,
        "reason": sub.reason,
        "idempotency_key": sub.idempotency_key,
        "max_matches": sub.max_matches,
        "match_count": sub.match_count,
        "metadata": sub.metadata,
    }


# ---------------------------------------------------------------------------
# PmaTimer
# ---------------------------------------------------------------------------


def normalize_pma_timer(data: Any) -> Optional[PmaTimer]:
    if isinstance(data, PmaTimer):
        return data
    if not isinstance(data, dict):
        return None
    metadata_raw = data.get("metadata")
    metadata = _dict_field(metadata_raw)
    return PmaTimer(
        timer_id=_fallback_text(data.get("timer_id"), fallback=str(uuid.uuid4())),
        due_at=_fallback_text(data.get("due_at"), fallback=_stamp()),
        created_at=_fallback_text(data.get("created_at"), fallback=_stamp()),
        updated_at=_fallback_text(
            data.get("updated_at"),
            fallback=_fallback_text(data.get("created_at"), fallback=_stamp()),
        ),
        state=_fallback_text(data.get("state"), fallback=TIMER_STATE_PENDING).lower(),
        fired_at=_normalize_text(data.get("fired_at")),
        timer_type=_normalize_timer_type(data.get("timer_type")),
        idle_seconds=_normalize_non_negative_int(data.get("idle_seconds")),
        subscription_id=_normalize_text(data.get("subscription_id")),
        repo_id=_normalize_text(data.get("repo_id")),
        run_id=_normalize_text(data.get("run_id")),
        thread_id=_normalize_text(data.get("thread_id")),
        lane_id=_normalize_lane_id(data.get("lane_id")),
        from_state=_normalize_text(data.get("from_state")),
        to_state=_normalize_text(data.get("to_state")),
        reason=_normalize_text(data.get("reason")),
        idempotency_key=_normalize_text(data.get("idempotency_key")),
        metadata=metadata,
    )


def pma_timer_to_dict(timer: PmaTimer) -> dict[str, Any]:
    return {
        "timer_id": timer.timer_id,
        "due_at": timer.due_at,
        "created_at": timer.created_at,
        "updated_at": timer.updated_at,
        "state": timer.state,
        "fired_at": timer.fired_at,
        "timer_type": timer.timer_type,
        "idle_seconds": timer.idle_seconds,
        "subscription_id": timer.subscription_id,
        "repo_id": timer.repo_id,
        "run_id": timer.run_id,
        "thread_id": timer.thread_id,
        "lane_id": timer.lane_id,
        "from_state": timer.from_state,
        "to_state": timer.to_state,
        "reason": timer.reason,
        "idempotency_key": timer.idempotency_key,
        "metadata": timer.metadata,
    }


# ---------------------------------------------------------------------------
# PmaWakeup
# ---------------------------------------------------------------------------


def normalize_pma_wakeup(data: Any) -> Optional[PmaWakeup]:
    if isinstance(data, PmaWakeup):
        return data
    if not isinstance(data, dict):
        return None
    event_data = _dict_field(data.get("event_data"))
    metadata = _dict_field(data.get("metadata"))
    return PmaWakeup(
        wakeup_id=_fallback_text(data.get("wakeup_id"), fallback=str(uuid.uuid4())),
        created_at=_fallback_text(data.get("created_at"), fallback=_stamp()),
        updated_at=_fallback_text(
            data.get("updated_at"),
            fallback=_fallback_text(data.get("created_at"), fallback=_stamp()),
        ),
        state=_fallback_text(data.get("state"), fallback=WAKEUP_STATE_PENDING).lower(),
        dispatched_at=_normalize_text(data.get("dispatched_at")),
        source=_fallback_text(data.get("source"), fallback="automation"),
        repo_id=_normalize_text(data.get("repo_id")),
        run_id=_normalize_text(data.get("run_id")),
        thread_id=_normalize_text(data.get("thread_id")),
        lane_id=_normalize_lane_id(data.get("lane_id")),
        from_state=_normalize_text(data.get("from_state")),
        to_state=_normalize_text(data.get("to_state")),
        reason=_normalize_text(data.get("reason")),
        timestamp=_normalize_text(data.get("timestamp")),
        idempotency_key=_normalize_text(data.get("idempotency_key")),
        subscription_id=_normalize_text(data.get("subscription_id")),
        timer_id=_normalize_text(data.get("timer_id")),
        event_id=_normalize_text(data.get("event_id")),
        event_type=_normalize_text(data.get("event_type")),
        event_data=event_data,
        metadata=metadata,
    )


def pma_wakeup_to_dict(wakeup: PmaWakeup) -> dict[str, Any]:
    return {
        "wakeup_id": wakeup.wakeup_id,
        "created_at": wakeup.created_at,
        "updated_at": wakeup.updated_at,
        "state": wakeup.state,
        "dispatched_at": wakeup.dispatched_at,
        "source": wakeup.source,
        "repo_id": wakeup.repo_id,
        "run_id": wakeup.run_id,
        "thread_id": wakeup.thread_id,
        "lane_id": wakeup.lane_id,
        "from_state": wakeup.from_state,
        "to_state": wakeup.to_state,
        "reason": wakeup.reason,
        "timestamp": wakeup.timestamp,
        "idempotency_key": wakeup.idempotency_key,
        "subscription_id": wakeup.subscription_id,
        "timer_id": wakeup.timer_id,
        "event_id": wakeup.event_id,
        "event_type": wakeup.event_type,
        "event_data": wakeup.event_data,
        "metadata": wakeup.metadata,
    }


# ---------------------------------------------------------------------------
# PmaDispatchDecision / PmaDispatchAttempt
# ---------------------------------------------------------------------------


def normalize_pma_dispatch_attempt(data: Any) -> Optional[PmaDispatchAttempt]:
    if isinstance(data, PmaDispatchAttempt):
        return data
    if not isinstance(data, Mapping):
        return None
    route = _normalize_text(data.get("route"))
    delivery_mode = _normalize_text(data.get("delivery_mode"))
    surface_kind = _normalize_text(data.get("surface_kind"))
    if route is None or delivery_mode is None or surface_kind is None:
        return None
    workspace_root_raw = _normalize_text(data.get("workspace_root"))
    return PmaDispatchAttempt(
        route=route,
        delivery_mode=delivery_mode,
        surface_kind=surface_kind,
        surface_key=_normalize_text(data.get("surface_key")),
        repo_id=_normalize_text(data.get("repo_id")),
        workspace_root=workspace_root_raw,
    )


def normalize_pma_dispatch_decision(data: Any) -> Optional[PmaDispatchDecision]:
    if isinstance(data, PmaDispatchDecision):
        return data
    if not isinstance(data, Mapping):
        return None
    requested_delivery = _normalize_text(data.get("requested_delivery"))
    if requested_delivery is None:
        return None
    attempts_raw = data.get("attempts")
    attempts: list[PmaDispatchAttempt] = []
    if isinstance(attempts_raw, (list, tuple)):
        for entry in attempts_raw:
            attempt = normalize_pma_dispatch_attempt(entry)
            if attempt is not None:
                attempts.append(attempt)
    return PmaDispatchDecision(
        requested_delivery=requested_delivery,
        suppress_publish=bool(data.get("suppress_publish")),
        attempts=tuple(attempts),
    )


def pma_dispatch_decision_to_dict(decision: PmaDispatchDecision) -> dict[str, Any]:
    return {
        "requested_delivery": decision.requested_delivery,
        "suppress_publish": bool(decision.suppress_publish),
        "attempts": [
            {
                "route": attempt.route,
                "delivery_mode": attempt.delivery_mode,
                "surface_kind": attempt.surface_kind,
                "surface_key": attempt.surface_key,
                "repo_id": attempt.repo_id,
                "workspace_root": attempt.workspace_root,
            }
            for attempt in decision.attempts
        ],
    }


# ---------------------------------------------------------------------------
# PmaDeliveryTarget / PmaDeliveryAttempt / PmaDeliveryIntent
# ---------------------------------------------------------------------------


def normalize_pma_delivery_target(data: Any) -> Optional[PmaDeliveryTarget]:
    if isinstance(data, PmaDeliveryTarget):
        return data
    if not isinstance(data, dict):
        return None
    surface_kind = _normalize_surface_kind(data.get("surface_kind"))
    surface_key = _normalize_text(data.get("surface_key"))
    if surface_kind is None:
        return None
    return PmaDeliveryTarget(
        surface_kind=surface_kind,
        surface_key=surface_key,
    )


def normalize_pma_delivery_attempt(data: Any) -> Optional[PmaDeliveryAttempt]:
    if isinstance(data, PmaDeliveryAttempt):
        return data
    if not isinstance(data, Mapping):
        return None
    route = _normalize_text(data.get("route"))
    delivery_mode = _normalize_text(data.get("delivery_mode"))
    if route is None or delivery_mode is None:
        return None
    target_data = data.get("target")
    target = normalize_pma_delivery_target(target_data)
    if target is None:
        target = PmaDeliveryTarget(
            surface_kind=_normalize_text(data.get("surface_kind")) or "",
        )
    return PmaDeliveryAttempt(
        route=route,
        delivery_mode=delivery_mode,
        target=target,
        repo_id=_normalize_text(data.get("repo_id")),
        workspace_root=_normalize_text(data.get("workspace_root")),
    )


def normalize_pma_delivery_intent(data: Any) -> Optional[PmaDeliveryIntent]:
    if isinstance(data, PmaDeliveryIntent):
        return data
    if not isinstance(data, Mapping):
        return None
    message = _normalize_text(data.get("message"))
    if message is None:
        return None
    attempts_raw = data.get("attempts")
    attempts: list[PmaDeliveryAttempt] = []
    if isinstance(attempts_raw, (list, tuple)):
        for entry in attempts_raw:
            attempt = normalize_pma_delivery_attempt(entry)
            if attempt is not None:
                attempts.append(attempt)
    return PmaDeliveryIntent(
        message=message,
        correlation_id=_normalize_text(data.get("correlation_id")) or "",
        source_kind=_normalize_text(data.get("source_kind")) or "automation",
        requested_delivery=_normalize_text(data.get("requested_delivery"))
        or DELIVERY_MODE_AUTO,
        attempts=tuple(attempts),
        repo_id=_normalize_text(data.get("repo_id")),
        workspace_root=_normalize_text(data.get("workspace_root")),
        run_id=_normalize_text(data.get("run_id")),
        managed_thread_id=_normalize_text(data.get("managed_thread_id")),
    )


# ---------------------------------------------------------------------------
# PmaDeliveryState
# ---------------------------------------------------------------------------


def normalize_pma_delivery_state(data: Any) -> Optional[PmaDeliveryState]:
    if isinstance(data, PmaDeliveryState):
        return data
    if not isinstance(data, dict):
        return None
    delivery_id = _normalize_text(data.get("delivery_id"))
    if delivery_id is None:
        return None
    dispatch_decision = normalize_pma_dispatch_decision(data.get("dispatch_decision"))
    return PmaDeliveryState(
        delivery_id=delivery_id,
        wakeup_id=_normalize_text(data.get("wakeup_id")),
        dispatch_decision=dispatch_decision,
        status=_normalize_text(data.get("status")) or "pending",
        attempts_made=_normalize_non_negative_int(data.get("attempts_made"), fallback=0)
        or 0,
        last_attempt_at=_normalize_text(data.get("last_attempt_at")),
        last_error=_normalize_text(data.get("last_error")),
        created_at=_normalize_text(data.get("created_at")),
        updated_at=_normalize_text(data.get("updated_at")),
        metadata=_dict_field(data.get("metadata")),
    )


# ---------------------------------------------------------------------------
# PmaDomainEvent
# ---------------------------------------------------------------------------


def normalize_pma_domain_event(data: Any) -> Optional[PmaDomainEvent]:
    if isinstance(data, PmaDomainEvent):
        return data
    if not isinstance(data, dict):
        return None
    event_type_raw = _normalize_text(data.get("event_type"))
    if event_type_raw is None:
        return None
    try:
        event_type = PmaDomainEventType(event_type_raw)
    except ValueError:
        return None
    return PmaDomainEvent(
        event_type=event_type,
        event_id=_normalize_text(data.get("event_id")) or str(uuid.uuid4()),
        timestamp=_normalize_text(data.get("timestamp")) or _stamp(),
        payload=_dict_field(data.get("payload")),
        correlation_id=_normalize_text(data.get("correlation_id")),
    )
