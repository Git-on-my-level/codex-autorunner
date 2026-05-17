from __future__ import annotations

import asyncio
import json
import logging
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
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
    AutomationSchedule,
    AutomationStore,
    mirror_pma_subscription_rule,
    mirror_pma_timer_schedule,
)
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
from .....core.pma_automation_store import (
    PmaAutomationStore,
    PmaAutomationThreadNotFoundError,
    PmaAutomationTimer,
    PmaLifecycleSubscription,
)
from .....core.pma_automation_types import (
    DEFAULT_WATCHDOG_IDLE_SECONDS,
    TIMER_TYPE_WATCHDOG,
    _iso_after_seconds,
    _normalize_due_timestamp,
    _normalize_non_negative_int,
    _normalize_positive_int,
    _normalize_timer_type,
)
from .....core.text_utils import _truncate_text
from .....core.time_utils import now_iso
from ...schemas import (
    ManagedThreadBulkArchiveRequest,
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
from ...services.pma.managed_thread_followup import (
    ManagedThreadAutomationClient,
    ManagedThreadAutomationUnavailable,
    apply_origin_followup_context,
)
from .automation_adapter import (
    call_store_action_with_id,
    call_store_create_with_payload,
    call_store_list,
    get_automation_store,
    normalize_optional_text,
)
from .hermes_supervisors import resolve_cached_hermes_supervisor
from .managed_thread_route_helpers import (
    _apply_chat_binding_fields,
    _attach_latest_execution_fields,
    _load_chat_binding_metadata_by_thread,
    _resolve_running_or_latest_execution,
    _serialize_managed_thread,
    _serialize_thread_target,
    managed_thread_metadata_for_provisioned_workspace,
    provision_managed_thread_workspace,
    resolve_managed_thread_create_resolution,
    resolve_managed_thread_list_query,
    resolve_owner_scoped_query,
    serialize_active_work_summary,
    serialize_binding_record,
    serialize_managed_thread_turn_summary,
)

_logger = logging.getLogger(__name__)


def _archiveable_notification_surfaces(
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


def _emit_notification_surface_archived(
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
        source_kind="hub.pma.archive_active",
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
    try:
        store = AutomationStore(context.hub_root)
        rules = [
            rule
            for rule in store.list_rules()
            if rule.metadata.get("purpose") == purpose
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


def _is_builtin_pma_store(store: Any) -> bool:
    return isinstance(store, PmaAutomationStore)


def _create_unified_pma_subscription(
    request: Request, payload: dict[str, Any]
) -> dict[str, Any]:
    store = AutomationStore(get_pma_request_context(request).hub_root)
    idempotency_key = normalize_optional_text(payload.get("idempotency_key"))
    if idempotency_key is not None:
        existing = _find_pma_rule_by_idempotency(
            store,
            purpose="pma_lifecycle_subscription",
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return {
                "subscription": _subscription_row_from_rule(existing),
                "deduped": True,
            }

    metadata = _pma_origin_metadata_from_payload(payload)
    subscription = PmaLifecycleSubscription.create(
        event_types=payload.get("event_types"),
        repo_id=normalize_optional_text(payload.get("repo_id")),
        run_id=normalize_optional_text(payload.get("run_id")),
        thread_id=normalize_optional_text(payload.get("thread_id")),
        lane_id=normalize_optional_text(payload.get("lane_id")),
        from_state=normalize_optional_text(payload.get("from_state")),
        to_state=normalize_optional_text(payload.get("to_state")),
        reason=normalize_optional_text(payload.get("reason")),
        idempotency_key=idempotency_key,
        max_matches=_normalize_positive_int(payload.get("max_matches"), fallback=None),
        metadata=metadata,
    )
    mirror_pma_subscription_rule(store, subscription=subscription)
    return {"subscription": subscription.to_dict(), "deduped": False}


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
            rows = _timer_rows_from_rules_and_schedules(
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

    metadata_raw = payload.get("metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else None
    timer = PmaAutomationTimer.create(
        due_at=due_at,
        timer_type=timer_type,
        idle_seconds=idle_seconds,
        subscription_id=subscription_id,
        repo_id=normalize_optional_text(payload.get("repo_id")),
        run_id=normalize_optional_text(payload.get("run_id")),
        thread_id=normalize_optional_text(payload.get("thread_id")),
        lane_id=normalize_optional_text(payload.get("lane_id")),
        from_state=normalize_optional_text(payload.get("from_state")),
        to_state=normalize_optional_text(payload.get("to_state")),
        reason=normalize_optional_text(payload.get("reason")),
        idempotency_key=idempotency_key,
        metadata=metadata,
    )
    requested_timer_id = normalize_optional_text(payload.get("timer_id"))
    if requested_timer_id is not None:
        timer.timer_id = requested_timer_id
    mirror_pma_timer_schedule(store, timer=timer)
    return {"timer": timer.to_dict(), "deduped": False}


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
    rows = _timer_rows_from_rules_and_schedules(
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
    for rule in store.list_rules():
        if rule.metadata.get("purpose") != purpose:
            continue
        if rule.metadata.get("legacy_idempotency_key") == idempotency_key:
            return rule
    return None


def _pma_origin_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    origin = {
        key: value
        for key, value in {
            "thread_id": normalize_optional_text(payload.get("origin_thread_id")),
            "lane_id": normalize_optional_text(payload.get("origin_lane_id")),
            "surface_kind": normalize_optional_text(payload.get("origin_surface_kind")),
            "surface_key": normalize_optional_text(payload.get("origin_surface_key")),
        }.items()
        if value is not None
    }
    return {"pma_origin": origin} if origin else {}


def _subscription_row_from_rule(rule: Any) -> dict[str, Any]:
    executor = rule.executor if isinstance(rule.executor, dict) else {}
    target = rule.target if isinstance(rule.target, dict) else {}
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    return {
        "subscription_id": rule.metadata.get("legacy_subscription_id")
        or rule.rule_id.removeprefix(PMA_SUBSCRIPTION_RULE_PREFIX),
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
        "state": "active" if rule.enabled else "cancelled",
        "event_types": list(rule.trigger.get("event_types") or []),
        "repo_id": target.get("repo_id") or filters.get("event.repo_id"),
        "run_id": target.get("run_id") or filters.get("event.payload.run_id"),
        "thread_id": target.get("thread_id") or filters.get("event.payload.thread_id"),
        "lane_id": executor.get("lane_id") or "pma:default",
        "from_state": filters.get("event.payload.from_state"),
        "to_state": filters.get("event.payload.to_state"),
        "reason": rule.metadata.get("legacy_reason"),
        "idempotency_key": rule.metadata.get("legacy_idempotency_key"),
        "max_matches": rule.metadata.get("legacy_max_matches"),
        "match_count": rule.metadata.get("legacy_match_count") or 0,
        "metadata": dict(rule.metadata.get("legacy_metadata") or {}),
    }


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
        store.backfill_legacy_pma_automation()
        rules = [
            rule
            for rule in store.list_rules(enabled=True)
            if rule.rule_id.startswith(PMA_SUBSCRIPTION_RULE_PREFIX)
            or rule.metadata.get("purpose") == "pma_lifecycle_subscription"
        ]
    except (RuntimeError, OSError, ValueError, TypeError):
        _logger.exception("Failed to list unified PMA subscription rows")
        return []

    out: list[dict[str, Any]] = []
    for rule in rules:
        row = _subscription_row_from_rule(rule)
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


def _timer_rows_from_rules_and_schedules(
    rules: dict[str, Any], schedules: list[AutomationSchedule]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for schedule in schedules:
        rule = rules.get(schedule.rule_id)
        if rule is None:
            continue
        schedule_config = (
            schedule.schedule if isinstance(schedule.schedule, dict) else {}
        )
        payload = schedule_config.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        out.append(
            {
                "timer_id": payload.get("timer_id")
                or rule.metadata.get("legacy_timer_id")
                or schedule.schedule_id.removeprefix("pma-timer:"),
                "due_at": schedule.next_fire_at,
                "created_at": schedule.created_at,
                "updated_at": schedule.updated_at,
                "state": "pending" if schedule.state == "active" else schedule.state,
                "fired_at": schedule.last_fire_at,
                "timer_type": payload.get("timer_type")
                or schedule_config.get("timer_kind")
                or "one_shot",
                "idle_seconds": payload.get("idle_seconds"),
                "subscription_id": payload.get("subscription_id"),
                "repo_id": payload.get("repo_id"),
                "run_id": payload.get("run_id"),
                "thread_id": payload.get("thread_id"),
                "lane_id": payload.get("lane_id") or "pma:default",
                "from_state": payload.get("from_state"),
                "to_state": payload.get("to_state"),
                "reason": payload.get("reason"),
                "idempotency_key": rule.metadata.get("legacy_idempotency_key"),
                "metadata": dict(payload.get("metadata") or {}),
            }
        )
    return out


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
        store.backfill_legacy_pma_automation()
        rules = {
            rule.rule_id: rule
            for rule in store.list_rules(enabled=True)
            if rule.rule_id.startswith(PMA_TIMER_RULE_PREFIX)
            or rule.metadata.get("purpose") == "pma_timer"
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
    for row in _timer_rows_from_rules_and_schedules(rules, schedules):
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
        store = await get_automation_store(request, runtime_state)
        try:
            normalized_payload = payload.normalized_payload()
            if not _subscription_request_has_explicit_routing(normalized_payload):
                normalized_payload = apply_origin_followup_context(
                    normalized_payload,
                    runtime_state,
                )
            if _is_builtin_pma_store(store):
                created = _create_unified_pma_subscription(request, normalized_payload)
            else:
                created = await call_store_create_with_payload(
                    store,
                    (
                        "create_subscription",
                        "upsert_subscription",
                    ),
                    normalized_payload,
                )
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
        store = await get_automation_store(request, get_runtime_state())
        unified = _unified_pma_automation_read_model(
            request,
            purpose="pma_lifecycle_subscription",
            limit=limit,
        )
        if _is_builtin_pma_store(store):
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
        subscriptions = await call_store_list(
            store,
            ("list_subscriptions", "get_subscriptions"),
            {
                k: v
                for k, v in {
                    "repo_id": normalize_optional_text(repo_id),
                    "run_id": normalize_optional_text(run_id),
                    "thread_id": normalize_optional_text(thread_id),
                    "lane_id": normalize_optional_text(lane_id),
                    "limit": limit,
                }.items()
                if v is not None
            },
        )
        if isinstance(subscriptions, dict) and "subscriptions" in subscriptions:
            return {**subscriptions, "unified": unified}
        return {"subscriptions": list(subscriptions or []), "unified": unified}

    @router.delete("/automation/subscriptions/{subscription_id}")
    @router.delete("/subscriptions/{subscription_id}")
    async def delete_automation_subscription(
        subscription_id: str, request: Request
    ) -> dict[str, Any]:
        normalized_id = (subscription_id or "").strip()
        if not normalized_id:
            raise HTTPException(status_code=400, detail="subscription_id is required")
        store = await get_automation_store(request, get_runtime_state())
        if _is_builtin_pma_store(store):
            unified_deleted = _cancel_unified_pma_subscription(request, normalized_id)
            return {
                "status": "ok",
                "subscription_id": normalized_id,
                "deleted": unified_deleted,
                "unified_deleted": unified_deleted,
                "legacy_deleted": False,
            }
        deleted = await call_store_action_with_id(
            store,
            ("cancel_subscription",),
            normalized_id,
            payload={},
            id_aliases=("subscription_id", "id"),
        )
        if isinstance(deleted, dict):
            payload = dict(deleted)
            payload.setdefault("status", "ok")
            payload.setdefault("subscription_id", normalized_id)
            return payload
        return {
            "status": "ok",
            "subscription_id": normalized_id,
            "deleted": True if deleted is None else bool(deleted),
        }

    @router.post("/automation/timers")
    @router.post("/timers")
    async def create_automation_timer(
        request: Request, payload: PmaAutomationTimerCreateRequest
    ) -> dict[str, Any]:
        store = await get_automation_store(request, get_runtime_state())
        try:
            normalized_payload = payload.normalized_payload()
            if _is_builtin_pma_store(store):
                created = _create_unified_pma_timer(request, normalized_payload)
            else:
                created = await call_store_create_with_payload(
                    store,
                    ("create_timer", "upsert_timer"),
                    normalized_payload,
                )
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
        store = await get_automation_store(request, get_runtime_state())
        unified = _unified_pma_automation_read_model(
            request,
            purpose="pma_timer",
            limit=limit,
        )
        if _is_builtin_pma_store(store):
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
        timers = await call_store_list(
            store,
            ("list_timers", "get_timers"),
            {
                k: v
                for k, v in {
                    "timer_type": normalize_optional_text(timer_type),
                    "subscription_id": normalize_optional_text(subscription_id),
                    "repo_id": normalize_optional_text(repo_id),
                    "run_id": normalize_optional_text(run_id),
                    "thread_id": normalize_optional_text(thread_id),
                    "lane_id": normalize_optional_text(lane_id),
                    "limit": limit,
                }.items()
                if v is not None
            },
        )
        if isinstance(timers, dict) and "timers" in timers:
            return {**timers, "unified": unified}
        return {"timers": list(timers or []), "unified": unified}

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
        store = await get_automation_store(request, get_runtime_state())
        try:
            normalized_payload = payload.normalized_payload() if payload else {}
            if _is_builtin_pma_store(store):
                touched = _touch_unified_pma_timer(
                    request, normalized_id, normalized_payload
                )
            else:
                touched = await call_store_action_with_id(
                    store,
                    ("touch_timer",),
                    normalized_id,
                    payload=normalized_payload,
                    id_aliases=("timer_id", "id"),
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
        store = await get_automation_store(request, get_runtime_state())
        try:
            if _is_builtin_pma_store(store):
                unified_deleted = _cancel_unified_pma_timer(request, normalized_id)
                return {
                    "status": "ok",
                    "timer_id": normalized_id,
                    "cancelled": unified_deleted,
                    "unified_deleted": unified_deleted,
                    "legacy_deleted": False,
                }
            normalized_payload = payload.normalized_payload() if payload else {}
            cancelled = await call_store_action_with_id(
                store,
                ("cancel_timer",),
                normalized_id,
                payload=normalized_payload,
                id_aliases=("timer_id", "id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(cancelled, dict):
            out = dict(cancelled)
            out.setdefault("status", "ok")
            out.setdefault("timer_id", normalized_id)
            return out
        return {"status": "ok", "timer_id": normalized_id}


def build_managed_thread_crud_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build managed-thread CRUD routes (create, list, get, compact, resume, archive)."""

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

    @router.post("/threads/{managed_thread_id}/archive")
    def archive_managed_thread(
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
            "managed_thread_archive",
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

    @router.post("/threads/archive")
    def archive_managed_threads(
        payload: ManagedThreadBulkArchiveRequest, request: Request
    ) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        context = get_pma_request_context(request)
        store = context.thread_store()
        archived_threads: list[Any] = []
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
                "managed_thread_archive",
                managed_thread_id=managed_thread_id,
                payload_json=json.dumps({"old_status": old_status}, ensure_ascii=True),
            )
            archived_threads.append(updated)

        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "threads": [
                _serialize_thread_target(
                    thread,
                    binding_metadata_by_thread=binding_metadata,
                )
                for thread in archived_threads
            ],
            "archived_count": len(archived_threads),
            "requested_count": len(payload.thread_ids),
            "errors": errors,
            "error_count": len(errors),
        }

    @router.post("/threads/archive-active")
    def archive_active_managed_threads(request: Request) -> dict[str, Any]:
        service = build_managed_thread_orchestration_service(request)
        context = get_pma_request_context(request)
        store = context.thread_store()
        archive_targets = ChatSurfaceReadService(
            context.hub_root, durable=True
        ).chat_index_archive_targets()
        archived_threads: list[Any] = []
        errors: list[dict[str, str]] = []
        archived_row_ids: set[str] = set()
        archived_thread_ids: set[str] = set()
        archived_surfaces: list[dict[str, str]] = []

        for row in archive_targets:
            row_id = (
                normalize_optional_text(row.get("row_id"))
                or normalize_optional_text(row.get("chat_id"))
                or "unknown-chat-row"
            )
            row_archived = False
            managed_thread_id = normalize_optional_text(row.get("managed_thread_id"))
            if managed_thread_id is not None:
                if managed_thread_id in archived_thread_ids:
                    row_archived = True
                else:
                    thread = service.get_thread_target(managed_thread_id)
                    if thread is None:
                        if not _archiveable_notification_surfaces(row):
                            errors.append(
                                {
                                    "thread_id": managed_thread_id,
                                    "detail": "Managed thread not found",
                                }
                            )
                    elif normalize_optional_text(thread.lifecycle_status) == "archived":
                        archived_thread_ids.add(managed_thread_id)
                        row_archived = True
                    else:
                        old_status = normalize_optional_text(thread.lifecycle_status)
                        updated = service.archive_thread_target(managed_thread_id)
                        store.append_action(
                            "managed_thread_archive",
                            managed_thread_id=managed_thread_id,
                            payload_json=json.dumps(
                                {"old_status": old_status}, ensure_ascii=True
                            ),
                        )
                        archived_threads.append(updated)
                        archived_thread_ids.add(managed_thread_id)
                        row_archived = True

            for surface in _archiveable_notification_surfaces(row):
                surface_key = surface["surface_key"]
                _emit_notification_surface_archived(
                    hub_root=context.hub_root,
                    surface_key=surface_key,
                    row=row,
                )
                archived_surfaces.append(surface)
                row_archived = True

            if row_archived:
                archived_row_ids.add(row_id)

        binding_metadata = _load_chat_binding_metadata_by_thread(context.hub_root)
        return {
            "threads": [
                _serialize_thread_target(
                    thread,
                    binding_metadata_by_thread=binding_metadata,
                )
                for thread in archived_threads
            ],
            "archived_surfaces": archived_surfaces,
            "archived_count": len(archived_row_ids),
            "requested_count": len(archive_targets),
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
