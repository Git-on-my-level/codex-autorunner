from __future__ import annotations

from typing import Any

from .models import (
    EXECUTOR_PMA_TURN,
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    AutomationRule,
    AutomationSchedule,
)
from .store import AutomationStore

BUILTIN_PMA_REACTIVE_RULE_ID = "builtin:pma:reactive-lifecycle"
PMA_TIMER_RULE_PREFIX = "builtin:pma:timer:"
PMA_TIMER_SCHEDULE_PREFIX = "pma-timer:"
PMA_SUBSCRIPTION_RULE_PREFIX = "builtin:pma:subscription:"

_LIFECYCLE_EVENT_MAP = {
    "dispatch_created": "lifecycle.dispatch_created",
    "flow_started": "lifecycle.flow_started",
    "flow_resumed": "lifecycle.flow_resumed",
    "flow_paused": "lifecycle.flow_paused",
    "flow_completed": "lifecycle.flow_completed",
    "flow_failed": "lifecycle.flow_failed",
    "flow_stopped": "lifecycle.flow_stopped",
    "managed_thread_started": "lifecycle.flow_started",
    "managed_thread_resumed": "lifecycle.flow_resumed",
    "managed_thread_paused": "lifecycle.flow_paused",
    "managed_thread_completed": "lifecycle.flow_completed",
    "managed_thread_failed": "lifecycle.flow_failed",
    "managed_thread_stopped": "lifecycle.flow_stopped",
}

_DEFAULT_REACTIVE_EVENTS = [
    "lifecycle.flow_paused",
    "lifecycle.flow_failed",
    "lifecycle.flow_completed",
    "lifecycle.dispatch_created",
]


def ensure_builtin_pma_reactive_rule(
    store: AutomationStore, *, pma_config: Any
) -> AutomationRule:
    event_types = _normalize_reactive_event_types(
        getattr(pma_config, "reactive_event_types", None)
    )
    origin_blocklist = [
        value
        for value in (
            str(item).strip().lower()
            for item in (getattr(pma_config, "reactive_origin_blocklist", []) or [])
        )
        if value
    ]
    filters: dict[str, Any] = {}
    if origin_blocklist:
        filters["event.payload.origin"] = {"not_in": origin_blocklist}
    filters["event.payload.pma_dispatch_action"] = {
        "not_in": ["auto_resolved", "ignore"]
    }
    rule = AutomationRule.create(
        rule_id=BUILTIN_PMA_REACTIVE_RULE_ID,
        name="Built-in PMA lifecycle reaction",
        enabled=bool(getattr(pma_config, "enabled", True))
        and bool(getattr(pma_config, "reactive_enabled", True)),
        system_owned=True,
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"kind": "lifecycle_event", "event_types": event_types},
        filters=filters,
        target_policy=TARGET_POLICY_HUB,
        target={
            "repo_id": "{{ event.repo_id }}",
            "run_id": "{{ event.payload.run_id }}",
        },
        executor_kind=EXECUTOR_PMA_TURN,
        executor={
            "lane_id": "pma:default",
            "message": (
                "Lifecycle event received.\n"
                "type: {{ event.event_type }}\n"
                "repo_id: {{ event.repo_id }}\n"
                "run_id: {{ event.payload.run_id }}\n"
                "event_id: {{ metadata.lifecycle_event_id }}"
            ),
        },
        policy={
            "dedupe_key": "lifecycle:{{ metadata.lifecycle_event_id }}",
            "approval_mode": "pause_and_request_user",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
            "requires_pma_safety": True,
            "reactive_debounce_seconds": max(
                0, int(getattr(pma_config, "reactive_debounce_seconds", 0) or 0)
            ),
            "reactive_debounce_key": (
                "{{ event.event_type }}:{{ event.repo_id }}:{{ event.payload.run_id }}"
            ),
        },
        metadata={"builtin": True, "purpose": "pma_reactive_lifecycle"},
    )
    return store.upsert_rule(rule)


