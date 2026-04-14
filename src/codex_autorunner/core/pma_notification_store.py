from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional

if TYPE_CHECKING:
    from .hub_control_plane.models import NotificationRecord

from .orchestration.sqlite import open_orchestration_sqlite
from .text_utils import (
    _json_dumps,
    _json_loads_object,
    _normalize_optional_text,
    _normalize_text,
)
from .time_utils import now_iso


def _normalize_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


@dataclass(frozen=True)
class NotificationConversation:
    notification_id: str
    correlation_id: str
    source_kind: str
    delivery_mode: str
    surface_kind: str
    surface_key: str
    delivery_record_id: str
    delivered_message_id: Optional[str]
    repo_id: Optional[str] = None
    workspace_root: Optional[str] = None
    run_id: Optional[str] = None
    managed_thread_id: Optional[str] = None
    continuation_thread_target_id: Optional[str] = None
    context: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _conversation_from_row(row: sqlite3.Row) -> NotificationConversation:
    return NotificationConversation(
        notification_id=str(row["notification_id"]),
        correlation_id=str(row["correlation_id"]),
        source_kind=str(row["source_kind"]),
        delivery_mode=str(row["delivery_mode"]),
        surface_kind=str(row["surface_kind"]),
        surface_key=str(row["surface_key"]),
        delivery_record_id=str(row["delivery_record_id"]),
        delivered_message_id=_normalize_text(row["delivered_message_id"]),
        repo_id=_normalize_text(row["repo_id"]),
        workspace_root=_normalize_text(row["workspace_root"]),
        run_id=_normalize_text(row["run_id"]),
        managed_thread_id=_normalize_text(row["managed_thread_id"]),
        continuation_thread_target_id=_normalize_text(
            row["continuation_thread_target_id"]
        ),
        context=_json_loads_object(row["context_json"]),
        created_at=_normalize_text(row["created_at"]),
        updated_at=_normalize_text(row["updated_at"]),
    )


def notification_surface_key(notification_id: str) -> str:
    normalized = _normalize_text(notification_id)
    if normalized is None:
        raise ValueError("notification_id is required")
    return f"notification:{normalized}"


def build_notification_context_block(
    conversation: "NotificationConversation | NotificationRecord",
) -> str:
    payload = {
        "notification_id": conversation.notification_id,
        "correlation_id": conversation.correlation_id,
        "source_kind": conversation.source_kind,
        "delivery_mode": conversation.delivery_mode,
        "repo_id": conversation.repo_id,
        "workspace_root": conversation.workspace_root,
        "run_id": conversation.run_id,
        "managed_thread_id": conversation.managed_thread_id,
        "continuation_thread_target_id": conversation.continuation_thread_target_id,
        "context": conversation.context,
    }
    return (
        "<notification_context>\n"
        f"{json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)}\n"
        "</notification_context>"
    )


