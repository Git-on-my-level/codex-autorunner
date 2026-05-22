from __future__ import annotations

import dataclasses
import json
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
from ..text_utils import _json_dumps
from .builtins import (
    PMA_SUBSCRIPTION_RULE_PREFIX,
    PMA_TIMER_RULE_PREFIX,
    PMA_TIMER_SCHEDULE_PREFIX,
)
from .models import (
    AUTOMATION_CHILD_KIND_AGENT_TASK,
    AUTOMATION_CHILD_KIND_PMA_OPERATOR,
    EXECUTOR_AGENT_TASK_TURN,
    EXECUTOR_PMA_OPERATOR_TURN,
    LEGACY_EXECUTOR_KINDS,
    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
    LEGACY_EXECUTOR_PMA_TURN,
    TRIGGER_KIND_EVENT,
    TRIGGER_KIND_SCHEDULE,
    AutomationChildExecutionEdge,
    AutomationRule,
    AutomationRuntimeContract,
)
from .store import AutomationStore

AUTOMATION_MIGRATION_SCHEMA_PENDING = "AUTOMATION_MIGRATION_SCHEMA_PENDING"
AUTOMATION_MIGRATION_LEGACY_BACKFILL_PENDING = (
    "AUTOMATION_MIGRATION_LEGACY_BACKFILL_PENDING"
)
AUTOMATION_MIGRATION_LEGACY_RESIDUE = "AUTOMATION_MIGRATION_LEGACY_RESIDUE"
AUTOMATION_MIGRATION_MIRROR_INCOMPLETE = "AUTOMATION_MIGRATION_MIRROR_INCOMPLETE"
AUTOMATION_MIGRATION_INSPECTION_FAILED = "AUTOMATION_MIGRATION_INSPECTION_FAILED"
AUTOMATION_MIGRATION_LEGACY_EXECUTOR_SHAPE = (
    "AUTOMATION_MIGRATION_LEGACY_EXECUTOR_SHAPE"
)
AUTOMATION_MIGRATION_LEGACY_JOB_AMBIGUOUS = "AUTOMATION_MIGRATION_LEGACY_JOB_AMBIGUOUS"

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
class AutomationLegacyExecutorMigrationResult:
    rules_migrated: int = 0
    jobs_migrated: int = 0
    child_edges_created: int = 0
    diagnostics: tuple[AutomationMigrationDiagnostic, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules_migrated": self.rules_migrated,
            "jobs_migrated": self.jobs_migrated,
            "child_edges_created": self.child_edges_created,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


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
            status="complete",
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
    diagnostics.extend(_legacy_executor_shape_diagnostics(hub_root, durable=durable))

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
        "blocked"
        if any(item.severity == "error" for item in diagnostics)
        else "complete"
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


def migrate_legacy_automation_executor_shapes(
    hub_root: Path, *, durable: bool = True
) -> AutomationLegacyExecutorMigrationResult:
    """Rewrite unambiguous legacy automation executor modes to canonical modes."""
    store = AutomationStore(hub_root, durable=durable)
    diagnostics: list[AutomationMigrationDiagnostic] = []
    rules_migrated = 0
    jobs_migrated = 0
    child_edges_created = 0

    for rule in store.list_rules():
        if rule.executor_kind not in LEGACY_EXECUTOR_KINDS:
            continue
        target_kind = _migration_target_for_rule(rule, store)
        if target_kind is None:
            diagnostics.append(_ambiguous_rule_diagnostic(rule))
            continue
        if target_kind == EXECUTOR_AGENT_TASK_TURN and not _runtime_agent(
            rule.executor
        ):
            diagnostics.append(
                AutomationMigrationDiagnostic(
                    code=AUTOMATION_MIGRATION_LEGACY_EXECUTOR_SHAPE,
                    table="orch_automation_rules",
                    legacy_id=rule.rule_id,
                    message=(
                        f"Legacy rule {rule.rule_id} looks like concrete agent work "
                        "but does not declare an agent runtime."
                    ),
                    next_step=(
                        "Set executor.requested_runtime.agent or executor.agent, "
                        "then rerun PMA automation legacy executor migration."
                    ),
                )
            )
            continue
        store.upsert_rule(_migrated_rule(rule, target_kind))
        rules_migrated += 1

    with open_orchestration_sqlite(hub_root, durable=durable) as conn:
        rows = conn.execute("""
            SELECT jobs.*, rules.executor_kind AS rule_executor_kind
              FROM orch_automation_jobs AS jobs
              LEFT JOIN orch_automation_rules AS rules
                ON rules.rule_id = jobs.rule_id
             ORDER BY jobs.created_at ASC, jobs.job_id ASC
            """).fetchall()

    for row in rows:
        executor = _json_object(row["executor_json"])
        job_kind = _optional_text(executor.get("kind"))
        rule_kind = _optional_text(row["rule_executor_kind"])
        if (
            job_kind not in LEGACY_EXECUTOR_KINDS
            and rule_kind not in LEGACY_EXECUTOR_KINDS
        ):
            continue
        target_kind = _migration_target_for_job(row, executor, rule_kind)
        if target_kind is None:
            diagnostics.append(_ambiguous_job_diagnostic(row, executor))
            continue
        new_executor = _migrated_executor(executor, target_kind)
        _update_job_executor(
            hub_root, durable=durable, job_id=row["job_id"], executor=new_executor
        )
        jobs_migrated += 1
        child_edges_created += _migrate_legacy_job_edge(
            store,
            hub_root=hub_root,
            durable=durable,
            row=row,
            executor=new_executor,
            target_kind=target_kind,
            diagnostics=diagnostics,
        )

    return AutomationLegacyExecutorMigrationResult(
        rules_migrated=rules_migrated,
        jobs_migrated=jobs_migrated,
        child_edges_created=child_edges_created,
        diagnostics=tuple(diagnostics),
    )


def _legacy_executor_shape_diagnostics(
    hub_root: Path, *, durable: bool
) -> tuple[AutomationMigrationDiagnostic, ...]:
    store = AutomationStore(hub_root, durable=durable)
    diagnostics: list[AutomationMigrationDiagnostic] = []
    try:
        for rule in store.list_rules():
            if rule.executor_kind in LEGACY_EXECUTOR_KINDS:
                diagnostics.append(_ambiguous_rule_diagnostic(rule))
        with open_orchestration_sqlite(
            hub_root, durable=durable, migrate=False
        ) as conn:
            rows = conn.execute(
                """
                SELECT jobs.*, rules.executor_kind AS rule_executor_kind
                  FROM orch_automation_jobs AS jobs
                  LEFT JOIN orch_automation_rules AS rules
                    ON rules.rule_id = jobs.rule_id
                 WHERE json_extract(jobs.executor_json, '$.kind') IN (?, ?)
                    OR rules.executor_kind IN (?, ?)
                 ORDER BY jobs.created_at ASC, jobs.job_id ASC
                """,
                (
                    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
                    LEGACY_EXECUTOR_PMA_TURN,
                    LEGACY_EXECUTOR_MANAGED_THREAD_TURN,
                    LEGACY_EXECUTOR_PMA_TURN,
                ),
            ).fetchall()
        for row in rows:
            executor = _json_object(row["executor_json"])
            diagnostics.append(_ambiguous_job_diagnostic(row, executor))
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        diagnostics.append(
            AutomationMigrationDiagnostic(
                code=AUTOMATION_MIGRATION_INSPECTION_FAILED,
                message=f"Legacy automation executor inspection failed: {exc}",
                next_step="Apply pending orchestration migrations and rerun automation migration diagnostics.",
            )
        )
    return tuple(diagnostics)


def _migration_target_for_rule(
    rule: AutomationRule, store: AutomationStore
) -> str | None:
    if rule.executor_kind == LEGACY_EXECUTOR_PMA_TURN:
        return EXECUTOR_PMA_OPERATOR_TURN
    if _is_lifecycle_pma_rule(rule):
        return EXECUTOR_PMA_OPERATOR_TURN
    schedules = store.list_schedules(rule_id=rule.rule_id)
    if rule.trigger_kind == TRIGGER_KIND_SCHEDULE or schedules:
        return EXECUTOR_AGENT_TASK_TURN
    if _looks_like_security_scan(rule):
        return EXECUTOR_AGENT_TASK_TURN
    if rule.trigger_kind == TRIGGER_KIND_EVENT and rule.executor.get("wake_up_kind"):
        return EXECUTOR_PMA_OPERATOR_TURN
    return None


def _migration_target_for_job(
    row: sqlite3.Row, executor: dict[str, Any], rule_kind: str | None
) -> str | None:
    job_kind = _optional_text(executor.get("kind"))
    if job_kind == LEGACY_EXECUTOR_PMA_TURN or rule_kind == EXECUTOR_PMA_OPERATOR_TURN:
        return EXECUTOR_PMA_OPERATOR_TURN
    if rule_kind == EXECUTOR_AGENT_TASK_TURN:
        return EXECUTOR_AGENT_TASK_TURN
    if job_kind == LEGACY_EXECUTOR_MANAGED_THREAD_TURN:
        if row["managed_thread_execution_id"]:
            return EXECUTOR_AGENT_TASK_TURN
        if row["pma_queue_item_id"] and _runtime_agent(executor):
            return EXECUTOR_AGENT_TASK_TURN
    return None


def _migrated_rule(rule: AutomationRule, target_kind: str) -> AutomationRule:
    executor = _migrated_executor(rule.executor, target_kind)
    metadata = {
        **rule.metadata,
        "migration": rule.metadata.get("migration") or "legacy_executor_shape_v1",
        "legacy_executor_kind": rule.executor_kind,
    }
    return AutomationRule.create(
        rule_id=rule.rule_id,
        name=rule.name,
        enabled=rule.enabled,
        system_owned=rule.system_owned,
        trigger_kind=rule.trigger_kind,
        trigger=rule.trigger,
        filters=rule.filters,
        target_policy=rule.target_policy,
        target=rule.target,
        executor_kind=target_kind,
        executor=executor,
        policy=rule.policy,
        metadata=metadata,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


def _migrated_executor(executor: dict[str, Any], target_kind: str) -> dict[str, Any]:
    migrated = dict(executor)
    legacy_kind = migrated.pop("kind", None) or migrated.pop(
        "legacy_executor_kind", None
    )
    migrated["kind"] = target_kind
    if legacy_kind:
        migrated["legacy_executor_kind"] = legacy_kind
    runtime = _runtime_contract_from_executor(migrated)
    if runtime:
        migrated["requested_runtime"] = runtime
    return migrated


def _migrate_legacy_job_edge(
    store: AutomationStore,
    *,
    hub_root: Path,
    durable: bool,
    row: sqlite3.Row,
    executor: dict[str, Any],
    target_kind: str,
    diagnostics: list[AutomationMigrationDiagnostic],
) -> int:
    job_id = str(row["job_id"])
    if store.list_child_execution_edges(job_id):
        return 0
    runtime = AutomationRuntimeContract.from_dict(
        {
            **_runtime_contract_from_executor(executor),
            "input_ref": {"kind": "automation_job", "job_id": job_id},
            "workspace_scope": _json_object(row["target_json"]),
        }
    )
    if target_kind == EXECUTOR_PMA_OPERATOR_TURN and row["pma_queue_item_id"]:
        store.upsert_child_execution_edge(
            AutomationChildExecutionEdge.create(
                parent_job_id=job_id,
                child_kind=AUTOMATION_CHILD_KIND_PMA_OPERATOR,
                child_id=str(row["pma_queue_item_id"]),
                requested_runtime=runtime,
                actual_runtime=None,
                authoritative_for_parent_completion=True,
            )
        )
        return 1
    if row["managed_thread_execution_id"]:
        store.upsert_child_execution_edge(
            AutomationChildExecutionEdge.create(
                parent_job_id=job_id,
                child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
                child_id=str(row["managed_thread_execution_id"]),
                requested_runtime=runtime,
                actual_runtime=None,
                authoritative_for_parent_completion=True,
            )
        )
        return 1
    if target_kind == EXECUTOR_AGENT_TASK_TURN and row["pma_queue_item_id"]:
        explicit = _explicit_child_from_pma_queue(
            hub_root, durable=durable, item_id=str(row["pma_queue_item_id"])
        )
        if explicit is not None:
            requested_runtime = AutomationRuntimeContract.from_dict(
                {
                    **runtime.to_dict(),
                    "workspace_scope": {
                        **(runtime.workspace_scope or {}),
                        "thread_target_id": explicit["thread_target_id"],
                    },
                }
            )
            store.upsert_child_execution_edge(
                AutomationChildExecutionEdge.create(
                    parent_job_id=job_id,
                    child_kind=AUTOMATION_CHILD_KIND_AGENT_TASK,
                    child_id=str(explicit["execution_id"]),
                    requested_runtime=requested_runtime,
                    actual_runtime=None,
                    authoritative_for_parent_completion=True,
                )
            )
            return 1
        diagnostics.append(_insufficient_queue_evidence_diagnostic(row))
    return 0


def _explicit_child_from_pma_queue(
    hub_root: Path, *, durable: bool, item_id: str
) -> dict[str, str] | None:
    with open_orchestration_sqlite(hub_root, durable=durable, migrate=False) as conn:
        row = conn.execute(
            """
            SELECT result_json
              FROM orch_queue_items
             WHERE queue_item_id = ?
             LIMIT 1
            """,
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    result = _json_object(row["result_json"])
    thread_id = _first_text(
        result,
        "managed_thread_id",
        "managedThreadId",
        "thread_target_id",
        "threadTargetId",
    )
    execution_id = _first_text(
        result,
        "managed_thread_execution_id",
        "managedThreadExecutionId",
        "execution_id",
        "executionId",
    )
    if thread_id and execution_id:
        return {"thread_target_id": thread_id, "execution_id": execution_id}
    return None


def _update_job_executor(
    hub_root: Path, *, durable: bool, job_id: str, executor: dict[str, Any]
) -> None:
    with open_orchestration_sqlite(hub_root, durable=durable) as conn:
        with conn:
            conn.execute(
                """
                UPDATE orch_automation_jobs
                   SET executor_json = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE job_id = ?
                """,
                (_json_dumps(executor), job_id),
            )


def _runtime_contract_from_executor(executor: dict[str, Any]) -> dict[str, Any]:
    existing = executor.get("requested_runtime")
    runtime = dict(existing) if isinstance(existing, dict) else {}
    for key in (
        "agent",
        "model",
        "profile",
        "reasoning",
        "approval_policy",
        "sandbox_policy",
    ):
        value = _optional_text(executor.get(key))
        if value is not None and runtime.get(key) is None:
            runtime[key] = value
    agent_profile = _optional_text(executor.get("agent_profile"))
    if agent_profile is not None and runtime.get("profile") is None:
        runtime["profile"] = agent_profile
    return runtime


def _runtime_agent(executor: dict[str, Any]) -> str | None:
    runtime = executor.get("requested_runtime")
    if isinstance(runtime, dict):
        value = _optional_text(runtime.get("agent"))
        if value is not None:
            return value
    return _optional_text(executor.get("agent"))


def _is_lifecycle_pma_rule(rule: AutomationRule) -> bool:
    purpose = _optional_text(rule.metadata.get("purpose")) or ""
    if purpose.startswith("pma_"):
        return True
    if any(key.startswith("legacy_") for key in rule.metadata):
        return True
    if _optional_text(rule.executor.get("wake_up_kind")) is not None:
        return True
    return False


def _looks_like_security_scan(rule: AutomationRule) -> bool:
    values = [
        rule.name,
        rule.metadata.get("automation_kind"),
        rule.metadata.get("preset"),
        rule.metadata.get("description"),
    ]
    text = " ".join(str(value or "").lower() for value in values)
    return "security" in text and "scan" in text


def _ambiguous_rule_diagnostic(rule: AutomationRule) -> AutomationMigrationDiagnostic:
    return AutomationMigrationDiagnostic(
        code=AUTOMATION_MIGRATION_LEGACY_EXECUTOR_SHAPE,
        table="orch_automation_rules",
        legacy_id=rule.rule_id,
        message=(
            f"Automation rule {rule.rule_id} still uses legacy executor "
            f"{rule.executor_kind}."
        ),
        next_step=(
            "Run PMA automation legacy executor migration, or manually rewrite the "
            "rule to agent_task_turn, pma_operator_turn, or ticket_flow."
        ),
    )


def _ambiguous_job_diagnostic(
    row: sqlite3.Row, executor: dict[str, Any]
) -> AutomationMigrationDiagnostic:
    return AutomationMigrationDiagnostic(
        code=AUTOMATION_MIGRATION_LEGACY_JOB_AMBIGUOUS,
        table="orch_automation_jobs",
        legacy_id=str(row["job_id"]),
        message=(
            f"Automation job {row['job_id']} has ambiguous legacy executor "
            f"{executor.get('kind') or row['rule_executor_kind']}."
        ),
        next_step=(
            "Persist a durable child execution edge or rewrite the job executor to "
            "an explicit mode before relying on lifecycle reconciliation."
        ),
    )


def _insufficient_queue_evidence_diagnostic(
    row: sqlite3.Row,
) -> AutomationMigrationDiagnostic:
    return AutomationMigrationDiagnostic(
        code=AUTOMATION_MIGRATION_LEGACY_JOB_AMBIGUOUS,
        table="orch_automation_jobs",
        legacy_id=str(row["job_id"]),
        message=(
            f"Automation job {row['job_id']} only references PMA queue item "
            f"{row['pma_queue_item_id']} and has no durable managed-thread child ids."
        ),
        next_step=(
            "Record managed_thread_target_id and managed_thread_execution_id, or "
            "treat queue result text as a diagnostic hint and inspect manually."
        ),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _optional_text(data.get(key))
        if value is not None:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


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
    "AUTOMATION_MIGRATION_LEGACY_EXECUTOR_SHAPE",
    "AUTOMATION_MIGRATION_LEGACY_JOB_AMBIGUOUS",
    "AUTOMATION_MIGRATION_LEGACY_RESIDUE",
    "AUTOMATION_MIGRATION_MIRROR_INCOMPLETE",
    "AUTOMATION_MIGRATION_SCHEMA_PENDING",
    "AutomationLegacyExecutorMigrationResult",
    "AutomationMigrationDiagnostic",
    "AutomationMigrationReadModel",
    "AutomationMirrorHealth",
    "collect_automation_migration_read_model",
    "migrate_legacy_automation_executor_shapes",
]
