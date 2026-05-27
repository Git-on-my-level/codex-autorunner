from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Request

from .....core.automation import (
    EXECUTOR_PMA_OPERATOR_TURN,
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
    AutomationRule,
    AutomationSchedule,
    AutomationStore,
)
from .....core.automation.builtins import _normalize_reactive_event_types
from .....core.automation.models import (
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
)
from .....core.pma_automation_rule_projection import (
    subscription_row_from_rule,
    timer_rows_from_rules_and_schedules,
)
from .....core.pma_automation_services import (
    MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES,
    PmaAutomationThreadNotFoundError,
)
from .....core.pma_automation_types import (
    DEFAULT_PMA_LANE_ID,
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    TIMER_TYPE_WATCHDOG,
    _iso_after_seconds,
    _normalize_due_timestamp,
    _normalize_lane_id,
    _normalize_non_negative_int,
    _normalize_positive_int,
    _normalize_timer_type,
)
from .....core.time_utils import now_iso
from .common import normalize_optional_text
from .container import get_pma_request_context
from .managed_thread_read_models import _load_chat_binding_metadata_by_thread

_logger = logging.getLogger(__name__)

SUBSCRIPTION_PURPOSES = {
    "managed_thread_lifecycle_subscription",
    "pma_lifecycle_subscription",
}
TIMER_PURPOSES = {"managed_thread_timer", "pma_timer"}


def subscription_request_has_explicit_routing(payload: dict[str, Any]) -> bool:
    return any(
        normalize_optional_text(payload.get(field)) is not None
        for field in ("thread_id", "lane_id")
    )


def unified_pma_automation_read_model(
    request: Request,
    *,
    purpose: str,
    limit: int,
) -> dict[str, Any]:
    context = get_pma_request_context(request)
    purpose_set = _purpose_set(purpose)
    try:
        store = AutomationStore(context.hub_root)
        rules = [
            rule
            for rule in store.list_rules()
            if rule.metadata.get("purpose") in purpose_set
        ]
        rule_ids = {rule.rule_id for rule in rules}
        schedules = [
            schedule
            for schedule in store.list_schedules()
            if schedule.rule_id in rule_ids
        ]
        jobs = [
            job
            for job in store.list_jobs(limit=max(limit, 1))
            if job.rule_id in rule_ids
        ][:limit]
    except (RuntimeError, OSError, ValueError, TypeError):
        _logger.exception("Failed to build unified PMA automation read model")
        return {"rules": [], "schedules": [], "jobs": []}

    return {
        "rules": [rule.to_dict() for rule in rules[:limit]],
        "schedules": [schedule.to_dict() for schedule in schedules[:limit]],
        "jobs": [job.to_dict() for job in jobs],
    }


