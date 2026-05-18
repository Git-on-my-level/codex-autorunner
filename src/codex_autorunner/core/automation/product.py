from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..time_utils import now_iso
from .engine import AutomationRuleEngine
from .models import (
    EXECUTOR_PMA_TURN,
    EXECUTOR_TICKET_FLOW,
    SCHEDULE_DAILY,
    SCHEDULE_WEEKLY,
    TARGET_POLICY_HUB,
    TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
    TRIGGER_KIND_SCHEDULE,
    AutomationEvent,
    AutomationRule,
    AutomationSchedule,
    normalize_timestamp,
)
from .store import AutomationStore

AUTOMATION_METADATA_KIND = "web_automation"
AUTOMATION_RULE_PREFIX = "user:automation:"


@dataclass(frozen=True)
class AutomationPresetRequest:
    preset: str
    name: Optional[str] = None
    repo_id: Optional[str] = None
    timezone: str = "UTC"
    hour: int = 9
    minute: int = 0
    weekday: int = 0
    prompt: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    profile: Optional[str] = None
    enabled: bool = False


@dataclass(frozen=True)
class AutomationUpdateRequest:
    name: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None
    hour: Optional[int] = None
    minute: Optional[int] = None
    weekday: Optional[int] = None
    prompt: Optional[str] = None
    ticket_body: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None
    reasoning: Optional[str] = None
    profile: Optional[str] = None
    trigger_kind: Optional[str] = None
    trigger: Optional[dict[str, Any]] = None
    filters: Optional[dict[str, Any]] = None
    target_policy: Optional[str] = None
    target: Optional[dict[str, Any]] = None
    executor_kind: Optional[str] = None
    executor: Optional[dict[str, Any]] = None
    policy: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


def automation_store(hub_root: Path) -> AutomationStore:
    return AutomationStore(hub_root)


def automation_overview(store: AutomationStore, *, limit: int = 100) -> dict[str, Any]:
    take = max(1, min(int(limit or 100), 500))
    rules = store.list_rules()[:take]
    rows = [automation_row(store, rule) for rule in rules]
    summary = {
        "total": len(rows),
        "active": sum(1 for row in rows if row["enabled"]),
        "paused": sum(1 for row in rows if not row["enabled"]),
        "failed_jobs": sum(
            1
            for row in rows
            if str((row.get("last_job") or {}).get("state")) == "failed"
        ),
    }
    return {"automations": rows, "summary": summary}


