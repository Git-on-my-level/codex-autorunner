from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .locks import file_lock
from .orchestration.legacy_backfill_gate import ensure_legacy_orchestration_backfill
from .orchestration.sqlite import (
    open_orchestration_sqlite,
    resolve_orchestration_sqlite_path,
)
from .sqlite_utils import connect_sqlite
from .state_roots import (
    resolve_hub_lifecycle_events_db_path,
    resolve_hub_lifecycle_events_path,
)
from .text_utils import lock_path_for
from .utils import atomic_write

logger = logging.getLogger(__name__)

LIFECYCLE_EVENTS_FILENAME = "lifecycle_events.json"
LIFECYCLE_EVENTS_DB_FILENAME = "lifecycle_events.sqlite3"
LIFECYCLE_EVENTS_LOCK_SUFFIX = ".lock"
LIFECYCLE_EVENTS_MALFORMED_PREFIX = "lifecycle_events.malformed"
TRANSITION_TOKEN_KEY = "transition_token"


class LifecycleEventType(str, Enum):
    FLOW_STARTED = "flow_started"
    FLOW_RESUMED = "flow_resumed"
    FLOW_PAUSED = "flow_paused"
    FLOW_COMPLETED = "flow_completed"
    FLOW_FAILED = "flow_failed"
    FLOW_STOPPED = "flow_stopped"
    DISPATCH_CREATED = "dispatch_created"


@dataclass
class LifecycleEvent:
    event_type: LifecycleEventType
    repo_id: str
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)
    origin: str = "system"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    processed: bool = False
    event_id: str = ""

    def __post_init__(self):
        if not self.event_id:
            import uuid

            object.__setattr__(self, "event_id", str(uuid.uuid4()))


@dataclass
class LifecycleEventAppendResult:
    event: LifecycleEvent
    deduped: bool = False


@dataclass
class LegacyLifecycleLoadResult:
    events: list[LifecycleEvent]
    malformed: bool = False
    raw_text: Optional[str] = None
    error: Optional[str] = None


def default_lifecycle_events_path(hub_root: Path) -> Path:
    return resolve_hub_lifecycle_events_path(hub_root)


def default_lifecycle_events_db_path(hub_root: Path) -> Path:
    return resolve_hub_lifecycle_events_db_path(hub_root)


class _LegacyJsonLifecycleEventStore:
    """Legacy JSON lifecycle events reader used for migration compatibility."""

    def __init__(self, hub_root: Path) -> None:
        self._path = default_lifecycle_events_path(hub_root)

    @property
    def path(self) -> Path:
        return self._path

    def _lock_path(self) -> Path:
        return lock_path_for(self._path)

    @staticmethod
    def _deserialize_event(entry: Any) -> Optional[LifecycleEvent]:
        try:
            if not isinstance(entry, dict):
                return None
            event_type_str = entry.get("event_type")
            if not isinstance(event_type_str, str):
                return None
            try:
                event_type = LifecycleEventType(event_type_str)
            except ValueError:
                return None
            event_id_raw = entry.get("event_id")
            event_id = str(event_id_raw) if isinstance(event_id_raw, str) else ""
            if not event_id:
                import uuid

                event_id = str(uuid.uuid4())
            origin_raw = entry.get("origin")
            origin = (
                str(origin_raw).strip()
                if isinstance(origin_raw, str) and origin_raw.strip()
                else "system"
            )
            data_raw = entry.get("data", {})
            data = dict(data_raw) if isinstance(data_raw, dict) else {}
            timestamp_raw = entry.get("timestamp", "")
            timestamp = str(timestamp_raw) if isinstance(timestamp_raw, str) else ""
            processed = bool(entry.get("processed", False))
            return LifecycleEvent(
                event_type=event_type,
                repo_id=str(entry.get("repo_id", "")),
                run_id=str(entry.get("run_id", "")),
                data=data,
                origin=origin,
                timestamp=timestamp,
                processed=processed,
                event_id=event_id,
            )
        except (TypeError, ValueError, KeyError, AttributeError):
            return None

    def load_with_result(self) -> LegacyLifecycleLoadResult:
        if not self._path.exists():
            return LegacyLifecycleLoadResult(events=[])
        with file_lock(self._lock_path()):
            try:
                raw = self._path.read_text(encoding="utf-8")
            except OSError as exc:
                return LegacyLifecycleLoadResult(
                    events=[],
                    malformed=True,
                    error=f"read_error:{exc}",
                )
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                return LegacyLifecycleLoadResult(
                    events=[],
                    malformed=True,
                    raw_text=raw,
                    error=f"json_decode_error:{exc}",
                )

            entries: list[Any]
            if isinstance(payload, list):
                entries = payload
            elif isinstance(payload, dict):
                legacy_events = payload.get("events")
                if isinstance(legacy_events, list):
                    entries = legacy_events
                else:
                    return LegacyLifecycleLoadResult(
                        events=[],
                        malformed=True,
                        raw_text=raw,
                        error="unsupported_legacy_shape",
                    )
            else:
                return LegacyLifecycleLoadResult(
                    events=[],
                    malformed=True,
                    raw_text=raw,
                    error="unsupported_legacy_payload",
                )

            events: list[LifecycleEvent] = []
            for entry in entries:
                event = self._deserialize_event(entry)
                if event is not None:
                    events.append(event)
            return LegacyLifecycleLoadResult(events=events)


