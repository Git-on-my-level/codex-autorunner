from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Union

from ..text_utils import _normalize_optional_text
from ..time_utils import now_iso
from .chat_surface_models import (
    ChatSurfaceIdentity,
    normalize_chat_surface_key,
    normalize_chat_surface_kind,
    normalize_chat_surface_lifecycle,
)
from .sqlite import open_orchestration_sqlite

ChatSurfaceEventType = Literal[
    "surface.bound",
    "surface.rebound",
    "surface.archived",
    "lifecycle.status_changed",
    "queue.state_changed",
    "execution.progress",
    "delivery.status_changed",
    "notification.reply_context_changed",
    "channel_directory.discovered",
]

CHAT_SURFACE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "surface.bound",
        "surface.rebound",
        "surface.archived",
        "lifecycle.status_changed",
        "queue.state_changed",
        "execution.progress",
        "delivery.status_changed",
        "notification.reply_context_changed",
        "channel_directory.discovered",
    }
)

logger = logging.getLogger("codex_autorunner.chat_surface_events")


@dataclass(frozen=True)
class ChatSurfaceEvent:
    """A committed chat surface journal event with a durable resume cursor."""

    cursor: int
    idempotency_key: str
    event_type: ChatSurfaceEventType
    surface_kind: str
    surface_key: str
    occurred_at: str
    created_at: str
    managed_thread_id: Optional[str] = None
    external_conversation_id: Optional[str] = None
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    workspace_root: Optional[str] = None
    lifecycle_status: Optional[str] = None
    status: Optional[str] = None
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatSurfaceEventAppendResult:
    event: ChatSurfaceEvent
    inserted: bool


