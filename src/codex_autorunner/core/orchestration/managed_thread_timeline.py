from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..ports.run_event import (
    ApprovalRequested,
    OutputDelta,
    RunEvent,
    RunNotice,
    ToolCall,
    ToolResult,
)
from ..text_utils import _truncate_text
from .managed_thread_delivery_ledger import SQLiteManagedThreadDeliveryLedger
from .progress_projection import (
    ProgressProjectionInput,
    ProgressProjectionItem,
    ProgressProjectionState,
    reduce_progress_event,
)
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


def _progress_item_for_entry(
    entry: dict[str, Any],
    *,
    event_index: int,
    timestamp: Optional[str],
    state: ProgressProjectionState,
) -> Optional[ProgressProjectionItem]:
    if timestamp is None:
        return None
    event_type = str(entry.get("event_type") or "")
    event = _event_payload(entry)
    run_event: Optional[RunEvent] = None
    if event_type == "output_delta":
        run_event = OutputDelta(
            timestamp=timestamp,
            content=str(event.get("content") or ""),
            delta_type=str(event.get("delta_type") or "text"),
        )
    elif event_type == "run_notice":
        data = event.get("data")
        run_event = RunNotice(
            timestamp=timestamp,
            kind=str(event.get("kind") or ""),
            message=str(event.get("message") or ""),
            data=dict(data) if isinstance(data, dict) else {},
        )
    elif event_type == "tool_call":
        tool_input = event.get("tool_input")
        run_event = ToolCall(
            timestamp=timestamp,
            tool_name=str(event.get("tool_name") or ""),
            tool_input=dict(tool_input) if isinstance(tool_input, dict) else {},
        )
    elif event_type == "tool_result":
        run_event = ToolResult(
            timestamp=timestamp,
            tool_name=str(event.get("tool_name") or ""),
            status=str(event.get("status") or ""),
            result=event.get("result"),
            error=event.get("error"),
        )
    elif event_type == "approval_requested":
        context = event.get("context")
        run_event = ApprovalRequested(
            timestamp=timestamp,
            request_id=str(event.get("request_id") or ""),
            description=str(event.get("description") or ""),
            context=dict(context) if isinstance(context, dict) else {},
        )
    if run_event is None:
        return None
    item = reduce_progress_event(
        state,
        ProgressProjectionInput(
            event_id=event_index,
            timestamp=timestamp,
            event=run_event,
        ),
    )
    if item is None or item.hidden:
        return None
    return item


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
    tool_group: Optional[dict[str, Any]] = None
    projection_state = ProgressProjectionState()

    def flush_tool_group() -> None:
        # Timeline ordering is the contract; do not let grouped tool events drift
        # past later approvals, notices, or output items.
        nonlocal sequence, tool_group
        if tool_group is None:
            return
        item_id = (
            f"turn:{managed_turn_id}:tool:"
            f"{tool_group.get('first_index')}:{tool_group.get('tool_name')}"
        )
        timestamp = _normalize_optional_text(tool_group.get("timestamp"))
        items.append(
            ManagedThreadTimelineItem(
                item_id=item_id,
                kind="tool_group",
                order_key=_order_key(timestamp, sequence, item_id),
                timestamp=timestamp,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                status=str(tool_group.get("status") or "running"),
                payload={
                    "tool_name": tool_group.get("tool_name"),
                    "call": tool_group.get("call"),
                    "result": tool_group.get("result"),
                    "progress_items": [
                        item
                        for item in tool_group.get("progress_items", [])
                        if isinstance(item, dict)
                    ],
                },
            )
        )
        sequence += 1
        tool_group = None

    for fallback, entry in enumerate(entries, start=1):
        event_type = str(entry.get("event_type") or "")
        event = _event_payload(entry)
        event_index = _event_index(entry, fallback)
        timestamp = _event_timestamp(entry)
        event_stable_id = f"{event_index:04d}"
        progress_item = _progress_item_for_entry(
            entry,
            event_index=event_index,
            timestamp=timestamp,
            state=projection_state,
        )

        if event_type in {"tool_call", "tool_result"}:
            tool_name = str(event.get("tool_name") or "unknown")
            if event_type == "tool_call":
                flush_tool_group()
                tool_group = {
                    "tool_name": tool_name,
                    "first_index": event_index,
                    "timestamp": timestamp,
                    "call": event,
                    "result": None,
                    "status": "running",
                    "progress_items": (
                        [progress_item.to_dict()] if progress_item is not None else []
                    ),
                }
            else:
                if tool_group is None or tool_group.get("tool_name") != tool_name:
                    flush_tool_group()
                    tool_group = {
                        "tool_name": tool_name,
                        "first_index": event_index,
                        "timestamp": timestamp,
                        "call": None,
                        "result": None,
                        "status": "running",
                        "progress_items": [],
                    }
                tool_group["result"] = event
                tool_group["status"] = str(event.get("status") or "completed")
                if progress_item is not None:
                    tool_group.setdefault("progress_items", []).append(
                        progress_item.to_dict()
                    )
                flush_tool_group()
            continue

        flush_tool_group()
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
                    payload={
                        **event,
                        "source_event_ids": [event_index],
                        "source_event_type": event_type,
                        "progress_item": (
                            progress_item.to_dict()
                            if progress_item is not None
                            else None
                        ),
                    },
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
                        "source_event_ids": [event_index],
                        "source_event_type": event_type,
                        "detail_available": True,
                        "progress_item": (
                            progress_item.to_dict()
                            if progress_item is not None
                            else None
                        ),
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
                        "source_event_ids": [event_index],
                        "source_event_type": event_type,
                        "detail_available": True,
                        "progress_item": (
                            progress_item.to_dict()
                            if progress_item is not None
                            else None
                        ),
                    },
                )
            )
            sequence += 1

    flush_tool_group()
    return sequence