class _SqliteLifecycleEventStore:
    def __init__(self, hub_root: Path, *, initialize_schema: bool = True) -> None:
        self._path = default_lifecycle_events_db_path(hub_root)
        if initialize_schema:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_schema()

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _is_terminal_flow_event(event_type: LifecycleEventType) -> bool:
        return event_type in {
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.FLOW_FAILED,
            LifecycleEventType.FLOW_STOPPED,
        }

    @staticmethod
    def _extract_transition_token(data: dict[str, Any]) -> Optional[str]:
        raw = data.get(TRANSITION_TOKEN_KEY)
        if isinstance(raw, str):
            token = raw.strip()
            if token:
                return token
        return None

    def _semantic_identity(self, event: LifecycleEvent) -> tuple[str, ...]:
        key: tuple[str, ...] = (
            event.event_type.value,
            event.repo_id,
            event.run_id,
        )
        token = self._extract_transition_token(event.data)
        if token:
            key = (*key, token)
        return key

    @staticmethod
    def _parse_duplicate_count(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value if value >= 0 else 0
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
                return parsed if parsed >= 0 else 0
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _resolve_first_seen_at(
        existing: LifecycleEvent,
        event_data: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        first_seen = event_data.get("first_seen_at")
        if isinstance(first_seen, str) and first_seen.strip():
            return first_seen.strip()
        if isinstance(existing.timestamp, str) and existing.timestamp.strip():
            return existing.timestamp.strip()
        return fallback

    def _annotate_duplicate(
        self,
        existing: LifecycleEvent,
        *,
        duplicate_seen_at: str,
    ) -> None:
        data = dict(existing.data or {})
        duplicate_count = self._parse_duplicate_count(data.get("duplicate_count"))
        first_seen_at = self._resolve_first_seen_at(
            existing,
            data,
            fallback=duplicate_seen_at,
        )
        data["duplicate_count"] = duplicate_count + 1
        data["first_seen_at"] = first_seen_at
        data["last_seen_at"] = duplicate_seen_at
        existing.data = data

    def _connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._path)

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lifecycle_events_processed_seq
                ON lifecycle_events (processed, seq)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lifecycle_events_identity
                ON lifecycle_events (event_type, repo_id, run_id)
                """
            )

    @staticmethod
    def _event_to_record(
        event: LifecycleEvent,
    ) -> tuple[str, str, str, str, str, str, str, int]:
        return (
            event.event_id,
            event.event_type.value,
            event.repo_id,
            event.run_id,
            json.dumps(event.data or {}, sort_keys=True),
            event.origin,
            event.timestamp,
            1 if event.processed else 0,
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Optional[LifecycleEvent]:
        try:
            event_type_raw = row["event_type"]
            if not isinstance(event_type_raw, str):
                return None
            event_type = LifecycleEventType(event_type_raw)
            data_raw = row["data_json"]
            data_payload: Any = {}
            if isinstance(data_raw, str):
                try:
                    data_payload = json.loads(data_raw)
                except (json.JSONDecodeError, ValueError):
                    data_payload = {}
            data = dict(data_payload) if isinstance(data_payload, dict) else {}
            return LifecycleEvent(
                event_type=event_type,
                repo_id=str(row["repo_id"]),
                run_id=str(row["run_id"]),
                data=data,
                origin=str(row["origin"]),
                timestamp=str(row["timestamp"]),
                processed=bool(row["processed"]),
                event_id=str(row["event_id"]),
            )
        except (TypeError, ValueError, KeyError):
            return None

    def _load_rows(self, query: str, params: tuple[Any, ...]) -> list[LifecycleEvent]:
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        events: list[LifecycleEvent] = []
        for row in rows:
            event = self._row_to_event(row)
            if event is not None:
                events.append(event)
        return events

    def load(self, *, ensure_exists: bool = True) -> list[LifecycleEvent]:
        if not self._path.exists() and not ensure_exists:
            return []
        return self._load_rows(
            """
            SELECT event_id, event_type, repo_id, run_id, data_json, origin, timestamp, processed
            FROM lifecycle_events
            ORDER BY seq ASC
            """,
            (),
        )

    def save(self, events: list[LifecycleEvent]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM lifecycle_events")
            conn.executemany(
                """
                INSERT INTO lifecycle_events (
                    event_id, event_type, repo_id, run_id, data_json, origin, timestamp, processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._event_to_record(event) for event in events],
            )

    def _find_duplicate_terminal_event(
        self,
        conn: sqlite3.Connection,
        candidate: LifecycleEvent,
    ) -> Optional[LifecycleEvent]:
        if not self._is_terminal_flow_event(candidate.event_type):
            return None
        candidate_key = self._semantic_identity(candidate)
        rows = conn.execute(
            """
            SELECT event_id, event_type, repo_id, run_id, data_json, origin, timestamp, processed
            FROM lifecycle_events
            WHERE event_type = ? AND repo_id = ? AND run_id = ?
            ORDER BY seq ASC
            """,
            (candidate.event_type.value, candidate.repo_id, candidate.run_id),
        ).fetchall()
        for row in rows:
            existing = self._row_to_event(row)
            if existing is None:
                continue
            if self._semantic_identity(existing) == candidate_key:
                return existing
        return None

    def append_with_result(self, event: LifecycleEvent) -> LifecycleEventAppendResult:
        with self._connect() as conn:
            # Serialize duplicate detection + write to avoid concurrent
            # writers inserting semantically identical terminal events.
            conn.execute("BEGIN IMMEDIATE")
            duplicate = self._find_duplicate_terminal_event(conn, event)
            if duplicate is not None:
                seen_at = event.timestamp
                if not isinstance(seen_at, str) or not seen_at.strip():
                    seen_at = datetime.now(timezone.utc).isoformat()
                self._annotate_duplicate(duplicate, duplicate_seen_at=seen_at)
                conn.execute(
                    """
                    UPDATE lifecycle_events
                    SET data_json = ?
                    WHERE event_id = ?
                    """,
                    (
                        json.dumps(duplicate.data or {}, sort_keys=True),
                        duplicate.event_id,
                    ),
                )
                return LifecycleEventAppendResult(event=duplicate, deduped=True)
            conn.execute(
                """
                INSERT INTO lifecycle_events (
                    event_id, event_type, repo_id, run_id, data_json, origin, timestamp, processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._event_to_record(event),
            )
            return LifecycleEventAppendResult(event=event, deduped=False)

    def append(self, event: LifecycleEvent) -> None:
        self.append_with_result(event)

    def update_event(
        self,
        event_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        processed: Optional[bool] = None,
    ) -> Optional[LifecycleEvent]:
        if not event_id:
            return None

        set_clauses: list[str] = []
        params: list[Any] = []
        if data is not None:
            set_clauses.append("data_json = ?")
            params.append(json.dumps(data, sort_keys=True))
        if processed is not None:
            set_clauses.append("processed = ?")
            params.append(1 if processed else 0)
        if not set_clauses:
            return None

        params.append(event_id)

        with self._connect() as conn:
            conn.execute(
                f"UPDATE lifecycle_events SET {', '.join(set_clauses)} WHERE event_id = ?",
                tuple(params),
            )
            row = conn.execute(
                """
                SELECT event_id, event_type, repo_id, run_id, data_json, origin, timestamp, processed
                FROM lifecycle_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_event(row)

    def mark_processed(self, event_id: str) -> Optional[LifecycleEvent]:
        return self.update_event(event_id, processed=True)

    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]:
        safe_limit = max(0, int(limit))
        return self._load_rows(
            """
            SELECT event_id, event_type, repo_id, run_id, data_json, origin, timestamp, processed
            FROM lifecycle_events
            WHERE processed = 0
            ORDER BY seq ASC
            LIMIT ?
            """,
            (safe_limit,),
        )

    def prune_processed(self, *, keep_last: int = 100) -> None:
        safe_keep_last = max(0, int(keep_last))
        with self._connect() as conn:
            if safe_keep_last == 0:
                conn.execute("DELETE FROM lifecycle_events WHERE processed = 1")
                return
            conn.execute(
                """
                DELETE FROM lifecycle_events
                WHERE processed = 1
                  AND seq NOT IN (
                    SELECT seq FROM lifecycle_events
                    WHERE processed = 1
                    ORDER BY seq DESC
                    LIMIT ?
                  )
                """,
                (safe_keep_last,),
            )

    def count(self) -> int:
        if not self._path.exists():
            return 0
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM lifecycle_events").fetchone()
        if row is None:
            return 0
        return int(row["c"])