class SQLiteChatSurfaceEventJournal:
    """Durable append-only journal for normalized chat surface mutations."""

    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = Path(hub_root)
        self._durable = durable

    def append_event(
        self,
        *,
        idempotency_key: str,
        event_type: ChatSurfaceEventType,
        surface_kind: str,
        surface_key: str,
        managed_thread_id: Optional[str] = None,
        external_conversation_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        workspace_root: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        status: Optional[str] = None,
        source_kind: Optional[str] = None,
        source_id: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
        occurred_at: Optional[str] = None,
    ) -> ChatSurfaceEventAppendResult:
        started_at = time.perf_counter()
        normalized_key = _normalize_required_text(idempotency_key, "idempotency_key")
        existing = self.get_event_by_idempotency_key(normalized_key)
        if existing is not None:
            _log_journal_metric(
                "event_journal_write_latency",
                started_at,
                event_type=str(event_type),
                inserted=False,
                cursor=existing.cursor,
            )
            return ChatSurfaceEventAppendResult(event=existing, inserted=False)

        normalized_type = normalize_chat_surface_event_type(event_type)
        identity = ChatSurfaceIdentity.from_parts(surface_kind, surface_key)
        timestamp = now_iso()
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            with conn:
                result = conn.execute(
                    """
                    INSERT OR IGNORE INTO orch_chat_surface_events (
                        idempotency_key,
                        event_type,
                        surface_kind,
                        surface_key,
                        managed_thread_id,
                        external_conversation_id,
                        repo_id,
                        resource_kind,
                        resource_id,
                        workspace_root,
                        lifecycle_status,
                        status,
                        source_kind,
                        source_id,
                        occurred_at,
                        created_at,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_key,
                        normalized_type,
                        identity.surface_kind,
                        identity.surface_key,
                        _normalize_optional_text(managed_thread_id),
                        _normalize_optional_text(external_conversation_id),
                        _normalize_optional_text(repo_id),
                        _normalize_optional_text(resource_kind),
                        _normalize_optional_text(resource_id),
                        _normalize_optional_text(workspace_root),
                        _normalize_lifecycle(lifecycle_status),
                        _normalize_optional_text(status),
                        _normalize_optional_text(source_kind),
                        _normalize_optional_text(source_id),
                        _normalize_optional_text(occurred_at) or timestamp,
                        timestamp,
                        json.dumps(dict(payload or {}), sort_keys=True),
                    ),
                )
        stored = self.get_event_by_idempotency_key(normalized_key)
        if stored is None:
            raise RuntimeError(
                f"chat surface event missing after append: {normalized_key}"
            )
        append_result = ChatSurfaceEventAppendResult(
            event=stored,
            inserted=result.rowcount > 0,
        )
        _log_journal_metric(
            "event_journal_write_latency",
            started_at,
            event_type=normalized_type,
            inserted=append_result.inserted,
            cursor=stored.cursor,
        )
        return append_result

    def get_event_by_idempotency_key(
        self, idempotency_key: str
    ) -> Optional[ChatSurfaceEvent]:
        normalized_key = _normalize_optional_text(idempotency_key)
        if normalized_key is None:
            return None
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_chat_surface_events
                 WHERE idempotency_key = ?
                """,
                (normalized_key,),
            ).fetchone()
        if row is None:
            return None
        return _event_from_row(row)

    def read_events_since(
        self,
        cursor: Optional[Union[int, str]],
        *,
        limit: int = 100,
    ) -> list[ChatSurfaceEvent]:
        started_at = time.perf_counter()
        after_cursor = max(0, int(cursor or 0))
        row_limit = _bounded_limit(limit)
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM orch_chat_surface_events
                 WHERE event_id > ?
                 ORDER BY event_id ASC
                 LIMIT ?
                """,
                (after_cursor, row_limit),
            ).fetchall()
        events = [_event_from_row(row) for row in rows]
        _log_journal_metric(
            "event_journal_read_latency",
            started_at,
            cursor=after_cursor,
            limit=row_limit,
            returned=len(events),
        )
        return events

    def latest_cursor(self) -> int:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(event_id), 0) AS cursor FROM orch_chat_surface_events"
            ).fetchone()
        if row is None:
            return 0
        return int(row["cursor"] or 0)

    def read_history(self, *, limit: int = 100) -> list[ChatSurfaceEvent]:
        row_limit = _bounded_limit(limit)
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                """
                SELECT *
                  FROM (
                      SELECT *
                        FROM orch_chat_surface_events
                       ORDER BY event_id DESC
                       LIMIT ?
                  )
                 ORDER BY event_id ASC
                """,
                (row_limit,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]


def normalize_chat_surface_event_type(value: Any) -> ChatSurfaceEventType:
    normalized = _normalize_required_text(value, "event_type")
    if normalized not in CHAT_SURFACE_EVENT_TYPES:
        raise ValueError(f"unknown chat surface event type: {normalized}")
    return normalized  # type: ignore[return-value]


def _event_from_row(row: Any) -> ChatSurfaceEvent:
    payload: dict[str, Any]
    try:
        parsed = json.loads(row["payload_json"] or "{}")
        payload = parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return ChatSurfaceEvent(
        cursor=int(row["event_id"]),
        idempotency_key=str(row["idempotency_key"]),
        event_type=normalize_chat_surface_event_type(row["event_type"]),
        surface_kind=normalize_chat_surface_kind(row["surface_kind"]),
        surface_key=normalize_chat_surface_key(row["surface_key"]),
        managed_thread_id=_normalize_optional_text(row["managed_thread_id"]),
        external_conversation_id=_normalize_optional_text(
            row["external_conversation_id"]
        ),
        repo_id=_normalize_optional_text(row["repo_id"]),
        resource_kind=_normalize_optional_text(row["resource_kind"]),
        resource_id=_normalize_optional_text(row["resource_id"]),
        workspace_root=_normalize_optional_text(row["workspace_root"]),
        lifecycle_status=_normalize_lifecycle(row["lifecycle_status"]),
        status=_normalize_optional_text(row["status"]),
        source_kind=_normalize_optional_text(row["source_kind"]),
        source_id=_normalize_optional_text(row["source_id"]),
        occurred_at=str(row["occurred_at"]),
        created_at=str(row["created_at"]),
        payload=payload,
    )


def _log_journal_metric(
    metric: str,
    started_at: float,
    **fields: Any,
) -> None:
    logger.debug(
        "chat_surface_event_journal_metric",
        extra={
            "event": "chat_surface_event_journal_metric",
            "metric": metric,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            **fields,
        },
    )


def _normalize_required_text(value: Any, field_name: str) -> str:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _normalize_lifecycle(value: Any) -> Optional[str]:
    if _normalize_optional_text(value) is None:
        return None
    return normalize_chat_surface_lifecycle(value)


def _bounded_limit(limit: int) -> int:
    return min(1000, max(1, int(limit)))


__all__ = [
    "CHAT_SURFACE_EVENT_TYPES",
    "ChatSurfaceEvent",
    "ChatSurfaceEventAppendResult",
    "ChatSurfaceEventType",
    "SQLiteChatSurfaceEventJournal",
    "normalize_chat_surface_event_type",
]
