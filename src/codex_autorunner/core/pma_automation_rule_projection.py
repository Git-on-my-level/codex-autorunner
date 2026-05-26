from __future__ import annotations

from typing import Any

from .automation.builtins import (
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
)


def _rule_executor_target_filters(
    rule: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    executor = rule.executor if isinstance(rule.executor, dict) else {}
    target = rule.target if isinstance(rule.target, dict) else {}
    filters = rule.filters if isinstance(rule.filters, dict) else {}
    return executor, target, filters


def subscription_row_from_rule(
    rule: Any,
    *,
    include_rule_id: bool = False,
) -> dict[str, Any]:
    executor, target, filters = _rule_executor_target_filters(rule)
    row: dict[str, Any] = {
        "subscription_id": rule.metadata.get("subscription_id")
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
        "reason": rule.metadata.get("reason"),
        "idempotency_key": rule.metadata.get("idempotency_key"),
        "max_matches": rule.metadata.get("max_matches"),
        "match_count": rule.metadata.get("match_count") or 0,
        "metadata": dict(rule.metadata.get("metadata") or {}),
    }
    if include_rule_id:
        row["rule_id"] = rule.rule_id
    return row


def _schedule_payload(
    schedule: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    schedule_config = schedule.schedule if isinstance(schedule.schedule, dict) else {}
    payload = schedule_config.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return schedule_config, payload


def timer_row_from_rule_and_schedule(rule: Any, schedule: Any) -> dict[str, Any]:
    schedule_config, payload = _schedule_payload(schedule)
    return {
        "timer_id": payload.get("timer_id")
        or rule.metadata.get("timer_id")
        or schedule.schedule_id.removeprefix(PMA_TIMER_SCHEDULE_PREFIX),
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
        "idempotency_key": rule.metadata.get("idempotency_key"),
        "metadata": dict(payload.get("metadata") or {}),
    }


def timer_rows_from_rules_and_schedules(
    rules: dict[str, Any],
    schedules: list[Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for schedule in schedules:
        rule = rules.get(schedule.rule_id)
        if rule is None:
            continue
        out.append(timer_row_from_rule_and_schedule(rule, schedule))
    return out
