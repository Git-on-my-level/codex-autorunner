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
from .execution_graph import (
    AutomationExecutionSnapshot,
    automation_execution_snapshot,
    automation_execution_snapshots_by_job_id,
)
from .models import (
    EXECUTOR_AGENT_TASK_TURN,
    EXECUTOR_GITHUB_COMMENT,
    EXECUTOR_GITHUB_REACTION,
    EXECUTOR_MANAGED_THREAD_TURN,
    EXECUTOR_PMA_OPERATOR_TURN,
    EXECUTOR_PUBLISH_CHAT_NOTIFICATION,
    EXECUTOR_PUBLISH_OPERATION,
    EXECUTOR_TICKET_FLOW,
    JOB_CANCELLED,
    JOB_DEAD_LETTERED,
    JOB_FAILED,
    JOB_PAUSED,
    JOB_RUNNING,
    JOB_SKIPPED,
    JOB_SUCCEEDED,
    JOB_TERMINAL_STATES,
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONE_SHOT,
    SCHEDULE_WEEKLY,
    TARGET_POLICY_HUB,
    TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_MANUAL,
    TRIGGER_KIND_SCHEDULE,
    AutomationChildExecutionEdge,
    AutomationEvent,
    AutomationJob,
    AutomationRule,
    AutomationSchedule,
    normalize_timestamp,
)
from .store import AutomationStore

AUTOMATION_METADATA_KIND = "web_automation"
AUTOMATION_RULE_PREFIX = "user:automation:"


@dataclass(frozen=True)
class AutomationPresetDescriptor:
    id: str
    name: str
    automation_kind: str
    description: str
    schedule_kind: str
    executor_kind: str
    target_policy: str
    default_timezone: str
    default_hour: int
    default_minute: int
    default_weekday: Optional[int]
    target_shape: dict[str, Any]
    executor_shape: dict[str, Any]
    policy: dict[str, Any]
    prompt_template: str
    ticket_body_template: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.automation_kind,
            "description": self.description,
            "schedule": {
                "kind": self.schedule_kind,
                "timezone": self.default_timezone,
                "hour": self.default_hour,
                "minute": self.default_minute,
                "weekday": self.default_weekday,
            },
            "target_policy": self.target_policy,
            "target_shape": self.target_shape,
            "executor_kind": self.executor_kind,
            "executor_shape": self.executor_shape,
            "policy": self.policy,
            "prompt_template": self.prompt_template,
            "ticket_body_template": self.ticket_body_template,
        }


SECURITY_SCAN_PROMPT_TEMPLATE = (
    "Run a security scan for repo {repo_id}. Inspect dependency, secret, and "
    "static-analysis findings using the repo's existing tooling. If actionable "
    "issues are discovered, create a focused fix branch, make the smallest safe "
    "changes, run relevant checks, and open a draft PR with the findings and "
    "verification. If no issues are found, summarize the clean result."
)

