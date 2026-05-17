from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .engine import AutomationRuleEngine
from .models import (
    SCHEDULE_DAILY,
    SCHEDULE_INTERVAL,
    SCHEDULE_ONE_SHOT,
    SCHEDULE_WEEKLY,
    AutomationEvent,
    AutomationSchedule,
    normalize_timestamp,
)
from .store import AutomationStore


@dataclass(frozen=True)
class SchedulerProcessResult:
    schedules_fired: int = 0
    events_created: int = 0
    jobs_created: int = 0
    jobs_deduped: int = 0


class AutomationScheduler:
    def __init__(self, store: AutomationStore, engine: AutomationRuleEngine) -> None:
        self._store = store
        self._engine = engine

    def process_due(
        self, *, now: Optional[str] = None, limit: int = 100
    ) -> SchedulerProcessResult:
        stamp = normalize_timestamp(now)
        due = self._store.list_due_schedules(now=stamp, limit=limit)
        fired = 0
        events = 0
        jobs = 0
        deduped = 0
        for schedule in due:
            if schedule.next_fire_at is None:
                continue
            event = AutomationEvent.create(
                event_id=f"schedule.fire:{schedule.schedule_id}:{schedule.next_fire_at}",
                event_type="schedule.fire",
                source="automation_scheduler",
                target={
                    "schedule_id": schedule.schedule_id,
                    "rule_id": schedule.rule_id,
                },
                payload={"schedule": schedule.to_dict()},
                raw_payload={"schedule": schedule.to_dict()},
                metadata={"schedule_id": schedule.schedule_id},
                observed_at=schedule.next_fire_at,
            )
            result = self._engine.record_event_and_enqueue_jobs(event)
            next_fire_at = calculate_next_fire_at(schedule, now=stamp)
            self._store.update_schedule_fire(
                schedule.schedule_id,
                last_fire_at=schedule.next_fire_at,
                next_fire_at=next_fire_at,
                state="completed" if next_fire_at is None else "active",
                now=stamp,
            )
            fired += 1
            events += 1
            jobs += result.jobs_created
            deduped += result.jobs_deduped
        return SchedulerProcessResult(
            schedules_fired=fired,
            events_created=events,
            jobs_created=jobs,
            jobs_deduped=deduped,
        )


def calculate_next_fire_at(
    schedule: AutomationSchedule, *, now: Optional[str] = None
) -> Optional[str]:
    if schedule.schedule_kind == SCHEDULE_ONE_SHOT:
        return None
    now_dt = _parse(now)
    current = _parse(schedule.next_fire_at or now)
    if current is None:
        current = _parse(now)
    if current is None:
        current = now_dt or datetime.now(timezone.utc)
    advance_to_future = (
        str(schedule.misfire_policy or "fire_once").strip()
        in {
            "fire_once",
            "fire_once_then_advance_to_future",
            "skip_missed",
        }
        and now_dt is not None
    )
    advance_until = now_dt if advance_to_future else None
    schedule_json = schedule.schedule if isinstance(schedule.schedule, dict) else {}
    if schedule.schedule_kind == SCHEDULE_INTERVAL:
        seconds = _positive_int(schedule_json.get("interval_seconds"), fallback=60)
        candidate = current + timedelta(seconds=seconds)
        if advance_until is not None:
            while candidate <= advance_until:
                candidate = candidate + timedelta(seconds=seconds)
        return _iso(candidate)

    tz = _zone(schedule.timezone)
    local = current.astimezone(tz)
    hour = _bounded_int(schedule_json.get("hour"), fallback=local.hour, low=0, high=23)
    minute = _bounded_int(
        schedule_json.get("minute"), fallback=local.minute, low=0, high=59
    )
    second = _bounded_int(schedule_json.get("second"), fallback=0, low=0, high=59)

    if schedule.schedule_kind == SCHEDULE_DAILY:
        candidate = (local + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=second, microsecond=0
        )
        if advance_until is not None:
            while candidate.astimezone(timezone.utc) <= advance_until:
                candidate = candidate + timedelta(days=1)
        return _iso(candidate.astimezone(timezone.utc))

    if schedule.schedule_kind == SCHEDULE_WEEKLY:
        weekdays = schedule_json.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays:
            weekdays = [local.weekday()]
        normalized = sorted(
            {
                _bounded_int(item, fallback=local.weekday(), low=0, high=6)
                for item in weekdays
            }
        )
        for days_ahead in range(1, 8):
            candidate_day = local + timedelta(days=days_ahead)
            if candidate_day.weekday() not in normalized:
                continue
            candidate = candidate_day.replace(
                hour=hour, minute=minute, second=second, microsecond=0
            )
            if advance_until is not None:
                while candidate.astimezone(timezone.utc) <= advance_until:
                    candidate = candidate + timedelta(days=7)
            return _iso(candidate.astimezone(timezone.utc))
    return None


def _parse(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(
            normalize_timestamp(value).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except Exception:
        return ZoneInfo("UTC")


def _positive_int(value: Any, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _bounded_int(value: Any, *, fallback: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(high, max(low, parsed))


__all__ = ["AutomationScheduler", "SchedulerProcessResult", "calculate_next_fire_at"]
