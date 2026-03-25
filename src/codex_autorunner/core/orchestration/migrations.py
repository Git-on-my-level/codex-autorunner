from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Callable

from ..time_utils import now_iso
from .models import OrchestrationTableDefinition

ORCHESTRATION_SCHEMA_VERSION = 11


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


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT name
          FROM sqlite_master
         WHERE type = 'table'
           AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows if row["name"] is not None}


def _column_not_null(
    conn: sqlite3.Connection, table_name: str, column_name: str
) -> bool | None:
    if not _table_exists(conn, table_name):
        return None
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    for row in rows:
        if str(row["name"]) == column_name:
            return bool(row["notnull"])
    return None


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    ddl: str,
) -> None:
    if not _table_exists(conn, table_name):
        return
    if column_name in _table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")


def _ensure_resource_owner_columns(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    repo_column: str = "repo_id",
) -> None:
    if not _table_exists(conn, table_name):
        return
    _ensure_column(conn, table_name, "resource_kind", "resource_kind TEXT")
    _ensure_column(conn, table_name, "resource_id", "resource_id TEXT")
    columns = _table_columns(conn, table_name)
    if repo_column not in columns:
        return
    conn.execute(
        f"""
        UPDATE {table_name}
           SET resource_kind = CASE
                   WHEN NULLIF(TRIM(COALESCE(resource_kind, '')), '') IS NOT NULL
                       THEN resource_kind
                   WHEN NULLIF(TRIM(COALESCE({repo_column}, '')), '') IS NOT NULL
                       THEN 'repo'
                   ELSE resource_kind
               END,
               resource_id = CASE
                   WHEN NULLIF(TRIM(COALESCE(resource_id, '')), '') IS NOT NULL
                       THEN resource_id
                   WHEN NULLIF(TRIM(COALESCE({repo_column}, '')), '') IS NOT NULL
                       THEN {repo_column}
                   ELSE resource_id
               END
         WHERE NULLIF(TRIM(COALESCE({repo_column}, '')), '') IS NOT NULL
        """
    )


