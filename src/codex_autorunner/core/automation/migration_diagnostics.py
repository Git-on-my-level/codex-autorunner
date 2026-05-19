from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

from ..orchestration.legacy_backfill_gate import legacy_orchestration_backfill_complete
from ..orchestration.migrate_legacy_state import LEGACY_PMA_AUTOMATION_PATH
from ..orchestration.migrations import collect_orchestration_migration_status
from ..orchestration.sqlite import (
    open_orchestration_sqlite,
    resolve_orchestration_sqlite_path,
)
from ..pma_automation_unified import (
    PmaLegacyAutomationMigration,
    PmaLegacyAutomationMigrationDiagnostic,
)
from ..sqlite_utils import table_exists
from .builtins import (
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
)
from .store import AutomationStore

AUTOMATION_MIGRATION_SCHEMA_PENDING = "AUTOMATION_MIGRATION_SCHEMA_PENDING"
AUTOMATION_MIGRATION_LEGACY_BACKFILL_PENDING = (
    "AUTOMATION_MIGRATION_LEGACY_BACKFILL_PENDING"
)
AUTOMATION_MIGRATION_LEGACY_RESIDUE = "AUTOMATION_MIGRATION_LEGACY_RESIDUE"
AUTOMATION_MIGRATION_MIRROR_INCOMPLETE = "AUTOMATION_MIGRATION_MIRROR_INCOMPLETE"
AUTOMATION_MIGRATION_INSPECTION_FAILED = "AUTOMATION_MIGRATION_INSPECTION_FAILED"

_LEGACY_TABLES = (
    "orch_automation_subscriptions",
    "orch_automation_timers",
    "orch_automation_wakeups",
)


@dataclasses.dataclass(frozen=True)
class AutomationMirrorHealth:
    status: str
    expected: dict[str, int]
    mirrored: dict[str, int]
    missing: dict[str, list[str]]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class AutomationMigrationDiagnostic:
    code: str
    message: str
    next_step: str
    severity: str = "error"
    table: str | None = None
    legacy_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "next_step": self.next_step,
        }
        if self.table is not None:
            payload["table"] = self.table
        if self.legacy_id is not None:
            payload["legacy_id"] = self.legacy_id
        return payload


@dataclasses.dataclass(frozen=True)
class AutomationMigrationReadModel:
    status: str
    schema_version: int
    target_schema_version: int
    pending_migration_versions: tuple[int, ...]
    legacy_backfill_complete: bool
    legacy_residue: dict[str, int]
    legacy_file_present: bool
    mirror_health: AutomationMirrorHealth
    diagnostics: tuple[AutomationMigrationDiagnostic, ...]
    next_steps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "schema_version": self.schema_version,
            "target_schema_version": self.target_schema_version,
            "pending_migration_versions": list(self.pending_migration_versions),
            "legacy_backfill_complete": self.legacy_backfill_complete,
            "legacy_residue": dict(self.legacy_residue),
            "legacy_file_present": self.legacy_file_present,
            "mirror_health": self.mirror_health.to_dict(),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "next_steps": list(self.next_steps),
        }