class _OrchestrationLifecycleEventStore:
    def __init__(self, hub_root: Path) -> None:
        self._hub_root = hub_root
        self._path = resolve_orchestration_sqlite_path(hub_root)

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _is_terminal_flow_event(event_type: LifecycleEventType) -> bool:
        return event_type in {
            LifecycleEventType.FLOW_COMPLETED,
            LifecycleEventType.FLOW_FAILED,
            LifecycleEventType.FLOW_STOPPED,
        }

    @staticmethod
    def _extract_transition_token(data: dict[str, Any]) -> Optional[str]:
        raw = data.get(TRANSITION_TOKEN_KEY)
        if isinstance(raw, str):
            token = raw.strip()
            if token:
                return token
        return None

    def _semantic_identity(self, event: LifecycleEvent) -> tuple[str, ...]:
        key: tuple[str, ...] = (
            event.event_type.value,
            event.repo_id,
            event.run_id,
        )
        token = self._extract_transition_token(event.data)
        if token:
            key = (*key, token)
        return key

    @staticmethod
    def _parse_duplicate_count(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value if value >= 0 else 0
        if isinstance(value, str):
            try:
                parsed = int(value.strip())
                return parsed if parsed >= 0 else 0
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _resolve_first_seen_at(
        existing: LifecycleEvent,
        event_data: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        first_seen = event_data.get("first_seen_at")
        if isinstance(first_seen, str) and first_seen.strip():
            return first_seen.strip()
        if isinstance(existing.timestamp, str) and existing.timestamp.strip():
            return existing.timestamp.strip()
        return fallback

    def _annotate_duplicate(
        self,
        existing: LifecycleEvent,
        *,
        duplicate_seen_at: str,
    ) -> None:
        data = dict(existing.data or {})
        duplicate_count = self._parse_duplicate_count(data.get("duplicate_count"))
        first_seen_at = self._resolve_first_seen_at(
            existing,
            data,
            fallback=duplicate_seen_at,
        )
        data["duplicate_count"] = duplicate_count + 1
        data["first_seen_at"] = first_seen_at
        data["last_seen_at"] = duplicate_seen_at
        existing.data = data

    @staticmethod
    def _event_payload(event: LifecycleEvent) -> str:
        payload = {
            "data": event.data or {},
            "origin": event.origin or "system",
        }
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _row_to_event(row: Any) -> Optional[LifecycleEvent]:
        try:
            event_type_raw = row["event_type"]
            if not isinstance(event_type_raw, str):
                return None
            event_type = LifecycleEventType(event_type_raw)
            payload_raw = row["payload_json"]
            payload_data: Any = {}
            if isinstance(payload_raw, str):
                try:
                    payload_data = json.loads(payload_raw)
                except (json.JSONDecodeError, ValueError):
                    payload_data = {}
            payload = dict(payload_data) if isinstance(payload_data, dict) else {}
            data_raw = payload.get("data")
            data = dict(data_raw) if isinstance(data_raw, dict) else {}
            origin_raw = payload.get("origin")
            origin = (
                str(origin_raw).strip()
                if isinstance(origin_raw, str) and origin_raw.strip()
                else "system"
            )
            return LifecycleEvent(
                event_type=event_type,
                repo_id=str(row["repo_id"] or ""),
                run_id=str(row["run_id"] or ""),
                data=data,
                origin=origin,
                timestamp=str(row["timestamp"] or ""),
                processed=bool(row["processed"]),
                event_id=str(row["event_id"] or ""),
            )
        except (TypeError, ValueError, KeyError):
            return None

    def _load_rows(self, query: str, params: tuple[Any, ...]) -> list[LifecycleEvent]:
        with open_orchestration_sqlite(self._hub_root) as conn:
            rows = conn.execute(query, params).fetchall()
        events: list[LifecycleEvent] = []
        for row in rows:
            event = self._row_to_event(row)
            if event is not None:
                events.append(event)
        return events

    def load(self, *, ensure_exists: bool = True) -> list[LifecycleEvent]:
        _ = ensure_exists
        return self._load_rows(
            """
            SELECT event_id, event_type, repo_id, run_id, payload_json, timestamp, processed
              FROM orch_event_projections
             WHERE event_family = 'lifecycle'
             ORDER BY rowid ASC
            """,
            (),
        )

    def save(self, events: list[LifecycleEvent]) -> None:
        with open_orchestration_sqlite(self._hub_root) as conn:
            conn.execute(
                "DELETE FROM orch_event_projections WHERE event_family = 'lifecycle'"
            )
            conn.executemany(
                """
                INSERT INTO orch_event_projections (
                    event_id,
                    event_family,
                    event_type,
                    target_kind,
                    target_id,
                    execution_id,
                    repo_id,
                    run_id,
                    timestamp,
                    status,
                    payload_json,
                    processed
                ) VALUES (?, 'lifecycle', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.event_id,
                        event.event_type.value,
                        "flow_run",
                        event.run_id,
                        None,
                        event.repo_id,
                        event.run_id,
                        event.timestamp,
                        event.event_type.value,
                        self._event_payload(event),
                        1 if event.processed else 0,
                    )
                    for event in events
                ],
            )

    def _find_duplicate_terminal_event(
        self,
        conn: sqlite3.Connection,
        candidate: LifecycleEvent,
    ) -> Optional[LifecycleEvent]:
        if not self._is_terminal_flow_event(candidate.event_type):
            return None
        candidate_key = self._semantic_identity(candidate)
        rows = conn.execute(
            """
            SELECT event_id, event_type, repo_id, run_id, payload_json, timestamp, processed
              FROM orch_event_projections
             WHERE event_family = 'lifecycle'
               AND event_type = ?
               AND repo_id = ?
               AND run_id = ?
             ORDER BY rowid ASC
            """,
            (candidate.event_type.value, candidate.repo_id, candidate.run_id),
        ).fetchall()
        for row in rows:
            existing = self._row_to_event(row)
            if (
                existing is not None
                and self._semantic_identity(existing) == candidate_key
            ):
                return existing
        return None

    def append_with_result(self, event: LifecycleEvent) -> LifecycleEventAppendResult:
        with open_orchestration_sqlite(self._hub_root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = self._find_duplicate_terminal_event(conn, event)
            if duplicate is not None:
                seen_at = event.timestamp
                if not isinstance(seen_at, str) or not seen_at.strip():
                    seen_at = datetime.now(timezone.utc).isoformat()
                self._annotate_duplicate(duplicate, duplicate_seen_at=seen_at)
                conn.execute(
                    """
                    UPDATE orch_event_projections
                       SET payload_json = ?
                     WHERE event_family = 'lifecycle'
                       AND event_id = ?
                    """,
                    (
                        self._event_payload(duplicate),
                        duplicate.event_id,
                    ),
                )
                return LifecycleEventAppendResult(event=duplicate, deduped=True)
            conn.execute(
                """
                INSERT INTO orch_event_projections (
                    event_id,
                    event_family,
                    event_type,
                    target_kind,
                    target_id,
                    execution_id,
                    repo_id,
                    run_id,
                    timestamp,
                    status,
                    payload_json,
                    processed
                ) VALUES (?, 'lifecycle', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type.value,
                    "flow_run",
                    event.run_id,
                    None,
                    event.repo_id,
                    event.run_id,
                    event.timestamp,
                    event.event_type.value,
                    self._event_payload(event),
                    1 if event.processed else 0,
                ),
            )
            return LifecycleEventAppendResult(event=event, deduped=False)

    def append(self, event: LifecycleEvent) -> None:
        self.append_with_result(event)

    def update_event(
        self,
        event_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        processed: Optional[bool] = None,
    ) -> Optional[LifecycleEvent]:
        if not event_id:
            return None
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT event_id, event_type, repo_id, run_id, payload_json, timestamp, processed
                  FROM orch_event_projections
                 WHERE event_family = 'lifecycle'
                   AND event_id = ?
                """,
                (event_id,),
            ).fetchone()
            event = self._row_to_event(row)
            if event is None:
                return None
            if data is not None:
                event.data = data
            if processed is not None:
                event.processed = processed
            conn.execute(
                """
                UPDATE orch_event_projections
                   SET payload_json = ?,
                       processed = ?
                 WHERE event_family = 'lifecycle'
                   AND event_id = ?
                """,
                (
                    self._event_payload(event),
                    1 if event.processed else 0,
                    event_id,
                ),
            )
        return event

    def mark_processed(self, event_id: str) -> Optional[LifecycleEvent]:
        return self.update_event(event_id, processed=True)

    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]:
        safe_limit = max(0, int(limit))
        return self._load_rows(
            """
            SELECT event_id, event_type, repo_id, run_id, payload_json, timestamp, processed
              FROM orch_event_projections
             WHERE event_family = 'lifecycle'
               AND processed = 0
             ORDER BY rowid ASC
             LIMIT ?
            """,
            (safe_limit,),
        )

    def prune_processed(self, *, keep_last: int = 100) -> None:
        safe_keep_last = max(0, int(keep_last))
        with open_orchestration_sqlite(self._hub_root) as conn:
            if safe_keep_last == 0:
                conn.execute(
                    """
                    DELETE FROM orch_event_projections
                     WHERE event_family = 'lifecycle'
                       AND processed = 1
                    """
                )
                return
            conn.execute(
                """
                DELETE FROM orch_event_projections
                 WHERE event_family = 'lifecycle'
                   AND processed = 1
                   AND rowid NOT IN (
                        SELECT rowid
                          FROM orch_event_projections
                         WHERE event_family = 'lifecycle'
                           AND processed = 1
                         ORDER BY rowid DESC
                         LIMIT ?
                   )
                """,
                (safe_keep_last,),
            )

    def count(self) -> int:
        with open_orchestration_sqlite(self._hub_root) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                  FROM orch_event_projections
                 WHERE event_family = 'lifecycle'
                """
            ).fetchone()
        if row is None:
            return 0
        return int(row["c"] or 0)


def _quarantine_malformed_lifecycle_events_json(
    hub_root: Path,
    *,
    raw_text: Optional[str],
    error: Optional[str],
) -> None:
    legacy_path = default_lifecycle_events_path(hub_root)
    if not legacy_path.exists():
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine_path = legacy_path.with_name(
        f"{LIFECYCLE_EVENTS_MALFORMED_PREFIX}.{timestamp}.json"
    )
    try:
        legacy_path.rename(quarantine_path)
    except OSError:
        if raw_text is not None:
            try:
                atomic_write(quarantine_path, raw_text)
                legacy_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Failed to quarantine malformed lifecycle events file at %s",
                    legacy_path,
                )
                return
        else:
            logger.warning(
                "Malformed lifecycle events file at %s could not be quarantined",
                legacy_path,
            )
            return
    logger.warning(
        "Quarantined malformed lifecycle events file: %s (reason=%s)",
        quarantine_path,
        error or "unknown",
    )


