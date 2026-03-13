from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Callable

from ..time_utils import now_iso
from .models import OrchestrationTableDefinition

ORCHESTRATION_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class _MigrationStep:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _ensure_migration_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_migration_runs (
            run_id TEXT PRIMARY KEY,
            from_version INTEGER NOT NULL,
            target_version INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error_text TEXT
        )
        """
    )


def _apply_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_thread_targets (
            thread_target_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            backend_thread_id TEXT,
            repo_id TEXT,
            workspace_root TEXT,
            display_name TEXT,
            lifecycle_status TEXT,
            runtime_status TEXT,
            status_reason TEXT,
            status_turn_id TEXT,
            last_execution_id TEXT,
            last_message_preview TEXT,
            compact_seed TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_thread_executions (
            execution_id TEXT PRIMARY KEY,
            thread_target_id TEXT NOT NULL,
            client_request_id TEXT,
            request_kind TEXT NOT NULL,
            prompt_text TEXT,
            status TEXT NOT NULL,
            backend_turn_id TEXT,
            assistant_text TEXT,
            error_text TEXT,
            model_id TEXT,
            reasoning_level TEXT,
            transcript_mirror_id TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (thread_target_id) REFERENCES orch_thread_targets(thread_target_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_thread_actions (
            action_id TEXT PRIMARY KEY,
            thread_target_id TEXT NOT NULL,
            execution_id TEXT,
            action_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (thread_target_id) REFERENCES orch_thread_targets(thread_target_id)
                ON DELETE CASCADE,
            FOREIGN KEY (execution_id) REFERENCES orch_thread_executions(execution_id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_automation_subscriptions (
            subscription_id TEXT PRIMARY KEY,
            event_types_json TEXT NOT NULL DEFAULT '[]',
            repo_id TEXT,
            run_id TEXT,
            thread_target_id TEXT,
            binding_id TEXT,
            lane_id TEXT,
            from_state TEXT,
            to_state TEXT,
            notify_once INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            match_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            disabled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_automation_timers (
            timer_id TEXT PRIMARY KEY,
            subscription_id TEXT,
            repo_id TEXT,
            run_id TEXT,
            thread_target_id TEXT,
            timer_kind TEXT NOT NULL,
            schedule_key TEXT,
            available_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (subscription_id) REFERENCES orch_automation_subscriptions(subscription_id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_automation_wakeups (
            wakeup_id TEXT PRIMARY KEY,
            subscription_id TEXT,
            repo_id TEXT,
            run_id TEXT,
            thread_target_id TEXT,
            lane_id TEXT,
            wakeup_kind TEXT NOT NULL,
            state TEXT NOT NULL,
            available_at TEXT,
            claimed_at TEXT,
            completed_at TEXT,
            reason_text TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (subscription_id) REFERENCES orch_automation_subscriptions(subscription_id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_queue_items (
            queue_item_id TEXT PRIMARY KEY,
            lane_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_key TEXT,
            dedupe_key TEXT,
            state TEXT NOT NULL,
            visible_at TEXT,
            claimed_at TEXT,
            completed_at TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_reactive_debounce_state (
            debounce_key TEXT PRIMARY KEY,
            repo_id TEXT,
            thread_target_id TEXT,
            fingerprint TEXT,
            available_at TEXT,
            last_event_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_transcript_mirrors (
            transcript_mirror_id TEXT PRIMARY KEY,
            target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            execution_id TEXT,
            message_role TEXT NOT NULL,
            text_content TEXT NOT NULL,
            text_preview TEXT,
            repo_id TEXT,
            agent_id TEXT,
            model_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_event_projections (
            event_id TEXT PRIMARY KEY,
            event_family TEXT NOT NULL,
            event_type TEXT NOT NULL,
            target_kind TEXT,
            target_id TEXT,
            execution_id TEXT,
            repo_id TEXT,
            run_id TEXT,
            timestamp TEXT NOT NULL,
            status TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_audit_entries (
            audit_id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            actor_kind TEXT,
            actor_id TEXT,
            target_kind TEXT,
            target_id TEXT,
            repo_id TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_thread_targets_agent_status
            ON orch_thread_targets(agent_id, lifecycle_status, runtime_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_thread_executions_thread_status
            ON orch_thread_executions(thread_target_id, status, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_automation_wakeups_state_available
            ON orch_automation_wakeups(state, available_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_queue_items_lane_state
            ON orch_queue_items(lane_id, state, visible_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_transcript_mirrors_target
            ON orch_transcript_mirrors(target_kind, target_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_event_projections_target
            ON orch_event_projections(target_kind, target_id, timestamp)
        """
    )