WEEKLY_TICKET_BODY_TEMPLATE = """---
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

AUTOMATION_PRESET_DESCRIPTORS: dict[str, AutomationPresetDescriptor] = {
    "security_scan_pr": AutomationPresetDescriptor(
        id="security_scan_pr",
        name="Daily Security Scan",
        automation_kind="security_scan_pr",
        description=(
            "Daily agent security scan that opens a PR when issues are found."
        ),
        schedule_kind=SCHEDULE_DAILY,
        executor_kind=EXECUTOR_AGENT_TASK_TURN,
        target_policy=TARGET_POLICY_HUB,
        default_timezone="UTC",
        default_hour=9,
        default_minute=0,
        default_weekday=None,
        target_shape={"repo_id": "{repo_id}"},
        executor_shape={"message_text": "{prompt}"},
        policy={
            "approval_mode": "never_require_approval",
            "max_attempts": 3,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        prompt_template=SECURITY_SCAN_PROMPT_TEMPLATE,
    ),
    "weekly_ticket_flow": AutomationPresetDescriptor(
        id="weekly_ticket_flow",
        name="Weekly Preset Ticket Flow",
        automation_kind="weekly_ticket_flow",
        description=("Weekly scheduled ticket flow in a new automation worktree."),
        schedule_kind=SCHEDULE_WEEKLY,
        executor_kind=EXECUTOR_TICKET_FLOW,
        target_policy=TARGET_POLICY_NEW_AUTOMATION_WORKTREE,
        default_timezone="UTC",
        default_hour=10,
        default_minute=0,
        default_weekday=0,
        target_shape={"base_repo_id": "{repo_id}", "rule_slug": "{automation_slug}"},
        executor_shape={
            "ticket_pack": {
                "source": "inline",
                "tickets": [{"path": "TICKET-001.md", "content": "{ticket_body}"}],
            }
        },
        policy={
            "approval_mode": "never_require_approval",
            "max_attempts": 2,
            "max_concurrent_per_rule": 1,
            "max_concurrent_per_target": 1,
        },
        prompt_template=(
            "Run the configured weekly maintenance ticket flow and open a draft "
            "PR for useful changes."
        ),
        ticket_body_template=WEEKLY_TICKET_BODY_TEMPLATE,
    ),
}


@dataclass(frozen=True)
class AutomationPresetRequest:
    preset: str
    execution_mode: Optional[str] = None
    name: Optional[str] = None
    repo_id: Optional[str] = None
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
    worker_child_policy: Optional[dict[str, Any]] = None
    enabled: bool = False


@dataclass(frozen=True)
class AutomationUpdateRequest:
    name: Optional[str] = None
    enabled: Optional[bool] = None
    execution_mode: Optional[str] = None
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
    worker_child_policy: Optional[dict[str, Any]] = None
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
    rows = automation_rows(store, rules, recent_job_limit=25)
    summary = {
        "total": len(rows),
        "active": sum(1 for row in rows if row["enabled"]),
        "paused": sum(1 for row in rows if not row["enabled"]),
        "failed_jobs": sum(
            1
            for row in rows
            if str((row.get("last_job") or {}).get("effective_state")) == JOB_FAILED
        ),
    }
    return {"automations": rows, "summary": summary, "presets": automation_presets()}


def automation_rows(
    store: AutomationStore,
    rules: list[AutomationRule],
    *,
    recent_job_limit: int = 25,
) -> list[dict[str, Any]]:
    rule_ids = [rule.rule_id for rule in rules]
    schedules_by_rule = store.schedules_by_rule(rule_ids)
    recent_jobs_by_rule = store.recent_jobs_by_rule(
        rule_ids, per_rule_limit=recent_job_limit
    )
    job_counts_by_rule = store.job_counts_by_rule(rule_ids)
    all_jobs = [
        job for rule in rules for job in recent_jobs_by_rule.get(rule.rule_id, [])
    ]
    execution_snapshots = automation_execution_snapshots_by_job_id(
        all_jobs, hub_root=store.hub_root
    )
    return [
        _automation_row_from_enrichment(
            rule,
            store=store,
            schedules=schedules_by_rule.get(rule.rule_id, []),
            recent_jobs=recent_jobs_by_rule.get(rule.rule_id, []),
            job_count=job_counts_by_rule.get(rule.rule_id, 0),
            execution_snapshots=execution_snapshots,
        )
        for rule in rules
    ]


def automation_presets() -> list[dict[str, Any]]:
    return [
        descriptor.to_dict() for descriptor in AUTOMATION_PRESET_DESCRIPTORS.values()
    ]


def automation_row(store: AutomationStore, rule: AutomationRule) -> dict[str, Any]:
    schedules = store.list_schedules(rule_id=rule.rule_id)
    recent_jobs = store.list_jobs(rule_id=rule.rule_id, limit=25, order="newest")
    job_count = store.count_jobs(rule_id=rule.rule_id)
    return _automation_row_from_enrichment(
        rule,
        store=store,
        schedules=schedules,
        recent_jobs=recent_jobs,
        job_count=job_count,
    )


def _automation_row_from_enrichment(
    rule: AutomationRule,
    *,
    store: AutomationStore,
    schedules: list[AutomationSchedule],
    recent_jobs: list[AutomationJob],
    job_count: int,
    execution_snapshots: Optional[dict[str, AutomationExecutionSnapshot]] = None,
) -> dict[str, Any]:
    last_job = recent_jobs[0] if recent_jobs else None
    schedule = schedules[0] if schedules else None
    typed = _typed_product_projection(rule, schedule)
    snapshots = execution_snapshots
    if snapshots is None and recent_jobs:
        snapshots = automation_execution_snapshots_by_job_id(
            recent_jobs, hub_root=store.hub_root
        )
    return {
        "id": rule.rule_id,
        "name": rule.name,
        "enabled": rule.enabled,
        "system_owned": rule.system_owned,
        "kind": display_kind(rule),
        "execution_mode": rule.executor_kind,
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
        "last_job": (
            _automation_job_row(last_job, store=store, execution_snapshots=snapshots)
            if last_job is not None
            else None
        ),
        "jobs": [
            _automation_job_row(job, store=store, execution_snapshots=snapshots)
            for job in recent_jobs
        ],
        "job_count": job_count,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
        **typed,
    }


def _automation_job_row(
    job: AutomationJob,
    *,
    store: Optional[AutomationStore] = None,
    execution_snapshots: Optional[dict[str, AutomationExecutionSnapshot]] = None,
) -> dict[str, Any]:
    snapshot = (
        execution_snapshots.get(job.job_id) if execution_snapshots is not None else None
    )
    if snapshot is None:
        snapshot = automation_execution_snapshot(
            job,
            hub_root=store.hub_root if store is not None else None,
        )
    child_execution = snapshot.to_dict()
    pma_queue_result = child_execution.get("pma_queue")
    child_edges = (
        store.list_child_execution_edges(job.job_id) if store is not None else []
    )
    children = [_automation_child_row(edge) for edge in child_edges]
    effective_state = _effective_job_state(job, child_edges)
    terminal_reason = _job_terminal_reason(job, child_edges, effective_state)
    policy_violations = _job_policy_violations(job, child_edges)
    return {
        "job_id": job.job_id,
        "state": job.state,
        "raw_state": job.state,
        "effective_state": effective_state,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
        "result_summary": job.result_summary,
        "error_text": job.error_text,
        "attempt_count": job.attempt_count,
        "managed_thread_target_id": job.managed_thread_target_id,
        "managed_thread_execution_id": job.managed_thread_execution_id,
        "pma_lane_id": job.pma_lane_id,
        "pma_queue_item_id": job.pma_queue_item_id,
        "pma_queue_result": pma_queue_result,
        "child_execution": child_execution,
        "children": children,
        "runtime_contract": _job_runtime_contract(job, child_edges),
        "terminal_reason": terminal_reason,
        "policy_violations": policy_violations,
        "ticket_flow_run_id": job.ticket_flow_run_id,
        "ticket_flow_worktree_id": job.ticket_flow_worktree_id,
    }


def _automation_child_row(edge: AutomationChildExecutionEdge) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "parent_job_id": edge.parent_job_id,
        "child_kind": edge.child_kind,
        "child_id": edge.child_id,
        "authoritative_for_parent_completion": (
            edge.authoritative_for_parent_completion
        ),
        "requested_runtime": edge.requested_runtime.to_dict(),
        "actual_runtime": (
            edge.actual_runtime.to_dict() if edge.actual_runtime is not None else None
        ),
        "terminal_mapping": edge.terminal_mapping,
        "terminal_event_id": edge.terminal_event_id,
        "terminal_state": edge.terminal_state,
        "terminal_observed_at": edge.terminal_observed_at,
        "created_at": edge.created_at,
        "updated_at": edge.updated_at,
    }


def _effective_job_state(
    job: AutomationJob, edges: list[AutomationChildExecutionEdge]
) -> str:
    authoritative = [edge for edge in edges if edge.authoritative_for_parent_completion]
    if not authoritative or any(edge.terminal_state is None for edge in authoritative):
        return job.state
    mapped = [
        edge.terminal_mapping.get(str(edge.terminal_state), JOB_FAILED)
        for edge in authoritative
    ]
    if any(state in {JOB_FAILED, JOB_DEAD_LETTERED} for state in mapped):
        return JOB_FAILED
    if any(state == JOB_CANCELLED for state in mapped):
        return JOB_CANCELLED
    if any(state == JOB_PAUSED for state in mapped):
        return JOB_PAUSED
    if all(state in JOB_TERMINAL_STATES or state == JOB_FAILED for state in mapped):
        return JOB_SUCCEEDED
    return job.state


def _job_terminal_reason(
    job: AutomationJob, edges: list[AutomationChildExecutionEdge], effective_state: str
) -> Optional[str]:
    if job.error_text:
        return job.error_text
    if job.result_summary and effective_state in {
        JOB_SUCCEEDED,
        JOB_FAILED,
        JOB_CANCELLED,
        JOB_SKIPPED,
        JOB_PAUSED,
        JOB_DEAD_LETTERED,
    }:
        return job.result_summary
    authoritative = [edge for edge in edges if edge.authoritative_for_parent_completion]
    if job.state != effective_state and authoritative:
        child_states = ", ".join(
            f"{edge.child_kind}:{edge.terminal_state or 'open'}"
            for edge in authoritative
        )
        return f"Derived from authoritative child execution: {child_states}"
    return None


def _job_policy_violations(
    job: AutomationJob, edges: list[AutomationChildExecutionEdge]
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if job.state == JOB_RUNNING and _effective_job_state(job, edges) != job.state:
        violations.append(
            {
                "code": "AUTOMATION_PARENT_STATE_STALE",
                "severity": "warning",
                "message": (
                    "Parent job raw state is stale relative to authoritative child "
                    "execution."
                ),
            }
        )
    for edge in edges:
        if edge.terminal_state == "policy_violated":
            violations.append(
                {
                    "code": "AUTOMATION_CHILD_POLICY_VIOLATED",
                    "severity": "error",
                    "child_id": edge.child_id,
                    "message": "Authoritative child execution reported policy_violated.",
                }
            )
        requested = edge.requested_runtime.to_dict()
        actual = (
            edge.actual_runtime.to_dict() if edge.actual_runtime is not None else {}
        )
        for key in ("agent", "model", "profile", "reasoning"):
            requested_value = requested.get(key)
            actual_value = actual.get(key)
            if requested_value and actual_value and requested_value != actual_value:
                violations.append(
                    {
                        "code": "AUTOMATION_RUNTIME_MISMATCH",
                        "severity": "error",
                        "child_id": edge.child_id,
                        "field": key,
                        "message": (
                            f"Requested {key}={requested_value!r} but child ran "
                            f"{actual_value!r}."
                        ),
                    }
                )
    return violations


def _job_runtime_contract(
    job: AutomationJob, edges: list[AutomationChildExecutionEdge]
) -> dict[str, Any]:
    if edges:
        requested_edges = [edge.requested_runtime.to_dict() for edge in edges]
        actual_edges = [
            edge.actual_runtime.to_dict() if edge.actual_runtime is not None else None
            for edge in edges
        ]
        return {
            "requested": (
                requested_edges[0] if len(requested_edges) == 1 else requested_edges
            ),
            "actual": actual_edges[0] if len(actual_edges) == 1 else actual_edges,
        }
    executor = dict(job.executor)
    requested = _requested_runtime_from_executor(executor)
    return {
        "requested": requested or None,
        "actual": (
            executor.get("actual_runtime")
            if isinstance(executor.get("actual_runtime"), dict)
            else None
        ),
    }


def automation_detail(store: AutomationStore, rule_id: str) -> dict[str, Any]:
    rule = store.get_rule(rule_id)
    if rule is None:
        raise KeyError(rule_id)
    return automation_row(store, rule)


def create_preset_automation(
    store: AutomationStore, payload: AutomationPresetRequest
) -> dict[str, Any]:
    preset = _preset_descriptor(payload.preset)
    if preset.id == "security_scan_pr":
        rule, schedule = _build_security_scan_pr(payload)
    elif preset.id == "weekly_ticket_flow":
        rule, schedule = _build_weekly_ticket_flow(payload)
    else:
        raise ValueError(f"unsupported automation preset: {preset.id}")

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

    executor_kind = (
        payload.execution_mode or payload.executor_kind or existing.executor_kind
    )
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
    if payload.worker_child_policy is not None:
        executor["worker_child_policy"] = dict(payload.worker_child_policy)
    _materialize_product_runtime_contract(
        executor_kind=executor_kind,
        executor=executor,
        agent=payload.agent,
        model=payload.model,
        reasoning=payload.reasoning,
        profile=payload.profile,
    )
    _validate_product_executor_mode(executor_kind, executor)

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
        executor_kind=executor_kind,
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
    _validate_product_executor_mode(rule.executor_kind, dict(rule.executor))
    schedule = store.list_schedules(rule_id=rule_id)
    schedule_payload = schedule[0].to_dict() if schedule else {"rule_id": rule_id}
    manual_run_id = f"manual:{uuid.uuid4().hex}"
    schedule_payload["next_fire_at"] = manual_run_id
    schedule_payload["manual_run_id"] = manual_run_id
    event = AutomationEvent.create(
        event_type="schedule.fire",
        source=source,
        target={"rule_id": rule_id, **dict(rule.target or {})},
        payload={"trigger": f"{source}_run_now", "schedule": schedule_payload},
        metadata={},
    )
    saved_event = store.record_event(event)
    result = AutomationRuleEngine(store).enqueue_job_for_rule(rule, saved_event)
    process_now = getattr(supervisor, "process_automation_now", None)
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
    if rule.executor_kind == EXECUTOR_AGENT_TASK_TURN:
        return "agent_task"
    if rule.executor_kind == EXECUTOR_PMA_OPERATOR_TURN:
        return "pma_operator"
    if rule.executor_kind == EXECUTOR_MANAGED_THREAD_TURN:
        return "legacy_managed_thread_turn"
    return rule.executor_kind


def _typed_product_projection(
    rule: AutomationRule, schedule: Optional[AutomationSchedule]
) -> dict[str, Any]:
    schedule_editor = _schedule_editor_shape(rule, schedule)
    message = _message_projection(rule)
    action = _action_projection(rule)
    managed = _managed_projection(rule)
    runtime = _runtime_projection(rule)
    diagnostics = _product_diagnostics(rule, schedule, message)
    return {
        "product_api_version": 1,
        "execution_mode": rule.executor_kind,
        "runtime_contract": runtime,
        "direct_runtime_contract": (
            runtime["requested"]
            if rule.executor_kind == EXECUTOR_AGENT_TASK_TURN
            else None
        ),
        "coordinator_runtime_contract": (
            runtime["requested"]
            if rule.executor_kind == EXECUTOR_PMA_OPERATOR_TURN
            else None
        ),
        "worker_child_policy": (
            runtime["worker_child_policy"]
            if rule.executor_kind == EXECUTOR_PMA_OPERATOR_TURN
            else None
        ),
        "editable": _editable_projection(rule, schedule, message),
        "managed": managed,
        "managed_status": managed,
        "schedule_editor": schedule_editor,
        "trigger_summary": _trigger_summary(rule),
        "message": message,
        "message_source": message["source"],
        "message_preview": message["preview"],
        "action_preview": action,
        "target_summary": _target_summary(rule),
        "executor_summary": _executor_summary(rule, message, action),
        "policy_summary": _policy_summary(rule),
        "diagnostics": diagnostics,
        "raw_links": {
            "control_plane_rule": (
                f"/hub/api/control-plane/automations/rules/{rule.rule_id}"
            ),
            "control_plane_jobs": "/hub/api/control-plane/automations/jobs/query",
            "control_plane_schedules": (
                "/hub/api/control-plane/automations/schedules/query"
            ),
        },
    }


def _editable_projection(
    rule: AutomationRule,
    schedule: Optional[AutomationSchedule],
    message: dict[str, Any],
) -> dict[str, Any]:
    schedule_kind = schedule.schedule_kind if schedule is not None else None
    system_reason = _system_reason(rule)
    raw_editable = not rule.system_owned
    return {
        "can_enable": True,
        "can_rename": raw_editable,
        "can_edit_schedule": raw_editable
        and schedule_kind in {SCHEDULE_DAILY, SCHEDULE_WEEKLY},
        "can_edit_message": raw_editable and message["field"] == "prompt",
        "can_edit_ticket_body": raw_editable and message["field"] == "ticket_body",
        "can_run_now": True,
        "can_edit_raw": False,
        "raw_edit_blocked_reason": (
            "Raw rule edits are available through the control-plane API."
        ),
        "managed_reason": system_reason,
    }


def _managed_projection(rule: AutomationRule) -> dict[str, Any]:
    reason = _system_reason(rule)
    legacy_source = _legacy_source(rule)
    return {
        "system_owned": rule.system_owned,
        "managed": rule.system_owned or legacy_source is not None,
        "reason": reason,
        "legacy": legacy_source is not None,
        "legacy_source": legacy_source,
    }


def _system_reason(rule: AutomationRule) -> Optional[str]:
    if rule.system_owned:
        purpose = _optional_text(rule.metadata.get("purpose"))
        if purpose:
            return f"System-managed automation: {purpose}"
        return "System-managed automation"
    return None


def _legacy_source(rule: AutomationRule) -> Optional[str]:
    for key in (
        "legacy_source_table",
        "legacy_timer_id",
        "legacy_subscription_id",
        "legacy_wakeup_id",
    ):
        value = _optional_text(rule.metadata.get(key))
        if value is not None:
            return value if key == "legacy_source_table" else key
    return None


def _schedule_editor_shape(
    rule: AutomationRule, schedule: Optional[AutomationSchedule]
) -> dict[str, Any]:
    if schedule is None:
        kind = "event_driven" if rule.trigger_kind == TRIGGER_KIND_EVENT else "none"
        return {
            "kind": kind,
            "editable": False,
            "fields": {},
            "timezone": None,
            "next_fire_at": None,
            "summary": "Event driven" if kind == "event_driven" else "No schedule",
        }

    schedule_payload = dict(schedule.schedule)
    fields: dict[str, Any] = {}
    if schedule.schedule_kind in {SCHEDULE_DAILY, SCHEDULE_WEEKLY}:
        fields = {
            "timezone": schedule.timezone,
            "hour": schedule_payload.get("hour"),
            "minute": schedule_payload.get("minute"),
        }
        if schedule.schedule_kind == SCHEDULE_WEEKLY:
            fields["weekday"] = _first_weekday(schedule_payload.get("weekdays"))
    elif schedule.schedule_kind == SCHEDULE_ONE_SHOT:
        fields = {"due_at": schedule.next_fire_at}
    elif schedule.schedule_kind == SCHEDULE_INTERVAL:
        fields = {"interval_seconds": schedule_payload.get("interval_seconds")}

    return {
        "kind": schedule.schedule_kind,
        "editable": (not rule.system_owned)
        and schedule.schedule_kind in {SCHEDULE_DAILY, SCHEDULE_WEEKLY},
        "fields": fields,
        "timezone": schedule.timezone,
        "next_fire_at": schedule.next_fire_at,
        "last_fire_at": schedule.last_fire_at,
        "state": schedule.state,
        "summary": _format_schedule(schedule.to_dict()),
    }


def _trigger_summary(rule: AutomationRule) -> dict[str, Any]:
    event_types = rule.trigger.get("event_types")
    event_type_list = (
        [str(item) for item in event_types] if isinstance(event_types, list) else []
    )
    schedule_kind = _optional_text(rule.trigger.get("schedule_kind"))
    if rule.trigger_kind == TRIGGER_KIND_SCHEDULE:
        label = schedule_kind or "schedule.fire"
    elif rule.trigger_kind == TRIGGER_KIND_MANUAL:
        label = "Manual run"
    elif event_type_list:
        label = ", ".join(event_type_list[:3])
        if len(event_type_list) > 3:
            label += f" +{len(event_type_list) - 3}"
    else:
        label = rule.trigger_kind
    return {
        "kind": rule.trigger_kind,
        "label": label,
        "event_types": event_type_list,
        "filters": rule.filters,
    }


def _message_projection(rule: AutomationRule) -> dict[str, Any]:
    executor = rule.executor
    for key, field in (
        ("message_text", "prompt"),
        ("message", "prompt"),
        ("prompt", "prompt"),
        ("prompt_template", "prompt_template"),
    ):
        value = _optional_text(executor.get(key))
        if value is not None:
            return {
                "source": f"executor.{key}",
                "field": field,
                "preview": _preview(value),
                "template": "{{" in value,
                "editable": (not rule.system_owned) and field == "prompt",
            }
    ticket_body = _first_ticket_body(executor)
    if ticket_body is not None:
        return {
            "source": "executor.ticket_pack.tickets[0].content",
            "field": "ticket_body",
            "preview": _preview(ticket_body),
            "template": "{{" in ticket_body,
            "editable": not rule.system_owned,
        }
    action_message = _action_message_source(executor)
    if action_message is not None:
        return {
            "source": action_message[0],
            "field": None,
            "preview": _preview(action_message[1]),
            "template": "{{" in action_message[1],
            "editable": False,
        }
    if rule.executor_kind == EXECUTOR_PUBLISH_OPERATION:
        return {
            "source": "publish_operation.runtime",
            "field": None,
            "preview": "Message is generated by the publish operation at runtime.",
            "template": False,
            "editable": False,
        }
    return {
        "source": "none",
        "field": None,
        "preview": "",
        "template": False,
        "editable": False,
    }


def _action_projection(rule: AutomationRule) -> dict[str, Any]:
    executor = rule.executor
    actions = executor.get("actions")
    action_message = _action_message_source(executor)
    if isinstance(actions, dict):
        return {
            "source": actions.get("source") or "executor.actions",
            "kind": actions.get("kind")
            or actions.get("operation_kind")
            or rule.executor_kind,
            "preview": _preview(
                actions.get("summary")
                or (action_message[1] if action_message is not None else None)
                or actions.get("source")
                or rule.executor_kind
            ),
            "actions": actions,
        }
    if isinstance(actions, list):
        return {
            "source": "executor.actions",
            "kind": "action_list",
            "preview": f"{len(actions)} action(s)",
            "actions": actions,
        }
    operation_kind = _optional_text(executor.get("operation_kind"))
    if operation_kind is not None:
        return {
            "source": "executor.operation_kind",
            "kind": operation_kind,
            "preview": operation_kind.replace("_", " "),
            "actions": None,
        }
    return {
        "source": f"executor.{rule.executor_kind}",
        "kind": rule.executor_kind,
        "preview": _executor_label(rule.executor_kind),
        "actions": None,
    }


def _target_summary(rule: AutomationRule) -> dict[str, Any]:
    target = dict(rule.target)
    repo_id = (
        _optional_text(target.get("repo_id"))
        or _optional_text(target.get("base_repo_id"))
        or _optional_text(rule.metadata.get("repo_id"))
    )
    thread_id = _optional_text(target.get("thread_id")) or _optional_text(
        target.get("thread_target_id")
    )
    label_parts = [rule.target_policy]
    if repo_id is not None:
        label_parts.append(repo_id)
    if thread_id is not None:
        label_parts.append(thread_id)
    return {
        "policy": rule.target_policy,
        "repo_id": repo_id,
        "thread_id": thread_id,
        "worktree_id": _optional_text(target.get("worktree_id")),
        "label": " / ".join(label_parts),
    }


def _executor_summary(
    rule: AutomationRule, message: dict[str, Any], action: dict[str, Any]
) -> dict[str, Any]:
    executor = dict(rule.executor)
    return {
        "kind": rule.executor_kind,
        "execution_mode": rule.executor_kind,
        "label": _executor_label(rule.executor_kind),
        "agent": _optional_text(executor.get("agent")),
        "model": _optional_text(executor.get("model")),
        "reasoning": _optional_text(executor.get("reasoning")),
        "profile": _optional_text(executor.get("profile"))
        or _optional_text(executor.get("agent_profile")),
        "lane_id": _optional_text(executor.get("lane_id")),
        "message_source": message["source"],
        "action_kind": action["kind"],
    }


def _runtime_projection(rule: AutomationRule) -> dict[str, Any]:
    executor = dict(rule.executor)
    requested = _requested_runtime_from_executor(executor)
    return {
        "mode": rule.executor_kind,
        "requested": requested,
        "actual": (
            executor.get("actual_runtime")
            if isinstance(executor.get("actual_runtime"), dict)
            else None
        ),
        "coordinator_authoritative": (
            bool(executor.get("coordinator_authoritative", True))
            if rule.executor_kind == EXECUTOR_PMA_OPERATOR_TURN
            else None
        ),
        "worker_child_policy": (
            executor.get("worker_child_policy")
            if isinstance(executor.get("worker_child_policy"), dict)
            else None
        ),
    }


def _policy_summary(rule: AutomationRule) -> dict[str, Any]:
    policy = dict(rule.policy)
    return {
        "approval_mode": policy.get("approval_mode"),
        "max_attempts": policy.get("max_attempts"),
        "max_concurrent_per_rule": policy.get("max_concurrent_per_rule"),
        "max_concurrent_per_target": policy.get("max_concurrent_per_target"),
        "dedupe_key": policy.get("dedupe_key"),
    }


def _product_diagnostics(
    rule: AutomationRule,
    schedule: Optional[AutomationSchedule],
    message: dict[str, Any],
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if _legacy_source(rule) is not None:
        diagnostics.append(
            {
                "code": "AUTOMATION_LEGACY_MIGRATED",
                "severity": "info",
                "message": "This automation was migrated from legacy PMA state.",
            }
        )
    if message["source"] == "none":
        diagnostics.append(
            {
                "code": "AUTOMATION_MESSAGE_NOT_DECLARED",
                "severity": "warning",
                "message": "No product-visible message source is declared.",
            }
        )
    if schedule is not None and schedule.schedule_kind == SCHEDULE_ONE_SHOT:
        diagnostics.append(
            {
                "code": "AUTOMATION_ONE_SHOT_SCHEDULE",
                "severity": "info",
                "message": "One-shot schedules use next_fire_at instead of recurring time fields.",
            }
        )
    return diagnostics


def _executor_label(executor_kind: str) -> str:
    return {
        EXECUTOR_AGENT_TASK_TURN: "Agent task turn",
        EXECUTOR_PMA_OPERATOR_TURN: "PMA operator turn",
        EXECUTOR_TICKET_FLOW: "Ticket flow",
        EXECUTOR_MANAGED_THREAD_TURN: "Legacy managed thread turn",
        EXECUTOR_PUBLISH_OPERATION: "Publish operation",
        EXECUTOR_PUBLISH_CHAT_NOTIFICATION: "Chat notification",
        EXECUTOR_GITHUB_REACTION: "GitHub reaction",
        EXECUTOR_GITHUB_COMMENT: "GitHub comment",
    }.get(executor_kind, executor_kind)


def _first_weekday(value: Any) -> Optional[int]:
    if isinstance(value, list) and value:
        try:
            return int(value[0])
        except (TypeError, ValueError):
            return None
    return None


def _first_ticket_body(executor: dict[str, Any]) -> Optional[str]:
    ticket_pack = executor.get("ticket_pack") or executor.get("pack")
    if not isinstance(ticket_pack, dict):
        return None
    tickets = ticket_pack.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        return None
    first = tickets[0]
    if not isinstance(first, dict):
        return None
    return _optional_text(first.get("content") or first.get("body"))


def _action_message_source(executor: dict[str, Any]) -> Optional[tuple[str, str]]:
    actions = executor.get("actions")
    candidates: list[tuple[str, Any]] = []
    if isinstance(actions, dict):
        candidates.extend(
            [
                ("executor.actions.message.text", _nested(actions, "message", "text")),
                (
                    "executor.actions.message.template",
                    _nested(actions, "message", "template"),
                ),
                ("executor.actions.message", actions.get("message")),
                ("executor.actions.summary", actions.get("summary")),
            ]
        )
    elif isinstance(actions, list):
        for index, action in enumerate(actions):
            if isinstance(action, dict):
                candidates.extend(
                    [
                        (
                            f"executor.actions[{index}].message.text",
                            _nested(action, "message", "text"),
                        ),
                        (
                            f"executor.actions[{index}].message.template",
                            _nested(action, "message", "template"),
                        ),
                        (f"executor.actions[{index}].message", action.get("message")),
                        (f"executor.actions[{index}].summary", action.get("summary")),
                    ]
                )
    for source, value in candidates:
        text = _optional_text(value)
        if text is not None:
            return source, text
    return None


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _preview(value: Any, *, limit: int = 180) -> str:
    text = _optional_text(value)
    if text is None:
        return ""
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3].rstrip()}..."


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
        f"Execution mode: {row.get('execution_mode') or row.get('executor_kind') or 'unknown'}",
        f"Runtime: {_format_runtime(row)}",
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
        f"  execution mode: {row.get('execution_mode') or row.get('executor_kind') or 'unknown'}\n"
        f"  runtime: {_format_runtime(row)}\n"
        f"  schedule: {_format_schedule(row.get('schedule'))}\n"
        f"  last job: {_format_job(row.get('last_job'))}"
    )


def _format_runtime(row: dict[str, Any]) -> str:
    mode = row.get("execution_mode") or row.get("executor_kind")
    if mode == EXECUTOR_AGENT_TASK_TURN:
        contract = row.get("direct_runtime_contract")
        label = "direct"
    elif mode == EXECUTOR_PMA_OPERATOR_TURN:
        contract = row.get("coordinator_runtime_contract")
        label = "coordinator"
    else:
        contract = (
            row.get("runtime_contract", {}).get("requested")
            if isinstance(row.get("runtime_contract"), dict)
            else None
        )
        label = "runtime"
    if not isinstance(contract, dict) or not contract:
        return "none"
    agent = contract.get("agent") or "unspecified-agent"
    model = contract.get("model")
    profile = contract.get("profile")
    parts = [f"{label} {agent}"]
    if model:
        parts.append(str(model))
    if profile:
        parts.append(f"profile={profile}")
    return " / ".join(parts)


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
    state = job.get("effective_state") or job.get("state") or "unknown"
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
    descriptor = _preset_descriptor("security_scan_pr")
    execution_mode = _resolve_preset_execution_mode(
        payload.execution_mode,
        default=EXECUTOR_AGENT_TASK_TURN,
        allowed={EXECUTOR_AGENT_TASK_TURN, EXECUTOR_PMA_OPERATOR_TURN},
    )
    repo_id = _required_text(payload.repo_id, "repo_id")
    name = _optional_text(payload.name) or f"Daily security scan for {repo_id}"
    prompt = _optional_text(payload.prompt) or _render_preset_template(
        descriptor.prompt_template, repo_id=repo_id
    )
    rule_id = _rule_id(name)
    schedule = _daily_schedule(
        rule_id=rule_id,
        timezone_name=_preset_timezone(payload, descriptor),
        hour=_preset_int(payload.hour, descriptor.default_hour),
        minute=_preset_int(payload.minute, descriptor.default_minute),
    )
    policy = {
        **descriptor.policy,
        "dedupe_key": f"{rule_id}:{{{{ schedule.next_fire_at }}}}",
    }
    rule = AutomationRule.create(
        rule_id=rule_id,
        name=name,
        trigger_kind=TRIGGER_KIND_SCHEDULE,
        trigger={"event_types": ["schedule.fire"]},
        filters={"schedule.rule_id": rule_id},
        target_policy=TARGET_POLICY_HUB,
        target={"repo_id": repo_id},
        executor_kind=execution_mode,
        executor=_executor_for_product_mode(
            execution_mode,
            base={"message_text": prompt},
            payload=payload,
        ),
        enabled=payload.enabled,
        policy=policy,
        metadata={
            "kind": AUTOMATION_METADATA_KIND,
            "automation_kind": descriptor.automation_kind,
            "preset": descriptor.id,
            "repo_id": repo_id,
            "description": descriptor.description,
        },
    )
    return rule, schedule


def _build_weekly_ticket_flow(
    payload: AutomationPresetRequest,
) -> tuple[AutomationRule, AutomationSchedule]:
    descriptor = _preset_descriptor("weekly_ticket_flow")
    _resolve_preset_execution_mode(
        payload.execution_mode,
        default=EXECUTOR_TICKET_FLOW,
        allowed={EXECUTOR_TICKET_FLOW},
    )
    repo_id = _required_text(payload.repo_id, "repo_id")
    name = _optional_text(payload.name) or f"Weekly ticket flow for {repo_id}"
    rule_id = _rule_id(name)
    schedule = _weekly_schedule(
        rule_id=rule_id,
        timezone_name=_preset_timezone(payload, descriptor),
        hour=_preset_int(payload.hour, descriptor.default_hour),
        minute=_preset_int(payload.minute, descriptor.default_minute),
        weekday=_preset_int(payload.weekday, descriptor.default_weekday or 0),
    )
    ticket_id = f"tkt_{uuid.uuid4().hex}"
    default_ticket_body = _render_preset_template(
        descriptor.ticket_body_template or "",
        repo_id=repo_id,
        ticket_id=ticket_id,
    )
    ticket_body = _optional_text(payload.ticket_body) or default_ticket_body
    policy = {
        **descriptor.policy,
        "dedupe_key": f"{rule_id}:{{{{ schedule.next_fire_at }}}}",
    }
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
        policy=policy,
        metadata={
            "kind": AUTOMATION_METADATA_KIND,
            "automation_kind": descriptor.automation_kind,
            "preset": descriptor.id,
            "repo_id": repo_id,
            "description": descriptor.description,
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


def _preset_descriptor(preset: str) -> AutomationPresetDescriptor:
    preset_id = (preset or "").strip().lower()
    descriptor = AUTOMATION_PRESET_DESCRIPTORS.get(preset_id)
    if descriptor is None:
        choices = " or ".join(AUTOMATION_PRESET_DESCRIPTORS)
        raise ValueError(f"preset must be {choices}")
    return descriptor


def _preset_timezone(
    payload: AutomationPresetRequest, descriptor: AutomationPresetDescriptor
) -> str:
    return _optional_text(payload.timezone) or descriptor.default_timezone


def _preset_int(value: Optional[int], fallback: int) -> int:
    return fallback if value is None else int(value)


def _render_preset_template(template: str, **values: str) -> str:
    return template.format(**values)


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


def _executor_for_product_mode(
    execution_mode: str,
    *,
    base: dict[str, Any],
    payload: AutomationPresetRequest,
) -> dict[str, Any]:
    executor = _executor_with_agent_options(base, payload)
    if payload.worker_child_policy is not None:
        executor["worker_child_policy"] = dict(payload.worker_child_policy)
    _materialize_product_runtime_contract(
        executor_kind=execution_mode,
        executor=executor,
        agent=payload.agent,
        model=payload.model,
        reasoning=payload.reasoning,
        profile=payload.profile,
    )
    _validate_product_executor_mode(execution_mode, executor)
    return executor


def _resolve_preset_execution_mode(
    value: Optional[str],
    *,
    default: str,
    allowed: set[str],
) -> str:
    mode = _optional_text(value) or default
    if mode not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"execution_mode must be one of: {choices}")
    return mode


def _materialize_product_runtime_contract(
    *,
    executor_kind: str,
    executor: dict[str, Any],
    agent: Optional[str],
    model: Optional[str],
    reasoning: Optional[str],
    profile: Optional[str],
) -> None:
    if executor_kind not in {EXECUTOR_AGENT_TASK_TURN, EXECUTOR_PMA_OPERATOR_TURN}:
        return
    runtime = executor.get("requested_runtime")
    requested = dict(runtime) if isinstance(runtime, dict) else {}
    for key, value in (
        ("agent", agent or executor.get("agent")),
        ("model", model or executor.get("model")),
        ("reasoning", reasoning or executor.get("reasoning")),
        (
            "profile",
            profile or executor.get("profile") or executor.get("agent_profile"),
        ),
    ):
        text = _optional_text(value)
        if text is not None:
            requested[key] = text
    for key in ("approval_policy", "sandbox_policy"):
        text = _optional_text(executor.get(key))
        if text is not None:
            requested[key] = text
    executor["requested_runtime"] = requested
    for key in ("agent", "model", "reasoning", "profile"):
        text = _optional_text(requested.get(key))
        if text is not None:
            executor[key] = text
    profile_text = _optional_text(requested.get("profile"))
    if profile_text is not None:
        executor["agent_profile"] = profile_text


def _requested_runtime_from_executor(executor: dict[str, Any]) -> dict[str, Any]:
    runtime = executor.get("requested_runtime")
    requested = dict(runtime) if isinstance(runtime, dict) else {}
    for key, executor_keys in (
        ("agent", ("agent",)),
        ("model", ("model",)),
        ("reasoning", ("reasoning",)),
        ("profile", ("profile", "agent_profile")),
        ("approval_policy", ("approval_policy",)),
        ("sandbox_policy", ("sandbox_policy",)),
    ):
        if _optional_text(requested.get(key)) is not None:
            continue
        for executor_key in executor_keys:
            text = _optional_text(executor.get(executor_key))
            if text is not None:
                requested[key] = text
                break
    return requested


def _validate_product_executor_mode(
    executor_kind: str, executor: dict[str, Any]
) -> None:
    if executor_kind in {EXECUTOR_MANAGED_THREAD_TURN, "pma_turn"}:
        raise ValueError(
            "legacy automation execution modes are not accepted by the product API; "
            "use agent_task_turn, pma_operator_turn, or ticket_flow"
        )
    worker_policy = executor.get("worker_child_policy")
    if worker_policy is not None and executor_kind != EXECUTOR_PMA_OPERATOR_TURN:
        raise ValueError("worker_child_policy is only valid for pma_operator_turn")
    if executor_kind not in {EXECUTOR_AGENT_TASK_TURN, EXECUTOR_PMA_OPERATOR_TURN}:
        return
    requested = _requested_runtime_from_executor(executor)
    agent = _optional_text(requested.get("agent"))
    model = _optional_text(requested.get("model"))
    if agent is None:
        raise ValueError(f"{executor_kind} requires requested_runtime.agent")
    if model is not None and "/" in model and agent != "opencode":
        raise ValueError(
            "OpenCode provider/model values require requested_runtime.agent=opencode"
        )
    if agent == "opencode" and model is not None and "/" not in model:
        raise ValueError("OpenCode model must be in provider/model format")


__all__ = [
    "AUTOMATION_PRESET_DESCRIPTORS",
    "AutomationPresetDescriptor",
    "AutomationPresetRequest",
    "AutomationUpdateRequest",
    "automation_detail",
    "automation_overview",
    "automation_presets",
    "automation_row",
    "automation_store",
    "create_preset_automation",
    "format_automation_list",
    "format_automation_status",
    "run_automation_now",
    "set_automation_enabled",
    "update_automation",
]
