from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .sqlite_utils import open_sqlite
from .state_roots import resolve_hub_projection_db_path
from .time_utils import now_iso

logger = logging.getLogger("codex_autorunner.core.hub_projection_store")

_CACHE_TABLE = "projection_cache"
_EVENT_TABLE = "hub_ui_events"
_READ_MODEL_TABLE = "hub_screen_read_models"
_META_TABLE = "hub_projection_metadata"

REPO_RUNTIME_PROJECTION_NAMESPACE = "repo_runtime_v1"
HUB_LISTING_PROJECTION_NAMESPACE = "hub_listing_v1"
CHAT_BINDING_PROJECTION_NAMESPACE = "chat_binding_counts_v1"
CHAT_BINDING_PROJECTION_KEY = "active_by_source"
HUB_SNAPSHOT_PROJECTION_NAMESPACE = "hub_snapshot_v1"
REPO_CAPABILITY_HINT_PROJECTION_NAMESPACE = "repo_capability_hints_v1"
ENTITY_STATE_PROJECTION_FAMILY = "entity_state_v1"

HUB_PROJECTION_SCHEMA_VERSION = 1
HUB_UI_EVENT_READ_LIMIT_MAX = 1000


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _current_utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _parse_iso_ts(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def path_stat_fingerprint(path: Path) -> tuple[bool, Optional[int], Optional[int]]:
    try:
        stat = path.stat()
    except OSError:
        return (False, None, None)
    return (True, int(stat.st_mtime_ns), int(stat.st_size))


@dataclass(frozen=True)
class HubUiEvent:
    """Typed hub UI event with a durable, monotonic cursor."""

    cursor: int
    idempotency_key: str
    entity_kind: str
    entity_id: str
    operation: str
    payload: dict[str, Any] = field(default_factory=dict)
    source_revision: Optional[str] = None
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HubUiEventAppendResult:
    event: HubUiEvent
    inserted: bool


@dataclass(frozen=True)
class HubUiEventBatch:
    events: list[HubUiEvent]
    after_cursor: int
    latest_cursor: int
    gap_detected: bool
    next_cursor: int
    limit: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [event.to_dict() for event in self.events],
            "after_cursor": self.after_cursor,
            "latest_cursor": self.latest_cursor,
            "gap_detected": self.gap_detected,
            "next_cursor": self.next_cursor,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class HubProjectionSnapshot:
    family: str
    key: str
    revision: int
    cursor: int
    source_revision: Optional[str]
    payload: dict[str, Any]
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HubProjectionStore:
    def __init__(self, hub_root: Path, *, durable: bool = False) -> None:
        self._db_path = resolve_hub_projection_db_path(hub_root)
        self._durable = durable

    @property
    def path(self) -> Path:
        return self._db_path

    @classmethod
    def from_hub_root(cls, hub_root: Path) -> "HubProjectionStore":
        return cls(hub_root)

    def _ensure_schema(self, conn: Any) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_CACHE_TABLE} (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(namespace, cache_key)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_META_TABLE} (
                meta_key TEXT PRIMARY KEY,
                meta_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_EVENT_TABLE} (
                cursor INTEGER PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                entity_kind TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{{}}',
                source_revision TEXT,
                generated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_READ_MODEL_TABLE} (
                family TEXT NOT NULL,
                read_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                cursor INTEGER NOT NULL,
                source_revision TEXT,
                payload_json TEXT NOT NULL DEFAULT '{{}}',
                rebuilt_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(family, read_key)
            )
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{_EVENT_TABLE}_entity_cursor
                ON {_EVENT_TABLE}(entity_kind, entity_id, cursor)
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{_EVENT_TABLE}_generated_cursor
                ON {_EVENT_TABLE}(generated_at, cursor)
            """
        )
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{_READ_MODEL_TABLE}_family_cursor
                ON {_READ_MODEL_TABLE}(family, cursor, read_key)
            """
        )
        conn.execute(
            f"""
            INSERT INTO {_META_TABLE} (meta_key, meta_value, updated_at)
            VALUES ('schema_version', ?, ?)
            ON CONFLICT(meta_key) DO UPDATE SET
                meta_value = excluded.meta_value,
                updated_at = excluded.updated_at
            """,
            (str(HUB_PROJECTION_SCHEMA_VERSION), now_iso()),
        )

    def prepare_schema(self) -> None:
        """Create or validate all durable projection tables."""

        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            self.validate_schema(conn)

    def validate_schema(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Raise when projection storage is too stale/corrupt to trust."""

        required: dict[str, set[str]] = {
            _CACHE_TABLE: {"namespace", "cache_key", "fingerprint", "payload"},
            _META_TABLE: {"meta_key", "meta_value", "updated_at"},
            _EVENT_TABLE: {
                "cursor",
                "idempotency_key",
                "entity_kind",
                "entity_id",
                "operation",
                "payload_json",
                "source_revision",
                "generated_at",
            },
            _READ_MODEL_TABLE: {
                "family",
                "read_key",
                "revision",
                "cursor",
                "source_revision",
                "payload_json",
                "updated_at",
            },
        }

        def _validate(open_conn: sqlite3.Connection) -> None:
            for table_name, expected_columns in required.items():
                rows = open_conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                actual_columns = {str(row["name"]) for row in rows}
                missing = expected_columns - actual_columns
                if missing:
                    raise RuntimeError(
                        "hub projection store schema is missing "
                        f"{table_name}.{sorted(missing)}"
                    )
            row = open_conn.execute(
                f"SELECT meta_value FROM {_META_TABLE} WHERE meta_key = 'schema_version'"
            ).fetchone()
            version = int(row["meta_value"]) if row is not None else 0
            if version != HUB_PROJECTION_SCHEMA_VERSION:
                raise RuntimeError(
                    "hub projection store schema version "
                    f"{version} != {HUB_PROJECTION_SCHEMA_VERSION}"
                )

        if conn is not None:
            _validate(conn)
            return
        with open_sqlite(self._db_path, durable=self._durable) as owned_conn:
            self._ensure_schema(owned_conn)
            _validate(owned_conn)

    def reset_projection_state(self) -> None:
        """Clear rebuildable read models and events after validation failure."""

        with open_sqlite(self._db_path, durable=self._durable) as conn:
            for index_name in (
                f"idx_{_EVENT_TABLE}_entity_cursor",
                f"idx_{_EVENT_TABLE}_generated_cursor",
                f"idx_{_READ_MODEL_TABLE}_family_cursor",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {index_name}")
            for table_name in (_READ_MODEL_TABLE, _EVENT_TABLE, _META_TABLE):
                conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            self._ensure_schema(conn)
            conn.execute(f"DELETE FROM {_READ_MODEL_TABLE}")
            conn.execute(f"DELETE FROM {_EVENT_TABLE}")
            conn.execute(
                f"""
                INSERT INTO {_META_TABLE} (meta_key, meta_value, updated_at)
                VALUES ('last_rebuild_reason', 'reset_projection_state', ?)
                ON CONFLICT(meta_key) DO UPDATE SET
                    meta_value = excluded.meta_value,
                    updated_at = excluded.updated_at
                """,
                (now_iso(),),
            )

    def validate_or_reset_projection_state(self) -> None:
        try:
            self.prepare_schema()
        except (sqlite3.Error, RuntimeError, OSError, ValueError):
            logger.warning(
                "Hub projection store validation failed; resetting rebuildable state",
                exc_info=True,
            )
            self.reset_projection_state()

    def get_cache(
        self,
        cache_key: str,
        fingerprint: Any,
        *,
        max_age_seconds: Optional[float] = None,
        namespace: str = "default",
    ) -> Any | None:
        fingerprint_text = _stable_json(fingerprint)
        try:
            with open_sqlite(self._db_path, durable=self._durable) as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"""
                    SELECT fingerprint, payload, updated_at
                      FROM {_CACHE_TABLE}
                     WHERE namespace = ? AND cache_key = ?
                    """,
                    (namespace, cache_key),
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "Failed reading hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )
            return None
        if row is None or str(row["fingerprint"]) != fingerprint_text:
            return None
        if max_age_seconds is not None:
            updated_at_ts = _parse_iso_ts(row["updated_at"])
            if updated_at_ts is None:
                return None
            if (_current_utc_ts() - updated_at_ts) > max(0.0, float(max_age_seconds)):
                return None
        try:
            return json.loads(str(row["payload"]))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed decoding hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )
            return None

    def set_cache(
        self,
        cache_key: str,
        fingerprint: Any,
        payload: Any,
        *,
        namespace: str = "default",
    ) -> None:
        fingerprint_text = _stable_json(fingerprint)
        payload_text = _stable_json(payload)
        try:
            with open_sqlite(self._db_path, durable=self._durable) as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"""
                    INSERT INTO {_CACHE_TABLE} (
                        namespace,
                        cache_key,
                        fingerprint,
                        payload,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, cache_key) DO UPDATE SET
                        fingerprint = excluded.fingerprint,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        namespace,
                        cache_key,
                        fingerprint_text,
                        payload_text,
                        now_iso(),
                    ),
                )
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed writing hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )

    def invalidate_cache(self, cache_key: str, *, namespace: str = "default") -> None:
        try:
            with open_sqlite(self._db_path, durable=self._durable) as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"DELETE FROM {_CACHE_TABLE} WHERE namespace = ? AND cache_key = ?",
                    (namespace, cache_key),
                )
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "Failed deleting hub projection cache namespace=%s key=%s: %s",
                namespace,
                cache_key,
                exc,
            )

    def get(
        self,
        *,
        namespace: str,
        key: str,
        fingerprint: Any,
        max_age_seconds: Optional[float] = None,
    ) -> Any | None:
        return self.get_cache(
            key,
            fingerprint,
            namespace=namespace,
            max_age_seconds=max_age_seconds,
        )

    def put(
        self,
        *,
        namespace: str,
        key: str,
        fingerprint: Any,
        payload: Any,
    ) -> None:
        self.set_cache(key, fingerprint, payload, namespace=namespace)

    def delete(self, *, namespace: str, key: Optional[str] = None) -> None:
        if key is None:
            try:
                with open_sqlite(self._db_path, durable=self._durable) as conn:
                    self._ensure_schema(conn)
                    conn.execute(
                        f"DELETE FROM {_CACHE_TABLE} WHERE namespace = ?",
                        (namespace,),
                    )
            except (sqlite3.Error, OSError) as exc:
                logger.warning(
                    "Failed deleting hub projection cache namespace=%s key=%s: %s",
                    namespace,
                    key,
                    exc,
                )
            return
        self.invalidate_cache(key, namespace=namespace)

    def append_event(
        self,
        *,
        idempotency_key: str,
        entity_kind: str,
        entity_id: str,
        operation: str,
        payload: Optional[Mapping[str, Any]] = None,
        source_revision: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> HubUiEventAppendResult:
        normalized_key = _required_text(idempotency_key, "idempotency_key")
        normalized_kind = _required_text(entity_kind, "entity_kind")
        normalized_id = _required_text(entity_id, "entity_id")
        normalized_operation = _required_text(operation, "operation")
        payload_json = _stable_json(dict(payload or {}))
        timestamp = generated_at or now_iso()
        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            with conn:
                existing = conn.execute(
                    f"SELECT * FROM {_EVENT_TABLE} WHERE idempotency_key = ?",
                    (normalized_key,),
                ).fetchone()
                inserted = existing is None
                if inserted:
                    cursor_row = conn.execute(
                        f"SELECT COALESCE(MAX(cursor), 0) + 1 AS cursor FROM {_EVENT_TABLE}"
                    ).fetchone()
                    cursor = int(cursor_row["cursor"] or 1)
                    conn.execute(
                        f"""
                        INSERT INTO {_EVENT_TABLE} (
                            cursor,
                            idempotency_key,
                            entity_kind,
                            entity_id,
                            operation,
                            payload_json,
                            source_revision,
                            generated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cursor,
                            normalized_key,
                            normalized_kind,
                            normalized_id,
                            normalized_operation,
                            payload_json,
                            _optional_text(source_revision),
                            timestamp,
                        ),
                    )
                row = (
                    existing
                    or conn.execute(
                        f"""
                    SELECT *
                      FROM {_EVENT_TABLE}
                     WHERE idempotency_key = ?
                    """,
                        (normalized_key,),
                    ).fetchone()
                )
        if row is None:
            raise RuntimeError(f"hub UI event missing after append: {normalized_key}")
        event = _hub_ui_event_from_row(row)
        if inserted:
            self.apply_event_to_entity_state(event)
        return HubUiEventAppendResult(event=event, inserted=inserted)

    def latest_event_cursor(self) -> int:
        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                f"SELECT COALESCE(MAX(cursor), 0) AS cursor FROM {_EVENT_TABLE}"
            ).fetchone()
        return int(row["cursor"] or 0) if row is not None else 0

    def read_events_since(
        self, cursor: Optional[int | str], *, limit: int = 100
    ) -> HubUiEventBatch:
        after_cursor = max(0, int(cursor or 0))
        row_limit = min(HUB_UI_EVENT_READ_LIMIT_MAX, max(1, int(limit)))
        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            latest_row = conn.execute(
                f"SELECT COALESCE(MAX(cursor), 0) AS cursor FROM {_EVENT_TABLE}"
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT *
                  FROM {_EVENT_TABLE}
                 WHERE cursor > ?
                 ORDER BY cursor ASC
                 LIMIT ?
                """,
                (after_cursor, row_limit),
            ).fetchall()
        events = [_hub_ui_event_from_row(row) for row in rows]
        latest_cursor = int(latest_row["cursor"] or 0) if latest_row is not None else 0
        first_cursor = events[0].cursor if events else None
        gap_detected = bool(
            (after_cursor > latest_cursor and latest_cursor > 0)
            or (first_cursor is not None and first_cursor != after_cursor + 1)
        )
        next_cursor = events[-1].cursor if events else after_cursor
        return HubUiEventBatch(
            events=events,
            after_cursor=after_cursor,
            latest_cursor=latest_cursor,
            gap_detected=gap_detected,
            next_cursor=next_cursor,
            limit=row_limit,
        )

    def put_snapshot(
        self,
        *,
        family: str,
        key: str,
        payload: Mapping[str, Any],
        cursor: Optional[int] = None,
        source_revision: Optional[str] = None,
        rebuilt_at: Optional[str] = None,
    ) -> HubProjectionSnapshot:
        normalized_family = _required_text(family, "family")
        normalized_key = _required_text(key, "key")
        timestamp = now_iso()
        payload_json = _stable_json(dict(payload))
        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            with conn:
                conn.execute(
                    f"""
                    INSERT INTO {_READ_MODEL_TABLE} (
                        family,
                        read_key,
                        revision,
                        cursor,
                        source_revision,
                        payload_json,
                        rebuilt_at,
                        updated_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                    ON CONFLICT(family, read_key) DO UPDATE SET
                        revision = {_READ_MODEL_TABLE}.revision + 1,
                        cursor = excluded.cursor,
                        source_revision = excluded.source_revision,
                        payload_json = excluded.payload_json,
                        rebuilt_at = excluded.rebuilt_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_family,
                        normalized_key,
                        int(cursor or 0),
                        _optional_text(source_revision),
                        payload_json,
                        _optional_text(rebuilt_at),
                        timestamp,
                    ),
                )
            row = conn.execute(
                f"""
                SELECT *
                  FROM {_READ_MODEL_TABLE}
                 WHERE family = ? AND read_key = ?
                """,
                (normalized_family, normalized_key),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                f"hub projection snapshot missing after put: {normalized_family}/{normalized_key}"
            )
        return _projection_snapshot_from_row(row)

    def get_snapshot(self, *, family: str, key: str) -> Optional[HubProjectionSnapshot]:
        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            row = conn.execute(
                f"""
                SELECT *
                  FROM {_READ_MODEL_TABLE}
                 WHERE family = ? AND read_key = ?
                """,
                (_required_text(family, "family"), _required_text(key, "key")),
            ).fetchone()
        return _projection_snapshot_from_row(row) if row is not None else None

    def read_window(
        self,
        *,
        family: str,
        after_key: Optional[str] = None,
        after_cursor: Optional[int] = None,
        after_revision: Optional[int] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        row_limit = min(1000, max(1, int(limit)))
        normalized_family = _required_text(family, "family")
        after = _optional_text(after_key) or ""
        cursor_floor = max(0, int(after_cursor or 0))
        revision_floor = max(0, int(after_revision or 0))
        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"""
                SELECT *
                  FROM {_READ_MODEL_TABLE}
                 WHERE family = ?
                   AND read_key > ?
                   AND cursor > ?
                   AND revision > ?
                 ORDER BY read_key ASC
                 LIMIT ?
                """,
                (normalized_family, after, cursor_floor, revision_floor, row_limit),
            ).fetchall()
            latest = conn.execute(
                f"""
                SELECT COALESCE(MAX(cursor), 0) AS cursor,
                       COALESCE(MAX(revision), 0) AS revision
                  FROM {_READ_MODEL_TABLE}
                 WHERE family = ?
                """,
                (normalized_family,),
            ).fetchone()
        items = [_projection_snapshot_from_row(row) for row in rows]
        return {
            "family": normalized_family,
            "items": [item.to_dict() for item in items],
            "cursor": int(latest["cursor"] or 0) if latest is not None else 0,
            "revision": int(latest["revision"] or 0) if latest is not None else 0,
            "limits": {
                "requested": int(limit),
                "returned": len(items),
                "max": 1000,
            },
            "next_key": items[-1].key if len(items) == row_limit else None,
        }

    def apply_event_to_entity_state(self, event: HubUiEvent) -> HubProjectionSnapshot:
        key = f"{event.entity_kind}:{event.entity_id}"
        payload = {
            "entity_kind": event.entity_kind,
            "entity_id": event.entity_id,
            "operation": event.operation,
            "event_cursor": event.cursor,
            "source_revision": event.source_revision,
            "generated_at": event.generated_at,
            "payload": dict(event.payload),
        }
        return self.put_snapshot(
            family=ENTITY_STATE_PROJECTION_FAMILY,
            key=key,
            payload=payload,
            cursor=event.cursor,
            source_revision=event.source_revision,
        )

    def rebuild_entity_state_from_events(self) -> dict[str, Any]:
        rebuilt_at = now_iso()
        with open_sqlite(self._db_path, durable=self._durable) as conn:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT * FROM {_EVENT_TABLE} ORDER BY cursor ASC"
            ).fetchall()
            with conn:
                conn.execute(
                    f"DELETE FROM {_READ_MODEL_TABLE} WHERE family = ?",
                    (ENTITY_STATE_PROJECTION_FAMILY,),
                )
                count = 0
                for row in rows:
                    event = _hub_ui_event_from_row(row)
                    key = f"{event.entity_kind}:{event.entity_id}"
                    payload = {
                        "entity_kind": event.entity_kind,
                        "entity_id": event.entity_id,
                        "operation": event.operation,
                        "event_cursor": event.cursor,
                        "source_revision": event.source_revision,
                        "generated_at": event.generated_at,
                        "payload": dict(event.payload),
                    }
                    conn.execute(
                        f"""
                        INSERT INTO {_READ_MODEL_TABLE} (
                            family,
                            read_key,
                            revision,
                            cursor,
                            source_revision,
                            payload_json,
                            rebuilt_at,
                            updated_at
                        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                        ON CONFLICT(family, read_key) DO UPDATE SET
                            revision = {_READ_MODEL_TABLE}.revision + 1,
                            cursor = excluded.cursor,
                            source_revision = excluded.source_revision,
                            payload_json = excluded.payload_json,
                            rebuilt_at = excluded.rebuilt_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            ENTITY_STATE_PROJECTION_FAMILY,
                            key,
                            event.cursor,
                            event.source_revision,
                            _stable_json(payload),
                            rebuilt_at,
                            rebuilt_at,
                        ),
                    )
                    count += 1
        return {
            "family": ENTITY_STATE_PROJECTION_FAMILY,
            "rebuilt_at": rebuilt_at,
            "events_applied": count,
        }


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def _hub_ui_event_from_row(row: Any) -> HubUiEvent:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {}
    return HubUiEvent(
        cursor=int(row["cursor"]),
        idempotency_key=str(row["idempotency_key"]),
        entity_kind=str(row["entity_kind"]),
        entity_id=str(row["entity_id"]),
        operation=str(row["operation"]),
        payload=payload if isinstance(payload, dict) else {},
        source_revision=_optional_text(row["source_revision"]),
        generated_at=str(row["generated_at"]),
    )


def _projection_snapshot_from_row(row: Any) -> HubProjectionSnapshot:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        payload = {}
    return HubProjectionSnapshot(
        family=str(row["family"]),
        key=str(row["read_key"]),
        revision=int(row["revision"] or 0),
        cursor=int(row["cursor"] or 0),
        source_revision=_optional_text(row["source_revision"]),
        payload=payload if isinstance(payload, dict) else {},
        updated_at=str(row["updated_at"]),
    )