def _apply_v2(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_bindings (
            binding_id TEXT PRIMARY KEY,
            surface_kind TEXT NOT NULL,
            surface_key TEXT NOT NULL,
            target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            agent_id TEXT,
            repo_id TEXT,
            mode TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            disabled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_flow_run_projections (
            flow_run_id TEXT PRIMARY KEY,
            repo_id TEXT,
            flow_type TEXT NOT NULL,
            status TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_bindings_surface
            ON orch_bindings(surface_kind, surface_key, disabled_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_flow_run_projections_repo_status
            ON orch_flow_run_projections(repo_id, status, updated_at)
        """
    )


_MIGRATIONS = (
    _MigrationStep(1, "create_core_orchestration_schema", _apply_v1),
    _MigrationStep(2, "add_binding_and_flow_projection_scaffolding", _apply_v2),
)


_TABLE_DEFINITIONS = (
    OrchestrationTableDefinition(
        name="orch_thread_targets",
        role="authoritative",
        description="Canonical orchestration-owned thread target metadata.",
    ),
    OrchestrationTableDefinition(
        name="orch_thread_executions",
        role="authoritative",
        description="Canonical orchestration execution metadata for thread targets.",
    ),
    OrchestrationTableDefinition(
        name="orch_thread_actions",
        role="authoritative",
        description="Action/audit records attached to orchestration thread targets.",
    ),
    OrchestrationTableDefinition(
        name="orch_automation_subscriptions",
        role="authoritative",
        description="Automation subscription state owned by orchestration.",
    ),
    OrchestrationTableDefinition(
        name="orch_automation_timers",
        role="authoritative",
        description="Automation timer state owned by orchestration.",
    ),
    OrchestrationTableDefinition(
        name="orch_automation_wakeups",
        role="authoritative",
        description="Automation wakeup records owned by orchestration.",
    ),
    OrchestrationTableDefinition(
        name="orch_queue_items",
        role="authoritative",
        description="Queue items and dispatch state for orchestration lanes.",
    ),
    OrchestrationTableDefinition(
        name="orch_reactive_debounce_state",
        role="authoritative",
        description="Reactive debounce state that suppresses duplicate wakeups.",
    ),
    OrchestrationTableDefinition(
        name="orch_bindings",
        role="authoritative",
        description="Reserved authoritative binding table for later surface routing work.",
    ),
    OrchestrationTableDefinition(
        name="orch_transcript_mirrors",
        role="mirror",
        description="Plain-text transcript mirrors; searchable but non-authoritative.",
    ),
    OrchestrationTableDefinition(
        name="orch_event_projections",
        role="projection",
        description="Normalized event projections across thread and flow targets.",
    ),
    OrchestrationTableDefinition(
        name="orch_flow_run_projections",
        role="projection",
        description="Hub-wide flow summaries projected from repo-local flows.db.",
    ),
    OrchestrationTableDefinition(
        name="orch_audit_entries",
        role="projection",
        description="Operator-facing audit projection records.",
    ),
    OrchestrationTableDefinition(
        name="orch_schema_migrations",
        role="ops",
        description="Applied schema migration versions for orchestration.sqlite3.",
    ),
    OrchestrationTableDefinition(
        name="orch_migration_runs",
        role="ops",
        description="Migration run bookkeeping for cutover and rollback verification.",
    ),
)


def list_orchestration_table_definitions() -> tuple[OrchestrationTableDefinition, ...]:
    return _TABLE_DEFINITIONS


def current_orchestration_schema_version(conn: sqlite3.Connection) -> int:
    _ensure_migration_tables(conn)
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM orch_schema_migrations"
    ).fetchone()
    if row is None:
        return 0
    return int(row["version"] or 0)


def apply_orchestration_migrations(conn: sqlite3.Connection) -> int:
    _ensure_migration_tables(conn)
    current_version = current_orchestration_schema_version(conn)
    if current_version > ORCHESTRATION_SCHEMA_VERSION:
        raise RuntimeError(
            "orchestration.sqlite3 schema is newer than this build supports"
        )
    if current_version == ORCHESTRATION_SCHEMA_VERSION:
        return current_version

    run_id = str(uuid.uuid4())
    started_at = now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO orch_migration_runs (
                run_id,
                from_version,
                target_version,
                started_at,
                finished_at,
                status,
                error_text
            ) VALUES (?, ?, ?, ?, NULL, 'running', NULL)
            """,
            (
                run_id,
                current_version,
                ORCHESTRATION_SCHEMA_VERSION,
                started_at,
            ),
        )

    try:
        for step in _MIGRATIONS:
            if step.version <= current_version:
                continue
            applied_at = now_iso()
            with conn:
                step.apply(conn)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO orch_schema_migrations (
                        version,
                        name,
                        applied_at
                    ) VALUES (?, ?, ?)
                    """,
                    (step.version, step.name, applied_at),
                )
        with conn:
            conn.execute(
                """
                UPDATE orch_migration_runs
                   SET finished_at = ?,
                       status = 'completed'
                 WHERE run_id = ?
                """,
                (now_iso(), run_id),
            )
    except Exception as exc:
        with conn:
            conn.execute(
                """
                UPDATE orch_migration_runs
                   SET finished_at = ?,
                       status = 'failed',
                       error_text = ?
                 WHERE run_id = ?
                """,
                (now_iso(), str(exc), run_id),
            )
        raise

    return ORCHESTRATION_SCHEMA_VERSION


__all__ = [
    "ORCHESTRATION_SCHEMA_VERSION",
    "apply_orchestration_migrations",
    "current_orchestration_schema_version",
    "list_orchestration_table_definitions",
]