def create_unified_pma_subscription(
    request: Request, payload: dict[str, Any]
) -> dict[str, Any]:
    context = get_pma_request_context(request)
    hub_root = context.hub_root
    store = AutomationStore(hub_root)
    idempotency_key = normalize_optional_text(payload.get("idempotency_key"))
    normalized_thread_id = normalize_optional_text(payload.get("thread_id"))
    if idempotency_key is not None:
        existing = find_pma_rule_by_idempotency(
            store,
            purpose="pma_lifecycle_subscription",
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return {
                "subscription": subscription_row_from_rule(existing),
                "deduped": True,
            }

    event_types = payload.get("event_types")
    normalized_repo_id = normalize_optional_text(payload.get("repo_id"))
    normalized_run_id = normalize_optional_text(payload.get("run_id"))
    normalized_from_state = normalize_optional_text(payload.get("from_state"))
    normalized_to_state = normalize_optional_text(payload.get("to_state"))
    normalized_event_types = [
        str(item).strip().lower() for item in event_types or [] if str(item).strip()
    ]
    normalized_event_types_for_match = (
        _normalize_reactive_event_types(normalized_event_types)
        if normalized_event_types
        else []
    )
    confirm_duplicate = bool(payload.get("confirm"))
    if not confirm_duplicate:
        existing_auto = find_covering_auto_subscription_rule(
            store,
            event_types=normalized_event_types_for_match,
            repo_id=normalized_repo_id,
            run_id=normalized_run_id,
            thread_id=normalized_thread_id,
            from_state=normalized_from_state,
            to_state=normalized_to_state,
        )
        if existing_auto is not None:
            row = subscription_row_from_rule(existing_auto)
            if is_auto_subscription_key(idempotency_key):
                return {"subscription": row, "deduped": True}
            return {
                "subscription": row,
                "deduped": True,
                "warning": covering_auto_subscription_warning(
                    existing=row,
                    requested_event_types=normalized_event_types,
                    repo_id=normalized_repo_id,
                    run_id=normalized_run_id,
                    thread_id=normalized_thread_id,
                ),
            }

    created_at = now_iso()
    subscription_id = normalize_optional_text(payload.get("subscription_id")) or str(
        uuid.uuid4()
    )
    metadata = resolved_unified_subscription_metadata(hub_root, payload)
    lane_id = resolve_unified_subscription_lane_id(
        hub_root,
        payload,
        metadata=metadata,
    )
    reason = normalize_optional_text(payload.get("reason"))
    max_matches = _normalize_positive_int(payload.get("max_matches"), fallback=None)
    if max_matches is None and bool(payload.get("notify_once")):
        max_matches = 1
    event_types_for_rule = _normalize_reactive_event_types(normalized_event_types)
    filters: dict[str, Any] = {}
    for value, path in (
        (normalized_repo_id, "event.repo_id"),
        (normalized_run_id, "event.payload.run_id"),
        (normalized_thread_id, "event.payload.thread_id"),
        (normalized_from_state, "event.payload.from_state"),
        (normalized_to_state, "event.payload.to_state"),
    ):
        if value is not None:
            filters[path] = value
    rule = AutomationRule.create(
        rule_id=f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}",
        name=f"Managed-thread subscription {subscription_id}",
        enabled=True,
        system_owned=True,
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"kind": "lifecycle_event", "event_types": event_types_for_rule},
        filters=filters,
        target_policy=TARGET_POLICY_HUB,
        target={
            "repo_id": normalized_repo_id,
            "run_id": normalized_run_id,
            "thread_id": normalized_thread_id,
        },
        executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        executor={
            "wake_up_kind": "managed_thread_subscription",
            "source": "transition",
            "subscription_id": subscription_id,
            "lane_id": lane_id,
            "event_type": "{{ event.payload.event_type }}",
            "repo_id": "{{ event.repo_id }}",
            "run_id": "{{ event.payload.run_id }}",
            "thread_id": "{{ event.payload.thread_id }}",
            "from_state": "{{ event.payload.from_state }}",
            "to_state": "{{ event.payload.to_state }}",
            "reason": "{{ event.payload.reason }}",
            "timestamp": "{{ event.raw_payload.timestamp }}",
            "message_text": (
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
                "managed-thread automation subscriptions or timers as needed."
            ),
        },
        policy={
            "dedupe_key": (
                f"managed-thread-subscription:{subscription_id}:" "{{ event.event_id }}"
            ),
            "approval_mode": "pause_and_request_user",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        metadata={
            "builtin": True,
            "purpose": "managed_thread_lifecycle_subscription",
            "subscription_id": subscription_id,
            "idempotency_key": idempotency_key,
            "reason": reason,
            "max_matches": max_matches,
            "match_count": 0,
            "metadata": metadata,
        },
        created_at=created_at,
        updated_at=created_at,
    )
    store.upsert_rule(rule)
    return {
        "subscription": subscription_row_from_rule(rule),
        "deduped": False,
    }


