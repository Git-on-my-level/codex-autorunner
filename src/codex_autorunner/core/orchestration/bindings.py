from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..time_utils import now_iso
from .models import Binding
from .sqlite import open_orchestration_sqlite


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _decode_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _binding_from_row(row: sqlite3.Row) -> Binding:
    data = dict(row)
    data["thread_target_id"] = data.get("target_id")
    return Binding.from_mapping(data)


@dataclass(frozen=True)
class ActiveWorkSummary:
    thread_target_id: str
    agent_id: Optional[str]
    repo_id: Optional[str]
    workspace_root: Optional[str]
    display_name: Optional[str]
    lifecycle_status: Optional[str]
    runtime_status: Optional[str]
    execution_id: Optional[str]
    execution_status: Optional[str]
    queued_count: int
    message_preview: Optional[str]
    binding_count: int
    surface_kinds: tuple[str, ...]


class OrchestrationBindingStore:
    """Authoritative hub-SQLite binding CRUD and cross-surface query support."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def upsert_binding(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        thread_target_id: str,
        agent_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        mode: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Binding:
        normalized_surface_kind = _normalize_text(surface_kind)
        normalized_surface_key = _normalize_text(surface_key)
        normalized_thread_target_id = _normalize_text(thread_target_id)
        if normalized_surface_kind is None or normalized_surface_key is None:
            raise ValueError("surface_kind and surface_key are required")
        if normalized_thread_target_id is None:
            raise ValueError("thread_target_id is required")

        timestamp = now_iso()
        payload = json.dumps(metadata or {}, sort_keys=True, ensure_ascii=True)
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_bindings
                 WHERE surface_kind = ?
                   AND surface_key = ?
                   AND disabled_at IS NULL
                 LIMIT 1
                """,
                (normalized_surface_kind, normalized_surface_key),
            ).fetchone()
            if row is not None:
                existing_target_id = _normalize_text(row["target_id"])
                existing_agent_id = _normalize_text(row["agent_id"])
                existing_repo_id = _normalize_text(row["repo_id"])
                existing_mode = _normalize_text(row["mode"])
                existing_metadata = _decode_metadata(row["metadata_json"])
                if (
                    existing_target_id == normalized_thread_target_id
                    and existing_agent_id == _normalize_text(agent_id)
                    and existing_repo_id == _normalize_text(repo_id)
                    and existing_mode == _normalize_text(mode)
                    and existing_metadata == (metadata or {})
                ):
                    conn.execute(
                        """
                        UPDATE orch_bindings
                           SET updated_at = ?
                         WHERE binding_id = ?
                        """,
                        (timestamp, row["binding_id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE orch_bindings
                           SET disabled_at = ?,
                               updated_at = ?
                         WHERE binding_id = ?
                        """,
                        (timestamp, timestamp, row["binding_id"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO orch_bindings (
                            binding_id,
                            surface_kind,
                            surface_key,
                            target_kind,
                            target_id,
                            agent_id,
                            repo_id,
                            mode,
                            metadata_json,
                            created_at,
                            updated_at,
                            disabled_at
                        )
                        VALUES (?, ?, ?, 'thread', ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            uuid.uuid4().hex,
                            normalized_surface_kind,
                            normalized_surface_key,
                            normalized_thread_target_id,
                            _normalize_text(agent_id),
                            _normalize_text(repo_id),
                            _normalize_text(mode),
                            payload,
                            timestamp,
                            timestamp,
                        ),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO orch_bindings (
                        binding_id,
                        surface_kind,
                        surface_key,
                        target_kind,
                        target_id,
                        agent_id,
                        repo_id,
                        mode,
                        metadata_json,
                        created_at,
                        updated_at,
                        disabled_at
                    )
                    VALUES (?, ?, ?, 'thread', ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        uuid.uuid4().hex,
                        normalized_surface_kind,
                        normalized_surface_key,
                        normalized_thread_target_id,
                        _normalize_text(agent_id),
                        _normalize_text(repo_id),
                        _normalize_text(mode),
                        payload,
                        timestamp,
                        timestamp,
                    ),
                )
            refreshed = conn.execute(
                """
                SELECT *
                  FROM orch_bindings
                 WHERE surface_kind = ?
                   AND surface_key = ?
                   AND disabled_at IS NULL
                 LIMIT 1
                """,
                (normalized_surface_kind, normalized_surface_key),
            ).fetchone()
        if refreshed is None:
            raise RuntimeError("binding row missing after upsert")
        return _binding_from_row(refreshed)

    def disable_binding(self, *, binding_id: str) -> Optional[Binding]:
        normalized_binding_id = _normalize_text(binding_id)
        if normalized_binding_id is None:
            return None
        timestamp = now_iso()
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_bindings
                 WHERE binding_id = ?
                """,
                (normalized_binding_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE orch_bindings
                   SET disabled_at = COALESCE(disabled_at, ?),
                       updated_at = ?
                 WHERE binding_id = ?
                """,
                (timestamp, timestamp, normalized_binding_id),
            )
            refreshed = conn.execute(
                """
                SELECT *
                  FROM orch_bindings
                 WHERE binding_id = ?
                """,
                (normalized_binding_id,),
            ).fetchone()
        return _binding_from_row(refreshed) if refreshed is not None else None

    def get_binding(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        include_disabled: bool = False,
    ) -> Optional[Binding]:
        normalized_surface_kind = _normalize_text(surface_kind)
        normalized_surface_key = _normalize_text(surface_key)
        if normalized_surface_kind is None or normalized_surface_key is None:
            return None
        query = """
            SELECT *
              FROM orch_bindings
             WHERE surface_kind = ?
               AND surface_key = ?
        """
        params: list[Any] = [normalized_surface_kind, normalized_surface_key]
        if not include_disabled:
            query += " AND disabled_at IS NULL"
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT 1"
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(query, params).fetchone()
        return _binding_from_row(row) if row is not None else None

    def list_bindings(
        self,
        *,
        repo_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        surface_kind: Optional[str] = None,
        include_disabled: bool = False,
        limit: int = 200,
    ) -> list[Binding]:
        filters: list[str] = []
        params: list[Any] = []
        if not include_disabled:
            filters.append("b.disabled_at IS NULL")
        normalized_repo_id = _normalize_text(repo_id)
        if normalized_repo_id is not None:
            filters.append("COALESCE(b.repo_id, t.repo_id) = ?")
            params.append(normalized_repo_id)
        normalized_agent_id = _normalize_text(agent_id)
        if normalized_agent_id is not None:
            filters.append("COALESCE(b.agent_id, t.agent_id) = ?")
            params.append(normalized_agent_id)
        normalized_surface_kind = _normalize_text(surface_kind)
        if normalized_surface_kind is not None:
            filters.append("b.surface_kind = ?")
            params.append(normalized_surface_kind)
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        with open_orchestration_sqlite(self._hub_root) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    b.binding_id,
                    b.surface_kind,
                    b.surface_key,
                    b.target_id AS thread_target_id,
                    COALESCE(b.agent_id, t.agent_id) AS agent_id,
                    COALESCE(b.repo_id, t.repo_id) AS repo_id,
                    b.mode,
                    b.created_at,
                    b.updated_at,
                    b.disabled_at
                  FROM orch_bindings AS b
             LEFT JOIN orch_thread_targets AS t
                    ON t.thread_target_id = b.target_id
                {where_clause}
              ORDER BY b.updated_at DESC, b.created_at DESC
                 LIMIT ?
                """,
                (*params, max(limit, 1)),
            ).fetchall()
        return [Binding.from_mapping(dict(row)) for row in rows]

    def get_active_thread_for_binding(
        self, *, surface_kind: str, surface_key: str
    ) -> Optional[str]:
        binding = self.get_binding(surface_kind=surface_kind, surface_key=surface_key)
        if binding is None:
            return None
        return binding.thread_target_id

    def list_active_work_summaries(
        self,
        *,
        repo_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ActiveWorkSummary]:
        """Return busy-work summaries for threads with running or queued executions.

        `/bindings/work` is intentionally the "is work currently in flight?"
        view. It excludes active-but-idle threads, completed idle threads, and
        archived threads; callers that need a broader inventory should use a
        different query surface instead of widening this one.
        """

        filters = [
            "t.lifecycle_status != 'archived'",
            "(r.execution_id IS NOT NULL OR COALESCE(q.queued_count, 0) > 0)",
        ]
        params: list[Any] = []
        normalized_repo_id = _normalize_text(repo_id)
        if normalized_repo_id is not None:
            filters.append("t.repo_id = ?")
            params.append(normalized_repo_id)
        normalized_agent_id = _normalize_text(agent_id)
        if normalized_agent_id is not None:
            filters.append("t.agent_id = ?")
            params.append(normalized_agent_id)
        where_clause = " AND ".join(filters)
        with open_orchestration_sqlite(self._hub_root) as conn:
            rows = conn.execute(
                f"""
                WITH running_work AS (
                    SELECT thread_target_id, execution_id
                      FROM (
                            SELECT
                                thread_target_id,
                                execution_id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY thread_target_id
                                    ORDER BY COALESCE(started_at, created_at) DESC,
                                             rowid DESC
                                ) AS running_rank
                              FROM orch_thread_executions
                             WHERE status = 'running'
                      )
                     WHERE running_rank = 1
                ),
                queued_work AS (
                    SELECT
                        e.thread_target_id,
                        COUNT(*) AS queued_count
                      FROM orch_queue_items AS q
                      JOIN orch_thread_executions AS e
                        ON e.execution_id = q.source_key
                     WHERE q.source_kind = 'thread_execution'
                       AND q.state IN ('pending', 'queued', 'waiting')
                       AND e.status = 'queued'
                  GROUP BY e.thread_target_id
                ),
                next_queued_work AS (
                    SELECT thread_target_id, execution_id
                      FROM (
                            SELECT
                                e.thread_target_id,
                                e.execution_id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY e.thread_target_id
                                    ORDER BY COALESCE(q.visible_at, q.created_at) ASC,
                                             q.rowid ASC
                                ) AS queue_rank
                              FROM orch_queue_items AS q
                              JOIN orch_thread_executions AS e
                                ON e.execution_id = q.source_key
                             WHERE q.source_kind = 'thread_execution'
                               AND q.state IN ('pending', 'queued', 'waiting')
                               AND e.status = 'queued'
                      )
                     WHERE queue_rank = 1
                ),
                binding_summary AS (
                    SELECT
                        target_id AS thread_target_id,
                        COUNT(binding_id) AS binding_count,
                        GROUP_CONCAT(DISTINCT surface_kind) AS surface_kinds
                      FROM orch_bindings
                     WHERE disabled_at IS NULL
                  GROUP BY target_id
                )
                SELECT
                    t.thread_target_id,
                    t.agent_id,
                    t.repo_id,
                    t.workspace_root,
                    t.display_name,
                    t.lifecycle_status,
                    t.runtime_status,
                    t.last_message_preview,
                    CASE
                        WHEN r.execution_id IS NOT NULL THEN r.execution_id
                        ELSE nq.execution_id
                    END AS execution_id,
                    CASE
                        WHEN r.execution_id IS NOT NULL THEN 'running'
                        WHEN COALESCE(q.queued_count, 0) > 0 THEN 'queued'
                        ELSE NULL
                    END AS execution_status,
                    COALESCE(q.queued_count, 0) AS queued_count,
                    COALESCE(bs.binding_count, 0) AS binding_count,
                    bs.surface_kinds
                  FROM orch_thread_targets AS t
             LEFT JOIN running_work AS r
                    ON r.thread_target_id = t.thread_target_id
             LEFT JOIN queued_work AS q
                    ON q.thread_target_id = t.thread_target_id
             LEFT JOIN next_queued_work AS nq
                    ON nq.thread_target_id = t.thread_target_id
             LEFT JOIN binding_summary AS bs
                    ON bs.thread_target_id = t.thread_target_id
                 WHERE {where_clause}
              ORDER BY t.updated_at DESC, t.created_at DESC
                 LIMIT ?
                """,
                (*params, max(limit, 1)),
            ).fetchall()
        summaries: list[ActiveWorkSummary] = []
        for row in rows:
            raw_surface_kinds = _normalize_text(row["surface_kinds"])
            surface_kinds = (
                tuple(
                    kind
                    for kind in (
                        item.strip() for item in (raw_surface_kinds or "").split(",")
                    )
                    if kind
                )
                if raw_surface_kinds is not None
                else ()
            )
            summaries.append(
                ActiveWorkSummary(
                    thread_target_id=str(row["thread_target_id"]),
                    agent_id=_normalize_text(row["agent_id"]),
                    repo_id=_normalize_text(row["repo_id"]),
                    workspace_root=_normalize_text(row["workspace_root"]),
                    display_name=_normalize_text(row["display_name"]),
                    lifecycle_status=_normalize_text(row["lifecycle_status"]),
                    runtime_status=_normalize_text(row["runtime_status"]),
                    execution_id=_normalize_text(row["execution_id"]),
                    execution_status=_normalize_text(row["execution_status"]),
                    queued_count=int(row["queued_count"] or 0),
                    message_preview=_normalize_text(row["last_message_preview"]),
                    binding_count=int(row["binding_count"] or 0),
                    surface_kinds=surface_kinds,
                )
            )
        return summaries


__all__ = [
    "ActiveWorkSummary",
    "OrchestrationBindingStore",
]