def automation_row(store: AutomationStore, rule: AutomationRule) -> dict[str, Any]:
    schedules = store.list_schedules(rule_id=rule.rule_id)
    jobs = store.list_jobs(rule_id=rule.rule_id, limit=25)
    last_job = max(jobs, key=lambda item: item.updated_at, default=None)
    schedule = schedules[0] if schedules else None
    return {
        "id": rule.rule_id,
        "name": rule.name,
        "enabled": rule.enabled,
        "kind": display_kind(rule),
        "executor_kind": rule.executor_kind,
        "executor": rule.executor,
        "trigger_kind": rule.trigger_kind,
        "trigger": rule.trigger,
        "filters": rule.filters,
        "target_policy": rule.target_policy,
        "target": rule.target,
        "policy": rule.policy,
        "metadata": rule.metadata,
        "schedule": schedule.to_dict() if schedule is not None else None,
        "last_job": last_job.to_dict() if last_job is not None else None,
        "job_count": len(jobs),
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def automation_detail(store: AutomationStore, rule_id: str) -> dict[str, Any]:
    rule = store.get_rule(rule_id)
    if rule is None:
        raise KeyError(rule_id)
    return automation_row(store, rule)


def create_preset_automation(
    store: AutomationStore, payload: AutomationPresetRequest
) -> dict[str, Any]:
    preset = payload.preset.strip().lower()
    if preset == "security_scan_pr":
        rule, schedule = _build_security_scan_pr(payload)
    elif preset == "weekly_ticket_flow":
        rule, schedule = _build_weekly_ticket_flow(payload)
    else:
        raise ValueError("preset must be security_scan_pr or weekly_ticket_flow")

    saved_rule = store.upsert_rule(rule)
    schedule_payload = dict(schedule.to_dict())
    schedule_payload["rule_id"] = saved_rule.rule_id
    saved_schedule = store.upsert_schedule(
        AutomationSchedule.create(**schedule_payload)
    )
    return {
        **automation_row(store, saved_rule),
        "schedule": saved_schedule.to_dict(),
    }


def update_automation(
    store: AutomationStore, rule_id: str, payload: AutomationUpdateRequest
) -> dict[str, Any]:
    existing = store.get_rule(rule_id)
    if existing is None:
        raise KeyError(rule_id)

    executor = (
        dict(payload.executor)
        if payload.executor is not None
        else dict(existing.executor)
    )
    if payload.prompt is not None:
        executor["message"] = payload.prompt
    if payload.ticket_body is not None:
        ticket_pack = dict(executor.get("ticket_pack") or {})
        tickets = list(ticket_pack.get("tickets") or [])
        if tickets:
            first_ticket = dict(tickets[0])
            first_ticket["content"] = payload.ticket_body
            tickets[0] = first_ticket
        else:
            tickets = [{"path": "TICKET-001.md", "content": payload.ticket_body}]
        ticket_pack["tickets"] = tickets
        executor["ticket_pack"] = ticket_pack
    _apply_executor_option(executor, "agent", payload.agent)
    _apply_executor_option(executor, "model", payload.model)
    _apply_executor_option(executor, "reasoning", payload.reasoning)
    _apply_executor_option(executor, "profile", payload.profile)
    _apply_executor_option(executor, "agent_profile", payload.profile)

    updated_rule = AutomationRule.create(
        rule_id=existing.rule_id,
        name=_optional_text(payload.name) or existing.name,
        enabled=existing.enabled if payload.enabled is None else payload.enabled,
        system_owned=existing.system_owned,
        trigger_kind=payload.trigger_kind or existing.trigger_kind,
        trigger=payload.trigger if payload.trigger is not None else existing.trigger,
        filters=payload.filters if payload.filters is not None else existing.filters,
        target_policy=payload.target_policy or existing.target_policy,
        target=payload.target if payload.target is not None else existing.target,
        executor_kind=payload.executor_kind or existing.executor_kind,
        executor=executor,
        policy=payload.policy if payload.policy is not None else existing.policy,
        metadata=(
            payload.metadata if payload.metadata is not None else existing.metadata
        ),
        created_at=existing.created_at,
        updated_at=now_iso(),
    )
    saved_rule = store.upsert_rule(updated_rule)

    schedules = store.list_schedules(rule_id=rule_id)
    if schedules:
        current = schedules[0]
        schedule_payload = dict(current.schedule)
        timezone_name = payload.timezone or current.timezone
        if payload.hour is not None:
            schedule_payload["hour"] = _bounded_int(
                payload.hour, field_name="hour", low=0, high=23
            )
        if payload.minute is not None:
            schedule_payload["minute"] = _bounded_int(
                payload.minute, field_name="minute", low=0, high=59
            )
        if payload.weekday is not None:
            schedule_payload["weekdays"] = [
                _bounded_int(payload.weekday, field_name="weekday", low=0, high=6)
            ]
        hour_value = _bounded_int(
            int(schedule_payload.get("hour", 0)), field_name="hour", low=0, high=23
        )
        minute_value = _bounded_int(
            int(schedule_payload.get("minute", 0)),
            field_name="minute",
            low=0,
            high=59,
        )
        next_fire_at: Optional[str]
        if current.schedule_kind == SCHEDULE_WEEKLY:
            weekdays = schedule_payload.get("weekdays")
            weekday_value = 0
            if isinstance(weekdays, list) and weekdays:
                weekday_value = int(weekdays[0])
            weekday_value = _bounded_int(
                weekday_value, field_name="weekday", low=0, high=6
            )
            next_fire_at = _next_weekly_fire_at(
                timezone_name, hour_value, minute_value, weekday_value
            )
            schedule_payload["weekdays"] = [weekday_value]
        elif current.schedule_kind == SCHEDULE_DAILY:
            next_fire_at = _next_daily_fire_at(timezone_name, hour_value, minute_value)
        else:
            next_fire_at = current.next_fire_at

        saved_schedule = store.upsert_schedule(
            AutomationSchedule.create(
                schedule_id=current.schedule_id,
                rule_id=current.rule_id,
                schedule_kind=current.schedule_kind,
                timezone=timezone_name,
                next_fire_at=next_fire_at,
                last_fire_at=current.last_fire_at,
                misfire_policy=current.misfire_policy,
                schedule=schedule_payload,
                state=current.state,
                created_at=current.created_at,
                updated_at=now_iso(),
            )
        )
        return {
            **automation_row(store, saved_rule),
            "schedule": saved_schedule.to_dict(),
        }

    return automation_row(store, saved_rule)


def run_automation_now(
    store: AutomationStore,
    rule_id: str,
    *,
    source: str,
    supervisor: Any = None,
) -> dict[str, Any]:
    rule = store.get_rule(rule_id)
    if rule is None:
        raise KeyError(rule_id)
    schedule = store.list_schedules(rule_id=rule_id)
    schedule_payload = schedule[0].to_dict() if schedule else {"rule_id": rule_id}
    schedule_payload["next_fire_at"] = normalize_timestamp(None)
    event = AutomationEvent.create(
        event_type="schedule.fire",
        source=source,
        target={"rule_id": rule_id, **dict(rule.target or {})},
        payload={"trigger": f"{source}_run_now", "schedule": schedule_payload},
        metadata={},
    )
    saved_event = store.record_event(event)
    result = AutomationRuleEngine(store).enqueue_job_for_rule(rule, saved_event)
    process_now = getattr(supervisor, "process_pma_automation_now", None)
    processed: dict[str, Any] = {}
    if callable(process_now):
        processed_result = process_now(include_timers=False)
        processed = (
            processed_result
            if isinstance(processed_result, dict)
            else {"result": processed_result}
        )
    return {
        "status": "ok",
        "rule_id": rule_id,
        "event": saved_event.to_dict(),
        "matched_rules": result.matched_rules,
        "jobs_created": result.jobs_created,
        "jobs_deduped": result.jobs_deduped,
        "processed": processed,
    }


def set_automation_enabled(
    store: AutomationStore, rule_id: str, enabled: bool
) -> dict[str, Any]:
    updated = store.set_rule_enabled(rule_id, enabled)
    if updated is None:
        raise KeyError(rule_id)
    return automation_row(store, updated)


def display_kind(rule: AutomationRule) -> str:
    kind = str(
        rule.metadata.get("automation_kind") or rule.metadata.get("preset") or ""
    ).strip()
    if kind:
        return kind
    if rule.executor_kind == EXECUTOR_TICKET_FLOW:
        return "ticket_flow"
    if rule.executor_kind == EXECUTOR_PMA_TURN:
        return "pma_prompt"
    return rule.executor_kind


def format_automation_list(overview: dict[str, Any], *, limit: int = 10) -> str:
    rows = list(overview.get("automations") or [])[:limit]
    summary = dict(overview.get("summary") or {})
    if not rows:
        return "No automations are configured yet."
    lines = [
        (
            "Automations: "
            f"{summary.get('active', 0)} active, "
            f"{summary.get('paused', 0)} paused, "
            f"{summary.get('failed_jobs', 0)} failed latest jobs"
        )
    ]
    for row in rows:
        lines.append(_format_automation_brief(row))
    return "\n".join(lines)


def format_automation_status(row: dict[str, Any]) -> str:
    lines = [
        row.get("name") or row.get("id") or "Automation",
        f"ID: {row.get('id')}",
        f"State: {'active' if row.get('enabled') else 'paused'}",
        f"Kind: {row.get('kind') or row.get('executor_kind') or 'unknown'}",
        f"Schedule: {_format_schedule(row.get('schedule'))}",
        f"Target: {_format_target(row)}",
        f"Last job: {_format_job(row.get('last_job'))}",
    ]
    return "\n".join(lines)


def _format_automation_brief(row: dict[str, Any]) -> str:
    state = "active" if row.get("enabled") else "paused"
    kind = row.get("kind") or row.get("executor_kind") or "automation"
    return (
        f"- {row.get('name') or row.get('id')} [{state}, {kind}]\n"
        f"  id: {row.get('id')}\n"
        f"  schedule: {_format_schedule(row.get('schedule'))}\n"
        f"  last job: {_format_job(row.get('last_job'))}"
    )


def _format_schedule(schedule: Any) -> str:
    if not isinstance(schedule, dict):
        return "none"
    kind = schedule.get("schedule_kind") or "schedule"
    next_fire_at = schedule.get("next_fire_at") or "not scheduled"
    timezone_name = schedule.get("timezone") or "UTC"
    return f"{kind} in {timezone_name}; next {next_fire_at}"


def _format_job(job: Any) -> str:
    if not isinstance(job, dict):
        return "none"
    state = job.get("state") or "unknown"
    updated_at = job.get("updated_at") or job.get("created_at") or "unknown time"
    return f"{state} at {updated_at}"


def _format_target(row: dict[str, Any]) -> str:
    raw_metadata = row.get("metadata")
    raw_target = row.get("target")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    target: dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    repo_id = (
        metadata.get("repo_id") or target.get("repo_id") or target.get("base_repo_id")
    )
    policy = row.get("target_policy") or "target"
    if repo_id:
        return f"{repo_id} via {policy}"
    return str(policy)


def _build_security_scan_pr(
    payload: AutomationPresetRequest,
) -> tuple[AutomationRule, AutomationSchedule]:
    repo_id = _required_text(payload.repo_id, "repo_id")
    name = _optional_text(payload.name) or f"Daily security scan for {repo_id}"
    prompt = _optional_text(payload.prompt) or (
        f"Run a security scan for repo {repo_id}. Inspect dependency, "
        "secret, and static-analysis findings using the repo's existing tooling. "
        "If actionable issues are discovered, create a focused fix branch, make the "
        "smallest safe changes, run relevant checks, and open a draft PR with the "
        "findings and verification. If no issues are found, summarize the clean result."
    )
    rule_id = _rule_id(name)
    schedule = _daily_schedule(
        rule_id=rule_id,
        timezone_name=payload.timezone,
        hour=payload.hour,
        minute=payload.minute,
    )
    rule = AutomationRule.create(
        rule_id=rule_id,
        name=name,
        trigger_kind=TRIGGER_KIND_SCHEDULE,
        trigger={"event_types": ["schedule.fire"]},
        filters={"schedule.rule_id": rule_id},
        target_policy=TARGET_POLICY_HUB,
        target={"repo_id": repo_id},
        executor_kind=EXECUTOR_PMA_TURN,
        executor=_executor_with_agent_options(
            {"lane_id": "pma:default", "message": prompt}, payload
        ),
        enabled=payload.enabled,
        policy={
            "dedupe_key": f"{rule_id}:{{{{ schedule.next_fire_at }}}}",
            "approval_mode": "never_require_approval",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        metadata={
            "kind": AUTOMATION_METADATA_KIND,
            "automation_kind": "security_scan_pr",
            "preset": "security_scan_pr",
            "repo_id": repo_id,
            "description": "Daily PMA security scan that opens a PR when issues are found.",
        },
    )
    return rule, schedule


def _build_weekly_ticket_flow(
    payload: AutomationPresetRequest,
) -> tuple[AutomationRule, AutomationSchedule]:
    repo_id = _required_text(payload.repo_id, "repo_id")
    name = _optional_text(payload.name) or f"Weekly ticket flow for {repo_id}"
    rule_id = _rule_id(name)
    schedule = _weekly_schedule(
        rule_id=rule_id,
        timezone_name=payload.timezone,
        hour=payload.hour,
        minute=payload.minute,
        weekday=payload.weekday,
    )
    ticket_id = f"tkt_{uuid.uuid4().hex}"
    ticket_body = f"""---
agent: codex
done: false
ticket_id: "{ticket_id}"
title: Weekly maintenance automation
goal: Run the configured weekly maintenance pass and open a PR for useful changes
---

You are running a scheduled weekly ticket flow for `{repo_id}`.

- Sync context from `.codex-autorunner/contextspace/` and inspect the current repo state.
- Run dependency, test, lint, and maintenance checks that are already standard for this repo.
- Fix small, well-bounded issues that are clearly safe.
- If changes are made, run relevant verification and open a draft PR with a concise summary.
- If no changes are needed, record the checks performed and mark this ticket done.
"""
    rule = AutomationRule.create(
        rule_id=rule_id,
        name=name,
        trigger_kind=TRIGGER_KIND_SCHEDULE,
        trigger={"event_types": ["schedule.fire"]},
        filters={"schedule.rule_id": rule_id},
        target_policy=TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
        target={"base_repo_id": repo_id, "rule_slug": _slug(name)},
        executor_kind=EXECUTOR_TICKET_FLOW,
        executor={
            **_executor_with_agent_options(
                {
                    "ticket_pack": {
                        "source": "inline",
                        "tickets": [{"path": "TICKET-001.md", "content": ticket_body}],
                    }
                },
                payload,
            )
        },
        enabled=payload.enabled,
        policy={
            "dedupe_key": f"{rule_id}:{{{{ schedule.next_fire_at }}}}",
            "approval_mode": "never_require_approval",
            "max_attempts": 2,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        metadata={
            "kind": AUTOMATION_METADATA_KIND,
            "automation_kind": "weekly_ticket_flow",
            "preset": "weekly_ticket_flow",
            "repo_id": repo_id,
            "description": "Weekly scheduled ticket flow in a new automation worktree.",
        },
    )
    return rule, schedule


def _daily_schedule(
    *, rule_id: str, timezone_name: str, hour: int, minute: int
) -> AutomationSchedule:
    schedule = {
        "hour": _bounded_int(hour, field_name="hour", low=0, high=23),
        "minute": _bounded_int(minute, field_name="minute", low=0, high=59),
        "second": 0,
    }
    return AutomationSchedule.create(
        schedule_id=f"{rule_id}:schedule",
        rule_id=rule_id,
        schedule_kind=SCHEDULE_DAILY,
        timezone=timezone_name,
        next_fire_at=_next_daily_fire_at(
            timezone_name, schedule["hour"], schedule["minute"]
        ),
        schedule=schedule,
    )


def _weekly_schedule(
    *, rule_id: str, timezone_name: str, hour: int, minute: int, weekday: int
) -> AutomationSchedule:
    weekday_value = _bounded_int(weekday, field_name="weekday", low=0, high=6)
    hour_value = _bounded_int(hour, field_name="hour", low=0, high=23)
    minute_value = _bounded_int(minute, field_name="minute", low=0, high=59)
    schedule = {
        "hour": hour_value,
        "minute": minute_value,
        "second": 0,
        "weekdays": [weekday_value],
    }
    return AutomationSchedule.create(
        schedule_id=f"{rule_id}:schedule",
        rule_id=rule_id,
        schedule_kind=SCHEDULE_WEEKLY,
        timezone=timezone_name,
        next_fire_at=_next_weekly_fire_at(
            timezone_name, hour_value, minute_value, weekday_value
        ),
        schedule=schedule,
    )


def _next_daily_fire_at(timezone_name: str, hour: int, minute: int) -> str:
    tz = _zone(timezone_name)
    now = datetime.now(tz)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return _iso(candidate)


def _next_weekly_fire_at(
    timezone_name: str, hour: int, minute: int, weekday: int
) -> str:
    tz = _zone(timezone_name)
    now = datetime.now(tz)
    days_ahead = (weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return _iso(candidate)


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo((timezone_name or "UTC").strip() or "UTC")
    except Exception as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc


def _iso(value: datetime) -> str:
    return normalize_timestamp(value.astimezone(timezone.utc).isoformat())


def _rule_id(name: str) -> str:
    return f"{AUTOMATION_RULE_PREFIX}{_slug(name)}-{uuid.uuid4().hex[:8]}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:64] or "automation"


def _optional_text(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _required_text(value: Optional[str], field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def _bounded_int(value: int, *, field_name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    parsed = int(value)
    if parsed < low or parsed > high:
        raise ValueError(f"{field_name} must be between {low} and {high}")
    return parsed


def _apply_executor_option(
    executor: dict[str, Any], key: str, value: Optional[str]
) -> None:
    if value is None:
        return
    text = value.strip()
    if text:
        executor[key] = text
    else:
        executor.pop(key, None)


def _executor_with_agent_options(
    executor: dict[str, Any], payload: AutomationPresetRequest
) -> dict[str, Any]:
    configured = dict(executor)
    _apply_executor_option(configured, "agent", payload.agent)
    _apply_executor_option(configured, "model", payload.model)
    _apply_executor_option(configured, "reasoning", payload.reasoning)
    _apply_executor_option(configured, "profile", payload.profile)
    _apply_executor_option(configured, "agent_profile", payload.profile)
    return configured


__all__ = [
    "AutomationPresetRequest",
    "AutomationUpdateRequest",
    "automation_detail",
    "automation_overview",
    "automation_row",
    "automation_store",
    "create_preset_automation",
    "format_automation_list",
    "format_automation_status",
    "run_automation_now",
    "set_automation_enabled",
    "update_automation",
]
