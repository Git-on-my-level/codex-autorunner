from __future__ import annotations

import logging
from typing import Any

from .automation import (
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    AutomationStore,
)
from .hub import HubSupervisor

_logger = logging.getLogger(__name__)


def empty_automation_snapshot() -> dict[str, Any]:
    return {
        "subscriptions": {"active_count": 0, "sample": []},
        "timers": {"pending_count": 0, "sample": []},
        "wakeups": {
            "pending_count": 0,
            "dispatched_recent_count": 0,
            "pending_sample": [],
        },
        "rules": {"enabled_count": 0, "sample": []},
        "schedules": {"active_count": 0, "sample": []},
        "jobs": {"pending_count": 0, "recent_sample": []},
    }


def snapshot_pma_automation(
    supervisor: HubSupervisor, *, max_items: int = 10
) -> dict[str, Any]:
    out = empty_automation_snapshot()
    hub_root = getattr(getattr(supervisor, "hub_config", None), "root", None)
    if hub_root is None:
        return out
    try:
        store = AutomationStore(hub_root)
        rules = store.list_rules()
        schedules = store.list_schedules()
        pending_jobs = store.list_jobs(state="pending", limit=max_items)
    except (RuntimeError, OSError, ValueError, TypeError):
        _logger.exception("Failed to read automation snapshot")
        return out

    subscription_rules = [
        rule
        for rule in rules
        if rule.rule_id.startswith(PMA_SUBSCRIPTION_RULE_PREFIX)
        or rule.metadata.get("purpose")
        in {"managed_thread_lifecycle_subscription", "pma_lifecycle_subscription"}
    ]
    timer_rules = {
        rule.rule_id: rule
        for rule in rules
        if rule.rule_id.startswith(PMA_TIMER_RULE_PREFIX)
        or rule.metadata.get("purpose") in {"managed_thread_timer", "pma_timer"}
    }
    timer_schedules = [
        schedule
        for schedule in schedules
        if schedule.rule_id in timer_rules and schedule.state == "active"
    ]

    def _pick(entry: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        picked: dict[str, Any] = {}
        for field in fields:
            value = entry.get(field)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            picked[field] = value
        return picked

    out["subscriptions"] = {
        "active_count": sum(1 for rule in subscription_rules if rule.enabled),
        "sample": [
            _pick(
                _subscription_row_from_rule(rule),
                (
                    "subscription_id",
                    "event_types",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "from_state",
                    "to_state",
                    "reason",
                ),
            )
            for rule in subscription_rules[:max_items]
        ],
    }
    timer_rows = _timer_rows_from_rules_and_schedules(timer_rules, timer_schedules)
    out["timers"] = {
        "pending_count": len(timer_rows),
        "sample": [
            _pick(
                entry,
                (
                    "timer_id",
                    "timer_type",
                    "due_at",
                    "idle_seconds",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "lane_id",
                    "reason",
                ),
            )
            for entry in timer_rows[:max_items]
        ],
    }
    out["wakeups"] = {
        "pending_count": len(pending_jobs),
        "dispatched_recent_count": 0,
        "pending_sample": [
            _pick(
                {
                    **job.to_dict(),
                    **(
                        job.target
                        if isinstance(getattr(job, "target", None), dict)
                        else {}
                    ),
                },
                (
                    "job_id",
                    "rule_id",
                    "event_id",
                    "state",
                    "available_at",
                    "repo_id",
                    "run_id",
                    "thread_id",
                    "result_summary",
                ),
            )
            for job in pending_jobs[:max_items]
        ],
    }
    _attach_unified_automation_snapshot(supervisor, out, max_items=max_items)
    return out


def _attach_unified_automation_snapshot(
    supervisor: HubSupervisor, out: dict[str, Any], *, max_items: int
) -> None:
    hub_root = getattr(getattr(supervisor, "hub_config", None), "root", None)
    if hub_root is None:
        return
    try:
        store = AutomationStore(hub_root)
        rules = store.list_rules()
        schedules = store.list_schedules()
        pending_jobs = store.list_jobs(state="pending", limit=max_items)
        recent_jobs = store.list_jobs(limit=max_items)
    except (RuntimeError, OSError, ValueError, TypeError):
        _logger.exception("Failed to read unified automation snapshot")
        return

    def _pick(entry: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
        picked: dict[str, Any] = {}
        for field in fields:
            value = entry.get(field)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            picked[field] = value
        return picked

    out["rules"] = {
        "enabled_count": sum(1 for rule in rules if rule.enabled),
        "sample": [
            _pick(
                rule.to_dict(),
                (
                    "rule_id",
                    "name",
                    "enabled",
                    "system_owned",
                    "trigger_kind",
                    "target_policy",
                    "executor_kind",
                    "metadata",
                ),
            )
            for rule in rules[:max_items]
        ],
    }
    out["schedules"] = {
        "active_count": sum(1 for schedule in schedules if schedule.state == "active"),
        "sample": [
            _pick(
                schedule.to_dict(),
                (
                    "schedule_id",
                    "rule_id",
                    "schedule_kind",
                    "next_fire_at",
                    "last_fire_at",
                    "state",
                    "schedule",
                ),
            )
            for schedule in schedules[:max_items]
        ],
    }
    out["jobs"] = {
        "pending_count": len(pending_jobs),
        "recent_sample": [
            _pick(
                job.to_dict(),
                (
                    "job_id",
                    "rule_id",
                    "event_id",
                    "state",
                    "available_at",
                    "pma_lane_id",
                    "pma_queue_item_id",
                    "result_summary",
                    "error_text",
                ),
            )
            for job in recent_jobs[:max_items]
        ],
    }


def _subscription_row_from_rule(rule: Any) -> dict[str, Any]:
    executor = rule.executor if isinstance(rule.executor, dict) else {}
    target = rule.target if isinstance(rule.target, dict) else {}
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    return {
        "subscription_id": rule.metadata.get("legacy_subscription_id")
        or rule.rule_id.removeprefix(PMA_SUBSCRIPTION_RULE_PREFIX),
        "event_types": list(rule.trigger.get("event_types") or []),
        "repo_id": target.get("repo_id") or filters.get("event.repo_id"),
        "run_id": target.get("run_id") or filters.get("event.payload.run_id"),
        "thread_id": target.get("thread_id") or filters.get("event.payload.thread_id"),
        "lane_id": executor.get("lane_id") or "pma:default",
        "from_state": filters.get("event.payload.from_state"),
        "to_state": filters.get("event.payload.to_state"),
        "reason": rule.metadata.get("legacy_reason"),
    }


def _timer_rows_from_rules_and_schedules(
    rules: dict[str, Any], schedules: list[Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for schedule in schedules:
        rule = rules.get(schedule.rule_id)
        if rule is None:
            continue
        schedule_config = (
            schedule.schedule if isinstance(schedule.schedule, dict) else {}
        )
        payload = schedule_config.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        rows.append(
            {
                "timer_id": payload.get("timer_id")
                or rule.metadata.get("legacy_timer_id")
                or schedule.schedule_id.removeprefix("pma-timer:"),
                "timer_type": payload.get("timer_type")
                or schedule_config.get("timer_kind")
                or "one_shot",
                "due_at": schedule.next_fire_at,
                "idle_seconds": payload.get("idle_seconds"),
                "repo_id": payload.get("repo_id"),
                "run_id": payload.get("run_id"),
                "thread_id": payload.get("thread_id"),
                "lane_id": payload.get("lane_id") or "pma:default",
                "reason": payload.get("reason"),
            }
        )
    return rows
