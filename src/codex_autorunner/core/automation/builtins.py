from __future__ import annotations

from typing import Any

from .models import (
    EXECUTOR_PMA_OPERATOR_TURN,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    AutomationRule,
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
        executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        executor={
            "message_text": (
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


__all__ = [
    "BUILTIN_PMA_REACTIVE_RULE_ID",
    "PMA_TIMER_RULE_PREFIX",
    "PMA_TIMER_SCHEDULE_PREFIX",
    "PMA_SUBSCRIPTION_RULE_PREFIX",
    "ensure_builtin_pma_reactive_rule",
]