def _apply_v3(conn: sqlite3.Connection) -> None:
    _ensure_column(
        conn,
        "orch_thread_targets",
        "status_updated_at",
        "status_updated_at TEXT",
    )
    _ensure_column(
        conn,
        "orch_thread_targets",
        "status_terminal",
        "status_terminal INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        "orch_automation_subscriptions",
        "reason_text",
        "reason_text TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_subscriptions",
        "idempotency_key",
        "idempotency_key TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_subscriptions",
        "max_matches",
        "max_matches INTEGER",
    )
    _ensure_column(
        conn,
        "orch_automation_timers",
        "fired_at",
        "fired_at TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_timers",
        "reason_text",
        "reason_text TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_timers",
        "idempotency_key",
        "idempotency_key TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_timers",
        "idle_seconds",
        "idle_seconds INTEGER",
    )
    _ensure_column(
        conn,
        "orch_automation_wakeups",
        "dispatched_at",
        "dispatched_at TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_wakeups",
        "timestamp",
        "timestamp TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_wakeups",
        "idempotency_key",
        "idempotency_key TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_wakeups",
        "timer_id",
        "timer_id TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_wakeups",
        "event_id",
        "event_id TEXT",
    )
    _ensure_column(
        conn,
        "orch_automation_wakeups",
        "event_type",
        "event_type TEXT",
    )
    _ensure_column(
        conn,
        "orch_queue_items",
        "idempotency_key",
        "idempotency_key TEXT",
    )
    _ensure_column(
        conn,
        "orch_queue_items",
        "error_text",
        "error_text TEXT",
    )
    _ensure_column(
        conn,
        "orch_queue_items",
        "dedupe_reason",
        "dedupe_reason TEXT",
    )
    _ensure_column(
        conn,
        "orch_queue_items",
        "result_json",
        "result_json TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        conn,
        "orch_reactive_debounce_state",
        "last_enqueued_at",
        "last_enqueued_at REAL",
    )

    if _column_not_null(conn, "orch_thread_actions", "thread_target_id"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orch_thread_actions_v3 (
                action_id TEXT PRIMARY KEY,
                thread_target_id TEXT,
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
            INSERT INTO orch_thread_actions_v3 (
                action_id,
                thread_target_id,
                execution_id,
                action_type,
                payload_json,
                created_at
            )
            SELECT
                action_id,
                thread_target_id,
                execution_id,
                action_type,
                payload_json,
                created_at
              FROM orch_thread_actions
            """
        )
        conn.execute("DROP TABLE orch_thread_actions")
        conn.execute("ALTER TABLE orch_thread_actions_v3 RENAME TO orch_thread_actions")


def _apply_v4(conn: sqlite3.Connection) -> None:
    _ensure_column(
        conn,
        "orch_transcript_mirrors",
        "metadata_json",
        "metadata_json TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        conn,
        "orch_event_projections",
        "processed",
        "processed INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        "orch_audit_entries",
        "fingerprint",
        "fingerprint TEXT",
    )
    if _table_exists(conn, "orch_event_projections"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orch_event_projections_family_processed
                ON orch_event_projections(event_family, processed, timestamp)
            """
        )
    if _table_exists(conn, "orch_audit_entries"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orch_audit_entries_action_created
                ON orch_audit_entries(action_type, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orch_audit_entries_fingerprint_created
                ON orch_audit_entries(fingerprint, created_at)
            """
        )


def _apply_v5(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "orch_bindings"):
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orch_bindings_active_surface_unique
                ON orch_bindings(surface_kind, surface_key)
             WHERE disabled_at IS NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orch_bindings_agent_repo_active
                ON orch_bindings(agent_id, repo_id, updated_at)
             WHERE disabled_at IS NULL
            """
        )
    thread_target_columns = _table_columns(conn, "orch_thread_targets")
    if {"repo_id", "updated_at"}.issubset(thread_target_columns):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orch_thread_targets_repo_updated
                ON orch_thread_targets(repo_id, updated_at)
            """
        )


def _apply_v6(conn: sqlite3.Connection) -> None:
    for table_name in (
        "orch_thread_targets",
        "orch_bindings",
        "orch_automation_subscriptions",
        "orch_automation_timers",
        "orch_automation_wakeups",
        "orch_reactive_debounce_state",
        "orch_transcript_mirrors",
        "orch_event_projections",
        "orch_audit_entries",
        "orch_flow_run_projections",
    ):
        _ensure_resource_owner_columns(conn, table_name)
    if _table_exists(conn, "orch_bindings"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orch_bindings_resource_active
                ON orch_bindings(resource_kind, resource_id, updated_at)
             WHERE disabled_at IS NULL
            """
        )
    thread_target_columns = _table_columns(conn, "orch_thread_targets")
    if {"resource_kind", "resource_id", "updated_at"}.issubset(thread_target_columns):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orch_thread_targets_resource_updated
                ON orch_thread_targets(resource_kind, resource_id, updated_at)
            """
        )


def _apply_v7(conn: sqlite3.Connection) -> None:
    _apply_v6(conn)
    _ensure_column(
        conn,
        "orch_thread_targets",
        "metadata_json",
        "metadata_json TEXT NOT NULL DEFAULT '{}'",
    )


def _apply_v8(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_publish_operations (
            operation_id TEXT PRIMARY KEY,
            operation_key TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            response_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            next_attempt_at TEXT,
            last_error_text TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_publish_attempts (
            attempt_id TEXT PRIMARY KEY,
            operation_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            state TEXT NOT NULL,
            response_json TEXT NOT NULL DEFAULT '{}',
            error_text TEXT,
            claimed_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (operation_id) REFERENCES orch_publish_operations(operation_id)
                ON DELETE CASCADE,
            UNIQUE (operation_id, attempt_number)
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_orch_publish_operations_active_key
            ON orch_publish_operations(operation_key)
         WHERE state IN ('pending', 'running', 'succeeded')
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_publish_operations_state_next_attempt
            ON orch_publish_operations(state, next_attempt_at, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_publish_attempts_operation_attempt
            ON orch_publish_attempts(operation_id, attempt_number)
        """
    )