def collect_automation_migration_read_model(
    hub_root: Path, *, durable: bool = True
) -> AutomationMigrationReadModel:
    diagnostics: list[AutomationMigrationDiagnostic] = []
    legacy_file_present = (hub_root / LEGACY_PMA_AUTOMATION_PATH).exists()
    database_exists = resolve_orchestration_sqlite_path(hub_root).exists()
    schema_version = 0
    target_schema_version = 0
    pending_versions: tuple[int, ...] = ()
    legacy_backfill_done = False
    residue = {table: 0 for table in _LEGACY_TABLES}
    mirror_health = AutomationMirrorHealth(
        status="ok",
        expected={"subscriptions": 0, "timers": 0, "wakeups": 0},
        mirrored={
            "subscription_rules": 0,
            "timer_rules": 0,
            "timer_schedules": 0,
            "wakeup_events": 0,
            "wakeup_jobs": 0,
        },
        missing={"subscriptions": [], "timers": [], "wakeups": []},
    )

    if not database_exists and not legacy_file_present:
        return AutomationMigrationReadModel(
            status="ok",
            schema_version=schema_version,
            target_schema_version=target_schema_version,
            pending_migration_versions=pending_versions,
            legacy_backfill_complete=legacy_backfill_done,
            legacy_residue=residue,
            legacy_file_present=legacy_file_present,
            mirror_health=mirror_health,
            diagnostics=(),
            next_steps=(),
        )

    try:
        with open_orchestration_sqlite(
            hub_root, durable=durable, migrate=False
        ) as conn:
            migration_status = collect_orchestration_migration_status(conn)
            schema_version = migration_status.current_version
            target_schema_version = migration_status.target_version
            pending_versions = migration_status.pending_versions
            legacy_backfill_done = legacy_orchestration_backfill_complete(conn)
            residue = _legacy_residue_counts(conn)
            mirror_health = _collect_mirror_health(conn)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        diagnostics.append(
            AutomationMigrationDiagnostic(
                code=AUTOMATION_MIGRATION_INSPECTION_FAILED,
                message=f"Automation migration diagnostics could not inspect orchestration state: {exc}",
                next_step="Repair orchestration.sqlite3 visibility and rerun car doctor or hub orchestration status.",
            )
        )

    if pending_versions:
        diagnostics.append(
            AutomationMigrationDiagnostic(
                code=AUTOMATION_MIGRATION_SCHEMA_PENDING,
                message=(
                    "Orchestration SQLite has pending schema migrations: "
                    + ", ".join(str(v) for v in pending_versions)
                ),
                next_step="Start the hub or run an orchestration command that opens the database with migrations enabled.",
            )
        )
    if not legacy_backfill_done and (legacy_file_present or any(residue.values())):
        diagnostics.append(
            AutomationMigrationDiagnostic(
                code=AUTOMATION_MIGRATION_LEGACY_BACKFILL_PENDING,
                message="Legacy orchestration backfill has not completed while legacy automation input is present.",
                next_step="Start the hub once to run legacy orchestration backfill, then rerun automation migration diagnostics.",
            )
        )
    if legacy_file_present:
        diagnostics.append(
            AutomationMigrationDiagnostic(
                code=AUTOMATION_MIGRATION_LEGACY_RESIDUE,
                severity="warning",
                message=f"Legacy PMA automation JSON remains at {LEGACY_PMA_AUTOMATION_PATH}.",
                next_step="Confirm compatibility consumers no longer need the mirror, then remove or regenerate it from canonical state.",
            )
        )

    diagnostics.extend(_legacy_row_diagnostics(hub_root, durable=durable))

    if mirror_health.status != "ok":
        diagnostics.append(
            AutomationMigrationDiagnostic(
                code=AUTOMATION_MIGRATION_MIRROR_INCOMPLETE,
                message="Legacy PMA automation rows are not fully represented in unified automation tables.",
                next_step="Run the explicit PMA legacy automation migration and resolve any row-level diagnostics it reports.",
            )
        )

    next_steps = _dedupe(item.next_step for item in diagnostics)
    status = (
        "blocked" if any(item.severity == "error" for item in diagnostics) else "ok"
    )
    return AutomationMigrationReadModel(
        status=status,
        schema_version=schema_version,
        target_schema_version=target_schema_version,
        pending_migration_versions=pending_versions,
        legacy_backfill_complete=legacy_backfill_done,
        legacy_residue=residue,
        legacy_file_present=legacy_file_present,
        mirror_health=mirror_health,
        diagnostics=tuple(diagnostics),
        next_steps=tuple(next_steps),
    )


def _legacy_residue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _LEGACY_TABLES:
        if not table_exists(conn, table):
            counts[table] = 0
            continue
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        counts[table] = int(row["count"] if row is not None else 0)
    return counts