def migrate_legacy_lifecycle_event_sources_if_needed(hub_root: Path) -> bool:
    """Copy legacy lifecycle sqlite/JSON stores into orchestration DB when it is still empty.

    Returns False if JSON migration was aborted (malformed file not fully handled); the
    orchestration backfill gate must not record completion in that case so a later process
    can retry.
    """
    legacy_store = _LegacyJsonLifecycleEventStore(hub_root)
    legacy_sqlite_store = _SqliteLifecycleEventStore(hub_root, initialize_schema=False)
    orchestration_store = _OrchestrationLifecycleEventStore(hub_root)
    if orchestration_store.count() > 0:
        return True
    migrated = 0

    legacy_sqlite_events = legacy_sqlite_store.load(ensure_exists=False)
    if legacy_sqlite_events:
        logger.info(
            "Migrating %s legacy lifecycle events from %s to %s",
            len(legacy_sqlite_events),
            legacy_sqlite_store.path,
            orchestration_store.path,
        )
        for event in legacy_sqlite_events:
            orchestration_store.append_with_result(event)
            migrated += 1

    legacy_path = legacy_store.path
    if legacy_path.exists():
        result = legacy_store.load_with_result()
        if result.malformed:
            _quarantine_malformed_lifecycle_events_json(
                hub_root,
                raw_text=result.raw_text,
                error=result.error,
            )
            return False
        if result.events:
            logger.info(
                "Migrating %s legacy lifecycle JSON events from %s to %s",
                len(result.events),
                legacy_path,
                orchestration_store.path,
            )
            for event in result.events:
                orchestration_store.append_with_result(event)
                migrated += 1
    return True