def mirror_pma_timer_schedule(
    store: AutomationStore, *, timer: Any
) -> tuple[AutomationRule, AutomationSchedule]:
    timer_id = str(getattr(timer, "timer_id", "") or "").strip()
    if not timer_id:
        raise ValueError("timer_id is required")
    rule_id = f"{PMA_TIMER_RULE_PREFIX}{timer_id}"
    schedule_id = f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}"
    rule = AutomationRule.create(
        rule_id=rule_id,
        name=f"PMA timer {timer_id}",
        enabled=str(getattr(timer, "state", "pending") or "pending") == "pending",
        system_owned=True,
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["schedule.fire"]},
        filters={"schedule.rule_id": rule_id},
        target_policy=TARGET_POLICY_HUB,
        target={
            "repo_id": getattr(timer, "repo_id", None),
            "run_id": getattr(timer, "run_id", None),
            "thread_id": getattr(timer, "thread_id", None),
        },
        executor_kind=EXECUTOR_PMA_TURN,
        executor={
            "lane_id": getattr(timer, "lane_id", None) or "pma:default",
            "message": (
                "Automation wake-up received.\n"
                "source: timer\n"
                f"timer_id: {timer_id}\n"
                "repo_id: {{ schedule.payload.repo_id }}\n"
                "run_id: {{ schedule.payload.run_id }}\n"
                "thread_id: {{ schedule.payload.thread_id }}\n"
                "suggested_next_action: verify progress, then use "
                "/hub/pma/timers/{timer_id}/touch or /hub/pma/timers/{timer_id}/cancel."
            ),
            "wake_up_kind": "pma_timer",
        },
        policy={
            "dedupe_key": f"pma-timer:{timer_id}:{{{{ schedule.next_fire_at }}}}",
            "approval_mode": "pause_and_request_user",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        metadata={
            "builtin": True,
            "purpose": "pma_timer",
            "legacy_timer_id": timer_id,
            "legacy_idempotency_key": getattr(timer, "idempotency_key", None),
        },
        created_at=getattr(timer, "created_at", None),
        updated_at=getattr(timer, "updated_at", None),
    )
    saved_rule = store.upsert_rule(rule)
    schedule = AutomationSchedule.create(
        schedule_id=schedule_id,
        rule_id=rule_id,
        schedule_kind=SCHEDULE_ONE_SHOT,
        next_fire_at=(
            getattr(timer, "due_at", None)
            if str(getattr(timer, "state", "pending") or "pending") == "pending"
            else None
        ),
        last_fire_at=getattr(timer, "fired_at", None),
        schedule={
            "legacy_timer_id": timer_id,
            "timer_kind": getattr(timer, "timer_type", None),
            "payload": _timer_payload(timer),
        },
        state=(
            "active"
            if str(getattr(timer, "state", "pending") or "pending") == "pending"
            else str(getattr(timer, "state", "completed") or "completed")
        ),
        created_at=getattr(timer, "created_at", None),
        updated_at=getattr(timer, "updated_at", None),
    )
    return saved_rule, store.upsert_schedule(schedule)


