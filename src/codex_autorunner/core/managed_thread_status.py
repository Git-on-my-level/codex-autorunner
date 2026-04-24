"""Canonical managed-thread status model.

Normalized statuses:
- ``idle``: healthy and waiting for work
- ``running``: a managed turn is in flight
- ``paused``: execution is intentionally compacted/paused without finishing
- ``completed``: the latest managed turn finished successfully
- ``interrupted``: the latest managed turn was intentionally interrupted
- ``failed``: the latest managed turn finished unsuccessfully
- ``archived``: the thread is terminal and read-only

Lifecycle write-admission status remains separate:
- ``active``
- ``archived``

Transition table:

| Current | Signal | Next | Terminal |
| --- | --- | --- | --- |
| any | ``thread_created`` | ``idle`` | no |
| ``paused``/``archived`` | ``thread_resumed`` | ``idle`` | no |
| ``idle``/``completed``/``interrupted``/``failed``/``paused`` | ``turn_started`` | ``running`` | no |
| ``idle``/``completed``/``interrupted``/``failed``/``paused`` | ``thread_compacted`` | ``paused`` | no |
| ``running`` | ``managed_turn_completed`` | ``completed`` | yes |
| ``running`` | ``managed_turn_failed`` | ``failed`` | yes |
| ``running`` | ``managed_turn_interrupted`` | ``interrupted`` | yes |
| any | ``thread_archived`` | ``archived`` | yes |

Rules:
- duplicate signals are idempotent
- older signals are ignored when their timestamp predates the stored transition
- same-timestamp duplicates are ignored when status/reason/turn are unchanged
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from .text_utils import _normalize_text

_STATUS_ACTIVE = "active"
_STATUS_ARCHIVED = "archived"
_STATUS_IDLE = "idle"
_STATUS_RUNNING = "running"
_STATUS_PAUSED = "paused"
_STATUS_COMPLETED = "completed"
_STATUS_INTERRUPTED = "interrupted"
_STATUS_FAILED = "failed"

_OP_REUSABLE = "reusable"
_OP_ATTENTION_REQUIRED = "attention_required"


class ManagedThreadStatusReason(str, Enum):
    THREAD_CREATED = "thread_created"
    THREAD_RESUMED = "thread_resumed"
    THREAD_ARCHIVED = "thread_archived"
    THREAD_COMPACTED = "thread_compacted"
    TURN_STARTED = "turn_started"
    MANAGED_TURN_COMPLETED = "managed_turn_completed"
    MANAGED_TURN_FAILED = "managed_turn_failed"
    MANAGED_TURN_INTERRUPTED = "managed_turn_interrupted"


TERMINAL_STATUSES = frozenset(
    {
        _STATUS_COMPLETED,
        _STATUS_INTERRUPTED,
        _STATUS_FAILED,
        _STATUS_ARCHIVED,
    }
)

TRANSITION_TABLE: tuple[dict[str, Any], ...] = (
    {
        "signal": ManagedThreadStatusReason.THREAD_CREATED.value,
        "from": "*",
        "to": _STATUS_IDLE,
    },
    {
        "signal": ManagedThreadStatusReason.THREAD_RESUMED.value,
        "from": (
            _STATUS_PAUSED,
            _STATUS_ARCHIVED,
        ),
        "to": _STATUS_IDLE,
    },
    {
        "signal": ManagedThreadStatusReason.TURN_STARTED.value,
        "from": (
            _STATUS_IDLE,
            _STATUS_COMPLETED,
            _STATUS_INTERRUPTED,
            _STATUS_FAILED,
            _STATUS_PAUSED,
        ),
        "to": _STATUS_RUNNING,
    },
    {
        "signal": ManagedThreadStatusReason.THREAD_COMPACTED.value,
        "from": (
            _STATUS_IDLE,
            _STATUS_COMPLETED,
            _STATUS_INTERRUPTED,
            _STATUS_FAILED,
            _STATUS_PAUSED,
        ),
        "to": _STATUS_PAUSED,
    },
    {
        "signal": ManagedThreadStatusReason.MANAGED_TURN_COMPLETED.value,
        "from": (_STATUS_RUNNING,),
        "to": _STATUS_COMPLETED,
    },
    {
        "signal": ManagedThreadStatusReason.MANAGED_TURN_FAILED.value,
        "from": (_STATUS_RUNNING,),
        "to": _STATUS_FAILED,
    },
    {
        "signal": ManagedThreadStatusReason.MANAGED_TURN_INTERRUPTED.value,
        "from": (_STATUS_RUNNING,),
        "to": _STATUS_INTERRUPTED,
    },
    {
        "signal": ManagedThreadStatusReason.THREAD_ARCHIVED.value,
        "from": "*",
        "to": _STATUS_ARCHIVED,
    },
)

_TRANSITIONS = {
    str(entry["signal"]).strip().lower(): entry for entry in TRANSITION_TABLE
}


def _parse_iso(value: Any) -> Optional[datetime]:
    text = _normalize_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_reason(value: str | ManagedThreadStatusReason) -> str:
    if isinstance(value, ManagedThreadStatusReason):
        return value.value
    return str(value).strip().lower()


def normalize_status_timestamp(value: Optional[str]) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ManagedThreadStatusSnapshot:
    status: str = _STATUS_IDLE
    reason_code: str = ManagedThreadStatusReason.THREAD_CREATED.value
    changed_at: str = ""
    terminal: bool = False
    turn_id: Optional[str] = None

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any] | None
    ) -> "ManagedThreadStatusSnapshot":
        if not isinstance(data, Mapping):
            return cls()
        status = _normalize_text(data.get("normalized_status"))
        if status is None:
            status = _STATUS_IDLE
        reason_code = _normalize_text(data.get("status_reason_code")) or (
            ManagedThreadStatusReason.THREAD_ARCHIVED.value
            if status == _STATUS_ARCHIVED
            else ManagedThreadStatusReason.THREAD_CREATED.value
        )
        changed_at = normalize_status_timestamp(
            _normalize_text(data.get("status_updated_at"))
        )
        terminal_raw = data.get("status_terminal")
        terminal = (
            terminal_raw
            if isinstance(terminal_raw, bool)
            else (
                bool(terminal_raw)
                if terminal_raw is not None
                else status in TERMINAL_STATUSES
            )
        )
        return cls(
            status=status,
            reason_code=reason_code,
            changed_at=changed_at,
            terminal=terminal,
            turn_id=_normalize_text(data.get("status_turn_id")),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "normalized_status": self.status,
            "status_reason_code": self.reason_code,
            "status_updated_at": self.changed_at,
            "status_terminal": self.terminal,
            "status_turn_id": self.turn_id,
        }


def build_managed_thread_status_snapshot(
    *,
    reason: str | ManagedThreadStatusReason,
    changed_at: Optional[str],
    turn_id: Optional[str] = None,
) -> ManagedThreadStatusSnapshot:
    reason_code = _normalize_reason(reason)
    transition = _TRANSITIONS.get(reason_code)
    target = transition["to"] if transition is not None else _STATUS_IDLE
    return ManagedThreadStatusSnapshot(
        status=target,
        reason_code=reason_code,
        changed_at=normalize_status_timestamp(changed_at),
        terminal=target in TERMINAL_STATUSES,
        turn_id=_normalize_text(turn_id),
    )


def _transition_allowed(current_status: str, reason_code: str) -> bool:
    transition = _TRANSITIONS.get(reason_code)
    if transition is None:
        return False
    allowed = transition["from"]
    if allowed == "*":
        return True
    return current_status in allowed


def transition_managed_thread_status(
    current: ManagedThreadStatusSnapshot,
    *,
    reason: str | ManagedThreadStatusReason,
    changed_at: Optional[str],
    turn_id: Optional[str] = None,
) -> ManagedThreadStatusSnapshot:
    reason_code = _normalize_reason(reason)
    if not _transition_allowed(current.status, reason_code):
        return current

    incoming_at = _parse_iso(changed_at)
    current_at = _parse_iso(current.changed_at)
    if current_at is not None and incoming_at is not None and incoming_at < current_at:
        return current

    candidate = build_managed_thread_status_snapshot(
        reason=reason_code,
        changed_at=changed_at,
        turn_id=turn_id,
    )
    if (
        candidate.status == current.status
        and candidate.reason_code == current.reason_code
        and candidate.turn_id == current.turn_id
    ):
        return current
    return candidate


def backfill_managed_thread_status(
    *,
    lifecycle_status: str | None,
    latest_turn_status: str | None,
    changed_at: Optional[str],
    compacted: bool = False,
) -> ManagedThreadStatusSnapshot:
    normalized_lifecycle = str(lifecycle_status or "").strip().lower()
    normalized_turn = str(latest_turn_status or "").strip().lower()
    if normalized_lifecycle == _STATUS_ARCHIVED:
        return build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.THREAD_ARCHIVED,
            changed_at=changed_at,
        )
    if normalized_turn == "running":
        return build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.TURN_STARTED,
            changed_at=changed_at,
        )
    if normalized_turn == "ok":
        return build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.MANAGED_TURN_COMPLETED,
            changed_at=changed_at,
        )
    if normalized_turn == "interrupted":
        return build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.MANAGED_TURN_INTERRUPTED,
            changed_at=changed_at,
        )
    if normalized_turn == "error":
        return build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.MANAGED_TURN_FAILED,
            changed_at=changed_at,
        )
    if compacted:
        return build_managed_thread_status_snapshot(
            reason=ManagedThreadStatusReason.THREAD_COMPACTED,
            changed_at=changed_at,
        )
    return build_managed_thread_status_snapshot(
        reason=ManagedThreadStatusReason.THREAD_CREATED,
        changed_at=changed_at,
    )


def derive_managed_thread_operator_status(
    *,
    normalized_status: str | None,
    lifecycle_status: str | None,
) -> str:
    normalized_lifecycle = str(lifecycle_status or "").strip().lower()
    normalized_runtime = str(normalized_status or "").strip().lower()

    if normalized_lifecycle == _STATUS_ARCHIVED:
        return _STATUS_ARCHIVED
    if normalized_runtime == _STATUS_COMPLETED:
        return _OP_REUSABLE
    if normalized_runtime == _STATUS_INTERRUPTED:
        return _OP_REUSABLE
    if normalized_runtime == _STATUS_FAILED:
        return _OP_ATTENTION_REQUIRED
    if normalized_runtime in {
        _STATUS_IDLE,
        _STATUS_RUNNING,
        _STATUS_PAUSED,
        _STATUS_ARCHIVED,
    }:
        return normalized_runtime
    if normalized_lifecycle == _STATUS_ACTIVE:
        return _STATUS_IDLE
    if normalized_lifecycle == _STATUS_ARCHIVED:
        return _STATUS_ARCHIVED
    return _STATUS_IDLE


__all__ = [
    "ManagedThreadStatusReason",
    "ManagedThreadStatusSnapshot",
    "TERMINAL_STATUSES",
    "TRANSITION_TABLE",
    "backfill_managed_thread_status",
    "build_managed_thread_status_snapshot",
    "derive_managed_thread_operator_status",
    "normalize_status_timestamp",
    "transition_managed_thread_status",
]