class LifecycleEventStore:
    """Lifecycle event store facade using orchestration SQLite with legacy migration."""

    def __init__(self, hub_root: Path) -> None:
        ensure_legacy_orchestration_backfill(hub_root)
        self._hub_root = hub_root
        self._legacy_store = _LegacyJsonLifecycleEventStore(hub_root)
        self._legacy_sqlite_store = _SqliteLifecycleEventStore(
            hub_root, initialize_schema=False
        )
        self._orchestration_store = _OrchestrationLifecycleEventStore(hub_root)

    @property
    def path(self) -> Path:
        return self._orchestration_store.path

    @property
    def legacy_path(self) -> Path:
        return self._legacy_store.path

    def load(self, *, ensure_exists: bool = True) -> list[LifecycleEvent]:
        return self._orchestration_store.load(ensure_exists=ensure_exists)

    def save(self, events: list[LifecycleEvent]) -> None:
        self._orchestration_store.save(events)

    def append_with_result(self, event: LifecycleEvent) -> LifecycleEventAppendResult:
        return self._orchestration_store.append_with_result(event)

    def append(self, event: LifecycleEvent) -> None:
        self._orchestration_store.append(event)

    def update_event(
        self,
        event_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        processed: Optional[bool] = None,
    ) -> Optional[LifecycleEvent]:
        return self._orchestration_store.update_event(
            event_id,
            data=data,
            processed=processed,
        )

    def mark_processed(self, event_id: str) -> Optional[LifecycleEvent]:
        return self._orchestration_store.mark_processed(event_id)

    def get_unprocessed(self, *, limit: int = 100) -> list[LifecycleEvent]:
        return self._orchestration_store.get_unprocessed(limit=limit)

    def prune_processed(self, *, keep_last: int = 100) -> None:
        self._orchestration_store.prune_processed(keep_last=keep_last)


