from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .orchestration.runtime_bindings import get_runtime_thread_binding
from .sqlite_utils import open_sqlite
from .text_utils import _json_dumps

LEGACY_MIRROR_MIGRATION_VERSION = 1


def legacy_mirror_enabled() -> bool:
    val = os.environ.get("CAR_LEGACY_MIRROR_ENABLED", "true").strip().lower()
    return val in ("true", "1", "yes")


def sync_legacy_mirror(
    *,
    hub_root: Path,
    legacy_db_path: Path,
    durable: bool,
    orchestration_conn: Any,
    thread_row_to_record: Callable[[Any], dict[str, Any]],
    execution_row_to_record: Callable[[Any], dict[str, Any]],
    ensure_legacy_schema: Callable[[Any], None],
) -> None:
    with open_sqlite(legacy_db_path, durable=durable) as legacy_conn:
        ensure_legacy_schema(legacy_conn)
        thread_rows = orchestration_conn.execute(
            """
            SELECT *
              FROM orch_thread_targets
             ORDER BY created_at ASC, thread_target_id ASC
            """
        ).fetchall()
        execution_rows = orchestration_conn.execute(
            """
            SELECT *
              FROM orch_thread_executions
             ORDER BY started_at ASC, execution_id ASC
            """
        ).fetchall()
        action_rows = orchestration_conn.execute(
            """
            SELECT *
              FROM orch_thread_actions
             ORDER BY CAST(action_id AS INTEGER) ASC, action_id ASC
            """
        ).fetchall()
        with legacy_conn:
            legacy_conn.execute("DELETE FROM pma_managed_actions")
            legacy_conn.execute("DELETE FROM pma_managed_turns")
            legacy_conn.execute("DELETE FROM pma_managed_threads")
            for row in thread_rows:
                legacy = thread_row_to_record(row)
                runtime_binding = get_runtime_thread_binding(
                    hub_root, legacy["managed_thread_id"]
                )
                legacy_conn.execute(
                    """
                    INSERT INTO pma_managed_threads (
                        managed_thread_id,
                        agent,
                        repo_id,
                        resource_kind,
                        resource_id,
                        workspace_root,
                        name,
                        backend_thread_id,
                        status,
                        normalized_status,
                        status_reason_code,
                        status_updated_at,
                        status_terminal,
                        status_turn_id,
                        last_turn_id,
                        last_message_preview,
                        compact_seed,
                        metadata_json,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        legacy["managed_thread_id"],
                        legacy["agent"],
                        legacy["repo_id"],
                        legacy["resource_kind"],
                        legacy["resource_id"],
                        legacy["workspace_root"],
                        legacy["name"],
                        (
                            runtime_binding.backend_thread_id
                            if runtime_binding is not None
                            else None
                        ),
                        legacy["status"],
                        legacy["normalized_status"],
                        legacy["status_reason_code"],
                        legacy["status_updated_at"],
                        1 if legacy["status_terminal"] else 0,
                        legacy["status_turn_id"],
                        legacy["last_turn_id"],
                        legacy["last_message_preview"],
                        legacy["compact_seed"],
                        _json_dumps(
                            legacy["metadata"]
                            if isinstance(legacy.get("metadata"), dict)
                            else {}
                        ),
                        legacy["created_at"],
                        legacy["updated_at"],
                    ),
                )
            for row in execution_rows:
                legacy = execution_row_to_record(row)
                legacy_conn.execute(
                    """
                    INSERT INTO pma_managed_turns (
                        managed_turn_id,
                        managed_thread_id,
                        client_turn_id,
                        backend_turn_id,
                        prompt,
                        status,
                        assistant_text,
                        transcript_turn_id,
                        model,
                        reasoning,
                        error,
                        started_at,
                        finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        legacy["managed_turn_id"],
                        legacy["managed_thread_id"],
                        legacy["client_turn_id"],
                        legacy["backend_turn_id"],
                        legacy["prompt"],
                        legacy["status"],
                        legacy["assistant_text"],
                        legacy["transcript_turn_id"],
                        legacy["model"],
                        legacy["reasoning"],
                        legacy["error"],
                        legacy["started_at"],
                        legacy["finished_at"],
                    ),
                )
            for row in action_rows:
                action_id = str(row["action_id"] or "").strip()
                try:
                    legacy_action_id = int(action_id)
                except ValueError:
                    continue
                legacy_conn.execute(
                    """
                    INSERT INTO pma_managed_actions (
                        action_id,
                        managed_thread_id,
                        action_type,
                        payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        legacy_action_id,
                        row["thread_target_id"],
                        row["action_type"],
                        row["payload_json"],
                        row["created_at"],
                    ),
                )


__all__ = [
    "LEGACY_MIRROR_MIGRATION_VERSION",
    "legacy_mirror_enabled",
    "sync_legacy_mirror",
]