def timeline_item_from_tail_event(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    tail_event: dict[str, Any],
) -> dict[str, Any] | None:
    """Project one live tail event into the canonical PMA timeline item shape.

    Live web streams should append/update the same durable item contract returned
    by `/timeline`; chat adapters can keep rendering compact progress summaries.
    """

    normalized_thread_id = _normalize_optional_text(managed_thread_id)
    normalized_turn_id = _normalize_optional_text(managed_turn_id)
    if normalized_thread_id is None or normalized_turn_id is None:
        return None

    event_type = str(tail_event.get("event_type") or "").strip()
    event_id = str(tail_event.get("event_id") or "").strip()
    timestamp = _normalize_optional_text(tail_event.get("received_at"))
    progress_item = tail_event.get("progress_item")
    progress = dict(progress_item) if isinstance(progress_item, dict) else {}
    progress_kind = str(
        tail_event.get("progress_kind") or progress.get("kind") or ""
    ).strip()
    progress_group_id = _normalize_optional_text(
        tail_event.get("progress_group_id") or progress.get("group_id")
    )
    progress_item_id = _normalize_optional_text(
        tail_event.get("progress_item_id") or progress.get("item_id")
    )
    stable_suffix = (
        progress_group_id
        or progress_item_id
        or event_id
        or str(tail_event.get("summary") or "event")
    )
    source_event_ids = progress.get("event_ids")
    if not isinstance(source_event_ids, list) or not source_event_ids:
        source_event_ids = [int(tail_event.get("event_id") or 0)]
    try:
        source_event_key = f"{int(source_event_ids[-1]):04d}"
    except (TypeError, ValueError):
        source_event_key = event_id or stable_suffix

    base = {
        "order_key": _order_key(
            timestamp,
            int(tail_event.get("event_id") or 0),
            stable_suffix,
        ),
        "timestamp": timestamp,
        "managed_thread_id": normalized_thread_id,
        "managed_turn_id": normalized_turn_id,
        "status": str(tail_event.get("progress_state") or "recorded"),
    }

    if event_type in {"tool_started", "tool_completed", "tool_failed"}:
        tool_name = (
            _normalize_optional_text(tail_event.get("tool_name"))
            or _normalize_optional_text(progress.get("tool_name"))
            or "tool"
        )
        tool_stable_id = stable_suffix
        if progress_group_id and progress_group_id.startswith("tools:"):
            parts = progress_group_id.split(":", 2)
            if len(parts) >= 2:
                try:
                    tool_stable_id = str(int(parts[1]))
                except ValueError:
                    tool_stable_id = parts[1]
        item_id = f"turn:{normalized_turn_id}:tool:{tool_stable_id}:{tool_name}"
        state = str(tail_event.get("tool_state") or progress.get("state") or "")
        result = None
        if state in {"completed", "failed"}:
            result = {
                "status": "error" if state == "failed" else "completed",
                "summary": tail_event.get("summary"),
            }
        return {
            **base,
            "item_id": item_id,
            "kind": "tool_group",
            "status": state or base["status"],
            "payload": {
                "tool_name": tool_name,
                "call": {
                    "tool_name": tool_name,
                    "summary": tail_event.get("summary"),
                },
                "result": result,
                "progress_items": [progress] if progress else [],
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "detail_available": True,
                "live_tail_event": dict(tail_event),
            },
        }

    if event_type in {"progress", "assistant_update"} or progress_kind in {
        "assistant_update",
        "notice",
    }:
        item_id = f"turn:{normalized_turn_id}:intermediate:{source_event_key}"
        text = str(tail_event.get("summary") or "")
        title = str(tail_event.get("title") or progress.get("title") or "").strip()
        intermediate_kind = (
            "thinking"
            if progress_kind == "assistant_update" or title.lower() == "thinking"
            else event_type or "notice"
        )
        return {
            **base,
            "item_id": item_id,
            "kind": "intermediate",
            "payload": {
                "intermediate_kind": intermediate_kind,
                "text": text,
                "event_type": event_type,
                "event": dict(tail_event),
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "detail_available": True,
                "progress_item": progress or None,
                "live_tail_event": dict(tail_event),
            },
        }

    if progress_kind == "approval" or event_type == "approval_requested":
        request_id = stable_suffix
        return {
            **base,
            "item_id": f"turn:{normalized_turn_id}:approval:{request_id}",
            "kind": "approval",
            "status": str(tail_event.get("progress_state") or "waiting"),
            "payload": {
                "request_id": request_id,
                "description": tail_event.get("summary") or "Approval requested",
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "detail_available": True,
                "progress_item": progress or None,
                "live_tail_event": dict(tail_event),
            },
        }

    if event_type in {"turn_failed", "turn_interrupted"}:
        item_id = f"turn:{normalized_turn_id}:intermediate:{source_event_key}"
        return {
            **base,
            "item_id": item_id,
            "kind": "intermediate",
            "status": "error" if event_type == "turn_failed" else "interrupted",
            "payload": {
                "intermediate_kind": event_type,
                "text": str(tail_event.get("summary") or ""),
                "event_type": event_type,
                "event": dict(tail_event),
                "source_event_ids": source_event_ids,
                "source_event_type": event_type,
                "detail_available": True,
                "progress_item": progress or None,
                "live_tail_event": dict(tail_event),
            },
        }

    return None


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