def create_unified_pma_timer(
    request: Request, payload: dict[str, Any]
) -> dict[str, Any]:
    store = AutomationStore(get_pma_request_context(request).hub_root)
    subscription_id = normalize_optional_text(payload.get("subscription_id"))
    if (
        subscription_id is not None
        and store.get_rule(f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}") is None
    ):
        raise ValueError(f"Unknown subscription_id: {subscription_id}")

    idempotency_key = normalize_optional_text(payload.get("idempotency_key"))
    if idempotency_key is not None:
        existing = find_pma_rule_by_idempotency(
            store,
            purpose="pma_timer",
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            rows = timer_rows_from_rules_and_schedules(
                {existing.rule_id: existing},
                store.list_schedules(rule_id=existing.rule_id),
            )
            timer_row = rows[0] if rows else {"timer_id": existing.rule_id}
            return {"timer": timer_row, "deduped": True}

    timer_type = _normalize_timer_type(payload.get("timer_type"))
    idle_seconds = _normalize_non_negative_int(
        payload.get("idle_seconds"), fallback=None
    )
    delay_seconds = _normalize_non_negative_int(
        payload.get("delay_seconds"), fallback=None
    )
    due_at = _normalize_due_timestamp(payload.get("due_at"), field_name="due_at")
    if due_at is None:
        due_at = _normalize_due_timestamp(
            payload.get("timestamp"), field_name="timestamp"
        )
    if due_at is None:
        if timer_type == TIMER_TYPE_WATCHDOG:
            idle_seconds = idle_seconds or DEFAULT_WATCHDOG_IDLE_SECONDS
            due_at = _iso_after_seconds(idle_seconds)
        else:
            due_at = _iso_after_seconds(delay_seconds or 0)

    created_at = now_iso()
    timer_id = normalize_optional_text(payload.get("timer_id")) or str(uuid.uuid4())
    metadata_raw = payload.get("metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
    repo_id = normalize_optional_text(payload.get("repo_id"))
    run_id = normalize_optional_text(payload.get("run_id"))
    thread_id = normalize_optional_text(payload.get("thread_id"))
    lane_id = _normalize_lane_id(payload.get("lane_id"))
    from_state = normalize_optional_text(payload.get("from_state"))
    to_state = normalize_optional_text(payload.get("to_state"))
    reason = normalize_optional_text(payload.get("reason"))
    rule = AutomationRule.create(
        rule_id=f"{PMA_TIMER_RULE_PREFIX}{timer_id}",
        name=f"Managed-thread timer {timer_id}",
        enabled=True,
        system_owned=True,
        trigger_kind=TRIGGER_KIND_EVENT,
        trigger={"event_types": ["schedule.fire"]},
        filters={"schedule.rule_id": f"{PMA_TIMER_RULE_PREFIX}{timer_id}"},
        target_policy=TARGET_POLICY_HUB,
        target={
            "repo_id": repo_id,
            "run_id": run_id,
            "thread_id": thread_id,
        },
        executor_kind=EXECUTOR_PMA_OPERATOR_TURN,
        executor={
            "message_text": (
                "Automation wake-up received.\n"
                "source: timer\n"
                f"timer_id: {timer_id}\n"
                "repo_id: {{ schedule.payload.repo_id }}\n"
                "run_id: {{ schedule.payload.run_id }}\n"
                "thread_id: {{ schedule.payload.thread_id }}\n"
                "suggested_next_action: verify progress, then touch or cancel "
                "the managed-thread automation timer."
            ),
            "wake_up_kind": "managed_thread_timer",
            "source": "timer",
        },
        policy={
            "dedupe_key": f"managed-thread-timer:{timer_id}:{{{{ schedule.next_fire_at }}}}",
            "approval_mode": "pause_and_request_user",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        metadata={
            "builtin": True,
            "purpose": "managed_thread_timer",
            "timer_id": timer_id,
            "idempotency_key": idempotency_key,
        },
        created_at=created_at,
        updated_at=created_at,
    )
    schedule = AutomationSchedule.create(
        schedule_id=f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}",
        rule_id=rule.rule_id,
        schedule_kind=SCHEDULE_ONE_SHOT,
        next_fire_at=due_at,
        schedule={
            "timer_id": timer_id,
            "timer_kind": timer_type,
            "payload": {
                "timer_id": timer_id,
                "timer_type": timer_type,
                "idle_seconds": idle_seconds,
                "subscription_id": subscription_id,
                "repo_id": repo_id,
                "run_id": run_id,
                "thread_id": thread_id,
                "lane_id": lane_id,
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
                "timestamp": due_at,
                "metadata": metadata,
            },
        },
        state="active",
        created_at=created_at,
        updated_at=created_at,
    )
    store.upsert_rule(rule)
    saved_schedule = store.upsert_schedule(schedule)
    rows = timer_rows_from_rules_and_schedules({rule.rule_id: rule}, [saved_schedule])
    return {
        "timer": rows[0] if rows else {"timer_id": timer_id, "due_at": due_at},
        "deduped": False,
    }


def touch_unified_pma_timer(
    request: Request, timer_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    store = AutomationStore(get_pma_request_context(request).hub_root)
    schedule_id = f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}"
    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        return {"status": "ok", "timer_id": timer_id, "touched": False}

    due_at = _normalize_due_timestamp(payload.get("timestamp"), field_name="timestamp")
    if due_at is None:
        due_at = _normalize_due_timestamp(payload.get("due_at"), field_name="due_at")
    if due_at is None:
        delay_seconds = _normalize_non_negative_int(
            payload.get("delay_seconds"), fallback=None
        )
        if delay_seconds is not None:
            due_at = _iso_after_seconds(delay_seconds)
    if due_at is None:
        due_at = schedule.next_fire_at or now_iso()

    schedule_config = dict(schedule.schedule)
    schedule_payload_raw = schedule_config.get("payload")
    schedule_payload = (
        dict(schedule_payload_raw) if isinstance(schedule_payload_raw, dict) else {}
    )
    schedule_payload["timestamp"] = due_at
    reason = normalize_optional_text(payload.get("reason"))
    if reason is not None:
        schedule_payload["reason"] = reason
    schedule_config["payload"] = schedule_payload
    updated = AutomationSchedule.create(
        schedule_id=schedule.schedule_id,
        rule_id=schedule.rule_id,
        schedule_kind=schedule.schedule_kind,
        timezone=schedule.timezone,
        next_fire_at=due_at,
        last_fire_at=schedule.last_fire_at,
        misfire_policy=schedule.misfire_policy,
        schedule=schedule_config,
        state="active",
        created_at=schedule.created_at,
        updated_at=now_iso(),
    )
    saved = store.upsert_schedule(updated)
    rows = timer_rows_from_rules_and_schedules(
        {schedule.rule_id: store.get_rule(schedule.rule_id)},
        [saved],
    )
    return {
        "status": "ok",
        "timer_id": timer_id,
        "timer": rows[0] if rows else {"timer_id": timer_id, "due_at": due_at},
        "touched": True,
    }


def unified_subscription_rows(
    request: Request,
    *,
    repo_id: Optional[str],
    run_id: Optional[str],
    thread_id: Optional[str],
    lane_id: Optional[str],
    limit: int,
) -> list[dict[str, Any]]:
    context = get_pma_request_context(request)
    repo_id_norm = normalize_optional_text(repo_id)
    run_id_norm = normalize_optional_text(run_id)
    thread_id_norm = normalize_optional_text(thread_id)
    lane_id_norm = normalize_optional_text(lane_id)
    store = AutomationStore(context.hub_root)
    try:
        rules = [
            rule
            for rule in store.list_rules(enabled=True)
            if rule.rule_id.startswith(PMA_SUBSCRIPTION_RULE_PREFIX)
            or rule.metadata.get("purpose") in SUBSCRIPTION_PURPOSES
        ]
    except (RuntimeError, OSError, ValueError, TypeError):
        _logger.exception("Failed to list unified PMA subscription rows")
        return []

    out: list[dict[str, Any]] = []
    for rule in rules:
        row = subscription_row_from_rule(rule)
        if repo_id_norm is not None and row["repo_id"] != repo_id_norm:
            continue
        if run_id_norm is not None and row["run_id"] != run_id_norm:
            continue
        if thread_id_norm is not None and row["thread_id"] != thread_id_norm:
            continue
        if lane_id_norm is not None and row["lane_id"] != lane_id_norm:
            continue
        out.append(row)
    return out[:limit]


def unified_timer_rows(
    request: Request,
    *,
    timer_type: Optional[str],
    subscription_id: Optional[str],
    repo_id: Optional[str],
    run_id: Optional[str],
    thread_id: Optional[str],
    lane_id: Optional[str],
    limit: int,
) -> list[dict[str, Any]]:
    context = get_pma_request_context(request)
    timer_type_norm = normalize_optional_text(timer_type)
    subscription_id_norm = normalize_optional_text(subscription_id)
    repo_id_norm = normalize_optional_text(repo_id)
    run_id_norm = normalize_optional_text(run_id)
    thread_id_norm = normalize_optional_text(thread_id)
    lane_id_norm = normalize_optional_text(lane_id)
    store = AutomationStore(context.hub_root)
    try:
        rules = {
            rule.rule_id: rule
            for rule in store.list_rules(enabled=True)
            if rule.rule_id.startswith(PMA_TIMER_RULE_PREFIX)
            or rule.metadata.get("purpose") in TIMER_PURPOSES
        }
        schedules = [
            schedule
            for schedule in store.list_schedules()
            if schedule.rule_id in rules and schedule.state == "active"
        ]
    except (RuntimeError, OSError, ValueError, TypeError):
        _logger.exception("Failed to list unified PMA timer rows")
        return []

    out: list[dict[str, Any]] = []
    for row in timer_rows_from_rules_and_schedules(rules, schedules):
        if timer_type_norm is not None and row["timer_type"] != timer_type_norm:
            continue
        if (
            subscription_id_norm is not None
            and row["subscription_id"] != subscription_id_norm
        ):
            continue
        if repo_id_norm is not None and row["repo_id"] != repo_id_norm:
            continue
        if run_id_norm is not None and row["run_id"] != run_id_norm:
            continue
        if thread_id_norm is not None and row["thread_id"] != thread_id_norm:
            continue
        if lane_id_norm is not None and row["lane_id"] != lane_id_norm:
            continue
        out.append(row)
    return out[:limit]


def cancel_unified_pma_subscription(request: Request, subscription_id: str) -> bool:
    store = AutomationStore(get_pma_request_context(request).hub_root)
    rule_id = f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}"
    existing = store.get_rule(rule_id)
    updated = store.set_rule_enabled(rule_id, False)
    return existing is not None and updated is not None and not updated.enabled


def cancel_unified_pma_timer(request: Request, timer_id: str) -> bool:
    store = AutomationStore(get_pma_request_context(request).hub_root)
    rule_id = f"{PMA_TIMER_RULE_PREFIX}{timer_id}"
    schedule_id = f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}"
    existing_rule = store.get_rule(rule_id)
    updated_rule = store.set_rule_enabled(rule_id, False)
    existing_schedule = store.get_schedule(schedule_id)
    updated_schedule = store.cancel_schedule(schedule_id)
    rule_changed = (
        existing_rule is not None
        and updated_rule is not None
        and not updated_rule.enabled
    )
    schedule_changed = (
        existing_schedule is not None
        and updated_schedule is not None
        and updated_schedule.state == "cancelled"
    )
    return rule_changed or schedule_changed