class LifecycleEventEmitter:
    def __init__(self, hub_root: Path) -> None:
        self._store = LifecycleEventStore(hub_root)
        self._listeners: list[Callable[[LifecycleEvent], None]] = []

    def emit(self, event: LifecycleEvent) -> str:
        append_result = self._store.append_with_result(event)
        if append_result.deduped:
            return append_result.event.event_id
        for listener in self._listeners:
            try:
                listener(append_result.event)
            except (
                Exception
            ) as exc:  # intentional: listener errors must not break emission
                logger.exception("Error in lifecycle event listener: %s", exc)
        return append_result.event.event_id

    def emit_flow_started(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_STARTED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_resumed(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_RESUMED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_paused(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_PAUSED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_completed(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_COMPLETED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_failed(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_FAILED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_flow_stopped(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.FLOW_STOPPED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def emit_dispatch_created(
        self,
        repo_id: str,
        run_id: str,
        *,
        data: Optional[dict[str, Any]] = None,
        origin: str = "system",
    ) -> str:
        event = LifecycleEvent(
            event_type=LifecycleEventType.DISPATCH_CREATED,
            repo_id=repo_id,
            run_id=run_id,
            data=data or {},
            origin=origin,
        )
        return self.emit(event)

    def add_listener(self, listener: Callable[[LifecycleEvent], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[LifecycleEvent], None]) -> None:
        self._listeners = [lst for lst in self._listeners if lst != listener]


__all__ = [
    "LifecycleEvent",
    "LifecycleEventEmitter",
    "LifecycleEventStore",
    "LifecycleEventType",
    "default_lifecycle_events_db_path",
    "default_lifecycle_events_path",
]
