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


class ManagedThreadStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ManagedThreadOperatorStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    REUSABLE = "reusable"
    ATTENTION_REQUIRED = "attention_required"
    ARCHIVED = "archived"


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
        ManagedThreadStatus.COMPLETED.value,
        ManagedThreadStatus.INTERRUPTED.value,
        ManagedThreadStatus.FAILED.value,
        ManagedThreadStatus.ARCHIVED.value,
    }
)

TRANSITION_TABLE: tuple[dict[str, Any], ...] = (
    {
        "signal": ManagedThreadStatusReason.THREAD_CREATED.value,
        "from": "*",
        "to": ManagedThreadStatus.IDLE.value,
    },
    {
        "signal": ManagedThreadStatusReason.THREAD_RESUMED.value,
        "from": (
            ManagedThreadStatus.PAUSED.value,
            ManagedThreadStatus.ARCHIVED.value,
        ),
        "to": ManagedThreadStatus.IDLE.value,
    },
    {
        "signal": ManagedThreadStatusReason.TURN_STARTED.value,
        "from": (
            ManagedThreadStatus.IDLE.value,
            ManagedThreadStatus.COMPLETED.value,
            ManagedThreadStatus.INTERRUPTED.value,
            ManagedThreadStatus.FAILED.value,
            ManagedThreadStatus.PAUSED.value,
        ),
        "to": ManagedThreadStatus.RUNNING.value,
    },
    {
        "signal": ManagedThreadStatusReason.THREAD_COMPACTED.value,
        "from": (
            ManagedThreadStatus.IDLE.value,
            ManagedThreadStatus.COMPLETED.value,
            ManagedThreadStatus.INTERRUPTED.value,
            ManagedThreadStatus.FAILED.value,
            ManagedThreadStatus.PAUSED.value,
        ),
        "to": ManagedThreadStatus.PAUSED.value,
    },
    {
        "signal": ManagedThreadStatusReason.MANAGED_TURN_COMPLETED.value,
        "from": (ManagedThreadStatus.RUNNING.value,),
        "to": ManagedThreadStatus.COMPLETED.value,
    },
    {
        "signal": ManagedThreadStatusReason.MANAGED_TURN_FAILED.value,
        "from": (ManagedThreadStatus.RUNNING.value,),
        "to": ManagedThreadStatus.FAILED.value,
    },
    {
        "signal": ManagedThreadStatusReason.MANAGED_TURN_INTERRUPTED.value,
        "from": (ManagedThreadStatus.RUNNING.value,),
        "to": ManagedThreadStatus.INTERRUPTED.value,
    },
    {
        "signal": ManagedThreadStatusReason.THREAD_ARCHIVED.value,
        "from": "*",
        "to": ManagedThreadStatus.ARCHIVED.value,
    },
)

_TRANSITIONS = {
    str(entry["signal"]).strip().lower(): entry for entry in TRANSITION_TABLE
}


def _normalize_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


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
    status: str = ManagedThreadStatus.IDLE.value
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
        status = _normalize_text(data.get("normalized_status")) or _normalize_text(
            data.get("status")
        )
        if status is None:
            status = ManagedThreadStatus.IDLE.value
        reason_code = _normalize_text(
            data.get("status_reason_code") or data.get("status_reason")
        ) or (
            ManagedThreadStatusReason.THREAD_ARCHIVED.value
            if status == ManagedThreadStatus.ARCHIVED.value
            else ManagedThreadStatusReason.THREAD_CREATED.value
        )
        changed_at = normalize_status_timestamp(
            _normalize_text(
                data.get("status_updated_at") or data.get("status_changed_at")
            )
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
    target = (
        transition["to"] if transition is not None else ManagedThreadStatus.IDLE.value
    )
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
    if normalized_lifecycle == ManagedThreadStatus.ARCHIVED.value:
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

    if normalized_lifecycle == ManagedThreadStatus.ARCHIVED.value:
        return ManagedThreadOperatorStatus.ARCHIVED.value
    if normalized_runtime == ManagedThreadStatus.COMPLETED.value:
        return ManagedThreadOperatorStatus.REUSABLE.value
    if normalized_runtime == ManagedThreadStatus.INTERRUPTED.value:
        return ManagedThreadOperatorStatus.REUSABLE.value
    if normalized_runtime == ManagedThreadStatus.FAILED.value:
        return ManagedThreadOperatorStatus.ATTENTION_REQUIRED.value
    if normalized_runtime in {
        ManagedThreadStatus.IDLE.value,
        ManagedThreadStatus.RUNNING.value,
        ManagedThreadStatus.PAUSED.value,
        ManagedThreadOperatorStatus.ARCHIVED.value,
    }:
        return normalized_runtime
    if normalized_lifecycle == ManagedThreadStatus.ACTIVE.value:
        return ManagedThreadOperatorStatus.IDLE.value
    if normalized_lifecycle == ManagedThreadStatus.ARCHIVED.value:
        return ManagedThreadOperatorStatus.ARCHIVED.value
    return ManagedThreadOperatorStatus.IDLE.value


__all__ = [
    "ManagedThreadOperatorStatus",
    "ManagedThreadStatus",
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
