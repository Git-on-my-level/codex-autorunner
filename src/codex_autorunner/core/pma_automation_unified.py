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
    AutomationJobAttempt,
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


@dataclass(frozen=True)
class PmaLegacyAutomationMigrationDiagnostic:
    code: str
    table: str
    legacy_id: str
    message: str
    next_step: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "table": self.table,
            "legacy_id": self.legacy_id,
            "message": self.message,
            "next_step": self.next_step,
        }


@dataclass(frozen=True)
class PmaLegacyAutomationMigrationResult:
    rules: int = 0
    events: int = 0
    jobs: int = 0
    schedules: int = 0
    attempts: int = 0
    diagnostics: tuple[PmaLegacyAutomationMigrationDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": self.rules,
            "events": self.events,
            "jobs": self.jobs,
            "schedules": self.schedules,
            "attempts": self.attempts,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


class PmaLegacyAutomationMigrationError(RuntimeError):
    def __init__(
        self, diagnostics: list[PmaLegacyAutomationMigrationDiagnostic]
    ) -> None:
        self.diagnostics = tuple(diagnostics)
        summary = "; ".join(
            f"{item.code}({item.table}:{item.legacy_id})"
            for item in diagnostics[:5]
        )
        if len(diagnostics) > 5:
            summary += f"; +{len(diagnostics) - 5} more"
        super().__init__(f"PMA legacy automation migration blocked: {summary}")


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

    def migrate_legacy_rows(self) -> PmaLegacyAutomationMigrationResult:
        return PmaLegacyAutomationMigration(self._store).run()


class PmaLegacyAutomationMigration:
    def __init__(self, store: "AutomationStore") -> None:
        self._store = store

    def run(self) -> PmaLegacyAutomationMigrationResult:
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
                return PmaLegacyAutomationMigrationResult()

            subscriptions = conn.execute(
                "SELECT * FROM orch_automation_subscriptions ORDER BY created_at ASC"
            ).fetchall()
            timers = conn.execute(
                "SELECT * FROM orch_automation_timers ORDER BY created_at ASC"
            ).fetchall()
            wakeups = conn.execute(
                "SELECT * FROM orch_automation_wakeups ORDER BY created_at ASC"
            ).fetchall()

        diagnostics = self._validate_rows(subscriptions, timers, wakeups)
        if diagnostics:
            raise PmaLegacyAutomationMigrationError(diagnostics)

        counts = {"rules": 0, "events": 0, "jobs": 0, "schedules": 0, "attempts": 0}
        subscription_rules: dict[str, str] = {}
        for row in subscriptions:
            rule = self.subscription_migration_rule(row)
            counts["rules"] += int(self._upsert_rule_created(rule))
            subscription_rules[str(row["subscription_id"])] = rule.rule_id

        for row in timers:
            rule_id = f"{PMA_TIMER_RULE_PREFIX}{row['timer_id']}"
            if self._store.get_rule(rule_id) is None:
                counts["rules"] += int(
                    self._upsert_rule_created(self.timer_migration_rule(row, rule_id))
                )
            schedule = self.timer_migration_schedule(row, rule_id)
            if self._store.get_schedule(schedule.schedule_id) is None:
                counts["schedules"] += 1
            self._store.upsert_schedule(schedule)

        for row in wakeups:
            event = self.wakeup_migration_event(row)
            if self._store.get_event(event.event_id) is None:
                counts["events"] += 1
            self._store.record_event(event)
            subscription_id = _optional_text(row["subscription_id"])
            rule_id = (
                subscription_rules.get(subscription_id)
                if subscription_id is not None
                else None
            ) or f"pma-wakeup:{row['wakeup_id']}"
            if self._store.get_rule(rule_id) is None:
                counts["rules"] += int(
                    self._upsert_rule_created(
                        self.wakeup_migration_rule(row, rule_id, event)
                    )
                )
            job, deduped = self._store.enqueue_job(
                self.wakeup_migration_job(row, rule_id, event)
            )
            counts["jobs"] += int(not deduped)
            if row["completed_at"]:
                attempt = self.wakeup_migration_attempt(row, job.job_id)
                if self._store.get_attempt(attempt.attempt_id) is None:
                    counts["attempts"] += 1
                self._store.record_attempt(attempt)

        return PmaLegacyAutomationMigrationResult(**counts)

    def subscription_migration_rule(self, row: sqlite3.Row) -> AutomationRule:
        return AutomationRule.create(
            rule_id=f"{PMA_SUBSCRIPTION_RULE_PREFIX}{row['subscription_id']}",
            name=f"PMA subscription {row['subscription_id']}",
            enabled=str(row["state"] or "active") == "active",
            system_owned=True,
            trigger_kind=TRIGGER_KIND_EVENT,
            trigger={
                "event_types": _normalize_reactive_event_types(
                    _load_json_array(row["event_types_json"])
                )
            },
            filters={
                **_optional_filter("event.repo_id", row["repo_id"]),
                **_optional_filter("event.payload.run_id", row["run_id"]),
                **_optional_filter("event.payload.thread_id", row["thread_target_id"]),
                **_optional_filter("event.payload.from_state", row["from_state"]),
                **_optional_filter("event.payload.to_state", row["to_state"]),
            },
            target_policy=TARGET_POLICY_HUB,
            target={
                "repo_id": row["repo_id"],
                "run_id": row["run_id"],
                "thread_id": row["thread_target_id"],
            },
            executor_kind=EXECUTOR_PMA_TURN,
            executor={
                "lane_id": row["lane_id"] or "pma:default",
                "wake_up_kind": "pma_subscription",
                "source": "transition",
                "message": (
                    "Automation wake-up received.\n"
                    "source: transition\n"
                    f"subscription_id: {row['subscription_id']}\n"
                    "repo_id: {{ event.repo_id }}\n"
                    "run_id: {{ event.payload.run_id }}\n"
                    "thread_id: {{ event.payload.thread_id }}"
                ),
            },
            policy={
                "max_attempts": 3,
                "approval_mode": "pause_and_request_user",
                "max_concurrent_per_rule": 1,
                "max_concurrent_per_target": 1,
            },
            metadata={
                "migration": "pma_legacy_automation_v1",
                "legacy_source_table": "orch_automation_subscriptions",
                "legacy_subscription_id": row["subscription_id"],
                "legacy_reason": row["reason_text"],
                "legacy_idempotency_key": row["idempotency_key"],
                "legacy_max_matches": row["max_matches"],
                "legacy_match_count": row["match_count"] or 0,
                "legacy_metadata": _json_object_from_row(row, "metadata_json"),
                "purpose": "pma_lifecycle_subscription",
            },
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def timer_migration_rule(self, row: sqlite3.Row, rule_id: str) -> AutomationRule:
        return AutomationRule.create(
            rule_id=rule_id,
            name=f"PMA timer {row['timer_id']}",
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
            executor={
                "lane_id": _json_object_from_row(row, "payload_json").get("lane_id")
                or "pma:default",
                "source": "timer",
                "wake_up_kind": "pma_timer",
                "message": (
                    "Automation wake-up received.\n"
                    "source: timer\n"
                    f"timer_id: {row['timer_id']}"
                ),
            },
            metadata={
                "migration": "pma_legacy_automation_v1",
                "legacy_source_table": "orch_automation_timers",
                "legacy_timer_id": row["timer_id"],
                "legacy_idempotency_key": row["idempotency_key"],
                "purpose": "pma_timer",
            },
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def timer_migration_schedule(
        self, row: sqlite3.Row, rule_id: str
    ) -> AutomationSchedule:
        state = str(row["state"] or "pending")
        return AutomationSchedule.create(
            schedule_id=f"{PMA_TIMER_SCHEDULE_PREFIX}{row['timer_id']}",
            rule_id=rule_id,
            schedule_kind=SCHEDULE_ONE_SHOT,
            next_fire_at=row["available_at"] if state == "pending" else None,
            last_fire_at=row["fired_at"],
            schedule={
                "legacy_timer_id": row["timer_id"],
                "timer_kind": row["timer_kind"],
                "payload": _json_object_from_row(row, "payload_json"),
            },
            state="active" if state == "pending" else state,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def wakeup_migration_event(self, row: sqlite3.Row) -> AutomationEvent:
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
                "migration": "pma_legacy_automation_v1",
                "legacy_source_table": "orch_automation_wakeups",
                "legacy_wakeup_id": row["wakeup_id"],
                "legacy_event_id": row["event_id"],
            },
            observed_at=row["created_at"],
        )

    def wakeup_migration_rule(
        self, row: sqlite3.Row, rule_id: str, event: AutomationEvent
    ) -> AutomationRule:
        return AutomationRule.create(
            rule_id=rule_id,
            name=f"PMA wakeup {row['wakeup_id']}",
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
                "migration": "pma_legacy_automation_v1",
                "legacy_source_table": "orch_automation_wakeups",
                "legacy_wakeup_id": row["wakeup_id"],
            },
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def wakeup_migration_job(
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

    def wakeup_migration_attempt(
        self, row: sqlite3.Row, job_id: str
    ) -> AutomationJobAttempt:
        return AutomationJobAttempt.create(
            attempt_id=f"legacy-pma-wakeup:{row['wakeup_id']}:attempt:1",
            job_id=job_id,
            attempt_number=1,
            status=JOB_SUCCEEDED,
            started_at=row["claimed_at"] or row["created_at"],
            finished_at=row["completed_at"],
            executor_result={
                "migration": "pma_legacy_automation_v1",
                "legacy_wakeup_id": row["wakeup_id"],
                "result_summary": row["reason_text"],
            },
        )

    def _validate_rows(
        self,
        subscriptions: list[sqlite3.Row],
        timers: list[sqlite3.Row],
        wakeups: list[sqlite3.Row],
    ) -> list[PmaLegacyAutomationMigrationDiagnostic]:
        diagnostics: list[PmaLegacyAutomationMigrationDiagnostic] = []
        subscription_ids = {
            str(row["subscription_id"]).strip()
            for row in subscriptions
            if _optional_text(row["subscription_id"]) is not None
        }
        for row in subscriptions:
            self._validate_required(
                diagnostics,
                row,
                table="orch_automation_subscriptions",
                id_column="subscription_id",
                required=("subscription_id", "created_at", "updated_at", "state"),
            )
            self._validate_json_array(
                diagnostics,
                row,
                table="orch_automation_subscriptions",
                id_column="subscription_id",
                column="event_types_json",
            )
            self._validate_json_object(
                diagnostics,
                row,
                table="orch_automation_subscriptions",
                id_column="subscription_id",
                column="metadata_json",
            )
            self._validate_state(
                diagnostics,
                row,
                table="orch_automation_subscriptions",
                id_column="subscription_id",
                allowed={"active", "cancelled", "disabled"},
            )

        for row in timers:
            self._validate_required(
                diagnostics,
                row,
                table="orch_automation_timers",
                id_column="timer_id",
                required=(
                    "timer_id",
                    "timer_kind",
                    "available_at",
                    "state",
                    "created_at",
                    "updated_at",
                ),
            )
            self._validate_json_object(
                diagnostics,
                row,
                table="orch_automation_timers",
                id_column="timer_id",
                column="payload_json",
            )
            self._validate_state(
                diagnostics,
                row,
                table="orch_automation_timers",
                id_column="timer_id",
                allowed={"pending", "fired", "cancelled"},
            )
            self._validate_subscription_ref(
                diagnostics,
                row,
                table="orch_automation_timers",
                id_column="timer_id",
                subscription_ids=subscription_ids,
            )

        for row in wakeups:
            self._validate_required(
                diagnostics,
                row,
                table="orch_automation_wakeups",
                id_column="wakeup_id",
                required=("wakeup_id", "wakeup_kind", "state", "created_at", "updated_at"),
            )
            self._validate_json_object(
                diagnostics,
                row,
                table="orch_automation_wakeups",
                id_column="wakeup_id",
                column="payload_json",
            )
            self._validate_state(
                diagnostics,
                row,
                table="orch_automation_wakeups",
                id_column="wakeup_id",
                allowed={"pending", "queued", "dispatched", "completed", "cancelled"},
            )
            self._validate_subscription_ref(
                diagnostics,
                row,
                table="orch_automation_wakeups",
                id_column="wakeup_id",
                subscription_ids=subscription_ids,
            )
        return diagnostics

    @staticmethod
    def _diagnostic(
        diagnostics: list[PmaLegacyAutomationMigrationDiagnostic],
        *,
        code: str,
        table: str,
        legacy_id: str,
        message: str,
        next_step: str,
    ) -> None:
        diagnostics.append(
            PmaLegacyAutomationMigrationDiagnostic(
                code=code,
                table=table,
                legacy_id=legacy_id or "<blank>",
                message=message,
                next_step=next_step,
            )
        )

    def _validate_required(
        self,
        diagnostics: list[PmaLegacyAutomationMigrationDiagnostic],
        row: sqlite3.Row,
        *,
        table: str,
        id_column: str,
        required: tuple[str, ...],
    ) -> None:
        legacy_id = str(row[id_column] or "").strip()
        for column in required:
            if _optional_text(row[column]) is None:
                self._diagnostic(
                    diagnostics,
                    code="PMA_LEGACY_AUTOMATION_REQUIRED_FIELD",
                    table=table,
                    legacy_id=legacy_id,
                    message=f"{column} is required for PMA automation migration",
                    next_step=f"Repair or delete {table}.{column} before rerunning migration.",
                )

    def _validate_json_array(
        self,
        diagnostics: list[PmaLegacyAutomationMigrationDiagnostic],
        row: sqlite3.Row,
        *,
        table: str,
        id_column: str,
        column: str,
    ) -> None:
        value = row[column]
        if not isinstance(value, str) or not value.strip():
            return
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, list):
            self._diagnostic(
                diagnostics,
                code="PMA_LEGACY_AUTOMATION_MALFORMED_JSON",
                table=table,
                legacy_id=str(row[id_column] or ""),
                message=f"{column} must contain a JSON array",
                next_step=f"Rewrite {table}.{column} as a JSON array before rerunning migration.",
            )

    def _validate_json_object(
        self,
        diagnostics: list[PmaLegacyAutomationMigrationDiagnostic],
        row: sqlite3.Row,
        *,
        table: str,
        id_column: str,
        column: str,
    ) -> None:
        value = row[column]
        if not isinstance(value, str) or not value.strip():
            return
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            self._diagnostic(
                diagnostics,
                code="PMA_LEGACY_AUTOMATION_MALFORMED_JSON",
                table=table,
                legacy_id=str(row[id_column] or ""),
                message=f"{column} must contain a JSON object",
                next_step=f"Rewrite {table}.{column} as a JSON object before rerunning migration.",
            )

    def _validate_state(
        self,
        diagnostics: list[PmaLegacyAutomationMigrationDiagnostic],
        row: sqlite3.Row,
        *,
        table: str,
        id_column: str,
        allowed: set[str],
    ) -> None:
        state = str(row["state"] or "").strip()
        if state and state in allowed:
            return
        self._diagnostic(
            diagnostics,
            code="PMA_LEGACY_AUTOMATION_UNSUPPORTED_STATE",
            table=table,
            legacy_id=str(row[id_column] or ""),
            message=f"state {state or '<blank>'} cannot be migrated",
            next_step=f"Set {table}.state to one of {sorted(allowed)} before rerunning migration.",
        )

    def _validate_subscription_ref(
        self,
        diagnostics: list[PmaLegacyAutomationMigrationDiagnostic],
        row: sqlite3.Row,
        *,
        table: str,
        id_column: str,
        subscription_ids: set[str],
    ) -> None:
        subscription_id = _optional_text(row["subscription_id"])
        if subscription_id is None or subscription_id in subscription_ids:
            return
        self._diagnostic(
            diagnostics,
            code="PMA_LEGACY_AUTOMATION_ORPHANED_ROW",
            table=table,
            legacy_id=str(row[id_column] or ""),
            message=f"subscription_id {subscription_id} does not exist",
            next_step=(
                "Create the missing subscription row, clear subscription_id, "
                "or delete the orphaned row before rerunning migration."
            ),
        )

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


def _optional_filter(key: str, value: Any) -> dict[str, Any]:
    return {key: value} if value is not None else {}


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
    "PmaLegacyAutomationMigration",
    "PmaLegacyAutomationMigrationDiagnostic",
    "PmaLegacyAutomationMigrationError",
    "PmaLegacyAutomationMigrationResult",
    "PmaUnifiedAutomationAdapter",
    "PmaUnifiedMirrorResult",
]
