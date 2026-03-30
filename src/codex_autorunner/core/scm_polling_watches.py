from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .orchestration.sqlite import open_orchestration_sqlite
from .time_utils import now_iso

_WATCH_STATES = frozenset({"active", "expired", "closed", "error"})


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_int(value: Any, *, field_name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if normalized <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return normalized


def _normalize_limit(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_state(value: Any, *, field_name: str = "state") -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    if normalized not in _WATCH_STATES:
        allowed = ", ".join(sorted(_WATCH_STATES))
        raise ValueError(f"{field_name} must be one of: {allowed}")
    return normalized


def _normalize_timestamp(value: Any, *, field_name: str) -> str:
    normalized = _normalize_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{field_name} must be a JSON object")


def _json_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _json_loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class ScmPollingWatch:
    watch_id: str
    provider: str
    binding_id: str
    repo_slug: str
    pr_number: int
    workspace_root: str
    poll_interval_seconds: int
    state: str
    started_at: str
    updated_at: str
    expires_at: str
    next_poll_at: str
    repo_id: Optional[str] = None
    thread_target_id: Optional[str] = None
    last_polled_at: Optional[str] = None
    last_error_text: Optional[str] = None
    reaction_config: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _watch_from_row(row: sqlite3.Row) -> ScmPollingWatch:
    return ScmPollingWatch(
        watch_id=str(row["watch_id"]),
        provider=str(row["provider"]),
        binding_id=str(row["binding_id"]),
        repo_slug=str(row["repo_slug"]),
        pr_number=_normalize_int(row["pr_number"], field_name="pr_number"),
        workspace_root=str(row["workspace_root"]),
        poll_interval_seconds=_normalize_int(
            row["poll_interval_seconds"], field_name="poll_interval_seconds"
        ),
        state=_normalize_state(row["state"]),
        started_at=str(row["started_at"]),
        updated_at=str(row["updated_at"]),
        expires_at=str(row["expires_at"]),
        next_poll_at=str(row["next_poll_at"]),
        repo_id=_normalize_text(row["repo_id"]),
        thread_target_id=_normalize_text(row["thread_target_id"]),
        last_polled_at=_normalize_text(row["last_polled_at"]),
        last_error_text=_normalize_text(row["last_error_text"]),
        reaction_config=_json_loads_object(row["reaction_config_json"]),
        snapshot=_json_loads_object(row["snapshot_json"]),
    )


class ScmPollingWatchStore:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def upsert_watch(
        self,
        *,
        provider: str,
        binding_id: str,
        repo_slug: str,
        pr_number: int,
        workspace_root: str,
        poll_interval_seconds: int,
        next_poll_at: str,
        expires_at: str,
        repo_id: Optional[str] = None,
        thread_target_id: Optional[str] = None,
        reaction_config: Optional[dict[str, Any]] = None,
        snapshot: Optional[dict[str, Any]] = None,
    ) -> ScmPollingWatch:
        normalized_provider = _normalize_text(provider)
        normalized_binding_id = _normalize_text(binding_id)
        normalized_repo_slug = _normalize_text(repo_slug)
        normalized_workspace_root = _normalize_text(workspace_root)
        normalized_pr_number = _normalize_int(pr_number, field_name="pr_number")
        normalized_poll_interval = _normalize_int(
            poll_interval_seconds,
            field_name="poll_interval_seconds",
        )
        normalized_next_poll_at = _normalize_timestamp(
            next_poll_at,
            field_name="next_poll_at",
        )
        normalized_expires_at = _normalize_timestamp(
            expires_at,
            field_name="expires_at",
        )
        normalized_repo_id = _normalize_text(repo_id)
        normalized_thread_target_id = _normalize_text(thread_target_id)
        reaction_config_object = _normalize_json_object(
            reaction_config,
            field_name="reaction_config",
        )
        snapshot_object = _normalize_json_object(snapshot, field_name="snapshot")
        if normalized_provider is None:
            raise ValueError("provider is required")
        if normalized_binding_id is None:
            raise ValueError("binding_id is required")
        if normalized_repo_slug is None:
            raise ValueError("repo_slug is required")
        if normalized_workspace_root is None:
            raise ValueError("workspace_root is required")

        timestamp = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = self._load_watch_row(
                conn,
                provider=normalized_provider,
                binding_id=normalized_binding_id,
            )
            if row is None:
                watch_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO orch_scm_polling_watches (
                        watch_id,
                        provider,
                        binding_id,
                        repo_slug,
                        repo_id,
                        pr_number,
                        workspace_root,
                        thread_target_id,
                        poll_interval_seconds,
                        state,
                        started_at,
                        updated_at,
                        expires_at,
                        next_poll_at,
                        last_polled_at,
                        last_error_text,
                        reaction_config_json,
                        snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        watch_id,
                        normalized_provider,
                        normalized_binding_id,
                        normalized_repo_slug,
                        normalized_repo_id,
                        normalized_pr_number,
                        normalized_workspace_root,
                        normalized_thread_target_id,
                        normalized_poll_interval,
                        timestamp,
                        timestamp,
                        normalized_expires_at,
                        normalized_next_poll_at,
                        _json_dumps(reaction_config_object),
                        _json_dumps(snapshot_object),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE orch_scm_polling_watches
                       SET repo_slug = ?,
                           repo_id = COALESCE(?, repo_id),
                           pr_number = ?,
                           workspace_root = ?,
                           thread_target_id = CASE
                               WHEN ? IS NULL THEN thread_target_id
                               ELSE ?
                           END,
                           poll_interval_seconds = ?,
                           state = 'active',
                           updated_at = ?,
                           expires_at = ?,
                           next_poll_at = ?,
                           last_error_text = NULL,
                           reaction_config_json = ?,
                           snapshot_json = ?
                     WHERE watch_id = ?
                    """,
                    (
                        normalized_repo_slug,
                        normalized_repo_id,
                        normalized_pr_number,
                        normalized_workspace_root,
                        normalized_thread_target_id,
                        normalized_thread_target_id,
                        normalized_poll_interval,
                        timestamp,
                        normalized_expires_at,
                        normalized_next_poll_at,
                        _json_dumps(reaction_config_object),
                        _json_dumps(snapshot_object),
                        str(row["watch_id"]),
                    ),
                )
            refreshed = self._load_watch_row(
                conn,
                provider=normalized_provider,
                binding_id=normalized_binding_id,
            )
        if refreshed is None:
            raise RuntimeError("SCM polling watch row missing after upsert")
        return _watch_from_row(refreshed)

    def list_due_watches(
        self,
        *,
        provider: Optional[str] = None,
        now_timestamp: Optional[str] = None,
        limit: int = 50,
    ) -> list[ScmPollingWatch]:
        resolved_limit = _normalize_limit(limit, default=50)
        if resolved_limit <= 0:
            return []

        current_timestamp = _normalize_timestamp(
            now_timestamp or now_iso(),
            field_name="now_timestamp",
        )
        where_clauses = ["state = 'active'", "next_poll_at <= ?"]
        params: list[Any] = [current_timestamp]

        normalized_provider = _normalize_text(provider)
        if normalized_provider is not None:
            where_clauses.append("provider = ?")
            params.append(normalized_provider)

        params.append(resolved_limit)
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE {' AND '.join(where_clauses)}
                 ORDER BY next_poll_at ASC, started_at ASC, watch_id ASC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_watch_from_row(row) for row in rows]

    def refresh_watch(
        self,
        *,
        watch_id: str,
        snapshot: Optional[dict[str, Any]] = None,
        next_poll_at: Optional[str] = None,
        expires_at: Optional[str] = None,
        last_polled_at: Optional[str] = None,
        last_error_text: Optional[str] = None,
    ) -> Optional[ScmPollingWatch]:
        normalized_watch_id = _normalize_text(watch_id)
        if normalized_watch_id is None:
            raise ValueError("watch_id is required")
        snapshot_object = _normalize_json_object(snapshot, field_name="snapshot")
        snapshot_json = _json_dumps(snapshot_object) if snapshot is not None else None
        has_snapshot = 1 if snapshot is not None else None
        normalized_next_poll_at = (
            _normalize_timestamp(next_poll_at, field_name="next_poll_at")
            if next_poll_at is not None
            else None
        )
        normalized_expires_at = (
            _normalize_timestamp(expires_at, field_name="expires_at")
            if expires_at is not None
            else None
        )
        normalized_last_polled_at = (
            _normalize_timestamp(last_polled_at, field_name="last_polled_at")
            if last_polled_at is not None
            else None
        )
        normalized_last_error_text = _normalize_text(last_error_text)
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            cursor = conn.execute(
                """
                UPDATE orch_scm_polling_watches
                   SET updated_at = ?,
                       snapshot_json = CASE
                           WHEN ? IS NULL THEN snapshot_json
                           ELSE ?
                       END,
                       next_poll_at = COALESCE(?, next_poll_at),
                       expires_at = COALESCE(?, expires_at),
                       last_polled_at = COALESCE(?, last_polled_at),
                       last_error_text = ?,
                       state = CASE
                           WHEN state = 'active' THEN 'active'
                           ELSE state
                       END
                 WHERE watch_id = ?
                """,
                (
                    timestamp,
                    has_snapshot,
                    snapshot_json,
                    normalized_next_poll_at,
                    normalized_expires_at,
                    normalized_last_polled_at,
                    normalized_last_error_text,
                    normalized_watch_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE watch_id = ?
                """,
                (normalized_watch_id,),
            ).fetchone()
        return _watch_from_row(row) if row is not None else None

    def close_watch(
        self,
        *,
        watch_id: str,
        state: str,
        last_error_text: Optional[str] = None,
    ) -> Optional[ScmPollingWatch]:
        normalized_watch_id = _normalize_text(watch_id)
        normalized_state = _normalize_state(state)
        normalized_last_error_text = _normalize_text(last_error_text)
        if normalized_watch_id is None:
            raise ValueError("watch_id is required")
        timestamp = now_iso()

        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            cursor = conn.execute(
                """
                UPDATE orch_scm_polling_watches
                   SET state = ?,
                       updated_at = ?,
                       last_error_text = ?
                 WHERE watch_id = ?
                """,
                (
                    normalized_state,
                    timestamp,
                    normalized_last_error_text,
                    normalized_watch_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT *
                  FROM orch_scm_polling_watches
                 WHERE watch_id = ?
                """,
                (normalized_watch_id,),
            ).fetchone()
        return _watch_from_row(row) if row is not None else None

    def _load_watch_row(
        self,
        conn: sqlite3.Connection,
        *,
        provider: str,
        binding_id: str,
    ) -> Optional[sqlite3.Row]:
        row = conn.execute(
            """
            SELECT *
              FROM orch_scm_polling_watches
             WHERE provider = ?
               AND binding_id = ?
             LIMIT 1
            """,
            (provider, binding_id),
        ).fetchone()
        return row if isinstance(row, sqlite3.Row) else None


__all__ = ["ScmPollingWatch", "ScmPollingWatchStore"]
