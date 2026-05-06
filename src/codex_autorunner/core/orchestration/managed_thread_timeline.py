from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..text_utils import _truncate_text
from .managed_thread_delivery_ledger import SQLiteManagedThreadDeliveryLedger
from .turn_timeline import list_turn_timeline

TIMELINE_CONTRACT_VERSION = "managed_thread_timeline.v1"


@dataclass(frozen=True)
class ManagedThreadTimelineItem:
    item_id: str
    kind: str
    order_key: str
    timestamp: Optional[str]
    managed_thread_id: str
    managed_turn_id: Optional[str] = None
    status: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "item_id": self.item_id,
            "kind": self.kind,
            "order_key": self.order_key,
            "timestamp": self.timestamp,
            "managed_thread_id": self.managed_thread_id,
            "managed_turn_id": self.managed_turn_id,
            "status": self.status,
            "payload": dict(self.payload),
        }
        return data


def _normalize_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata(turn: dict[str, Any]) -> dict[str, Any]:
    value = turn.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _turn_timestamp(turn: dict[str, Any]) -> Optional[str]:
    return _normalize_optional_text(
        turn.get("started_at") or turn.get("created_at") or turn.get("finished_at")
    )


def _order_key(timestamp: Optional[str], sequence: int, item_id: str) -> str:
    return f"{sequence:08d}|{timestamp or ''}|{item_id}"


def _event_timestamp(entry: dict[str, Any]) -> Optional[str]:
    event = entry.get("event")
    if isinstance(event, dict):
        timestamp = _normalize_optional_text(event.get("timestamp"))
        if timestamp is not None:
            return timestamp
    return _normalize_optional_text(entry.get("timestamp"))


def _event_payload(entry: dict[str, Any]) -> dict[str, Any]:
    event = entry.get("event")
    return dict(event) if isinstance(event, dict) else {}


def _event_index(entry: dict[str, Any], fallback: int) -> int:
    raw = entry.get("event_index")
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return fallback


def _assistant_text_from_timeline(entries: Iterable[dict[str, Any]]) -> Optional[str]:
    final_message: Optional[str] = None
    for entry in entries:
        if str(entry.get("event_type") or "") != "turn_completed":
            continue
        event = _event_payload(entry)
        final_message = str(event.get("final_message") or "")
    return final_message if final_message else None


def _terminal_timestamp_from_timeline(
    entries: Iterable[dict[str, Any]],
) -> Optional[str]:
    timestamp: Optional[str] = None
    for entry in entries:
        if str(entry.get("event_type") or "") not in {"turn_completed", "turn_failed"}:
            continue
        timestamp = _event_timestamp(entry) or timestamp
    return timestamp


def _append_user_message(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    timestamp = _turn_timestamp(turn)
    metadata = _metadata(turn)
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    item_id = f"turn:{managed_turn_id}:user"
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="user_message",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            status=str(turn.get("status") or ""),
            payload={
                "text": str(turn.get("prompt") or ""),
                "text_preview": _truncate_text(str(turn.get("prompt") or ""), 240),
                "client_turn_id": turn.get("client_turn_id"),
                "request_kind": turn.get("request_kind"),
                "attachments": [a for a in attachments if isinstance(a, dict)],
            },
        )
    )
    return sequence + 1


def _append_status(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
    terminal_timestamp: Optional[str] = None,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    status = str(turn.get("status") or "unknown")
    timestamp = terminal_timestamp or _turn_timestamp(turn)
    item_id = f"turn:{managed_turn_id}:status:{status}"
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="status",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            status=status,
            payload={
                "status": status,
                "error": turn.get("error"),
                "started_at": turn.get("started_at"),
                "finished_at": turn.get("finished_at"),
                "backend_turn_id": turn.get("backend_turn_id"),
            },
        )
    )
    return sequence + 1