def mirror_pma_subscription_rule(
    store: AutomationStore, *, subscription: Any
) -> AutomationRule:
    subscription_id = str(getattr(subscription, "subscription_id", "") or "").strip()
    if not subscription_id:
        raise ValueError("subscription_id is required")
    rule_id = f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}"
    event_types = _normalize_reactive_event_types(
        getattr(subscription, "event_types", None)
    )
    filters: dict[str, Any] = {}
    for field, path in (
        ("repo_id", "event.repo_id"),
        ("run_id", "event.payload.run_id"),
        ("thread_id", "event.payload.thread_id"),
        ("from_state", "event.payload.from_state"),
        ("to_state", "event.payload.to_state"),
    ):
        value = getattr(subscription, field, None)
        if value is not None:
            filters[path] = value
    rule = AutomationRule.create(
        rule_id=rule_id,
        name=f"PMA subscription {subscription_id}",
        enabled=str(getattr(subscription, "state", "active") or "active") == "active",
        system_owned=True,
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"kind": "lifecycle_event", "event_types": event_types},
        filters=filters,
        target_policy=TARGET_POLICY_HUB,
        target={
            "repo_id": getattr(subscription, "repo_id", None),
            "run_id": getattr(subscription, "run_id", None),
            "thread_id": getattr(subscription, "thread_id", None),
        },
        executor_kind=EXECUTOR_PMA_TURN,
        executor={
            "lane_id": getattr(subscription, "lane_id", None) or "pma:default",
            "wake_up_kind": "pma_subscription",
            "source": "transition",
            "subscription_id": subscription_id,
            "event_type": "{{ event.payload.event_type }}",
            "repo_id": "{{ event.repo_id }}",
            "run_id": "{{ event.payload.run_id }}",
            "thread_id": "{{ event.payload.thread_id }}",
            "from_state": "{{ event.payload.from_state }}",
            "to_state": "{{ event.payload.to_state }}",
            "reason": "{{ event.payload.reason }}",
            "timestamp": "{{ event.raw_payload.timestamp }}",
            "message": (
                "Automation wake-up received.\n"
                "source: transition\n"
                "event_type: {{ event.payload.event_type }}\n"
                f"subscription_id: {subscription_id}\n"
                "repo_id: {{ event.repo_id }}\n"
                "run_id: {{ event.payload.run_id }}\n"
                "thread_id: {{ event.payload.thread_id }}\n"
                "from_state: {{ event.payload.from_state }}\n"
                "to_state: {{ event.payload.to_state }}\n"
                "reason: {{ event.payload.reason }}\n"
                "timestamp: {{ event.raw_payload.timestamp }}\n"
                "suggested_next_action: inspect the transition and adjust "
                "/hub/pma/subscriptions or /hub/pma/timers as needed."
            ),
            **_subscription_executor_metadata(subscription),
        },
        policy={
            "dedupe_key": f"pma-subscription:{subscription_id}:{{{{ event.event_id }}}}",
            "approval_mode": "pause_and_request_user",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        metadata={
            "builtin": True,
            "purpose": "pma_lifecycle_subscription",
            "legacy_subscription_id": subscription_id,
            "legacy_idempotency_key": getattr(subscription, "idempotency_key", None),
            "legacy_reason": getattr(subscription, "reason", None),
            "legacy_max_matches": getattr(subscription, "max_matches", None),
            "legacy_match_count": getattr(subscription, "match_count", 0),
            "legacy_metadata": dict(getattr(subscription, "metadata", None) or {}),
        },
        created_at=getattr(subscription, "created_at", None),
        updated_at=getattr(subscription, "updated_at", None),
    )
    return store.upsert_rule(rule)


def _normalize_reactive_event_types(raw: Any) -> list[str]:
    if not raw:
        return list(_DEFAULT_REACTIVE_EVENTS)
    normalized: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        normalized.append(_LIFECYCLE_EVENT_MAP.get(text, text))
    return normalized or list(_DEFAULT_REACTIVE_EVENTS)


def _timer_payload(timer: Any) -> dict[str, Any]:
    return {
        "timer_id": getattr(timer, "timer_id", None),
        "source": "timer",
        "repo_id": getattr(timer, "repo_id", None),
        "run_id": getattr(timer, "run_id", None),
        "thread_id": getattr(timer, "thread_id", None),
        "lane_id": getattr(timer, "lane_id", None) or "pma:default",
        "from_state": getattr(timer, "from_state", None),
        "to_state": getattr(timer, "to_state", None),
        "reason": getattr(timer, "reason", None),
        "timestamp": getattr(timer, "due_at", None),
        "subscription_id": getattr(timer, "subscription_id", None),
        "timer_type": getattr(timer, "timer_type", None),
        "metadata": dict(getattr(timer, "metadata", None) or {}),
    }


def _subscription_executor_metadata(subscription: Any) -> dict[str, Any]:
    metadata = dict(getattr(subscription, "metadata", None) or {})
    out: dict[str, Any] = {}
    delivery_target = metadata.get("delivery_target")
    if isinstance(delivery_target, dict):
        out["delivery_target"] = dict(delivery_target)
    pma_origin = metadata.get("pma_origin")
    if isinstance(pma_origin, dict):
        out["pma_origin"] = dict(pma_origin)
    if metadata:
        out["metadata"] = metadata
    return out


__all__ = [
    "BUILTIN_PMA_REACTIVE_RULE_ID",
    "PMA_TIMER_RULE_PREFIX",
    "PMA_TIMER_SCHEDULE_PREFIX",
    "PMA_SUBSCRIPTION_RULE_PREFIX",
    "ensure_builtin_pma_reactive_rule",
    "mirror_pma_subscription_rule",
    "mirror_pma_timer_schedule",
]