class PmaNotificationStore:
    """Durable reply-continuation storage for PMA-originated notifications."""

    def __init__(self, hub_root: Path) -> None:
        self._hub_root = Path(hub_root)

    def record_notification(
        self,
        *,
        correlation_id: str,
        source_kind: str,
        delivery_mode: str,
        surface_kind: str,
        surface_key: str,
        delivery_record_id: str,
        repo_id: Optional[str] = None,
        workspace_root: Optional[str] = None,
        run_id: Optional[str] = None,
        managed_thread_id: Optional[str] = None,
        continuation_thread_target_id: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        notification_id: Optional[str] = None,
    ) -> NotificationConversation:
        normalized_correlation_id = _normalize_text(correlation_id)
        normalized_source_kind = _normalize_text(source_kind)
        normalized_delivery_mode = _normalize_text(delivery_mode)
        normalized_surface_kind = _normalize_text(surface_kind)
        normalized_surface_key = _normalize_text(surface_key)
        normalized_delivery_record_id = _normalize_text(delivery_record_id)
        if (
            normalized_correlation_id is None
            or normalized_source_kind is None
            or normalized_delivery_mode is None
            or normalized_surface_kind is None
            or normalized_surface_key is None
            or normalized_delivery_record_id is None
        ):
            raise ValueError("notification delivery metadata is incomplete")
        timestamp = now_iso()
        resolved_notification_id = _normalize_text(notification_id) or uuid.uuid4().hex
        context_json = _json_dumps(_normalize_json_object(context))
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_notification_conversations
                 WHERE delivery_record_id = ?
                 LIMIT 1
                """,
                (normalized_delivery_record_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO orch_notification_conversations (
                        notification_id,
                        correlation_id,
                        source_kind,
                        delivery_mode,
                        surface_kind,
                        surface_key,
                        delivery_record_id,
                        delivered_message_id,
                        repo_id,
                        workspace_root,
                        run_id,
                        managed_thread_id,
                        continuation_thread_target_id,
                        context_json,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_notification_id,
                        normalized_correlation_id,
                        normalized_source_kind,
                        normalized_delivery_mode,
                        normalized_surface_kind,
                        normalized_surface_key,
                        normalized_delivery_record_id,
                        None,
                        _normalize_text(repo_id),
                        _normalize_text(workspace_root),
                        _normalize_text(run_id),
                        _normalize_text(managed_thread_id),
                        _normalize_text(continuation_thread_target_id),
                        context_json,
                        timestamp,
                        timestamp,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT *
                      FROM orch_notification_conversations
                     WHERE notification_id = ?
                     LIMIT 1
                    """,
                    (resolved_notification_id,),
                ).fetchone()
            else:
                conn.execute(
                    """
                    UPDATE orch_notification_conversations
                       SET updated_at = ?
                     WHERE notification_id = ?
                    """,
                    (timestamp, row["notification_id"]),
                )
                row = conn.execute(
                    """
                    SELECT *
                      FROM orch_notification_conversations
                     WHERE notification_id = ?
                     LIMIT 1
                    """,
                    (row["notification_id"],),
                ).fetchone()
        if row is None:
            raise RuntimeError("notification row missing after insert")
        return _conversation_from_row(row)

    def mark_delivered(
        self, *, delivery_record_id: str, delivered_message_id: Any
    ) -> Optional[NotificationConversation]:
        normalized_record_id = _normalize_text(delivery_record_id)
        normalized_message_id = _normalize_optional_text(delivered_message_id)
        if normalized_record_id is None or normalized_message_id is None:
            return None
        timestamp = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            conn.execute(
                """
                UPDATE orch_notification_conversations
                   SET delivered_message_id = ?,
                       updated_at = ?
                 WHERE delivery_record_id = ?
                """,
                (normalized_message_id, timestamp, normalized_record_id),
            )
            row = conn.execute(
                """
                SELECT *
                  FROM orch_notification_conversations
                 WHERE delivery_record_id = ?
                 LIMIT 1
                """,
                (normalized_record_id,),
            ).fetchone()
        return _conversation_from_row(row) if row is not None else None

    def bind_continuation_thread(
        self, *, notification_id: str, thread_target_id: str
    ) -> Optional[NotificationConversation]:
        normalized_notification_id = _normalize_text(notification_id)
        normalized_thread_target_id = _normalize_text(thread_target_id)
        if normalized_notification_id is None or normalized_thread_target_id is None:
            return None
        timestamp = now_iso()
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            conn.execute(
                """
                UPDATE orch_notification_conversations
                   SET continuation_thread_target_id = ?,
                       updated_at = ?
                 WHERE notification_id = ?
                """,
                (normalized_thread_target_id, timestamp, normalized_notification_id),
            )
            row = conn.execute(
                """
                SELECT *
                  FROM orch_notification_conversations
                 WHERE notification_id = ?
                 LIMIT 1
                """,
                (normalized_notification_id,),
            ).fetchone()
        return _conversation_from_row(row) if row is not None else None

    def get_reply_target(
        self,
        *,
        surface_kind: str,
        surface_key: str,
        delivered_message_id: Any,
    ) -> Optional[NotificationConversation]:
        normalized_surface_kind = _normalize_text(surface_kind)
        normalized_surface_key = _normalize_text(surface_key)
        normalized_message_id = _normalize_optional_text(delivered_message_id)
        if (
            normalized_surface_kind is None
            or normalized_surface_key is None
            or normalized_message_id is None
        ):
            return None
        with open_orchestration_sqlite(self._hub_root, durable=True) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_notification_conversations
                 WHERE surface_kind = ?
                   AND surface_key = ?
                   AND delivered_message_id = ?
                 ORDER BY updated_at DESC, created_at DESC
                 LIMIT 1
                """,
                (
                    normalized_surface_kind,
                    normalized_surface_key,
                    normalized_message_id,
                ),
            ).fetchone()
        return _conversation_from_row(row) if row is not None else None


__all__ = [
    "NotificationConversation",
    "PmaNotificationStore",
    "build_notification_context_block",
    "notification_surface_key",
]