def _append_timeline_event_items(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    entries: list[dict[str, Any]],
    sequence: int,
) -> int:
    tool_groups: dict[str, dict[str, Any]] = {}
    for fallback, entry in enumerate(entries, start=1):
        event_type = str(entry.get("event_type") or "")
        event = _event_payload(entry)
        event_index = _event_index(entry, fallback)
        timestamp = _event_timestamp(entry)
        event_stable_id = str(entry.get("event_id") or f"{event_index:04d}")

        if event_type in {"tool_call", "tool_result"}:
            tool_name = str(event.get("tool_name") or "unknown")
            group_key = f"{managed_turn_id}:{tool_name}:{event_index}"
            if event_type == "tool_result":
                matching_key = next(
                    (
                        key
                        for key, value in reversed(list(tool_groups.items()))
                        if value.get("tool_name") == tool_name
                        and value.get("result") is None
                    ),
                    None,
                )
                group_key = matching_key or group_key
            group = tool_groups.setdefault(
                group_key,
                {
                    "tool_name": tool_name,
                    "first_index": event_index,
                    "timestamp": timestamp,
                    "call": None,
                    "result": None,
                    "status": "running",
                },
            )
            if event_type == "tool_call":
                group["call"] = event
                group["timestamp"] = group.get("timestamp") or timestamp
            else:
                group["result"] = event
                group["status"] = str(event.get("status") or "completed")
            continue

        if event_type == "approval_requested":
            request_id = str(event.get("request_id") or event_stable_id)
            item_id = f"turn:{managed_turn_id}:approval:{request_id}"
            items.append(
                ManagedThreadTimelineItem(
                    item_id=item_id,
                    kind="approval",
                    order_key=_order_key(timestamp, sequence, item_id),
                    timestamp=timestamp,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    status=str(entry.get("status") or "recorded"),
                    payload=event,
                )
            )
            sequence += 1
            continue

        if event_type == "run_notice":
            item_id = f"turn:{managed_turn_id}:intermediate:{event_stable_id}"
            items.append(
                ManagedThreadTimelineItem(
                    item_id=item_id,
                    kind="intermediate",
                    order_key=_order_key(timestamp, sequence, item_id),
                    timestamp=timestamp,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    status=str(entry.get("status") or "recorded"),
                    payload={
                        "intermediate_kind": event.get("kind") or "notice",
                        "text": event.get("message") or "",
                        "event_type": event_type,
                        "event": event,
                    },
                )
            )
            sequence += 1
            continue

        if event_type == "output_delta":
            item_id = f"turn:{managed_turn_id}:intermediate:{event_stable_id}"
            items.append(
                ManagedThreadTimelineItem(
                    item_id=item_id,
                    kind="intermediate",
                    order_key=_order_key(timestamp, sequence, item_id),
                    timestamp=timestamp,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    status=str(entry.get("status") or "recorded"),
                    payload={
                        "intermediate_kind": event.get("delta_type") or "output",
                        "text": event.get("content") or "",
                        "event_type": event_type,
                        "event": event,
                    },
                )
            )
            sequence += 1

    for group in sorted(
        tool_groups.values(),
        key=lambda value: (
            str(value.get("timestamp") or ""),
            int(value.get("first_index") or 0),
            str(value.get("tool_name") or ""),
        ),
    ):
        item_id = (
            f"turn:{managed_turn_id}:tool:"
            f"{group.get('first_index')}:{group.get('tool_name')}"
        )
        timestamp = _normalize_optional_text(group.get("timestamp"))
        items.append(
            ManagedThreadTimelineItem(
                item_id=item_id,
                kind="tool_group",
                order_key=_order_key(timestamp, sequence, item_id),
                timestamp=timestamp,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                status=str(group.get("status") or "running"),
                payload={
                    "tool_name": group.get("tool_name"),
                    "call": group.get("call"),
                    "result": group.get("result"),
                },
            )
        )
        sequence += 1
    return sequence


def _append_assistant_message(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    entries: list[dict[str, Any]],
    sequence: int,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    assistant_text = _normalize_optional_text(turn.get("assistant_text"))
    if assistant_text is None:
        assistant_text = _assistant_text_from_timeline(entries)
    if assistant_text is None:
        return sequence
    timestamp = (
        _terminal_timestamp_from_timeline(entries)
        or _normalize_optional_text(turn.get("finished_at"))
        or _turn_timestamp(turn)
    )
    item_id = f"turn:{managed_turn_id}:assistant"
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="assistant_message",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            status=str(turn.get("status") or ""),
            payload={
                "text": assistant_text,
                "text_preview": _truncate_text(assistant_text, 240),
                "backend_turn_id": turn.get("backend_turn_id"),
            },
        )
    )
    return sequence + 1


def _append_attachment_artifacts(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "")
    metadata = _metadata(turn)
    timestamp = _turn_timestamp(turn)
    for field_name in ("attachments", "artifacts"):
        values = metadata.get(field_name)
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values, start=1):
            if not isinstance(value, dict):
                continue
            item_id = f"turn:{managed_turn_id}:{field_name}:{index}"
            items.append(
                ManagedThreadTimelineItem(
                    item_id=item_id,
                    kind="artifact",
                    order_key=_order_key(timestamp, sequence, item_id),
                    timestamp=timestamp,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    status=str(turn.get("status") or ""),
                    payload={"artifact_kind": field_name.rstrip("s"), **value},
                )
            )
            sequence += 1
    return sequence