def is_auto_subscription_key(idempotency_key: Optional[str]) -> bool:
    normalized_key = normalize_optional_text(idempotency_key)
    if normalized_key is None:
        return False
    return normalized_key.startswith(MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES)


def find_covering_auto_subscription_rule(
    store: AutomationStore,
    *,
    event_types: list[str],
    repo_id: Optional[str],
    run_id: Optional[str],
    thread_id: Optional[str],
    from_state: Optional[str],
    to_state: Optional[str],
) -> Optional[Any]:
    for rule in store.list_rules(enabled=True):
        if rule.metadata.get("purpose") not in SUBSCRIPTION_PURPOSES:
            continue
        row = subscription_row_from_rule(rule)
        if not is_auto_subscription_key(row.get("idempotency_key")):
            continue
        if not _subscription_event_types_are_covered(
            requested=event_types,
            existing=list(row.get("event_types") or []),
        ):
            continue
        if not _subscription_scope_value_is_covered(
            requested=repo_id,
            existing=normalize_optional_text(row.get("repo_id")),
        ):
            continue
        if not _subscription_scope_value_is_covered(
            requested=run_id,
            existing=normalize_optional_text(row.get("run_id")),
        ):
            continue
        if not _subscription_scope_value_is_covered(
            requested=thread_id,
            existing=normalize_optional_text(row.get("thread_id")),
        ):
            continue
        if not _subscription_scope_value_is_covered(
            requested=from_state,
            existing=normalize_optional_text(row.get("from_state")),
        ):
            continue
        if not _subscription_scope_value_is_covered(
            requested=to_state,
            existing=normalize_optional_text(row.get("to_state")),
        ):
            continue
        return rule
    return None


