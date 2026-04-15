from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, cast

from ..time_utils import now_iso
from .chat_operation_state import (
    CHAT_OPERATION_TERMINAL_STATES,
    ChatOperationSnapshot,
    ChatOperationState,
    ChatOperationStore,
    is_valid_chat_operation_transition,
)
from .sqlite import open_orchestration_sqlite

_UNSET = object()
_DEFAULT_UNACKED_EXPIRY = timedelta(minutes=5)
_DEFAULT_DELIVERY_STALE_WINDOW = timedelta(minutes=15)
_DEFAULT_MAX_DELIVERY_ATTEMPTS = 3


@dataclass(frozen=True)
class ChatOperationRegistration:
    snapshot: ChatOperationSnapshot
    inserted: bool


class ChatOperationRecoveryAction(str):
    NOOP = "noop"
    RESUME_EXECUTION = "resume_execution"
    REPLAY_DELIVERY = "replay_delivery"
    MARK_ABANDONED = "mark_abandoned"
    MARK_EXPIRED = "mark_expired"


@dataclass(frozen=True)
class ChatOperationRecoveryDecision:
    action: str
    reason: str


class SQLiteChatOperationLedger(ChatOperationStore):
    """Durable orchestration-backed ledger for user-visible chat operations."""

    def __init__(self, hub_root: Path, *, durable: bool = True) -> None:
        self._hub_root = Path(hub_root)
        self._durable = durable

    def get_operation(self, operation_id: str) -> Optional[ChatOperationSnapshot]:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_chat_operations
                 WHERE operation_id = ?
                """,
                (str(operation_id or "").strip(),),
            ).fetchone()
        if row is None:
            return None
        return _snapshot_from_row(row)

    def get_operation_by_surface(
        self, surface_kind: str, surface_operation_key: str
    ) -> Optional[ChatOperationSnapshot]:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_chat_operations
                 WHERE surface_kind = ?
                   AND surface_operation_key = ?
                """,
                (
                    str(surface_kind or "").strip(),
                    str(surface_operation_key or "").strip(),
                ),
            ).fetchone()
        if row is None:
            return None
        return _snapshot_from_row(row)

    def register_operation(
        self,
        *,
        surface_kind: str,
        surface_operation_key: str,
        operation_id: Optional[str] = None,
        state: ChatOperationState = ChatOperationState.RECEIVED,
        thread_target_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ChatOperationRegistration:
        normalized_surface_kind = str(surface_kind or "").strip()
        normalized_surface_key = str(surface_operation_key or "").strip()
        if not normalized_surface_kind:
            raise ValueError("surface_kind is required")
        if not normalized_surface_key:
            raise ValueError("surface_operation_key is required")
        existing = self.get_operation_by_surface(
            normalized_surface_kind, normalized_surface_key
        )
        if existing is not None:
            return ChatOperationRegistration(snapshot=existing, inserted=False)
        timestamp = now_iso()
        snapshot = ChatOperationSnapshot(
            operation_id=str(operation_id or uuid.uuid4()),
            surface_kind=normalized_surface_kind,
            surface_operation_key=normalized_surface_key,
            state=state,
            thread_target_id=_normalized_optional_text(thread_target_id),
            conversation_id=_normalized_optional_text(conversation_id),
            created_at=timestamp,
            updated_at=timestamp,
            metadata=dict(metadata or {}),
        )
        return ChatOperationRegistration(
            snapshot=self.upsert_operation(snapshot),
            inserted=True,
        )

    def upsert_operation(
        self, snapshot: ChatOperationSnapshot
    ) -> ChatOperationSnapshot:
        normalized = _normalize_snapshot(snapshot)
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO orch_chat_operations (
                        operation_id,
                        surface_kind,
                        surface_operation_key,
                        conversation_id,
                        thread_target_id,
                        state,
                        execution_id,
                        backend_turn_id,
                        status_message,
                        blocking_reason,
                        ack_requested_at,
                        ack_completed_at,
                        first_visible_feedback_at,
                        anchor_ref,
                        interrupt_ref,
                        delivery_state,
                        delivery_cursor_json,
                        delivery_attempt_count,
                        delivery_claimed_at,
                        terminal_outcome,
                        terminal_detail,
                        created_at,
                        updated_at,
                        metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(operation_id) DO UPDATE SET
                        surface_kind = excluded.surface_kind,
                        surface_operation_key = excluded.surface_operation_key,
                        conversation_id = excluded.conversation_id,
                        thread_target_id = excluded.thread_target_id,
                        state = excluded.state,
                        execution_id = excluded.execution_id,
                        backend_turn_id = excluded.backend_turn_id,
                        status_message = excluded.status_message,
                        blocking_reason = excluded.blocking_reason,
                        ack_requested_at = excluded.ack_requested_at,
                        ack_completed_at = excluded.ack_completed_at,
                        first_visible_feedback_at = excluded.first_visible_feedback_at,
                        anchor_ref = excluded.anchor_ref,
                        interrupt_ref = excluded.interrupt_ref,
                        delivery_state = excluded.delivery_state,
                        delivery_cursor_json = excluded.delivery_cursor_json,
                        delivery_attempt_count = excluded.delivery_attempt_count,
                        delivery_claimed_at = excluded.delivery_claimed_at,
                        terminal_outcome = excluded.terminal_outcome,
                        terminal_detail = excluded.terminal_detail,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at,
                        metadata_json = excluded.metadata_json
                    """,
                    _snapshot_to_row_values(normalized),
                )
        stored = self.get_operation(normalized.operation_id)
        if stored is None:
            raise RuntimeError(
                f"chat operation missing after upsert: {normalized.operation_id}"
            )
        return stored

    def patch_operation(
        self,
        operation_id: str,
        *,
        state: ChatOperationState | object = _UNSET,
        validate_transition: bool = True,
        metadata_updates: Optional[Mapping[str, Any]] = None,
        **changes: Any,
    ) -> Optional[ChatOperationSnapshot]:
        current = self.get_operation(operation_id)
        if current is None:
            return None
        next_state = current.state
        has_state_update = isinstance(state, ChatOperationState)
        if has_state_update:
            next_state = cast(ChatOperationState, state)
        if (
            validate_transition
            and has_state_update
            and next_state != current.state
            and not is_valid_chat_operation_transition(current.state, next_state)
        ):
            raise ValueError(
                f"invalid chat operation transition: {current.state.value} -> {next_state.value}"
            )
        metadata = dict(current.metadata)
        if metadata_updates:
            metadata.update(dict(metadata_updates))
        delivery_attempt_count = changes.get("delivery_attempt_count", _UNSET)
        if delivery_attempt_count in (_UNSET, None):
            next_delivery_attempt_count = current.delivery_attempt_count or 0
        else:
            next_delivery_attempt_count = int(delivery_attempt_count)
        first_visible_feedback_at = changes.get("first_visible_feedback_at", _UNSET)
        next_first_visible_feedback_at: Optional[str]
        if current.first_visible_feedback_at is not None:
            next_first_visible_feedback_at = current.first_visible_feedback_at
        elif first_visible_feedback_at is _UNSET:
            next_first_visible_feedback_at = current.first_visible_feedback_at
        else:
            next_first_visible_feedback_at = first_visible_feedback_at
        updated = replace(
            current,
            state=next_state,
            execution_id=changes.get("execution_id", current.execution_id),
            backend_turn_id=changes.get("backend_turn_id", current.backend_turn_id),
            status_message=changes.get("status_message", current.status_message),
            blocking_reason=changes.get("blocking_reason", current.blocking_reason),
            thread_target_id=changes.get("thread_target_id", current.thread_target_id),
            conversation_id=changes.get("conversation_id", current.conversation_id),
            ack_requested_at=changes.get("ack_requested_at", current.ack_requested_at),
            ack_completed_at=changes.get("ack_completed_at", current.ack_completed_at),
            first_visible_feedback_at=next_first_visible_feedback_at,
            anchor_ref=changes.get("anchor_ref", current.anchor_ref),
            interrupt_ref=changes.get("interrupt_ref", current.interrupt_ref),
            delivery_state=changes.get("delivery_state", current.delivery_state),
            delivery_cursor=changes.get("delivery_cursor", current.delivery_cursor),
            delivery_attempt_count=next_delivery_attempt_count,
            delivery_claimed_at=changes.get(
                "delivery_claimed_at", current.delivery_claimed_at
            ),
            terminal_outcome=changes.get("terminal_outcome", current.terminal_outcome),
            terminal_detail=changes.get("terminal_detail", current.terminal_detail),
            created_at=changes.get("created_at", current.created_at),
            updated_at=changes.get("updated_at", now_iso()),
            metadata=metadata,
        )
        return self.upsert_operation(updated)

    def list_operations_for_thread(
        self,
        thread_target_id: str,
        *,
        include_terminal: bool = False,
        limit: int = 20,
    ) -> list[ChatOperationSnapshot]:
        normalized_thread_id = str(thread_target_id or "").strip()
        if not normalized_thread_id:
            return []
        params: list[Any] = [normalized_thread_id]
        where = "WHERE thread_target_id = ?"
        if not include_terminal:
            placeholders = ", ".join("?" for _ in CHAT_OPERATION_TERMINAL_STATES)
            where += f" AND state NOT IN ({placeholders})"
            params.extend(state.value for state in CHAT_OPERATION_TERMINAL_STATES)
        params.append(max(1, int(limit)))
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_chat_operations
                  {where}
                 ORDER BY updated_at DESC, operation_id DESC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_snapshot_from_row(row) for row in rows]

    def list_recoverable_operations(
        self,
        *,
        surface_kind: Optional[str] = None,
        limit: int = 200,
    ) -> list[ChatOperationSnapshot]:
        params: list[Any] = []
        clauses = ["terminal_outcome IS NULL"]
        placeholders = ", ".join("?" for _ in CHAT_OPERATION_TERMINAL_STATES)
        clauses.append(f"state NOT IN ({placeholders})")
        params.extend(state.value for state in CHAT_OPERATION_TERMINAL_STATES)
        normalized_surface_kind = _normalized_optional_text(surface_kind)
        if normalized_surface_kind is not None:
            clauses.append("surface_kind = ?")
            params.append(normalized_surface_kind)
        params.append(max(1, int(limit)))
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                  FROM orch_chat_operations
                 WHERE {" AND ".join(clauses)}
                 ORDER BY created_at ASC, operation_id ASC
                 LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_snapshot_from_row(row) for row in rows]

    def delete_operation(self, operation_id: str) -> None:
        with open_orchestration_sqlite(
            self._hub_root, durable=self._durable, migrate=True
        ) as conn:
            with conn:
                conn.execute(
                    "DELETE FROM orch_chat_operations WHERE operation_id = ?",
                    (str(operation_id or "").strip(),),
                )


def plan_chat_operation_recovery(
    snapshot: ChatOperationSnapshot,
    *,
    now: Optional[datetime] = None,
    max_delivery_attempts: int = _DEFAULT_MAX_DELIVERY_ATTEMPTS,
    unacked_expiry: timedelta = _DEFAULT_UNACKED_EXPIRY,
    delivery_stale_window: timedelta = _DEFAULT_DELIVERY_STALE_WINDOW,
) -> ChatOperationRecoveryDecision:
    current_at = now or datetime.now(timezone.utc)
    if snapshot.terminal_outcome:
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="terminal_outcome_already_recorded",
        )
    if snapshot.state in CHAT_OPERATION_TERMINAL_STATES:
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="terminal_state",
        )
    if snapshot.delivery_state in {"pending", "failed"}:
        if int(snapshot.delivery_attempt_count or 0) >= max_delivery_attempts:
            return ChatOperationRecoveryDecision(
                action=ChatOperationRecoveryAction.MARK_ABANDONED,
                reason="delivery_attempt_budget_exhausted",
            )
        updated_at = _parse_iso_timestamp(snapshot.updated_at)
        if updated_at is None or current_at - updated_at >= delivery_stale_window:
            return ChatOperationRecoveryDecision(
                action=ChatOperationRecoveryAction.REPLAY_DELIVERY,
                reason="delivery_replay_required",
            )
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.NOOP,
            reason="delivery_backoff_active",
        )
    if snapshot.state in {
        ChatOperationState.ACKNOWLEDGED,
        ChatOperationState.VISIBLE,
        ChatOperationState.QUEUED,
        ChatOperationState.RUNNING,
        ChatOperationState.INTERRUPTING,
        ChatOperationState.ROUTING,
        ChatOperationState.BLOCKED,
    }:
        return ChatOperationRecoveryDecision(
            action=ChatOperationRecoveryAction.RESUME_EXECUTION,
            reason="execution_resume_required",
        )
    if snapshot.state == ChatOperationState.RECEIVED:
        created_at = _parse_iso_timestamp(snapshot.created_at or snapshot.updated_at)
        if created_at is None or current_at - created_at >= unacked_expiry:
            return ChatOperationRecoveryDecision(
                action=ChatOperationRecoveryAction.MARK_EXPIRED,
                reason="accepted_operation_never_acknowledged",
            )
    return ChatOperationRecoveryDecision(
        action=ChatOperationRecoveryAction.NOOP,
        reason="no_recovery_action",
    )


def _normalize_snapshot(snapshot: ChatOperationSnapshot) -> ChatOperationSnapshot:
    operation_id = str(snapshot.operation_id or "").strip()
    surface_kind = str(snapshot.surface_kind or "").strip()
    surface_operation_key = str(snapshot.surface_operation_key or "").strip()
    if not operation_id:
        raise ValueError("operation_id is required")
    if not surface_kind:
        raise ValueError("surface_kind is required")
    if not surface_operation_key:
        raise ValueError("surface_operation_key is required")
    return replace(
        snapshot,
        operation_id=operation_id,
        surface_kind=surface_kind,
        surface_operation_key=surface_operation_key,
        thread_target_id=_normalized_optional_text(snapshot.thread_target_id),
        conversation_id=_normalized_optional_text(snapshot.conversation_id),
        execution_id=_normalized_optional_text(snapshot.execution_id),
        backend_turn_id=_normalized_optional_text(snapshot.backend_turn_id),
        status_message=_normalized_optional_text(snapshot.status_message),
        blocking_reason=_normalized_optional_text(snapshot.blocking_reason),
        ack_requested_at=_normalized_optional_text(snapshot.ack_requested_at),
        ack_completed_at=_normalized_optional_text(snapshot.ack_completed_at),
        first_visible_feedback_at=_normalized_optional_text(
            snapshot.first_visible_feedback_at
        ),
        anchor_ref=_normalized_optional_text(snapshot.anchor_ref),
        interrupt_ref=_normalized_optional_text(snapshot.interrupt_ref),
        delivery_state=_normalized_optional_text(snapshot.delivery_state),
        delivery_claimed_at=_normalized_optional_text(snapshot.delivery_claimed_at),
        terminal_outcome=_normalized_optional_text(snapshot.terminal_outcome),
        terminal_detail=_normalized_optional_text(snapshot.terminal_detail),
        created_at=_normalized_optional_text(snapshot.created_at) or now_iso(),
        updated_at=_normalized_optional_text(snapshot.updated_at) or now_iso(),
        delivery_attempt_count=max(0, int(snapshot.delivery_attempt_count or 0)),
        metadata=dict(snapshot.metadata or {}),
        delivery_cursor=(
            dict(snapshot.delivery_cursor)
            if isinstance(snapshot.delivery_cursor, Mapping)
            else None
        ),
    )


def _snapshot_to_row_values(snapshot: ChatOperationSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.operation_id,
        snapshot.surface_kind,
        snapshot.surface_operation_key,
        snapshot.conversation_id,
        snapshot.thread_target_id,
        snapshot.state.value,
        snapshot.execution_id,
        snapshot.backend_turn_id,
        snapshot.status_message,
        snapshot.blocking_reason,
        snapshot.ack_requested_at,
        snapshot.ack_completed_at,
        snapshot.first_visible_feedback_at,
        snapshot.anchor_ref,
        snapshot.interrupt_ref,
        snapshot.delivery_state,
        (
            json.dumps(dict(snapshot.delivery_cursor), sort_keys=True)
            if isinstance(snapshot.delivery_cursor, Mapping)
            else None
        ),
        max(0, int(snapshot.delivery_attempt_count or 0)),
        snapshot.delivery_claimed_at,
        snapshot.terminal_outcome,
        snapshot.terminal_detail,
        snapshot.created_at or now_iso(),
        snapshot.updated_at or now_iso(),
        json.dumps(dict(snapshot.metadata or {}), sort_keys=True),
    )


def _snapshot_from_row(row: Any) -> ChatOperationSnapshot:
    delivery_cursor = _decode_json_object(row["delivery_cursor_json"])
    metadata = _decode_json_object(row["metadata_json"])
    return ChatOperationSnapshot(
        operation_id=str(row["operation_id"]),
        surface_kind=str(row["surface_kind"]),
        surface_operation_key=str(row["surface_operation_key"]),
        state=ChatOperationState(str(row["state"])),
        thread_target_id=_normalized_optional_text(row["thread_target_id"]),
        conversation_id=_normalized_optional_text(row["conversation_id"]),
        execution_id=_normalized_optional_text(row["execution_id"]),
        backend_turn_id=_normalized_optional_text(row["backend_turn_id"]),
        status_message=_normalized_optional_text(row["status_message"]),
        blocking_reason=_normalized_optional_text(row["blocking_reason"]),
        ack_requested_at=_normalized_optional_text(row["ack_requested_at"]),
        ack_completed_at=_normalized_optional_text(row["ack_completed_at"]),
        first_visible_feedback_at=_normalized_optional_text(
            row["first_visible_feedback_at"]
        ),
        anchor_ref=_normalized_optional_text(row["anchor_ref"]),
        interrupt_ref=_normalized_optional_text(row["interrupt_ref"]),
        delivery_state=_normalized_optional_text(row["delivery_state"]),
        delivery_cursor=delivery_cursor or None,
        delivery_attempt_count=max(0, int(row["delivery_attempt_count"] or 0)),
        delivery_claimed_at=_normalized_optional_text(row["delivery_claimed_at"]),
        terminal_outcome=_normalized_optional_text(row["terminal_outcome"]),
        terminal_detail=_normalized_optional_text(row["terminal_detail"]),
        created_at=_normalized_optional_text(row["created_at"]),
        updated_at=_normalized_optional_text(row["updated_at"]),
        metadata=metadata,
    )


def _decode_json_object(raw_value: Any) -> dict[str, Any]:
    if not isinstance(raw_value, str) or not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalized_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    normalized = _normalized_optional_text(value)
    if normalized is None:
        return None
    try:
        if normalized.endswith("Z"):
            return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "ChatOperationRecoveryAction",
    "ChatOperationRecoveryDecision",
    "ChatOperationRegistration",
    "SQLiteChatOperationLedger",
    "plan_chat_operation_recovery",
]