def _list_delivery_records(hub_root: Any, managed_thread_id: str) -> list[Any]:
    ledger = SQLiteManagedThreadDeliveryLedger(hub_root, durable=False)
    return ledger.list_records_for_managed_thread(managed_thread_id)


def _append_delivery_state_items(
    items: list[ManagedThreadTimelineItem],
    *,
    hub_root: Any,
    managed_thread_id: str,
    sequence: int,
) -> int:
    for record in _list_delivery_records(hub_root, managed_thread_id):
        item_id = f"delivery:{record.delivery_id}"
        timestamp = record.updated_at or record.created_at
        items.append(
            ManagedThreadTimelineItem(
                item_id=item_id,
                kind="delivery_state",
                order_key=_order_key(timestamp, sequence, item_id),
                timestamp=timestamp,
                managed_thread_id=managed_thread_id,
                managed_turn_id=record.managed_turn_id,
                status=record.state.value,
                payload={
                    "delivery_id": record.delivery_id,
                    "surface_kind": record.target.surface_kind,
                    "surface_key": record.target.surface_key,
                    "adapter_key": record.target.adapter_key,
                    "state": record.state.value,
                    "attempt_count": record.attempt_count,
                    "delivered_at": record.delivered_at,
                    "last_error": record.last_error,
                    "final_status": record.envelope.final_status,
                },
            )
        )
        sequence += 1
    return sequence


def build_managed_thread_timeline(
    hub_root: Any,
    *,
    thread_store: Any,
    managed_thread_id: str,
    limit: int = 500,
) -> dict[str, Any]:
    normalized_thread_id = str(managed_thread_id or "").strip()
    if not normalized_thread_id:
        raise ValueError("managed_thread_id is required")
    bounded_limit = min(max(int(limit or 500), 1), 1000)
    thread = thread_store.get_thread(normalized_thread_id)
    if thread is None:
        raise KeyError(normalized_thread_id)

    turns = list(
        reversed(thread_store.list_turns(normalized_thread_id, limit=bounded_limit))
    )
    items: list[ManagedThreadTimelineItem] = []
    sequence = 1
    for turn in turns:
        managed_turn_id = str(turn.get("managed_turn_id") or "").strip()
        if not managed_turn_id:
            continue
        entries = list_turn_timeline(hub_root, execution_id=managed_turn_id)
        sequence = _append_user_message(
            items,
            managed_thread_id=normalized_thread_id,
            turn=turn,
            sequence=sequence,
        )
        if str(turn.get("status") or "") in {"queued", "running"}:
            sequence = _append_status(
                items,
                managed_thread_id=normalized_thread_id,
                turn=turn,
                sequence=sequence,
            )
        sequence = _append_timeline_event_items(
            items,
            managed_thread_id=normalized_thread_id,
            managed_turn_id=managed_turn_id,
            entries=entries,
            sequence=sequence,
        )
        sequence = _append_assistant_message(
            items,
            managed_thread_id=normalized_thread_id,
            turn=turn,
            entries=entries,
            sequence=sequence,
        )
        if str(turn.get("status") or "") not in {"queued", "running"}:
            sequence = _append_status(
                items,
                managed_thread_id=normalized_thread_id,
                turn=turn,
                sequence=sequence,
                terminal_timestamp=(
                    _terminal_timestamp_from_timeline(entries)
                    or _normalize_optional_text(turn.get("finished_at"))
                ),
            )
        sequence = _append_attachment_artifacts(
            items,
            managed_thread_id=normalized_thread_id,
            turn=turn,
            sequence=sequence,
        )
    sequence = _append_delivery_state_items(
        items,
        hub_root=hub_root,
        managed_thread_id=normalized_thread_id,
        sequence=sequence,
    )

    ordered = sorted(items, key=lambda item: item.order_key)
    return {
        "managed_thread_id": normalized_thread_id,
        "contract_version": TIMELINE_CONTRACT_VERSION,
        "items": [item.to_dict() for item in ordered],
        "item_count": len(ordered),
    }


__all__ = [
    "TIMELINE_CONTRACT_VERSION",
    "ManagedThreadTimelineItem",
    "build_managed_thread_timeline",
]