def _decode_action_payload(action: dict[str, Any]) -> dict[str, Any]:
    payload_json = action.get("payload_json")
    if not isinstance(payload_json, str) or not payload_json.strip():
        return {}
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return {"payload_decode_error": True, "payload_json": payload_json}
    return dict(payload) if isinstance(payload, dict) else {}


def _turn_merge_timestamp(turn: dict[str, Any]) -> Optional[str]:
    """Timestamp used to interleave compaction lifecycle rows with turns."""
    return _normalize_optional_text(turn.get("started_at") or turn.get("created_at"))


def _sorted_compact_actions(
    thread_store: Any, managed_thread_id: str
) -> list[dict[str, Any]]:
    list_actions = getattr(thread_store, "list_thread_actions", None)
    if not callable(list_actions):
        return []
    actions: list[dict[str, Any]] = []
    for action in list_actions(managed_thread_id):
        if str(action.get("action_type") or "") != "managed_thread_compact":
            continue
        action_id = str(action.get("action_id") or "")
        if action_id:
            actions.append(action)
    actions.sort(
        key=lambda a: (
            _normalize_optional_text(a.get("created_at")) or "\uffff",
            str(a.get("action_id") or ""),
        )
    )
    return actions


def _append_compact_lifecycle_item(
    items: list[ManagedThreadTimelineItem],
    *,
    managed_thread_id: str,
    action: dict[str, Any],
    sequence: int,
) -> int:
    action_type = str(action.get("action_type") or "")
    action_id = str(action.get("action_id") or "")
    payload = _decode_action_payload(action)
    timestamp = _normalize_optional_text(action.get("created_at"))
    item_id = f"action:{action_id}:compact"
    items.append(
        ManagedThreadTimelineItem(
            item_id=item_id,
            kind="lifecycle",
            order_key=_order_key(timestamp, sequence, item_id),
            timestamp=timestamp,
            managed_thread_id=managed_thread_id,
            managed_turn_id=None,
            status="recorded",
            payload={
                **payload,
                "lifecycle_kind": "chat_compacted",
                "title": "Chat compacted",
                "text": "Chat compacted. The next message starts a fresh backend session with the compacted context.",
                "action_id": action_id,
                "action_type": action_type,
            },
        )
    )
    return sequence + 1


