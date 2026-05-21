from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional, cast

from fastapi import APIRouter, Body, HTTPException, Query, Request

from .....adapters.chat.execution_event_journal import list_chat_execution_journal
from .....agents.registry import (
    get_registered_agents,
    resolve_agent_runtime,
    wrap_requested_agent_context,
)
from .....core.automation import (
    EXECUTOR_MANAGED_THREAD_TURN,
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
from .....core.managed_thread_store import ManagedThreadStore
from .....core.orchestration import (
    ChatSurfaceReadService,
    build_harness_backed_orchestration_service,
)
from .....core.orchestration.catalog import RuntimeAgentDescriptor
from .....core.orchestration.chat_surface_emitters import emit_chat_surface_event
from .....core.orchestration.managed_thread_timeline import (
    MAX_MANAGED_THREAD_TIMELINE_LIMIT,
    build_managed_thread_timeline,
)
from .....core.orchestration.turn_timeline import list_turn_timeline
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
from .....core.text_utils import _truncate_text
from .....core.time_utils import now_iso
from ...schemas import (
    ManagedThreadBulkRetireRequest,
    ManagedThreadCompactRequest,
    ManagedThreadCreateRequest,
    ManagedThreadForkRequest,
    ManagedThreadResumeRequest,
    PmaAutomationSubscriptionCreateRequest,
    PmaAutomationTimerCancelRequest,
    PmaAutomationTimerCreateRequest,
    PmaAutomationTimerTouchRequest,
)
from ...services.pma import get_pma_request_context
from ...services.pma.automation import (
    normalize_optional_text,
)
from ...services.pma.managed_thread_followup import (
    ManagedThreadAutomationClient,
    ManagedThreadAutomationUnavailable,
    apply_origin_followup_context,
)
from ...services.pma.managed_thread_read_models import (
    _apply_chat_binding_fields,
    _attach_latest_execution_fields,
    _load_chat_binding_metadata_by_thread,
    _resolve_running_or_latest_execution,
    _serialize_managed_thread,
    _serialize_thread_target,
    resolve_managed_thread_list_query,
    resolve_owner_scoped_query,
    serialize_active_work_summary,
    serialize_binding_record,
    serialize_managed_thread_turn_summary,
)
from ...services.pma.managed_thread_scope import (
    managed_thread_metadata_for_provisioned_workspace,
    provision_managed_thread_workspace,
    resolve_managed_thread_create_resolution,
)
from .hermes_supervisors import resolve_cached_hermes_supervisor

_logger = logging.getLogger(__name__)

_SUBSCRIPTION_PURPOSES = {
    "managed_thread_lifecycle_subscription",
    "pma_lifecycle_subscription",
}
_TIMER_PURPOSES = {"managed_thread_timer", "pma_timer"}


def _retirable_notification_surfaces(
    row: dict[str, Any],
) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    surfaces: list[dict[str, str]] = []
    raw_surfaces = list(row.get("surface_bindings") or row.get("surfaces") or [])
    primary = row.get("primary_surface") or row.get("surface")
    if isinstance(primary, dict):
        raw_surfaces.append(primary)
    for raw in raw_surfaces:
        if not isinstance(raw, dict):
            continue
        surface_kind = normalize_optional_text(raw.get("surface_kind"))
        surface_key = normalize_optional_text(raw.get("surface_key"))
        if surface_kind != "notification" or surface_key is None:
            continue
        key = (surface_kind, surface_key)
        if key in seen:
            continue
        seen.add(key)
        surfaces.append({"surface_kind": surface_kind, "surface_key": surface_key})
    return surfaces


def _emit_notification_surface_retired(
    *,
    hub_root: Path,
    surface_key: str,
    row: dict[str, Any],
) -> bool:
    occurred_at = now_iso()
    result = emit_chat_surface_event(
        hub_root,
        idempotency_key=f"notification_surface_archive:{surface_key}",
        event_type="surface.archived",
        surface_kind="notification",
        surface_key=surface_key,
        managed_thread_id=normalize_optional_text(row.get("managed_thread_id")),
        repo_id=normalize_optional_text(row.get("repo_id")),
        resource_kind=normalize_optional_text(row.get("resource_kind")),
        resource_id=normalize_optional_text(row.get("resource_id")),
        workspace_root=normalize_optional_text(row.get("workspace_root")),
        lifecycle_status="archived",
        status="archived",
        source_kind="hub.pma.retire_active",
        source_id=normalize_optional_text(row.get("row_id"))
        or normalize_optional_text(row.get("chat_id"))
        or surface_key,
        payload={"row_id": row.get("row_id"), "chat_id": row.get("chat_id")},
        occurred_at=occurred_at,
    )
    return bool(result.inserted)


def _subscription_request_has_explicit_routing(payload: dict[str, Any]) -> bool:
    return any(
        normalize_optional_text(payload.get(field)) is not None
        for field in ("thread_id", "lane_id")
    )


def _serialize_managed_thread_queue_item(
    item: dict[str, Any],
    *,
    position: int,
    queue_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    attachments: list[dict[str, Any]] = []
    request_payload = (
        queue_payload.get("request") if isinstance(queue_payload, dict) else None
    )
    metadata = (
        request_payload.get("metadata") if isinstance(request_payload, dict) else None
    )
    raw_attachments = (
        metadata.get("attachments") if isinstance(metadata, dict) else None
    )
    if isinstance(raw_attachments, list):
        attachments = [dict(item) for item in raw_attachments if isinstance(item, dict)]
    return {
        "managed_turn_id": item.get("managed_turn_id"),
        "request_kind": item.get("request_kind"),
        "state": item.get("state"),
        "position": position,
        "enqueued_at": item.get("enqueued_at"),
        "visible_at": item.get("visible_at"),
        "prompt": item.get("prompt") or "",
        "prompt_preview": _truncate_text(item.get("prompt") or "", 120),
        "model": item.get("model"),
        "reasoning": item.get("reasoning"),
        "attachments": attachments,
        "client_turn_id": item.get("client_turn_id"),
        "queue_item_id": item.get("queue_item_id"),
    }


def _unified_pma_automation_read_model(
    request: Request,
    *,
    purpose: str,
    limit: int,
) -> dict[str, Any]:
    context = get_pma_request_context(request)
    purpose_set = (
        _SUBSCRIPTION_PURPOSES
        if purpose in _SUBSCRIPTION_PURPOSES
        else _TIMER_PURPOSES if purpose in _TIMER_PURPOSES else {purpose}
    )
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


def _create_unified_pma_subscription(
    request: Request, payload: dict[str, Any]
) -> dict[str, Any]:
    context = get_pma_request_context(request)
    hub_root = context.hub_root
    store = AutomationStore(hub_root)
    idempotency_key = normalize_optional_text(payload.get("idempotency_key"))
    normalized_thread_id = normalize_optional_text(payload.get("thread_id"))
    if idempotency_key is not None:
        existing = _find_pma_rule_by_idempotency(
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
        existing_auto = _find_covering_auto_subscription_rule(
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
            if _is_auto_subscription_key(idempotency_key):
                return {"subscription": row, "deduped": True}
            return {
                "subscription": row,
                "deduped": True,
                "warning": _covering_auto_subscription_warning(
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
    metadata = _resolved_unified_subscription_metadata(hub_root, payload)
    lane_id = _resolve_unified_subscription_lane_id(
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
        executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
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
            "legacy_subscription_id": subscription_id,
            "legacy_idempotency_key": idempotency_key,
            "legacy_reason": reason,
            "legacy_max_matches": max_matches,
            "legacy_match_count": 0,
            "legacy_metadata": metadata,
        },
        created_at=created_at,
        updated_at=created_at,
    )
    store.upsert_rule(rule)
    return {
        "subscription": subscription_row_from_rule(rule),
        "deduped": False,
    }


def _is_auto_subscription_key(idempotency_key: Optional[str]) -> bool:
    normalized_key = normalize_optional_text(idempotency_key)
    if normalized_key is None:
        return False
    return normalized_key.startswith(MANAGED_THREAD_AUTO_SUBSCRIPTION_PREFIXES)


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


def _find_covering_auto_subscription_rule(
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
        if rule.metadata.get("purpose") not in _SUBSCRIPTION_PURPOSES:
            continue
        row = subscription_row_from_rule(rule)
        if not _is_auto_subscription_key(row.get("idempotency_key")):
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


def _covering_auto_subscription_warning(
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


def _thread_binding_metadata(
    hub_root: Path, thread_id: str
) -> Optional[dict[str, Any]]:
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


def _resolved_unified_subscription_metadata(
    hub_root: Path, payload: dict[str, Any]
) -> dict[str, Any]:
    metadata = _pma_origin_metadata_from_payload(payload)
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


def _resolve_unified_subscription_lane_id(
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


def _create_unified_pma_timer(
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
        existing = _find_pma_rule_by_idempotency(
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
        executor_kind=EXECUTOR_MANAGED_THREAD_TURN,
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
            "legacy_timer_id": timer_id,
            "legacy_idempotency_key": idempotency_key,
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
            "legacy_timer_id": timer_id,
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


def _touch_unified_pma_timer(
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


def _find_pma_rule_by_idempotency(
    store: AutomationStore, *, purpose: str, idempotency_key: str
):
    purpose_set = (
        _SUBSCRIPTION_PURPOSES
        if purpose in _SUBSCRIPTION_PURPOSES
        else _TIMER_PURPOSES if purpose in _TIMER_PURPOSES else {purpose}
    )
    for rule in store.list_rules():
        if rule.metadata.get("purpose") not in purpose_set:
            continue
        if rule.metadata.get("legacy_idempotency_key") == idempotency_key:
            return rule
    return None


def _pma_origin_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
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


def _unified_subscription_rows(
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
            or rule.metadata.get("purpose") in _SUBSCRIPTION_PURPOSES
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


def _unified_timer_rows(
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
            or rule.metadata.get("purpose") in _TIMER_PURPOSES
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


def _cancel_unified_pma_subscription(request: Request, subscription_id: str) -> bool:
    store = AutomationStore(get_pma_request_context(request).hub_root)
    rule_id = f"{PMA_SUBSCRIPTION_RULE_PREFIX}{subscription_id}"
    existing = store.get_rule(rule_id)
    updated = store.set_rule_enabled(rule_id, False)
    return existing is not None and updated is not None and not updated.enabled


def _cancel_unified_pma_timer(request: Request, timer_id: str) -> bool:
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


async def _cleanup_failed_provisioned_worktree(
    request: Request,
    *,
    worktree_repo_id: Optional[str],
) -> None:
    normalized_repo_id = normalize_optional_text(worktree_repo_id)
    if normalized_repo_id is None:
        return
    supervisor = get_pma_request_context(request).hub_supervisor
    if supervisor is None:
        return
    try:
        await asyncio.to_thread(
            supervisor.retire_worktree,
            worktree_repo_id=normalized_repo_id,
            delete_branch=True,
        )
    except (
        Exception
    ) as exc:  # intentional: cleanup must not mask caller's original error
        _logger.warning(
            "Failed to clean up provisioned PMA worktree %s after thread creation failed: %s",
            normalized_repo_id,
            exc,
        )


def build_managed_thread_orchestration_service(request: Request):
    context = get_pma_request_context(request)
    try:
        descriptors = get_registered_agents(context.agent_context)
    except TypeError as exc:
        if "positional argument" not in str(exc):
            raise
        descriptors = get_registered_agents()

    def _make_harness(agent_id: str, profile: Optional[str] = None):
        cache = context.managed_thread_harness_cache
        resolution = resolve_agent_runtime(
            agent_id,
            profile,
            context=context.agent_context,
        )
        use_logical_profile_descriptor = (
            profile is not None and resolution.logical_agent_id == agent_id
        )
        descriptor_agent_id = (
            resolution.logical_agent_id
            if use_logical_profile_descriptor
            else resolution.runtime_agent_id
        )
        descriptor_profile = (
            resolution.logical_profile
            if use_logical_profile_descriptor
            else resolution.runtime_profile
        )
        cache_key = (descriptor_agent_id, descriptor_profile or "")
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        descriptor = descriptors.get(descriptor_agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{descriptor_agent_id}'")
        harness = descriptor.make_harness(
            wrap_requested_agent_context(
                context.agent_context,
                agent_id=resolution.logical_agent_id,
                profile=resolution.logical_profile,
            )
        )
        cache[cache_key] = harness
        return harness

    return build_harness_backed_orchestration_service(
        descriptors=cast(dict[str, RuntimeAgentDescriptor], descriptors),
        harness_factory=_make_harness,
        managed_thread_store=context.thread_store(),
    )


def _resolve_hermes_supervisor(request: Request, *, profile: Optional[str]):
    return resolve_cached_hermes_supervisor(request, profile=profile)


def _resolve_fork_supervisor(request: Request, *, profile: Optional[str]):
    return _resolve_hermes_supervisor(request, profile=profile)


def build_automation_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build automation subscription and timer routes."""

    @router.post("/automation/subscriptions")
    @router.post("/subscriptions")
    async def create_automation_subscription(
        request: Request, payload: PmaAutomationSubscriptionCreateRequest
    ) -> dict[str, Any]:
        runtime_state = get_runtime_state()
        try:
            normalized_payload = payload.normalized_payload()
            if not _subscription_request_has_explicit_routing(normalized_payload):
                normalized_payload = apply_origin_followup_context(
                    normalized_payload,
                    runtime_state,
                )
            created = _create_unified_pma_subscription(request, normalized_payload)
        except PmaAutomationThreadNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(created, dict) and "subscription" in created:
            return created
        return {"subscription": created}

    @router.get("/automation/subscriptions")
    @router.get("/subscriptions")
    async def list_automation_subscriptions(
        request: Request,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        unified = _unified_pma_automation_read_model(
            request,
            purpose="managed_thread_lifecycle_subscription",
            limit=limit,
        )
        return {
            "subscriptions": _unified_subscription_rows(
                request,
                repo_id=repo_id,
                run_id=run_id,
                thread_id=thread_id,
                lane_id=lane_id,
                limit=limit,
            ),
            "unified": unified,
        }

    @router.delete("/automation/subscriptions/{subscription_id}")
    @router.delete("/subscriptions/{subscription_id}")
    async def delete_automation_subscription(
        subscription_id: str, request: Request
    ) -> dict[str, Any]:
        normalized_id = (subscription_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="subscription_id is required")
        deleted = _cancel_unified_pma_subscription(request, normalized_id)
        return {
            "status": "ok",
            "subscription_id": normalized_id,
            "deleted": deleted,
        }

    @router.post("/automation/timers")
    @router.post("/timers")
    async def create_automation_timer(
        request: Request, payload: PmaAutomationTimerCreateRequest
    ) -> dict[str, Any]:
        try:
            normalized_payload = payload.normalized_payload()
            created = _create_unified_pma_timer(request, normalized_payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(created, dict) and "timer" in created:
            return created
        return {"timer": created}

    @router.get("/automation/timers")
    @router.get("/timers")
    async def list_automation_timers(
        request: Request,
        timer_type: Optional[str] = None,
        subscription_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        run_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        unified = _unified_pma_automation_read_model(
            request,
            purpose="managed_thread_timer",
            limit=limit,
        )
        return {
            "timers": _unified_timer_rows(
                request,
                timer_type=timer_type,
                subscription_id=subscription_id,
                repo_id=repo_id,
                run_id=run_id,
                thread_id=thread_id,
                lane_id=lane_id,
                limit=limit,
            ),
            "unified": unified,
        }

    @router.post("/automation/timers/{timer_id}/touch")
    @router.post("/timers/{timer_id}/touch")
    async def touch_automation_timer(
        timer_id: str,
        request: Request,
        payload: Annotated[Optional[PmaAutomationTimerTouchRequest], Body()] = None,
    ) -> dict[str, Any]:
        normalized_id = (timer_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="timer_id is required")
        try:
            normalized_payload = payload.normalized_payload() if payload else {}
            touched = _touch_unified_pma_timer(
                request, normalized_id, normalized_payload
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(touched, dict):
            out = dict(touched)
            out.setdefault("status", "ok")
            out.setdefault("timer_id", normalized_id)
            return out
        return {"status": "ok", "timer_id": normalized_id}

    @router.post("/automation/timers/{timer_id}/cancel")
    @router.post("/timers/{timer_id}/cancel")
    @router.delete("/automation/timers/{timer_id}")
    @router.delete("/timers/{timer_id}")
    async def cancel_automation_timer(
        timer_id: str,
        request: Request,
        payload: Annotated[Optional[PmaAutomationTimerCancelRequest], Body()] = None,
    ) -> dict[str, Any]:
        normalized_id = (timer_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="timer_id is required")
        try:
            if payload is not None:
                payload.normalized_payload()
            cancelled = _cancel_unified_pma_timer(request, normalized_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "ok",
            "timer_id": normalized_id,
            "cancelled": cancelled,
        }


def build_managed_thread_crud_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build managed-thread CRUD routes (create, list, get, compact, resume, retire)."""

    @router.post("/threads")
    async def create_managed_thread(
        request: Request, payload: ManagedThreadCreateRequest
    ) -> dict[str, Any]:
        context = get_pma_request_context(request)
        hub_root = context.hub_root
        resolved = resolve_managed_thread_create_resolution(request, payload)
        provisioned_workspace = await asyncio.to_thread(
            provision_managed_thread_workspace,
            request,
            resolution=resolved,
            display_name=normalize_optional_text(payload.name),
        )

        service = build_managed_thread_orchestration_service(request)
        try:
            metadata = managed_thread_metadata_for_provisioned_workspace(
                resolved,
                provisioned_workspace,
            )
            try:
                if resolved.scope is not None:
                    thread = service.create_thread_target(
                        resolved.agent_id,
                        provisioned_workspace.workspace_root,
                        scope=resolved.scope,
                        display_name=normalize_optional_text(payload.name),
                        metadata=metadata,
                    )
                else:
                    thread = service.create_thread_target(
                        resolved.agent_id,
                        provisioned_workspace.workspace_root,
                        repo_id=resolved.repo_id,
                        resource_kind=resolved.resource_kind,
                        resource_id=resolved.resource_id,
                        display_name=normalize_optional_text(payload.name),
                        metadata=metadata,
                    )
            except Exception:
                await _cleanup_failed_provisioned_worktree(
                    request,
                    worktree_repo_id=provisioned_workspace.worktree_repo_id,
                )
                raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        notification: Optional[dict[str, Any]] = None
        if resolved.followup_policy.event_mode == "terminal":
            automation_client = ManagedThreadAutomationClient(
                request,
                get_runtime_state,
            )
            try:
                notification = await automation_client.create_terminal_followup(
                    managed_thread_id=thread.thread_target_id,
                    lane_id=resolved.followup_policy.lane_id,
                    notify_once=resolved.followup_policy.notify_once,
                    idempotency_key=(
                        f"managed-thread-notify:{thread.thread_target_id}"
                        if resolved.followup_policy.notify_once
                        else None
                    ),
                    required=resolved.followup_policy.required,
                )
            except ManagedThreadAutomationUnavailable as exc:
                raise HTTPException(
                    status_code=503, detail="Automation action unavailable"
                ) from exc
        binding_metadata = _load_chat_binding_metadata_by_thread(hub_root)
        response: dict[str, Any] = {
            "thread": _serialize_thread_target(
                thread,
                binding_metadata_by_thread=binding_metadata,
            )
        }
        if notification is not None:
            response["notification"] = notification
        return response

    @router.get("/threads")
    def list_managed_threads(
        request: Request,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        query = resolve_managed_thread_list_query(
            agent=agent,
            status=status,
            lifecycle_status=lifecycle_status,
            resource_kind=resource_kind,
            resource_id=resource_id,
            limit=limit,
        )
        service = build_managed_thread_orchestration_service(request)
        threads = service.list_thread_targets(
            agent_id=query.agent_id,
            lifecycle_status=query.lifecycle_status,
            runtime_status=query.runtime_status,
            repo_id=query.repo_id,
            resource_kind=query.resource_kind,
            resource_id=query.resource_id,
            limit=query.limit,
        )
        active_work_by_thread = {
            summary.thread_target_id: summary
            for summary in service.list_active_work_summaries(
                agent_id=query.agent_id,
                repo_id=query.repo_id,
                resource_kind=query.resource_kind,
                resource_id=query.resource_id,
                limit=max(query.limit, len(threads), 1),
            )
        }
        binding_metadata = _load_chat_binding_metadata_by_thread(
            get_pma_request_context(request).hub_root
        )
        latest_execution_by_thread = {
            thread.thread_target_id: _resolve_running_or_latest_execution(
                service, thread.thread_target_id
            )
            for thread in threads
        }
        return {
            "threads": [
                _attach_latest_execution_fields(
                    _serialize_thread_target(
                        thread,
                        binding_metadata_by_thread=binding_metadata,
                        active_work_summary=active_work_by_thread.get(
                            thread.thread_target_id
                        ),
                    ),
                    service=service,
                    managed_thread_id=thread.thread_target_id,
                    execution=latest_execution_by_thread[thread.thread_target_id],
                )
                for thread in threads
            ]
        }

    @router.get("/threads/{managed_thread_id}")
    def get_managed_thread(managed_thread_id: str, request: Request) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        binding_metadata = _load_chat_binding_metadata_by_thread(
            get_pma_request_context(request).hub_root
        )
        serialized_thread = _attach_latest_execution_fields(
            _serialize_thread_target(
                thread,
                binding_metadata_by_thread=binding_metadata,
            ),
            service=service,
            managed_thread_id=managed_thread_id,
        )
        return {
            "thread": serialized_thread,
        }

    @router.post("/threads/{managed_thread_id}/compact")
    def compact_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: ManagedThreadCompactRequest,
    ) -> dict[str, Any]:
        summary = (payload.summary or "").strip()
        if not summary:
            raise HTTPException(status_code=400, detail="summary is required")
        context = get_pma_request_context(request)
        max_text_chars = int(context.config.pma.max_text_chars or 0)
        if max_text_chars > 0 and len(summary) > max_text_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"summary exceeds max_text_chars ({max_text_chars} characters)"
                ),
            )

        store = context.thread_store()
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_backend_thread_id = normalize_optional_text(thread.get("backend_thread_id"))
        reset_backend = bool(payload.reset_backend)
        store.set_thread_compact_seed(
            managed_thread_id,
            summary,
            reset_backend_id=reset_backend,
        )
        store.append_action(
            "managed_thread_compact",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "old_backend_thread_id": old_backend_thread_id,
                    "summary_length": len(summary),
                    "summary_preview": _truncate_text(summary, 240),
                    "reset_backend": reset_backend,
                },
                ensure_ascii=True,
            ),
        )
        updated = store.get_thread(managed_thread_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        thread_payload = _serialize_managed_thread(updated)
        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "thread": _apply_chat_binding_fields(
                thread_payload,
                managed_thread_id=managed_thread_id,
                binding_metadata_by_thread=binding_metadata,
            )
        }

    @router.post("/threads/{managed_thread_id}/fork")
    async def fork_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: ManagedThreadForkRequest,
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        source_thread = service.get_thread_target(managed_thread_id)
        if source_thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        if not source_thread.workspace_root:
            raise HTTPException(
                status_code=500,
                detail="Managed thread is missing workspace_root",
            )
        runtime_resolution = resolve_agent_runtime(
            source_thread.agent_id,
            source_thread.agent_profile,
            context=get_pma_request_context(request).agent_context,
        )
        if runtime_resolution.logical_agent_id != "hermes":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Managed thread agent '{source_thread.agent_id}' does not "
                    "support session fork"
                ),
            )
        source_session_id = normalize_optional_text(source_thread.backend_thread_id)
        if source_session_id is None:
            raise HTTPException(
                status_code=409,
                detail="Managed thread has no backend session to fork",
            )
        supervisor = _resolve_fork_supervisor(
            request,
            profile=runtime_resolution.logical_profile,
        )
        if supervisor is None:
            raise HTTPException(status_code=503, detail="Hermes runtime unavailable")
        try:
            forked_session = await supervisor.fork_session(
                Path(source_thread.workspace_root),
                source_session_id,
                title=normalize_optional_text(payload.name)
                or source_thread.display_name
                or source_thread.thread_target_id,
                metadata={
                    "flow_type": "managed_thread_fork",
                    "managed_thread_id": managed_thread_id,
                    "source_backend_thread_id": source_session_id,
                },
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if forked_session is None or not forked_session.session_id:
            raise HTTPException(
                status_code=409,
                detail="Hermes runtime does not support session fork",
            )

        context = get_pma_request_context(request)
        store = context.thread_store()
        stored_source = store.get_thread(managed_thread_id)
        metadata = dict((stored_source or {}).get("metadata") or {})
        if source_thread.backend_runtime_instance_id:
            metadata["backend_runtime_instance_id"] = (
                source_thread.backend_runtime_instance_id
            )
        try:
            forked_thread = service.create_thread_target(
                source_thread.agent_id,
                Path(source_thread.workspace_root),
                repo_id=source_thread.repo_id,
                resource_kind=source_thread.resource_kind,
                resource_id=source_thread.resource_id,
                display_name=normalize_optional_text(payload.name)
                or source_thread.display_name,
                backend_thread_id=forked_session.session_id,
                metadata=metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        refreshed_forked_thread = service.get_thread_target(
            forked_thread.thread_target_id
        )
        if refreshed_forked_thread is not None:
            forked_thread = refreshed_forked_thread
        store.append_action(
            "managed_thread_fork",
            managed_thread_id=forked_thread.thread_target_id,
            payload_json=json.dumps(
                {
                    "source_managed_thread_id": managed_thread_id,
                    "source_backend_thread_id": source_session_id,
                    "forked_backend_thread_id": forked_session.session_id,
                },
                ensure_ascii=True,
            ),
        )
        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "thread": _serialize_thread_target(
                forked_thread,
                binding_metadata_by_thread=binding_metadata,
            ),
            "forked_from_managed_thread_id": managed_thread_id,
            "source_backend_thread_id": source_session_id,
        }

    @router.post("/threads/{managed_thread_id}/resume")
    async def resume_managed_thread(
        managed_thread_id: str,
        request: Request,
        payload: ManagedThreadResumeRequest,
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        if not thread.workspace_root:
            raise HTTPException(
                status_code=500,
                detail="Managed thread is missing workspace_root",
            )

        old_backend_thread_id = normalize_optional_text(thread.backend_thread_id)
        old_status = normalize_optional_text(thread.lifecycle_status)
        updated = service.resume_thread_target(managed_thread_id)
        context = get_pma_request_context(request)
        store = context.thread_store()
        store.append_action(
            "managed_thread_resume",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps(
                {
                    "old_backend_thread_id": old_backend_thread_id,
                    "old_status": old_status,
                },
                ensure_ascii=True,
            ),
        )
        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "thread": _serialize_thread_target(
                updated,
                binding_metadata_by_thread=binding_metadata,
            )
        }

    @router.post("/threads/{managed_thread_id}/retire")
    def retire_managed_thread(
        managed_thread_id: str, request: Request
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        old_status = normalize_optional_text(thread.lifecycle_status)
        updated = service.archive_thread_target(managed_thread_id)
        context = get_pma_request_context(request)
        store = context.thread_store()
        store.append_action(
            "managed_thread_retire",
            managed_thread_id=managed_thread_id,
            payload_json=json.dumps({"old_status": old_status}, ensure_ascii=True),
        )
        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "thread": _serialize_thread_target(
                updated,
                binding_metadata_by_thread=binding_metadata,
            )
        }

    @router.post("/threads/retire")
    def retire_managed_threads(
        payload: ManagedThreadBulkRetireRequest, request: Request
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        context = get_pma_request_context(request)
        store = context.thread_store()
        retired_threads: list[Any] = []
        errors: list[dict[str, str]] = []

        for managed_thread_id in payload.thread_ids:
            thread = service.get_thread_target(managed_thread_id)
            if thread is None:
                errors.append(
                    {
                        "thread_id": managed_thread_id,
                        "detail": "Managed thread not found",
                    }
                )
                continue

            old_status = normalize_optional_text(thread.lifecycle_status)
            updated = service.archive_thread_target(managed_thread_id)
            store.append_action(
                "managed_thread_retire",
                managed_thread_id=managed_thread_id,
                payload_json=json.dumps({"old_status": old_status}, ensure_ascii=True),
            )
            retired_threads.append(updated)

        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "threads": [
                _serialize_thread_target(
                    thread,
                    binding_metadata_by_thread=binding_metadata,
                )
                for thread in retired_threads
            ],
            "retired_count": len(retired_threads),
            "requested_count": len(payload.thread_ids),
            "errors": errors,
            "error_count": len(errors),
        }

    @router.post("/threads/retire-active")
    def retire_active_managed_threads(request: Request) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        context = get_pma_request_context(request)
        store = context.thread_store()
        retire_targets = ChatSurfaceReadService(
            context.hub_root, durable=True
        ).chat_index_archive_targets()
        retired_threads: list[Any] = []
        errors: list[dict[str, str]] = []
        retired_row_ids: set[str] = set()
        retired_thread_ids: set[str] = set()
        retired_surfaces: list[dict[str, str]] = []

        for row in retire_targets:
            row_id = (
                normalize_optional_text(row.get("row_id"))
                or normalize_optional_text(row.get("chat_id"))
                or "unknown-chat-row"
            )
            row_retired = False
            managed_thread_id = normalize_optional_text(row.get("managed_thread_id"))
            if managed_thread_id is not None:
                if managed_thread_id in retired_thread_ids:
                    row_retired = True
                else:
                    thread = service.get_thread_target(managed_thread_id)
                    if thread is None:
                        if not _retirable_notification_surfaces(row):
                            errors.append(
                                {
                                    "thread_id": managed_thread_id,
                                    "detail": "Managed thread not found",
                                }
                            )
                    elif normalize_optional_text(thread.lifecycle_status) == "archived":
                        retired_thread_ids.add(managed_thread_id)
                        row_retired = True
                    else:
                        old_status = normalize_optional_text(thread.lifecycle_status)
                        updated = service.archive_thread_target(managed_thread_id)
                        store.append_action(
                            "managed_thread_retire",
                            managed_thread_id=managed_thread_id,
                            payload_json=json.dumps(
                                {"old_status": old_status}, ensure_ascii=True
                            ),
                        )
                        retired_threads.append(updated)
                        retired_thread_ids.add(managed_thread_id)
                        row_retired = True

            for surface in _retirable_notification_surfaces(row):
                surface_key = surface["surface_key"]
                _emit_notification_surface_retired(
                    hub_root=context.hub_root,
                    surface_key=surface_key,
                    row=row,
                )
                retired_surfaces.append(surface)
                row_retired = True

            if row_retired:
                retired_row_ids.add(row_id)

        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "threads": [
                _serialize_thread_target(
                    thread,
                    binding_metadata_by_thread=binding_metadata,
                )
                for thread in retired_threads
            ],
            "retired_surfaces": retired_surfaces,
            "retired_count": len(retired_row_ids),
            "requested_count": len(retire_targets),
            "errors": errors,
            "error_count": len(errors),
        }

    @router.get("/threads/{managed_thread_id}/turns")
    def list_managed_thread_turns(
        managed_thread_id: str,
        request: Request,
        limit: int = 50,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        limit = min(limit, 200)

        store = get_pma_request_context(request).thread_store()
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        turns = store.list_turns(managed_thread_id, limit=limit)
        return {
            "turns": [serialize_managed_thread_turn_summary(turn) for turn in turns]
        }

    @router.get("/threads/{managed_thread_id}/timeline")
    def get_managed_thread_timeline(
        managed_thread_id: str,
        request: Request,
        limit: int = Query(
            50,
            ge=1,
            description=(
                "Maximum turns to include in the bounded transcript projection. "
                f"Values above {MAX_MANAGED_THREAD_TIMELINE_LIMIT} are clamped. "
                "Assistant/log output deltas are omitted from this default timeline; "
                "use the per-turn debug endpoint for raw trace detail."
            ),
        ),
    ) -> dict[str, Any]:
        context = get_pma_request_context(request)
        store = context.thread_store()
        try:
            return build_managed_thread_timeline(
                context.hub_root,
                thread_store=store,
                managed_thread_id=managed_thread_id,
                limit=min(limit, MAX_MANAGED_THREAD_TIMELINE_LIMIT),
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Managed thread not found"
            ) from exc

    @router.get("/threads/{managed_thread_id}/queue")
    def list_managed_thread_queue(
        managed_thread_id: str,
        request: Request,
        limit: int = 200,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        limit = min(limit, 500)

        store = get_pma_request_context(request).thread_store()
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        queued_items = store.list_pending_turn_queue_items(
            managed_thread_id, limit=limit
        )
        return {
            "managed_thread_id": managed_thread_id,
            "queue_depth": store.get_queue_depth(managed_thread_id),
            "queued_turns": [
                _serialize_managed_thread_queue_item(
                    item,
                    position=index,
                    queue_payload=store.get_queued_turn_queue_payload(
                        managed_thread_id,
                        str(item.get("managed_turn_id") or ""),
                    ),
                )
                for index, item in enumerate(queued_items, start=1)
            ],
        }

    @router.post("/threads/{managed_thread_id}/queue/{managed_turn_id}/cancel")
    def cancel_managed_thread_queued_turn(
        managed_thread_id: str,
        managed_turn_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = get_pma_request_context(request).thread_store()
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        queued_items = store.list_pending_turn_queue_items(
            managed_thread_id,
            limit=max(store.get_queue_depth(managed_thread_id), 1),
        )
        queued_lookup = {
            str(item.get("managed_turn_id") or "").strip(): index
            for index, item in enumerate(queued_items, start=1)
        }
        position = queued_lookup.get(managed_turn_id)
        if position is None:
            turn = store.get_turn(managed_thread_id, managed_turn_id)
            if turn is None:
                raise HTTPException(status_code=404, detail="Managed turn not found")
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Managed turn {managed_turn_id} is not queued "
                    f"(status: {turn.get('status') or 'unknown'})"
                ),
            )
        if not store.cancel_queued_turn(managed_thread_id, managed_turn_id):
            raise HTTPException(
                status_code=409,
                detail=f"Managed turn {managed_turn_id} is no longer queued",
            )
        return {
            "status": "ok",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "cancelled": True,
            "position": position,
            "queue_depth": store.get_queue_depth(managed_thread_id),
        }

    @router.post("/threads/{managed_thread_id}/queue/clear")
    def clear_managed_thread_queue(
        managed_thread_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = get_pma_request_context(request).thread_store()
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        cleared_turn_ids = store.cancel_queued_turns(managed_thread_id)
        return {
            "status": "ok",
            "managed_thread_id": managed_thread_id,
            "cleared_count": len(cleared_turn_ids),
            "cleared_turn_ids": cleared_turn_ids,
            "queue_depth": store.get_queue_depth(managed_thread_id),
        }

    @router.get("/threads/{managed_thread_id}/turns/{managed_turn_id}")
    def get_managed_thread_turn(
        managed_thread_id: str,
        managed_turn_id: str,
        request: Request,
    ) -> dict[str, Any]:
        context = get_pma_request_context(request)
        hub_root = context.hub_root
        store = context.thread_store()
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        turn = store.get_turn(managed_thread_id, managed_turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail="Managed turn not found")

        timeline = list_turn_timeline(
            hub_root,
            execution_id=managed_turn_id,
        )
        journal = list_chat_execution_journal(
            hub_root,
            execution_id=managed_turn_id,
        )

        cold_store = context.cold_trace_store()
        manifest = cold_store.get_manifest(managed_turn_id)
        checkpoint = cold_store.load_checkpoint(managed_turn_id)

        hot_truncated = False
        hot_preview_only = False
        for entry in timeline:
            event = entry.get("event")
            if not isinstance(event, dict):
                continue
            if event.get("details_in_cold_trace"):
                hot_truncated = True
                hot_preview_only = True
            for flag in (
                "tool_input_truncated",
                "result_truncated",
                "error_truncated",
                "message_truncated",
                "data_truncated",
                "content_truncated",
                "final_message_truncated",
                "error_message_truncated",
            ):
                if event.get(flag):
                    hot_truncated = True
                    break

        response: dict[str, Any] = {
            "turn": turn,
            "journal": journal,
            "timeline": timeline,
            "trace_metadata": {
                "hot_timeline_entries": len(timeline),
                "hot_preview_truncated": hot_truncated,
                "hot_preview_only": hot_preview_only,
                "cold_trace_available": manifest is not None,
                "checkpoint_available": checkpoint is not None,
            },
        }
        if manifest is not None:
            response["trace_metadata"]["cold_trace"] = {
                "trace_id": manifest.trace_id,
                "status": manifest.status,
                "event_count": manifest.event_count,
                "byte_count": manifest.byte_count,
                "includes_families": list(manifest.includes_families),
            }
        return response

    @router.get("/bindings")
    def list_bindings(
        request: Request,
        agent: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        surface_kind: Optional[str] = None,
        include_disabled: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        query = resolve_owner_scoped_query(
            agent=agent,
            resource_kind=resource_kind,
            resource_id=resource_id,
            limit=limit,
        )
        service = build_managed_thread_orchestration_service(request)
        bindings = service.list_bindings(
            agent_id=query.agent_id,
            repo_id=query.repo_id,
            resource_kind=query.resource_kind,
            resource_id=query.resource_id,
            surface_kind=normalize_optional_text(surface_kind),
            include_disabled=include_disabled,
            limit=query.limit,
        )
        return {"bindings": [serialize_binding_record(binding) for binding in bindings]}

    @router.get("/bindings/active")
    def get_active_thread_for_binding(
        request: Request,
        surface_kind: str,
        surface_key: str,
    ) -> dict[str, Any]:
        if not surface_kind or not surface_key:
            raise HTTPException(
                status_code=400, detail="surface_kind and surface_key are required"
            )
        service = build_managed_thread_orchestration_service(request)
        thread_target_id = service.get_active_thread_for_binding(
            surface_kind=surface_kind,
            surface_key=surface_key,
        )
        return {"thread_target_id": thread_target_id}

    @router.get("/bindings/work")
    def list_active_work_summaries(
        request: Request,
        agent: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List busy thread summaries for running or queued work only."""
        query = resolve_owner_scoped_query(
            agent=agent,
            resource_kind=resource_kind,
            resource_id=resource_id,
            limit=limit,
        )
        service = build_managed_thread_orchestration_service(request)
        summaries = service.list_active_work_summaries(
            agent_id=query.agent_id,
            repo_id=query.repo_id,
            resource_kind=query.resource_kind,
            resource_id=query.resource_id,
            limit=query.limit,
        )
        return {"summaries": [serialize_active_work_summary(s) for s in summaries]}