def covering_auto_subscription_warning(
    *,
    existing: dict[str, Any],
    requested_event_types: list[str],
    repo_id: Optional[str],
    run_id: Optional[str],
    thread_id: Optional[str],
) -> str:
    scope_label = "this scope"
    if thread_id is not None:
        scope_label = "this thread"
    elif run_id is not None:
        scope_label = "this run"
    elif repo_id is not None:
        scope_label = "this repo"
    event_label = (
        requested_event_types[0]
        if len(requested_event_types) == 1
        else "the requested event scope"
    )
    return (
        "An active auto-subscription "
        f"({existing.get('idempotency_key') or existing.get('subscription_id')}) "
        f"already covers {event_label} for {scope_label}. "
        "Pass confirm=true to create a duplicate subscription."
    )


def resolved_unified_subscription_metadata(
    hub_root: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    metadata = pma_origin_metadata_from_payload(payload)
    origin = metadata.get("pma_origin") if isinstance(metadata, dict) else None
    origin_thread_id = (
        normalize_optional_text(origin.get("thread_id"))
        if isinstance(origin, dict)
        else normalize_optional_text(payload.get("origin_thread_id"))
    )
    thread_id = normalize_optional_text(payload.get("thread_id"))
    delivery_target = None
    if origin_thread_id is not None:
        try:
            delivery_target = _delivery_target_for_thread(hub_root, origin_thread_id)
        except PmaAutomationThreadNotFoundError:
            delivery_target = None
    if delivery_target is None and thread_id is not None:
        delivery_target = _delivery_target_for_thread(hub_root, thread_id)
    if delivery_target is not None:
        metadata["delivery_target"] = delivery_target
    return metadata


def resolve_unified_subscription_lane_id(
    hub_root: Path,
    payload: dict[str, Any],
    *,
    metadata: dict[str, Any],
) -> str:
    lane_id = normalize_optional_text(payload.get("lane_id"))
    if lane_id is not None:
        return _normalize_lane_id(lane_id)
    origin = metadata.get("pma_origin") if isinstance(metadata, dict) else None
    origin_lane_id = (
        normalize_optional_text(origin.get("lane_id"))
        if isinstance(origin, dict)
        else normalize_optional_text(payload.get("origin_lane_id"))
    )
    if origin_lane_id is not None:
        return _normalize_lane_id(origin_lane_id)
    origin_thread_id = (
        normalize_optional_text(origin.get("thread_id"))
        if isinstance(origin, dict)
        else normalize_optional_text(payload.get("origin_thread_id"))
    )
    if origin_thread_id is not None:
        try:
            resolved_origin_lane = _lane_id_for_thread(hub_root, origin_thread_id)
        except PmaAutomationThreadNotFoundError:
            resolved_origin_lane = DEFAULT_PMA_LANE_ID
        if resolved_origin_lane != DEFAULT_PMA_LANE_ID:
            return resolved_origin_lane
    thread_id = normalize_optional_text(payload.get("thread_id"))
    if thread_id is None:
        return DEFAULT_PMA_LANE_ID
    resolved_lane = _lane_id_for_thread(hub_root, thread_id)
    if resolved_lane != DEFAULT_PMA_LANE_ID:
        return resolved_lane
    return DEFAULT_PMA_LANE_ID


def find_pma_rule_by_idempotency(
    store: AutomationStore, *, purpose: str, idempotency_key: str
):
    purpose_set = _purpose_set(purpose)
    for rule in store.list_rules():
        if rule.metadata.get("purpose") not in purpose_set:
            continue
        if rule.metadata.get("idempotency_key") == idempotency_key:
            return rule
    return None


def pma_origin_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_metadata = payload.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    existing_origin = metadata.get("pma_origin")
    origin = dict(existing_origin) if isinstance(existing_origin, dict) else {}
    origin.update(
        {
            key: value
            for key, value in {
                "thread_id": normalize_optional_text(payload.get("origin_thread_id")),
                "lane_id": normalize_optional_text(payload.get("origin_lane_id")),
                "agent": normalize_optional_text(payload.get("origin_agent")),
                "profile": normalize_optional_text(payload.get("origin_profile")),
                "surface_kind": normalize_optional_text(
                    payload.get("origin_surface_kind")
                ),
                "surface_key": normalize_optional_text(
                    payload.get("origin_surface_key")
                ),
            }.items()
            if value is not None
        }
    )
    if origin:
        metadata["pma_origin"] = origin
    return metadata


def _purpose_set(purpose: str) -> set[str]:
    if purpose in SUBSCRIPTION_PURPOSES:
        return SUBSCRIPTION_PURPOSES
    if purpose in TIMER_PURPOSES:
        return TIMER_PURPOSES
    return {purpose}


def _subscription_scope_value_is_covered(
    *, requested: Optional[str], existing: Optional[str]
) -> bool:
    if existing is None:
        return True
    if requested is None:
        return False
    return existing == requested


def _subscription_event_types_are_covered(
    *, requested: list[str], existing: list[str]
) -> bool:
    if not existing:
        return True
    if not requested:
        return False
    existing_set = set(existing)
    return all(event_type in existing_set for event_type in requested)


def _thread_binding_metadata(
    hub_root: Path, thread_id: str
) -> Optional[dict[str, Any]]:
    from .....core.managed_thread_store import ManagedThreadStore

    thread = ManagedThreadStore(hub_root).get_thread(thread_id)
    if thread is None:
        raise PmaAutomationThreadNotFoundError(thread_id)
    binding_metadata = _load_chat_binding_metadata_by_thread(hub_root).get(thread_id)
    return binding_metadata if isinstance(binding_metadata, dict) else None


def _delivery_target_for_thread(
    hub_root: Path, thread_id: str
) -> Optional[dict[str, str]]:
    binding_metadata = _thread_binding_metadata(hub_root, thread_id)
    if not isinstance(binding_metadata, dict):
        return None
    surface_kind = normalize_optional_text(binding_metadata.get("binding_kind"))
    surface_key = normalize_optional_text(binding_metadata.get("binding_id"))
    if surface_kind in {"discord", "telegram"} and surface_key is not None:
        return {"surface_kind": surface_kind, "surface_key": surface_key}
    return None


def _lane_id_for_thread(hub_root: Path, thread_id: str) -> str:
    binding_metadata = _thread_binding_metadata(hub_root, thread_id)
    if not isinstance(binding_metadata, dict):
        return DEFAULT_PMA_LANE_ID
    binding_kind = normalize_optional_text(binding_metadata.get("binding_kind"))
    if binding_kind in {"discord", "telegram"}:
        return binding_kind
    return DEFAULT_PMA_LANE_ID
