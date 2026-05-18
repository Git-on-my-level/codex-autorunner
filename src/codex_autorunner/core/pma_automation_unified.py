from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from .automation.builtins import (
    _LIFECYCLE_EVENT_MAP,
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
    _normalize_reactive_event_types,
)
from .automation.models import (
    EXECUTOR_PMA_TURN,
    JOB_PENDING,
    JOB_SUCCEEDED,
    SCHEDULE_ONE_SHOT,
    TARGET_POLICY_HUB,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
    AutomationEvent,
    AutomationJob,
    AutomationRule,
    AutomationSchedule,
)
from .orchestration.sqlite import open_orchestration_sqlite
from .sqlite_utils import table_exists
from .text_utils import _json_loads_object

if TYPE_CHECKING:
    from .automation.store import AutomationStore


@dataclass(frozen=True)
class PmaUnifiedMirrorResult:
    operation: str
    identifier: str
    ok: bool
    owner: str = "pma_automation"
    scope: str = "unified_automation"
    rule_id: Optional[str] = None
    schedule_id: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


class PmaUnifiedAutomationAdapter:
    def __init__(self, store: "AutomationStore") -> None:
        self._store = store

    def mirror_subscription_rule(self, *, subscription: Any) -> AutomationRule:
        return self._store.upsert_rule(self.subscription_rule(subscription))

    def mirror_timer_schedule(
        self, *, timer: Any
    ) -> tuple[AutomationRule, AutomationSchedule]:
        rule = self._store.upsert_rule(self.timer_rule(timer))
        schedule = self._store.upsert_schedule(self.timer_schedule(timer))
        return rule, schedule

    def subscription_rule(self, subscription: Any) -> AutomationRule:
        subscription_id = str(
            getattr(subscription, "subscription_id", "") or ""
        ).strip()
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
        return AutomationRule.create(
            rule_id=rule_id,
            name=f"PMA subscription {subscription_id}",
            enabled=str(getattr(subscription, "state", "active") or "active")
            == "active",
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
                "legacy_idempotency_key": getattr(
                    subscription, "idempotency_key", None
                ),
                "legacy_reason": getattr(subscription, "reason", None),
                "legacy_max_matches": getattr(subscription, "max_matches", None),
                "legacy_match_count": getattr(subscription, "match_count", 0),
                "legacy_metadata": dict(getattr(subscription, "metadata", None) or {}),
            },
            created_at=getattr(subscription, "created_at", None),
            updated_at=getattr(subscription, "updated_at", None),
        )

    def timer_rule(self, timer: Any) -> AutomationRule:
        timer_id = str(getattr(timer, "timer_id", "") or "").strip()
        if not timer_id:
            raise ValueError("timer_id is required")
        rule_id = f"{PMA_TIMER_RULE_PREFIX}{timer_id}"
        return AutomationRule.create(
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

    def timer_schedule(self, timer: Any) -> AutomationSchedule:
        timer_id = str(getattr(timer, "timer_id", "") or "").strip()
        if not timer_id:
            raise ValueError("timer_id is required")
        state = str(getattr(timer, "state", "pending") or "pending")
        return AutomationSchedule.create(
            schedule_id=f"{PMA_TIMER_SCHEDULE_PREFIX}{timer_id}",
            rule_id=f"{PMA_TIMER_RULE_PREFIX}{timer_id}",
            schedule_kind=SCHEDULE_ONE_SHOT,
            next_fire_at=getattr(timer, "due_at", None) if state == "pending" else None,
            last_fire_at=getattr(timer, "fired_at", None),
            schedule={
                "legacy_timer_id": timer_id,
                "timer_kind": getattr(timer, "timer_type", None),
                "payload": _timer_payload(timer),
            },
            state="active" if state == "pending" else state,
            created_at=getattr(timer, "created_at", None),
            updated_at=getattr(timer, "updated_at", None),
        )

    def backfill_legacy_rows(self) -> dict[str, int]:
        return PmaUnifiedAutomationBackfill(self._store).run()


class PmaUnifiedAutomationBackfill:
    def __init__(self, store: "AutomationStore") -> None:
        self._store = store

    def run(self) -> dict[str, int]:
        with open_orchestration_sqlite(
            self._store._hub_root, durable=self._store._durable
        ) as conn:
            if not all(
                table_exists(conn, table)
                for table in (
                    "orch_automation_subscriptions",
                    "orch_automation_timers",
                    "orch_automation_wakeups",
                )
            ):
                return {"rules": 0, "events": 0, "jobs": 0, "schedules": 0}

            subscriptions = conn.execute(
                "SELECT * FROM orch_automation_subscriptions ORDER BY created_at ASC"
            ).fetchall()
            timers = conn.execute(
                "SELECT * FROM orch_automation_timers ORDER BY created_at ASC"
            ).fetchall()
            wakeups = conn.execute(
                "SELECT * FROM orch_automation_wakeups ORDER BY created_at ASC"
            ).fetchall()

        counts = {"rules": 0, "events": 0, "jobs": 0, "schedules": 0}
        subscription_rules: dict[str, str] = {}
        for row in subscriptions:
            rule = self.subscription_backfill_rule(row)
            counts["rules"] += int(self._upsert_rule_created(rule))
            subscription_rules[str(row["subscription_id"])] = rule.rule_id

        for row in timers:
            subscription_id = _optional_text(row["subscription_id"])
            rule_id = (
                subscription_rules.get(subscription_id)
                if subscription_id is not None
                else None
            ) or f"legacy-pma-timer:{row['timer_id']}"
            if self._store.get_rule(rule_id) is None:
                counts["rules"] += int(
                    self._upsert_rule_created(self.timer_backfill_rule(row, rule_id))
                )
            schedule = self.timer_backfill_schedule(row, rule_id)
            if self._store.get_schedule(schedule.schedule_id) is None:
                counts["schedules"] += 1
            self._store.upsert_schedule(schedule)

        for row in wakeups:
            event = self.wakeup_backfill_event(row)
            if self._store.get_event(event.event_id) is None:
                counts["events"] += 1
            self._store.record_event(event)
            subscription_id = _optional_text(row["subscription_id"])
            rule_id = (
                subscription_rules.get(subscription_id)
                if subscription_id is not None
                else None
            ) or f"legacy-pma-wakeup:{row['wakeup_id']}"
            if self._store.get_rule(rule_id) is None:
                counts["rules"] += int(
                    self._upsert_rule_created(
                        self.wakeup_backfill_rule(row, rule_id, event)
                    )
                )
            job, deduped = self._store.enqueue_job(
                self.wakeup_backfill_job(row, rule_id, event)
            )
            _ = job
            counts["jobs"] += int(not deduped)

        return counts

    def subscription_backfill_rule(self, row: sqlite3.Row) -> AutomationRule:
        return AutomationRule.create(
            rule_id=f"legacy-pma-subscription:{row['subscription_id']}",
            name=f"Legacy PMA subscription {row['subscription_id']}",
            enabled=str(row["state"] or "active") == "active",
            system_owned=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": _load_json_array(row["event_types_json"])},
            filters={
                "repo_id": row["repo_id"],
                "run_id": row["run_id"],
                "thread_target_id": row["thread_target_id"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
            },
            target_policy=TARGET_POLICY_HUB,
            target={
                "repo_id": row["repo_id"],
                "thread_target_id": row["thread_target_id"],
            },
            executor_kind=EXECUTOR_PMA_TURN,
            executor={"lane_id": row["lane_id"]},
            policy={
                "max_attempts": 3,
                "approval_mode": "pause_and_request_user",
                "max_concurrent_per_rule": 1,
                "max_concurrent_per_target": 1,
            },
            metadata={
                "legacy_source": "orch_automation_subscriptions",
                "legacy_subscription_id": row["subscription_id"],
                "legacy_metadata": _json_object_from_row(row, "metadata_json"),
            },
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def timer_backfill_rule(self, row: sqlite3.Row, rule_id: str) -> AutomationRule:
        return AutomationRule.create(
            rule_id=rule_id,
            name=f"Legacy PMA timer {row['timer_id']}",
            enabled=str(row["state"] or "pending") == "pending",
            system_owned=True,
            trigger_kind=TRIGGER_KIND_SCHEDULE,
            trigger={"schedule_kind": SCHEDULE_ONE_SHOT},
            target_policy=TARGET_POLICY_HUB,
            target={
                "repo_id": row["repo_id"],
                "thread_target_id": row["thread_target_id"],
            },
            executor_kind=EXECUTOR_PMA_TURN,
            executor={"source": "legacy_timer"},
            metadata={
                "legacy_source": "orch_automation_timers",
                "legacy_timer_id": row["timer_id"],
            },
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def timer_backfill_schedule(
        self, row: sqlite3.Row, rule_id: str
    ) -> AutomationSchedule:
        return AutomationSchedule.create(
            schedule_id=f"legacy-pma-timer:{row['timer_id']}",
            rule_id=rule_id,
            schedule_kind=SCHEDULE_ONE_SHOT,
            next_fire_at=row["available_at"],
            last_fire_at=row["fired_at"],
            schedule={
                "legacy_timer_id": row["timer_id"],
                "timer_kind": row["timer_kind"],
                "payload": _json_object_from_row(row, "payload_json"),
            },
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def wakeup_backfill_event(self, row: sqlite3.Row) -> AutomationEvent:
        payload = _json_object_from_row(row, "payload_json")
        wake_up_payload = _legacy_wakeup_payload_from_row(row, payload)
        payload = {**payload, "wake_up": wake_up_payload}
        event_type = "manual.run"
        legacy_event_type = _optional_text(row["event_type"])
        if legacy_event_type in _LIFECYCLE_EVENT_MAP:
            event_type = _LIFECYCLE_EVENT_MAP[legacy_event_type]
        return AutomationEvent.create(
            event_id=f"legacy-pma-wakeup:{row['wakeup_id']}",
            event_type=event_type,
            source="legacy_pma_wakeup",
            repo_id=row["repo_id"],
            target={
                "thread_target_id": row["thread_target_id"],
                "run_id": row["run_id"],
            },
            payload=payload,
            raw_payload=payload,
            metadata={
                "legacy_source": "orch_automation_wakeups",
                "legacy_wakeup_id": row["wakeup_id"],
                "legacy_event_id": row["event_id"],
            },
            observed_at=row["created_at"],
        )

    def wakeup_backfill_rule(
        self, row: sqlite3.Row, rule_id: str, event: AutomationEvent
    ) -> AutomationRule:
        return AutomationRule.create(
            rule_id=rule_id,
            name=f"Legacy PMA wakeup {row['wakeup_id']}",
            enabled=True,
            system_owned=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={"event_types": [event.event_type]},
            target_policy=TARGET_POLICY_HUB,
            target={
                "repo_id": row["repo_id"],
                "thread_target_id": row["thread_target_id"],
            },
            executor_kind=EXECUTOR_PMA_TURN,
            executor={
                "lane_id": row["lane_id"],
                "wake_up_kind": "pma_legacy_wakeup",
            },
            metadata={
                "legacy_source": "orch_automation_wakeups",
                "legacy_wakeup_id": row["wakeup_id"],
            },
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def wakeup_backfill_job(
        self, row: sqlite3.Row, rule_id: str, event: AutomationEvent
    ) -> AutomationJob:
        payload = dict(event.payload)
        job = AutomationJob.create(
            job_id=f"legacy-pma-wakeup:{row['wakeup_id']}",
            rule_id=rule_id,
            event_id=event.event_id,
            dedupe_key=f"legacy-pma-wakeup:{row['wakeup_id']}",
            target={
                "repo_id": row["repo_id"],
                "thread_target_id": row["thread_target_id"],
            },
            executor={
                "kind": EXECUTOR_PMA_TURN,
                "lane_id": row["lane_id"],
                "wake_up_kind": "pma_legacy_wakeup",
            },
            policy={
                "max_attempts": 3,
                "retry_backoff_seconds": 0,
                "retry_backoff_max_seconds": 0,
                "max_concurrent_per_rule": 1,
                "max_concurrent_per_target": 1,
            },
            payload=payload,
            created_at=row["created_at"],
        )
        job.state = JOB_SUCCEEDED if row["completed_at"] else JOB_PENDING
        job.pma_lane_id = row["lane_id"]
        job.result_summary = row["reason_text"]
        return job

    def _upsert_rule_created(self, rule: AutomationRule) -> bool:
        created = self._store.get_rule(rule.rule_id) is None
        self._store.upsert_rule(rule)
        return created


def _json_object_from_row(row: sqlite3.Row, column: str) -> dict[str, Any]:
    return _json_loads_object(row[column])


def _load_json_array(value: Any) -> list[Any]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _optional_text(value: Any) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


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


def _legacy_wakeup_payload_from_row(
    row: sqlite3.Row, payload: dict[str, Any]
) -> dict[str, Any]:
    metadata = payload.get("metadata")
    event_data = payload.get("event_data")
    wake_up = {
        "wakeup_id": row["wakeup_id"],
        "repo_id": row["repo_id"],
        "run_id": row["run_id"],
        "thread_id": row["thread_target_id"],
        "lane_id": row["lane_id"] or "pma:default",
        "from_state": payload.get("from_state"),
        "to_state": payload.get("to_state"),
        "reason": row["reason_text"],
        "timestamp": row["timestamp"] or row["available_at"],
        "source": row["wakeup_kind"],
        "event_type": row["event_type"],
        "subscription_id": row["subscription_id"],
        "timer_id": row["timer_id"],
    }
    if isinstance(metadata, dict):
        wake_up["metadata"] = dict(metadata)
        delivery_target = metadata.get("delivery_target")
        if isinstance(delivery_target, dict):
            wake_up["delivery_target"] = dict(delivery_target)
    if isinstance(event_data, dict):
        wake_up["event_data"] = dict(event_data)
    return {
        key: value
        for key, value in wake_up.items()
        if value is not None and value != ""
    }


__all__ = [
    "PmaUnifiedAutomationAdapter",
    "PmaUnifiedAutomationBackfill",
    "PmaUnifiedMirrorResult",
]