def _collect_mirror_health(conn: sqlite3.Connection) -> AutomationMirrorHealth:
    if not all(table_exists(conn, table) for table in _LEGACY_TABLES):
        return AutomationMirrorHealth(
            status="ok",
            expected={"subscriptions": 0, "timers": 0, "wakeups": 0},
            mirrored={
                "subscription_rules": 0,
                "timer_rules": 0,
                "timer_schedules": 0,
                "wakeup_events": 0,
                "wakeup_jobs": 0,
            },
            missing={"subscriptions": [], "timers": [], "wakeups": []},
        )

    subscription_ids = _ids(conn, "orch_automation_subscriptions", "subscription_id")
    timer_ids = _ids(conn, "orch_automation_timers", "timer_id")
    wakeup_ids = _ids(conn, "orch_automation_wakeups", "wakeup_id")

    missing_subscriptions = [
        legacy_id
        for legacy_id in subscription_ids
        if not _row_exists(
            conn,
            "orch_automation_rules",
            "rule_id",
            f"{PMA_SUBSCRIPTION_RULE_PREFIX}{legacy_id}",
        )
    ]
    missing_timers = [
        legacy_id
        for legacy_id in timer_ids
        if not (
            _row_exists(
                conn,
                "orch_automation_rules",
                "rule_id",
                f"{PMA_TIMER_RULE_PREFIX}{legacy_id}",
            )
            and _row_exists(
                conn,
                "orch_automation_schedules",
                "schedule_id",
                f"{PMA_TIMER_SCHEDULE_PREFIX}{legacy_id}",
            )
        )
    ]
    missing_wakeups = [
        legacy_id
        for legacy_id in wakeup_ids
        if not (
            _row_exists(
                conn,
                "orch_automation_events",
                "event_id",
                f"legacy-pma-wakeup:{legacy_id}",
            )
            and _row_exists(
                conn,
                "orch_automation_jobs",
                "job_id",
                f"legacy-pma-wakeup:{legacy_id}",
            )
        )
    ]
    missing = {
        "subscriptions": missing_subscriptions,
        "timers": missing_timers,
        "wakeups": missing_wakeups,
    }
    mirrored = {
        "subscription_rules": len(subscription_ids) - len(missing_subscriptions),
        "timer_rules": sum(
            1
            for legacy_id in timer_ids
            if _row_exists(
                conn,
                "orch_automation_rules",
                "rule_id",
                f"{PMA_TIMER_RULE_PREFIX}{legacy_id}",
            )
        ),
        "timer_schedules": len(timer_ids) - len(missing_timers),
        "wakeup_events": sum(
            1
            for legacy_id in wakeup_ids
            if _row_exists(
                conn,
                "orch_automation_events",
                "event_id",
                f"legacy-pma-wakeup:{legacy_id}",
            )
        ),
        "wakeup_jobs": len(wakeup_ids) - len(missing_wakeups),
    }
    status = "ok" if not any(missing.values()) else "blocked"
    return AutomationMirrorHealth(
        status=status,
        expected={
            "subscriptions": len(subscription_ids),
            "timers": len(timer_ids),
            "wakeups": len(wakeup_ids),
        },
        mirrored=mirrored,
        missing=missing,
    )


def _ids(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    rows = conn.execute(
        f"SELECT {column} AS legacy_id FROM {table} ORDER BY created_at ASC, {column} ASC"
    ).fetchall()
    return [
        str(row["legacy_id"]) for row in rows if str(row["legacy_id"] or "").strip()
    ]


def _row_exists(conn: sqlite3.Connection, table: str, column: str, value: str) -> bool:
    if not table_exists(conn, table):
        return False
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",
        (value,),
    ).fetchone()
    return row is not None


def _legacy_row_diagnostics(
    hub_root: Path, *, durable: bool
) -> tuple[AutomationMigrationDiagnostic, ...]:
    store = AutomationStore(hub_root, durable=durable)
    try:
        low_level = PmaLegacyAutomationMigration(
            store, migrate=False
        ).collect_diagnostics()
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        return (
            AutomationMigrationDiagnostic(
                code=AUTOMATION_MIGRATION_INSPECTION_FAILED,
                message=f"Legacy PMA row validation failed: {exc}",
                next_step="Apply pending orchestration migrations and rerun automation migration diagnostics.",
            ),
        )
    return tuple(_from_pma_diagnostic(item) for item in low_level)


def _from_pma_diagnostic(
    item: PmaLegacyAutomationMigrationDiagnostic,
) -> AutomationMigrationDiagnostic:
    payload = item.to_dict()
    return AutomationMigrationDiagnostic(
        code=payload["code"],
        table=payload["table"],
        legacy_id=payload["legacy_id"],
        message=payload["message"],
        next_step=payload["next_step"],
    )


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


__all__ = [
    "AUTOMATION_MIGRATION_INSPECTION_FAILED",
    "AUTOMATION_MIGRATION_LEGACY_BACKFILL_PENDING",
    "AUTOMATION_MIGRATION_LEGACY_RESIDUE",
    "AUTOMATION_MIGRATION_MIRROR_INCOMPLETE",
    "AUTOMATION_MIGRATION_SCHEMA_PENDING",
    "AutomationMigrationDiagnostic",
    "AutomationMigrationReadModel",
    "AutomationMirrorHealth",
    "collect_automation_migration_read_model",
]