def _apply_v9(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_scm_events (
            event_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            event_type TEXT NOT NULL,
            repo_slug TEXT,
            repo_id TEXT,
            pr_number INTEGER,
            delivery_id TEXT,
            occurred_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            raw_payload_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_scm_events_provider_type_timestamp
            ON orch_scm_events(provider, event_type, occurred_at, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_scm_events_repo_slug_timestamp
            ON orch_scm_events(repo_slug, occurred_at, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_scm_events_repo_id_timestamp
            ON orch_scm_events(repo_id, occurred_at, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_scm_events_pr_timestamp
            ON orch_scm_events(pr_number, occurred_at, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_scm_events_delivery_timestamp
            ON orch_scm_events(delivery_id, occurred_at, created_at)
        """
    )


def _apply_v10(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_pr_bindings (
            binding_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            repo_slug TEXT NOT NULL,
            repo_id TEXT,
            pr_number INTEGER NOT NULL,
            pr_state TEXT NOT NULL,
            head_branch TEXT,
            base_branch TEXT,
            thread_target_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT,
            FOREIGN KEY (thread_target_id) REFERENCES orch_thread_targets(thread_target_id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_orch_pr_bindings_provider_repo_pr
            ON orch_pr_bindings(provider, repo_slug, pr_number)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_pr_bindings_repo_state_updated
            ON orch_pr_bindings(provider, repo_slug, pr_state, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_pr_bindings_branch_state_updated
            ON orch_pr_bindings(provider, repo_slug, head_branch, pr_state, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_pr_bindings_repo_id_updated
            ON orch_pr_bindings(repo_id, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_pr_bindings_thread_updated
            ON orch_pr_bindings(thread_target_id, updated_at)
        """
    )


def _apply_v11(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orch_reaction_state (
            binding_id TEXT NOT NULL,
            reaction_kind TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            state TEXT NOT NULL,
            first_event_id TEXT,
            last_event_id TEXT,
            last_operation_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            first_emitted_at TEXT,
            last_emitted_at TEXT,
            last_delivery_failed_at TEXT,
            resolved_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            delivery_failure_count INTEGER NOT NULL DEFAULT 0,
            last_error_text TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (binding_id, reaction_kind, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_reaction_state_binding_kind_state
            ON orch_reaction_state(binding_id, reaction_kind, state, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_orch_reaction_state_state_updated
            ON orch_reaction_state(state, updated_at)
        """
    )


_MIGRATIONS = (
    _MigrationStep(1, "create_core_orchestration_schema", _apply_v1),
    _MigrationStep(2, "add_binding_and_flow_projection_scaffolding", _apply_v2),
    _MigrationStep(3, "expand_pma_cutover_columns", _apply_v3),
    _MigrationStep(4, "add_transcript_metadata_and_projection_processing", _apply_v4),
    _MigrationStep(5, "enforce_active_binding_uniqueness", _apply_v5),
    _MigrationStep(6, "generalize_resource_ownership", _apply_v6),
    _MigrationStep(
        7,
        "backfill_thread_target_metadata_and_resource_ownership",
        _apply_v7,
    ),
    _MigrationStep(8, "add_publish_journal_tables", _apply_v8),
    _MigrationStep(9, "add_scm_event_store", _apply_v9),
    _MigrationStep(10, "add_pr_binding_store", _apply_v10),
    _MigrationStep(11, "add_scm_reaction_state_store", _apply_v11),
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
        description="Authoritative transport-agnostic bindings from surface context to thread target.",
    ),
    OrchestrationTableDefinition(
        name="orch_publish_operations",
        role="authoritative",
        description="Publish journal operations queued before external automation side effects run.",
    ),
    OrchestrationTableDefinition(
        name="orch_publish_attempts",
        role="authoritative",
        description="Per-attempt publish execution metadata for retry and outcome tracking.",
    ),
    OrchestrationTableDefinition(
        name="orch_scm_events",
        role="authoritative",
        description="Canonical normalized SCM events captured before provider-specific reaction handling.",
    ),
    OrchestrationTableDefinition(
        name="orch_pr_bindings",
        role="authoritative",
        description="Optional durable PR-to-thread binding records keyed by provider, repo, and PR number.",
    ),
    OrchestrationTableDefinition(
        name="orch_reaction_state",
        role="authoritative",
        description="Durable reaction fingerprints and delivery state used to suppress repeated SCM follow-ups.",
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
