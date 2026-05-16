from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from .managed_thread_timeline import (
    MAX_MANAGED_THREAD_TIMELINE_LIMIT,
    build_managed_thread_timeline,
    timeline_item_from_tail_event,
)

TRANSCRIPT_CONTRACT_VERSION = "managed_thread_transcript.v1"


def build_managed_thread_transcript(
    hub_root: Any,
    *,
    thread_store: Any,
    managed_thread_id: str,
    limit: int = MAX_MANAGED_THREAD_TIMELINE_LIMIT,
    progress_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the backend-owned chat transcript projection consumed by Web Hub.

    This is the ownership boundary for chat rendering. Runtime timeline rows and
    live tail/progress state can still be noisy below this layer, but the browser
    should receive one ordered transcript shape and render it without composing a
    parallel transcript from `/timeline` plus `/tail`.
    """

    clamped_limit = min(max(1, int(limit or 1)), MAX_MANAGED_THREAD_TIMELINE_LIMIT)
    timeline = build_managed_thread_timeline(
        hub_root,
        thread_store=thread_store,
        managed_thread_id=managed_thread_id,
        limit=clamped_limit,
    )
    # Transcript rows intentionally derive from canonical timeline items.
    # Live `/tail` events are admitted only after they are normalized through
    # `timeline_item_from_tail_event(...)`, so this layer has a single input
    # contract and does not compose web-local heuristics.
    timeline_items = [
        item for item in timeline.get("items", []) if isinstance(item, Mapping)
    ]
    rows = transcript_rows_from_timeline_items(timeline_items)

    status = dict(progress_snapshot or {})
    live_rows: list[dict[str, Any]] = []
    for event in status.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        item = timeline_item_from_tail_event(
            managed_thread_id=managed_thread_id,
            managed_turn_id=str(status.get("managed_turn_id") or ""),
            tail_event=dict(event),
        )
        if item is None:
            continue
        live_rows.extend(transcript_rows_from_timeline_items([item]))

    rows = _merge_transcript_rows(rows, live_rows)
    rows = _retain_transcript_window(rows, clamped_limit)
    return {
        "contract_version": TRANSCRIPT_CONTRACT_VERSION,
        "managed_thread_id": managed_thread_id,
        "cursor": {
            "last_event_id": status.get("last_event_id"),
            "managed_turn_id": status.get("managed_turn_id"),
        },
        "projection": {
            "kind": "transcript",
            "limit": clamped_limit,
            "source_timeline_contract_version": timeline.get("contract_version"),
            "backend_owned_rows": True,
        },
        "row_count": len(rows),
        "rows": rows,
        "status": status or None,
    }


def transcript_rows_from_timeline_items(
    items: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.extend(_timeline_item_to_transcript_rows(item))
    return rows


def transcript_row_from_tail_event(
    *,
    managed_thread_id: str,
    managed_turn_id: str,
    tail_event: Mapping[str, Any],
) -> list[dict[str, Any]]:
    item = timeline_item_from_tail_event(
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        tail_event=dict(tail_event),
    )
    if item is None:
        return []
    return transcript_rows_from_timeline_items([item])


def _timeline_item_to_transcript_rows(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    kind = str(item.get("kind") or "").strip()
    item_id = str(item.get("item_id") or item.get("id") or "")
    if not item_id:
        return []
    payload = _mapping(item.get("payload"))
    managed_thread_id = _optional_text(
        item.get("managed_thread_id") or item.get("thread_id") or item.get("chat_id")
    )
    managed_turn_id = _optional_text(item.get("managed_turn_id") or item.get("turn_id"))
    order_key = str(item.get("order_key") or item_id)
    timestamp = _optional_text(item.get("timestamp"))
    status = _optional_text(item.get("status"))
    identity = _mapping(item.get("identity"))
    payload_client_turn_id = _optional_text(payload.get("client_turn_id"))
    identity_correlation_id = _optional_text(identity.get("correlation_id"))
    client_turn_id = payload_client_turn_id or identity_correlation_id
    correlation_id = identity_correlation_id or payload_client_turn_id

    if kind in {"user_message", "assistant_message"}:
        text = _optional_text(payload.get("text")) or ""
        if not text.strip():
            return []
        role = "user" if kind == "user_message" else "assistant"
        return [
            {
                "kind": "message",
                "id": item_id,
                "turn_id": managed_turn_id,
                "client_turn_id": client_turn_id,
                "correlation_id": correlation_id,
                "identity": dict(identity),
                "order_key": order_key,
                "timestamp": timestamp,
                "message": {
                    "id": item_id,
                    "chat_id": managed_thread_id,
                    "role": role,
                    "text": text,
                    "created_at": timestamp,
                    "status": status,
                    "client_turn_id": client_turn_id,
                    "correlation_id": correlation_id,
                    "identity": dict(identity),
                    "artifacts": _artifact_list(payload.get("attachments")),
                    "raw": dict(item),
                },
            }
        ]

    if kind == "intermediate":
        text = _optional_text(payload.get("text")) or ""
        if not text.strip() or _is_hidden_intermediate(payload):
            return []
        return [
            {
                "kind": "intermediate",
                "id": item_id,
                "title": _intermediate_title(payload),
                "text": text,
                "event_ids": _source_event_ids(item),
                "progress_source_ids": [],
                "detail": _json_detail(
                    payload.get("live_tail_event")
                    or payload.get("event")
                    or payload.get("result")
                    or payload.get("call")
                ),
                "turn_id": managed_turn_id,
                "order_key": order_key,
                "timestamp": timestamp,
            }
        ]

    if kind == "tool_group":
        return [
            {
                "kind": "tool_group",
                "id": item_id,
                "tools": [_tool_card_from_item(item, payload)],
                "turn_id": managed_turn_id,
                "order_key": order_key,
                "timestamp": timestamp,
            }
        ]

    if kind == "approval":
        summary = (
            _optional_text(payload.get("description"))
            or _optional_text(payload.get("summary"))
            or "Approval requested"
        )
        return [
            {
                "kind": "approval",
                "id": item_id,
                "title": "Approval requested",
                "summary": summary,
                "detail": _json_detail(payload.get("event") or payload),
                "turn_id": managed_turn_id,
                "order_key": order_key,
                "timestamp": timestamp,
            }
        ]

    if kind == "lifecycle":
        title = _optional_text(payload.get("title")) or "Chat compacted"
        text = _optional_text(payload.get("text")) or "Chat compacted."
        preview = _optional_text(payload.get("summary_preview"))
        return [
            {
                "kind": "lifecycle",
                "id": item_id,
                "title": title,
                "text": f"{text}\n\n{preview}" if preview else text,
                "detail": _json_detail(payload.get("event") or payload),
                "turn_id": managed_turn_id,
                "order_key": order_key,
                "timestamp": timestamp,
            }
        ]

    if kind == "artifact":
        return [{"kind": "artifact", "id": item_id, "artifact": dict(payload)}]
    return []


def _tool_card_from_item(
    item: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    result = _mapping(payload.get("result"))
    call = _mapping(payload.get("call"))
    raw_state = str(result.get("status") or item.get("status") or "").lower()
    if "fail" in raw_state or raw_state == "error":
        state = "failed"
    elif result:
        state = "completed"
    else:
        state = "started"
    return {
        "id": str(item.get("item_id") or item.get("id") or "tool"),
        "title": _optional_text(payload.get("tool_name"))
        or _optional_text(call.get("tool_name"))
        or "Tool call",
        "summary": _optional_text(result.get("summary"))
        or _optional_text(call.get("summary")),
        "detail": _json_detail(payload.get("result") or payload.get("call") or payload),
        "state": state,
        "event_ids": _source_event_ids(item),
    }


def _merge_transcript_rows(
    durable_rows: list[dict[str, Any]], live_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not live_rows:
        return _sort_transcript_rows(durable_rows)
    merged_by_id: dict[str, dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []
    for row in durable_rows:
        row_id = str(row.get("id") or "")
        if row_id:
            merged_by_id[row_id] = row
        merged.append(row)
    for row in live_rows:
        row_id = str(row.get("id") or "")
        if row_id and row_id in merged_by_id:
            replacement_index = next(
                index
                for index, current in enumerate(merged)
                if str(current.get("id") or "") == row_id
            )
            merged[replacement_index] = row
            merged_by_id[row_id] = row
            continue
        if row_id:
            merged_by_id[row_id] = row
        merged.append(row)
    return _sort_transcript_rows(merged)


def _retain_transcript_window(
    rows: list[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    retained = rows[-limit:]
    anchors: list[dict[str, Any]] = []
    for row in rows:
        if (
            row.get("kind") == "message"
            and _mapping(row.get("message")).get("role") == "user"
        ):
            anchors.append(row)
            break
    for anchor in anchors:
        anchor_id = anchor.get("id")
        if anchor_id and not any(row.get("id") == anchor_id for row in retained):
            retained = [anchor, *retained[1:]]
    return _sort_transcript_rows(retained)


def _sort_transcript_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turn_anchors: dict[str, str] = {}
    turn_fallbacks: dict[str, str] = {}
    for row in rows:
        turn_id = _optional_text(row.get("turn_id"))
        if turn_id is None:
            continue
        generic_key = _generic_row_sort_key(row)
        existing_fallback = turn_fallbacks.get(turn_id)
        if existing_fallback is None or generic_key < existing_fallback:
            turn_fallbacks[turn_id] = generic_key
        if _message_role(row) == "user":
            existing_anchor = turn_anchors.get(turn_id)
            if existing_anchor is None or generic_key < existing_anchor:
                turn_anchors[turn_id] = generic_key

    for turn_id, fallback_key in turn_fallbacks.items():
        turn_anchors.setdefault(turn_id, fallback_key)

    return sorted(rows, key=lambda row: _row_sort_key(row, turn_anchors))


def _row_sort_key(
    row: Mapping[str, Any], turn_anchors: Mapping[str, str]
) -> tuple[str, int, str, str]:
    generic_key = _generic_row_sort_key(row)
    turn_id = _optional_text(row.get("turn_id"))
    if turn_id is None:
        return (generic_key, _row_phase(row), generic_key, str(row.get("id") or ""))
    return (
        str(turn_anchors.get(turn_id) or generic_key),
        _row_phase(row),
        generic_key,
        str(row.get("id") or ""),
    )


def _generic_row_sort_key(row: Mapping[str, Any]) -> str:
    return str(row.get("order_key") or row.get("timestamp") or row.get("id") or "")


def _row_phase(row: Mapping[str, Any]) -> int:
    role = _message_role(row)
    if role == "user":
        return 0
    if role == "assistant":
        return 2
    return 1


def _message_role(row: Mapping[str, Any]) -> Optional[str]:
    if row.get("kind") != "message":
        return None
    role = _mapping(row.get("message")).get("role")
    return str(role) if role is not None else None


def _source_event_ids(item: Mapping[str, Any]) -> list[str]:
    provenance = _mapping(item.get("provenance"))
    values = provenance.get("source_event_ids")
    if not isinstance(values, list):
        return []
    return [str(value) for value in values]


def _artifact_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_detail(value: Any) -> Optional[str]:
    if not isinstance(value, Mapping):
        return None
    try:
        return json.dumps(dict(value), indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return None


def _is_hidden_intermediate(payload: Mapping[str, Any]) -> bool:
    if payload.get("hidden") is True:
        return True
    intermediate_kind = str(payload.get("intermediate_kind") or "").lower()
    event_type = str(payload.get("event_type") or "").lower()
    if intermediate_kind == "decode_failure":
        return True
    return event_type == "output_delta" and intermediate_kind in {
        "assistant_stream",
        "assistant_message",
        "log_line",
    }


def _intermediate_title(payload: Mapping[str, Any]) -> str:
    title = _optional_text(payload.get("title")) or _optional_text(
        payload.get("intermediate_kind")
    )
    if not title:
        return "Update"
    return title.replace("_", " ").replace(".", " ").strip().title()


__all__ = [
    "TRANSCRIPT_CONTRACT_VERSION",
    "build_managed_thread_transcript",
    "transcript_row_from_tail_event",
    "transcript_rows_from_timeline_items",
]