def _append_turn_timeline_items(
    items: list[ManagedThreadTimelineItem],
    *,
    hub_root: Any,
    managed_thread_id: str,
    turn: dict[str, Any],
    sequence: int,
) -> int:
    managed_turn_id = str(turn.get("managed_turn_id") or "").strip()
    if not managed_turn_id:
        return sequence
    entries = list_turn_timeline(hub_root, execution_id=managed_turn_id)
    sequence = _append_user_message(
        items,
        managed_thread_id=managed_thread_id,
        turn=turn,
        sequence=sequence,
    )
    if str(turn.get("status") or "") in {"queued", "running"}:
        sequence = _append_status(
            items,
            managed_thread_id=managed_thread_id,
            turn=turn,
            sequence=sequence,
        )
    sequence = _append_timeline_event_items(
        items,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        entries=entries,
        sequence=sequence,
    )
    sequence = _append_assistant_message(
        items,
        managed_thread_id=managed_thread_id,
        turn=turn,
        entries=entries,
        sequence=sequence,
    )
    if str(turn.get("status") or "") not in {"queued", "running"}:
        sequence = _append_status(
            items,
            managed_thread_id=managed_thread_id,
            turn=turn,
            sequence=sequence,
            terminal_timestamp=(
                _terminal_timestamp_from_timeline(entries)
                or _normalize_optional_text(turn.get("finished_at"))
            ),
        )
    sequence = _append_attachment_artifacts(
        items,
        managed_thread_id=managed_thread_id,
        turn=turn,
        sequence=sequence,
    )
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
    compact_actions = _sorted_compact_actions(thread_store, normalized_thread_id)
    items: list[ManagedThreadTimelineItem] = []
    sequence = 1
    turn_index = 0
    action_index = 0
    while turn_index < len(turns) and action_index < len(compact_actions):
        turn = turns[turn_index]
        action = compact_actions[action_index]
        turn_ts = _turn_merge_timestamp(turn)
        act_ts = _normalize_optional_text(action.get("created_at"))
        if turn_ts is None or act_ts is None:
            emit_turn_first = True
        else:
            emit_turn_first = act_ts >= turn_ts
        if emit_turn_first:
            sequence = _append_turn_timeline_items(
                items,
                hub_root=hub_root,
                managed_thread_id=normalized_thread_id,
                turn=turn,
                sequence=sequence,
            )
            turn_index += 1
        else:
            sequence = _append_compact_lifecycle_item(
                items,
                managed_thread_id=normalized_thread_id,
                action=action,
                sequence=sequence,
            )
            action_index += 1
    while turn_index < len(turns):
        sequence = _append_turn_timeline_items(
            items,
            hub_root=hub_root,
            managed_thread_id=normalized_thread_id,
            turn=turns[turn_index],
            sequence=sequence,
        )
        turn_index += 1
    while action_index < len(compact_actions):
        sequence = _append_compact_lifecycle_item(
            items,
            managed_thread_id=normalized_thread_id,
            action=compact_actions[action_index],
            sequence=sequence,
        )
        action_index += 1
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
    "timeline_item_from_tail_event",
]
