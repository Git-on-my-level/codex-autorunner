"""Managed-thread tail event serialization, diagnostics, and payload shaping.

Single owner for live runtime events, persisted timeline entries, progress phase
derivation, operator-diagnostics shaping, and status response assembly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..managed_thread_status import derive_managed_thread_operator_status
from ..orchestration.progress_projection import (
    ProgressProjectionInput,
    ProgressProjectionItem,
    ProgressProjectionState,
    project_progress_events,
    reduce_progress_event,
)
from ..orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
)
from ..ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from ..redaction import redact_text
from ..text_utils import _normalize_optional_text as normalize_optional_text


def coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_from_event_ms(value: Any) -> Optional[str]:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


def truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def _redact_nested(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): _redact_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_nested(item) for item in value]
    return value


_NO_STREAM_AVAILABLE_IDLE_SECONDS = 15
_LIKELY_HUNG_IDLE_SECONDS = 90
_STALL_IDLE_SECONDS = 30
_BATCHED_INITIAL_EVENT_GRACE_SECONDS = 5 * 60
_BATCHED_INITIAL_EVENT_AGENTS = frozenset({"codex"})
LIVE_ACTIVITY_CONTRACT_VERSION = "pma_live_activity.v1"
LIVE_ACTIVITY_EVENT_WINDOW = 5


def _agent_batches_initial_events(agent_id: Any) -> bool:
    text = normalize_optional_text(agent_id)
    return bool(text and text.lower() in _BATCHED_INITIAL_EVENT_AGENTS)


def _running_turn_stall_flags(
    *,
    idle_seconds: Optional[int],
    last_event_at: Optional[str],
    agent_id: Any = None,
    has_visible_events: Optional[bool] = None,
) -> tuple[bool, Optional[str]]:
    has_no_visible_events = (
        has_visible_events is False if has_visible_events is not None else False
    )
    idle = int(idle_seconds or 0)
    if (
        (last_event_at is None or has_no_visible_events)
        and _agent_batches_initial_events(agent_id)
        and idle < _BATCHED_INITIAL_EVENT_GRACE_SECONDS
    ):
        return (False, None)
    stalled = idle_seconds is not None and idle_seconds >= _STALL_IDLE_SECONDS
    if not stalled:
        return (False, None)
    reason = (
        "no_events_yet"
        if last_event_at is None or has_no_visible_events
        else "no_new_events_since_last_progress"
    )
    return (True, reason)


def _truncate_tool_name(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if text is None:
        return None
    return truncate_text(text, 80)


def _parse_inline_sse(raw_event: str) -> tuple[str, dict[str, Any]]:
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in str(raw_event).splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    payload: dict[str, Any] = {}
    data = "\n".join(data_lines)
    if data:
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            payload = {}
        else:
            payload = coerce_dict(parsed)
    return event_name, payload


def _runtime_raw_payload(raw_event: Any) -> dict[str, Any]:
    if isinstance(raw_event, dict):
        return dict(raw_event)
    if isinstance(raw_event, str):
        _event_name, payload = _parse_inline_sse(raw_event)
        return payload
    return {}


def _runtime_method_and_params(raw_event: Any) -> tuple[str, dict[str, Any]]:
    payload = _runtime_raw_payload(raw_event)
    message = coerce_dict(payload.get("message"))
    if message:
        return (
            str(message.get("method") or "").strip().lower(),
            coerce_dict(message.get("params")),
        )
    return (
        str(payload.get("method") or "").strip().lower(),
        coerce_dict(payload.get("params")),
    )


def _runtime_terminal_tail_event(
    *,
    raw_event: Any,
    event_id: int,
    received_at: str,
) -> dict[str, Any] | None:
    method, params = _runtime_method_and_params(raw_event)
    if not method:
        return None
    status = str(params.get("status") or "").strip().lower()
    if method in {"prompt/completed", "turn/completed", "session.idle"}:
        event_type = "turn_completed"
        summary = "Turn completed"
        if status in {"interrupt", "interrupted", "cancelled", "canceled", "aborted"}:
            event_type = "turn_interrupted"
            summary = "Turn interrupted"
        elif status in {"error", "failed"}:
            event_type = "turn_failed"
            summary = "Turn failed"
        return {
            "event_id": event_id,
            "event_type": event_type,
            "summary": summary,
            "lines": [summary],
            "received_at_ms": None,
            "received_at": received_at,
            "tool_name": None,
            "tool_state": None,
            "source_event_ids": [event_id],
            "progress_event_ids": [event_id],
        }
    if method in {"prompt/cancelled", "turn/cancelled"}:
        return {
            "event_id": event_id,
            "event_type": "turn_interrupted",
            "summary": "Turn interrupted",
            "lines": ["Turn interrupted"],
            "received_at_ms": None,
            "received_at": received_at,
            "tool_name": None,
            "tool_state": None,
            "source_event_ids": [event_id],
            "progress_event_ids": [event_id],
        }
    if method in {"prompt/failed", "turn/failed", "turn/error", "error"}:
        detail = (
            str(params.get("message") or params.get("error") or "Turn failed").strip()
            or "Turn failed"
        )
        return {
            "event_id": event_id,
            "event_type": "turn_failed",
            "summary": truncate_text(redact_text(detail), 220),
            "lines": [truncate_text(redact_text(detail), 220)],
            "received_at_ms": None,
            "received_at": received_at,
            "tool_name": None,
            "tool_state": None,
            "source_event_ids": [event_id],
            "progress_event_ids": [event_id],
        }
    return None


def _tail_event_from_run_event(
    run_event: Any,
    *,
    event_id: int,
    received_at: str,
    projection_state: ProgressProjectionState | None = None,
) -> dict[str, Any] | None:
    state = projection_state or ProgressProjectionState()
    item = reduce_progress_event(
        state,
        ProgressProjectionInput(
            event_id=event_id,
            timestamp=received_at,
            event=run_event,
        ),
    )
    if item is None or item.hidden:
        return None
    progress_items: list[ProgressProjectionItem] | None = None
    if item.kind == "tool" and item.group_id:
        grouped = (*state.tool_group_items.get(item.group_id, ()), item)
        state.tool_group_items[item.group_id] = grouped
        progress_items = list(grouped)
    return _tail_event_from_progress_item(
        item,
        received_at=received_at,
        progress_items=progress_items,
    )


def _tail_event_type_from_progress_item(item: ProgressProjectionItem) -> str:
    if item.kind == "tool":
        return {
            "started": "tool_started",
            "completed": "tool_completed",
            "failed": "tool_failed",
        }.get(item.state, "tool_started")
    if item.kind == "turn_failed":
        return "turn_failed"
    if item.kind == "turn_interrupted":
        return "turn_interrupted"
    if item.kind == "turn_completed":
        return "turn_completed"
    if item.kind == "assistant_update":
        return "assistant_update"
    return "progress"


def _tail_event_from_progress_item(
    item: ProgressProjectionItem,
    *,
    received_at: str,
    progress_items: list[ProgressProjectionItem] | None = None,
) -> dict[str, Any]:
    event_id = item.event_ids[-1] if item.event_ids else 0
    event_type = _tail_event_type_from_progress_item(item)
    grouped_items = progress_items or [item]
    progress_event_ids: list[int] = []
    progress_item_ids: list[str] = []
    for grouped_item in grouped_items:
        if grouped_item.item_id not in progress_item_ids:
            progress_item_ids.append(grouped_item.item_id)
        for grouped_event_id in grouped_item.event_ids:
            if grouped_event_id not in progress_event_ids:
                progress_event_ids.append(grouped_event_id)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "summary": item.summary or item.title,
        "title": item.title,
        "lines": [item.summary or item.title] if item.summary or item.title else [],
        "received_at_ms": None,
        "received_at": received_at,
        "tool_name": item.tool_name,
        "tool_state": item.state if item.kind == "tool" else None,
        "progress_item": item.to_dict(),
        "progress_items": [progress_item.to_dict() for progress_item in grouped_items],
        "progress_item_ids": progress_item_ids,
        "progress_item_id": item.item_id,
        "progress_kind": item.kind,
        "progress_state": item.state,
        "progress_group_id": item.group_id,
        "progress_group_kind": item.group_kind,
        "progress_event_ids": progress_event_ids,
    }


def _run_event_from_timeline_entry(entry: dict[str, Any]) -> Any | None:
    event_type = str(entry.get("event_type") or "").strip().lower()
    event = coerce_dict(entry.get("event"))
    timestamp = normalize_optional_text(
        event.get("timestamp")
    ) or normalize_optional_text(entry.get("timestamp"))
    if timestamp is None:
        return None
    if event_type == "output_delta":
        return OutputDelta(
            timestamp=timestamp,
            content=str(event.get("content") or ""),
            delta_type=str(event.get("delta_type") or "text"),
            stream_mode=str(event.get("stream_mode") or "delta"),
        )
    if event_type == "tool_call":
        return ToolCall(
            timestamp=timestamp,
            tool_name=str(event.get("tool_name") or ""),
            tool_input=coerce_dict(event.get("tool_input")),
        )
    if event_type == "tool_result":
        return ToolResult(
            timestamp=timestamp,
            tool_name=str(event.get("tool_name") or ""),
            status=str(event.get("status") or ""),
            result=event.get("result"),
            error=event.get("error"),
        )
    if event_type == "approval_requested":
        return ApprovalRequested(
            timestamp=timestamp,
            request_id=str(event.get("request_id") or ""),
            description=str(event.get("description") or ""),
            context=coerce_dict(event.get("context")),
        )
    if event_type == "token_usage":
        usage = event.get("usage")
        return TokenUsage(
            timestamp=timestamp,
            usage=usage if isinstance(usage, dict) else {},
        )
    if event_type == "run_notice":
        return RunNotice(
            timestamp=timestamp,
            kind=str(event.get("kind") or ""),
            message=str(event.get("message") or ""),
            data=coerce_dict(event.get("data")),
        )
    if event_type == "turn_completed":
        return Completed(
            timestamp=timestamp,
            final_message=str(event.get("final_message") or ""),
        )
    if event_type == "turn_failed":
        return Failed(
            timestamp=timestamp,
            error_message=str(event.get("error_message") or "Turn failed"),
        )
    return None


def _derive_last_tool(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    last_tool: dict[str, Any] | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        tool_name = normalize_optional_text(event.get("tool_name"))
        tool_state = normalize_optional_text(event.get("tool_state"))
        if tool_name is None or tool_state is None:
            continue
        if last_tool is None or last_tool.get("name") != tool_name:
            last_tool = {
                "name": tool_name,
                "started_at": None,
                "completed_at": None,
                "status": None,
                "in_flight": False,
            }
        if tool_state == "started":
            last_tool["started_at"] = event.get("received_at")
            last_tool["status"] = "running"
            last_tool["in_flight"] = True
        elif tool_state in {"completed", "failed"}:
            last_tool["completed_at"] = event.get("received_at")
            last_tool["status"] = tool_state
            last_tool["in_flight"] = False
    return last_tool


def _derive_progress_phase(
    *,
    turn_status: str,
    stream_available: bool,
    events: list[dict[str, Any]],
    idle_seconds: Optional[int],
    agent_id: Any = None,
) -> tuple[str, str, str, dict[str, Any] | None]:
    last_tool = _derive_last_tool(events)
    if turn_status == "ok":
        return ("completed", "turn_status", "Turn completed successfully.", last_tool)
    if turn_status == "interrupted":
        return ("interrupted", "turn_status", "Turn was interrupted.", last_tool)
    if turn_status in {"error", "failed"}:
        return ("failed", "turn_status", "Turn failed.", last_tool)

    if last_tool is not None and bool(last_tool.get("in_flight")):
        name = str(last_tool.get("name") or "tool")
        return (
            "waiting_on_tool_call",
            "recent_tool_event",
            f"Waiting on tool '{name}'.",
            last_tool,
        )

    if events:
        last_event_type = str(events[-1].get("event_type") or "").strip().lower()
        if last_event_type in {"assistant_update", "progress"}:
            return (
                "model_running",
                "recent_event",
                "Model is still producing intermediate activity.",
                last_tool,
            )
        if last_event_type in {"tool_completed", "tool_failed"}:
            return (
                "model_running",
                "recent_tool_event",
                "Tool activity finished; waiting for the model to continue or finalize.",
                last_tool,
            )

    idle = int(idle_seconds or 0)
    if (
        not events
        and _agent_batches_initial_events(agent_id)
        and idle < _BATCHED_INITIAL_EVENT_GRACE_SECONDS
    ):
        return (
            "model_running",
            "agent_event_batching",
            "Agent is running; progress events may arrive when the turn completes.",
            last_tool,
        )
    if not stream_available:
        if idle >= _LIKELY_HUNG_IDLE_SECONDS:
            return (
                "likely_hung",
                "idle_timeout",
                "No recent activity; inspect deeper or retry the interrupt.",
                last_tool,
            )
        if idle >= _NO_STREAM_AVAILABLE_IDLE_SECONDS:
            return (
                "no_stream_available",
                "idle_timeout",
                "Runtime is running but has not emitted streamable progress yet.",
                last_tool,
            )
        return (
            "booting_runtime",
            "runtime_start",
            "Waiting for the runtime to start emitting progress.",
            last_tool,
        )

    if idle >= _LIKELY_HUNG_IDLE_SECONDS:
        return (
            "likely_hung",
            "idle_timeout",
            "No recent activity; inspect deeper or retry the interrupt.",
            last_tool,
        )
    if idle >= _NO_STREAM_AVAILABLE_IDLE_SECONDS:
        return (
            "no_stream_available",
            "idle_timeout",
            "Connected stream has been quiet; the turn may be waiting on backend work.",
            last_tool,
        )
    return (
        "booting_runtime",
        "runtime_start",
        "Waiting for the runtime to emit the first progress event.",
        last_tool,
    )


def _event_source_ids(event: dict[str, Any]) -> list[int]:
    raw_ids = event.get("progress_event_ids") or event.get("source_event_ids")
    ids: list[int] = []
    if isinstance(raw_ids, list):
        for value in raw_ids:
            if isinstance(value, int) and value not in ids:
                ids.append(value)
    event_id = event.get("event_id")
    if isinstance(event_id, int) and event_id not in ids:
        ids.append(event_id)
    return ids


def _merge_activity_events(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(previous)
    merged["event_id"] = current.get("event_id", previous.get("event_id"))
    merged["summary"] = current.get("summary") or previous.get("summary")
    merged["title"] = current.get("title") or previous.get("title")
    merged["lines"] = current.get("lines") or previous.get("lines") or []
    merged["received_at"] = current.get("received_at") or previous.get("received_at")
    merged["received_at_ms"] = current.get("received_at_ms") or previous.get(
        "received_at_ms"
    )
    merged["progress_item"] = current.get("progress_item") or previous.get(
        "progress_item"
    )
    merged["progress_item_id"] = current.get("progress_item_id") or previous.get(
        "progress_item_id"
    )
    merged["progress_item_ids"] = list(
        dict.fromkeys(
            [
                *(previous.get("progress_item_ids") or []),
                *(current.get("progress_item_ids") or []),
            ]
        )
    )
    merged["progress_event_ids"] = list(
        dict.fromkeys([*_event_source_ids(previous), *_event_source_ids(current)])
    )
    merged["coalesced_event_count"] = int(
        previous.get("coalesced_event_count") or len(_event_source_ids(previous)) or 1
    ) + int(
        current.get("coalesced_event_count") or len(_event_source_ids(current)) or 1
    )
    return merged


def _live_activity_event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or "").strip().lower()


def _can_coalesce_live_activity_event(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    event_type = _live_activity_event_type(current)
    if _live_activity_event_type(previous) != event_type:
        return False
    # Only assistant-update events are known to be high-volume, replaceable
    # stream progress. Generic "progress" events can include actionable state
    # such as approvals and should remain distinct in the live activity window.
    return event_type == "assistant_update"


def build_live_activity_projection(
    *,
    snapshot: dict[str, Any],
    event_window: int = LIVE_ACTIVITY_EVENT_WINDOW,
) -> dict[str, Any]:
    """Project live turn state into a small replaceable activity model."""

    events = snapshot.get("events")
    event_list = (
        [event for event in events if isinstance(event, dict)]
        if isinstance(events, list)
        else []
    )
    coalesced_events: list[dict[str, Any]] = []
    for event in event_list:
        if coalesced_events and _can_coalesce_live_activity_event(
            coalesced_events[-1], event
        ):
            coalesced_events[-1] = _merge_activity_events(coalesced_events[-1], event)
            continue
        copied = dict(event)
        copied["coalesced_event_count"] = int(
            copied.get("coalesced_event_count") or len(_event_source_ids(copied)) or 1
        )
        coalesced_events.append(copied)

    bounded_events = coalesced_events[-max(1, int(event_window)) :]
    latest_event = bounded_events[-1] if bounded_events else {}
    latest_summary = normalize_optional_text(latest_event.get("summary"))
    if latest_summary is None:
        latest_summary = normalize_optional_text(snapshot.get("guidance"))
    if latest_summary is None:
        activity = normalize_optional_text(snapshot.get("activity")) or "idle"
        latest_summary = activity.replace("_", " ").title()

    managed_turn_id = normalize_optional_text(snapshot.get("managed_turn_id"))
    activity_id_subject = managed_turn_id or normalize_optional_text(
        snapshot.get("managed_thread_id")
    )
    activity_id = (
        f"turn:{activity_id_subject}:current"
        if activity_id_subject
        else "thread:unknown:current"
    )
    raw_event_count = len(event_list)
    visible_event_count = sum(
        int(event.get("coalesced_event_count") or 1) for event in bounded_events
    )
    return {
        "contract_version": LIVE_ACTIVITY_CONTRACT_VERSION,
        "activity_id": activity_id,
        "managed_thread_id": snapshot.get("managed_thread_id"),
        "managed_turn_id": snapshot.get("managed_turn_id"),
        "state": snapshot.get("activity") or "idle",
        "phase": snapshot.get("phase"),
        "phase_source": snapshot.get("phase_source"),
        "summary": latest_summary,
        "current_tool": snapshot.get("last_tool"),
        "elapsed_seconds": snapshot.get("elapsed_seconds"),
        "idle_seconds": snapshot.get("idle_seconds"),
        "latest_event_id": latest_event.get("event_id"),
        "latest_event_at": latest_event.get("received_at"),
        "raw_event_count": raw_event_count,
        "visible_event_count": visible_event_count,
        "coalesced_event_count": max(0, raw_event_count - len(coalesced_events)),
        "event_window_limit": max(1, int(event_window)),
        "events": bounded_events,
        "terminal": snapshot.get("terminal"),
        "stream_available": bool(snapshot.get("stream_available")),
    }


_TURN_STATUS_ALIASES = {
    "active": "running",
    "in_progress": "running",
    "progress": "running",
    "pending": "queued",
    "done": "ok",
    "complete": "ok",
    "completed": "ok",
    "errored": "error",
    "cancelled": "interrupted",
    "canceled": "interrupted",
    "interrupt": "interrupted",
    "aborted": "interrupted",
}
_TERMINAL_TURN_STATUSES = {"ok", "error", "failed", "interrupted"}


def _normalize_turn_lifecycle_status(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if text is None:
        return None
    lowered = text.lower()
    return _TURN_STATUS_ALIASES.get(lowered, lowered)


def build_managed_thread_stream_lifecycle(
    *,
    managed_turn_id: Any,
    turn_status: Any,
    thread_status: Any,
    lifecycle_status: Any,
    operator_status: Any = None,
    stream_available: bool,
    queue_depth: int = 0,
) -> dict[str, Any]:
    """Project normalized managed-thread status into the PMA stream contract."""

    normalized_turn_status = _normalize_turn_lifecycle_status(turn_status)
    normalized_thread_status = normalize_optional_text(thread_status)
    if normalized_thread_status is not None:
        normalized_thread_status = normalized_thread_status.lower()
    normalized_lifecycle_status = normalize_optional_text(lifecycle_status)
    if normalized_lifecycle_status is not None:
        normalized_lifecycle_status = normalized_lifecycle_status.lower()
    resolved_operator_status = normalize_optional_text(operator_status)
    if resolved_operator_status is None:
        resolved_operator_status = derive_managed_thread_operator_status(
            normalized_status=normalized_thread_status,
            lifecycle_status=normalized_lifecycle_status,
        )

    has_turn = normalize_optional_text(managed_turn_id) is not None
    has_queue = int(queue_depth or 0) > 0
    turn_is_active = normalized_turn_status in {"running", "queued"}
    terminal = normalized_turn_status in _TERMINAL_TURN_STATUSES or bool(
        not turn_is_active
        and normalized_thread_status
        in {"completed", "failed", "interrupted", "archived"}
        and not has_queue
    )

    if normalized_turn_status in {"running", "queued"}:
        work_status = normalized_turn_status
    elif normalized_turn_status in _TERMINAL_TURN_STATUSES:
        work_status = normalized_turn_status
    elif has_queue:
        work_status = "queued"
    elif normalized_thread_status in {"running", "completed", "failed", "interrupted"}:
        work_status = normalized_thread_status
    else:
        work_status = "idle"

    stream_should_close = False
    stream_close_reason: str | None = None
    if terminal:
        stream_should_close = True
        stream_close_reason = f"terminal:{work_status}"
    elif not has_turn:
        stream_should_close = True
        stream_close_reason = "no_running_turn"
    elif normalized_turn_status == "queued":
        stream_should_close = True
        stream_close_reason = "queued"

    return {
        "work_status": work_status,
        "operator_status": resolved_operator_status,
        "terminal": terminal,
        "stream_should_close": stream_should_close,
        "stream_close_reason": stream_close_reason,
        "stream_available": bool(stream_available),
    }


def _redacted_prompt_preview(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return truncate_text(redact_text(text), 120)


def _derive_active_turn_diagnostics(
    *,
    snapshot: dict[str, Any],
    turn_record: Optional[dict[str, Any]],
) -> dict[str, Any] | None:
    managed_turn_id = normalize_optional_text(
        snapshot.get("managed_turn_id") or (turn_record or {}).get("managed_turn_id")
    )
    if managed_turn_id is None:
        return None

    events = snapshot.get("events")
    event_list = (
        [event for event in events if isinstance(event, dict)]
        if isinstance(events, list)
        else []
    )
    last_event = event_list[-1] if event_list else {}
    turn_status = str(snapshot.get("turn_status") or "").strip().lower()
    idle_seconds_raw = snapshot.get("idle_seconds")
    idle_seconds = int(idle_seconds_raw) if isinstance(idle_seconds_raw, int) else None
    last_event_at = normalize_optional_text(snapshot.get("last_event_at"))
    stalled, stall_reason = (
        _running_turn_stall_flags(
            idle_seconds=idle_seconds,
            last_event_at=last_event_at,
            agent_id=snapshot.get("agent"),
            has_visible_events=bool(event_list),
        )
        if turn_status == "running"
        else (False, None)
    )

    return {
        "managed_turn_id": managed_turn_id,
        "request_kind": normalize_optional_text(
            (turn_record or {}).get("request_kind")
        ),
        "model": normalize_optional_text((turn_record or {}).get("model")),
        "reasoning": normalize_optional_text((turn_record or {}).get("reasoning")),
        "prompt_preview": _redacted_prompt_preview((turn_record or {}).get("prompt")),
        "backend_thread_id": normalize_optional_text(snapshot.get("backend_thread_id")),
        "backend_turn_id": normalize_optional_text(
            snapshot.get("backend_turn_id")
            or (turn_record or {}).get("backend_turn_id")
        ),
        "stream_available": bool(snapshot.get("stream_available")),
        "phase": normalize_optional_text(snapshot.get("phase")),
        "guidance": normalize_optional_text(snapshot.get("guidance")),
        "last_event_at": last_event_at,
        "last_event_type": normalize_optional_text(last_event.get("event_type")),
        "last_event_summary": normalize_optional_text(last_event.get("summary")),
        "stalled": stalled,
        "stall_reason": stall_reason,
    }


def _refresh_active_turn_diagnostics(
    snapshot: dict[str, Any],
    *,
    turn_status: Optional[str] = None,
    idle_seconds: Optional[int] = None,
    last_event_at: Optional[str] = None,
    phase: Optional[str] = None,
    guidance: Optional[str] = None,
) -> dict[str, Any] | None:
    diagnostics = snapshot.get("active_turn_diagnostics")
    if not isinstance(diagnostics, dict):
        return None

    updated = dict(diagnostics)
    events = snapshot.get("events")
    event_list = (
        [event for event in events if isinstance(event, dict)]
        if isinstance(events, list)
        else []
    )
    last_event = event_list[-1] if event_list else {}
    resolved_status = (
        str(turn_status or snapshot.get("turn_status") or "").strip().lower()
    )
    resolved_last_event_at = normalize_optional_text(
        last_event_at or snapshot.get("last_event_at")
    )
    if idle_seconds is not None:
        resolved_idle = max(0, int(idle_seconds))
    else:
        resolved_idle = None
        last_event_dt = parse_iso_datetime(resolved_last_event_at)
        if last_event_dt is not None:
            resolved_idle = max(
                0, int((datetime.now(timezone.utc) - last_event_dt).total_seconds())
            )
        else:
            started_dt = parse_iso_datetime(snapshot.get("started_at"))
            if started_dt is not None:
                resolved_idle = max(
                    0, int((datetime.now(timezone.utc) - started_dt).total_seconds())
                )
            elif isinstance(snapshot.get("idle_seconds"), (int, float)):
                resolved_idle = max(0, int(snapshot.get("idle_seconds") or 0))
    stalled, stall_reason = (
        _running_turn_stall_flags(
            idle_seconds=resolved_idle,
            last_event_at=resolved_last_event_at,
            agent_id=snapshot.get("agent"),
            has_visible_events=bool(event_list),
        )
        if resolved_status == "running"
        else (False, None)
    )
    updated["phase"] = normalize_optional_text(phase) or normalize_optional_text(
        snapshot.get("phase")
    )
    updated["guidance"] = normalize_optional_text(guidance) or normalize_optional_text(
        snapshot.get("guidance")
    )
    updated["last_event_at"] = resolved_last_event_at
    updated["last_event_type"] = normalize_optional_text(last_event.get("event_type"))
    updated["last_event_summary"] = normalize_optional_text(last_event.get("summary"))
    updated["stalled"] = stalled
    updated["stall_reason"] = stall_reason
    return updated


def _event_received_at_iso(event: dict[str, Any]) -> Optional[str]:
    received_at_ms = int(event.get("received_at") or 0)
    if received_at_ms <= 0:
        return None
    return iso_from_event_ms(received_at_ms)


def _record_serialized_tail_event(
    snapshot: dict[str, Any], serialized_event: dict[str, Any]
) -> int:
    event_id = int(serialized_event.get("event_id") or 0)
    snapshot_events = snapshot.get("events")
    if isinstance(snapshot_events, list):
        snapshot_events.append(serialized_event)
    snapshot["last_event_at"] = serialized_event.get("received_at")
    return event_id


def _serialize_persisted_timeline_tail_events(
    timeline_entries: list[dict[str, Any]],
    *,
    level: str,
    since_ms: Optional[int],
    resume_after: Optional[int],
) -> tuple[list[dict[str, Any]], Optional[str]]:
    serialized: list[dict[str, Any]] = []
    last_activity_at: Optional[str] = None
    min_event_id = int(resume_after or 0)
    projection_inputs: list[ProgressProjectionInput] = []
    raw_entries_by_event_id: dict[int, dict[str, Any]] = {}
    timestamps_by_event_id: dict[int, str] = {}
    for entry in timeline_entries:
        if not isinstance(entry, dict):
            continue
        event_id = int(entry.get("event_index") or 0)
        if event_id <= 0 or event_id <= min_event_id:
            continue
        timestamp = normalize_optional_text(entry.get("timestamp"))
        if timestamp is None:
            continue
        if since_ms is not None:
            dt = parse_iso_datetime(timestamp)
            if dt is not None and int(dt.timestamp() * 1000) < since_ms:
                continue
        run_event = _run_event_from_timeline_entry(entry)
        if run_event is None:
            continue
        projection_inputs.append(
            ProgressProjectionInput(
                event_id=event_id,
                timestamp=timestamp,
                event=run_event,
            )
        )
        raw_entries_by_event_id[event_id] = entry
        timestamps_by_event_id[event_id] = timestamp
    for item in project_progress_events(projection_inputs):
        event_id = item.event_ids[-1] if item.event_ids else 0
        timestamp = timestamps_by_event_id.get(event_id) or item.timestamp
        payload = _tail_event_from_progress_item(item, received_at=timestamp)
        if level == "debug":
            payload["raw"] = _redact_nested(raw_entries_by_event_id.get(event_id) or {})
        serialized.append(payload)
        last_activity_at = timestamp
    return serialized, last_activity_at


def _latest_token_usage_from_timeline_entries(
    timeline_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for entry in timeline_entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("event_type") or "").strip().lower() != "token_usage":
            continue
        event = coerce_dict(entry.get("event"))
        usage = event.get("usage")
        if isinstance(usage, dict) and usage:
            latest = dict(usage)
    return latest


async def _serialize_runtime_raw_tail_events(
    raw_event: Any,
    state: RuntimeThreadRunEventState,
    *,
    level: str,
    event_id_start: int,
    since_ms: Optional[int] = None,
    projection_state: ProgressProjectionState | None = None,
    fallback_received_at: Optional[str] = None,
) -> list[dict[str, Any]]:
    received_at_ms = 0
    fallback_dt = parse_iso_datetime(fallback_received_at)
    if fallback_dt is not None:
        received_at = fallback_dt.isoformat()
    else:
        received_at = datetime.now(timezone.utc).isoformat()
    since_ms_from_buffered_timestamp = False
    if isinstance(raw_event, dict):
        rim = int(raw_event.get("received_at") or 0)
        if rim > 0:
            since_ms_from_buffered_timestamp = True
            received_at_ms = rim
            iso = iso_from_event_ms(rim)
            if iso:
                received_at = iso
        else:
            published = normalize_optional_text(raw_event.get("published_at"))
            if published:
                dt = parse_iso_datetime(published)
                if dt is not None:
                    since_ms_from_buffered_timestamp = True
                    received_at_ms = int(dt.timestamp() * 1000)
                    received_at = dt.isoformat()
    if (
        since_ms is not None
        and since_ms_from_buffered_timestamp
        and received_at_ms > 0
        and received_at_ms < since_ms
    ):
        return []
    serialized: list[dict[str, Any]] = []
    runtime_events = await normalize_runtime_thread_raw_event(
        raw_event,
        state,
        timestamp=received_at,
    )
    buffer_id = int(raw_event.get("id") or 0) if isinstance(raw_event, dict) else 0
    next_event_id = event_id_start
    n_runtime = len(runtime_events)
    for run_event in runtime_events:
        next_event_id += 1
        event_id_for_payload = (
            buffer_id if buffer_id > 0 and n_runtime == 1 else next_event_id
        )
        payload = _tail_event_from_run_event(
            run_event,
            event_id=event_id_for_payload,
            received_at=received_at,
            projection_state=projection_state,
        )
        if payload is None:
            continue
        if level == "debug":
            payload["raw"] = _redact_nested(raw_event)
        serialized.append(payload)
    if any(
        payload.get("event_type")
        in {"turn_completed", "turn_failed", "turn_interrupted"}
        for payload in serialized
    ):
        return serialized
    terminal = _runtime_terminal_tail_event(
        raw_event=raw_event,
        event_id=next_event_id + 1,
        received_at=received_at,
    )
    if terminal is not None:
        if level == "debug":
            terminal["raw"] = _redact_nested(raw_event)
        serialized.append(terminal)
    return serialized


def build_managed_thread_status_response(
    *,
    managed_thread_id: str,
    serialized_thread: dict[str, Any],
    snapshot: dict[str, Any],
    queued_turns: list[dict[str, Any]],
    queue_depth: int,
) -> dict[str, Any]:
    turn_status = str(snapshot.get("turn_status") or "")
    live_activity = snapshot.get("live_activity")
    if not isinstance(live_activity, dict):
        live_activity = build_live_activity_projection(snapshot=snapshot)
    lifecycle = build_managed_thread_stream_lifecycle(
        managed_turn_id=snapshot.get("managed_turn_id"),
        turn_status=snapshot.get("turn_status"),
        thread_status=serialized_thread.get("status"),
        lifecycle_status=serialized_thread.get("lifecycle_status"),
        operator_status=serialized_thread.get("operator_status"),
        stream_available=bool(snapshot.get("stream_available")),
        queue_depth=queue_depth,
    )
    return {
        "managed_thread_id": managed_thread_id,
        "thread": serialized_thread,
        "is_alive": bool(
            (serialized_thread.get("lifecycle_status") or "") == "active"
            and turn_status == "running"
        ),
        "status": str(serialized_thread.get("status") or ""),
        "operator_status": lifecycle["operator_status"],
        "is_reusable": bool(serialized_thread.get("is_reusable")),
        "status_reason": normalize_optional_text(serialized_thread.get("status_reason"))
        or "",
        "status_changed_at": normalize_optional_text(
            serialized_thread.get("status_changed_at")
        ),
        "status_terminal": bool(serialized_thread.get("status_terminal")),
        "turn": {
            "managed_turn_id": snapshot.get("managed_turn_id"),
            "status": snapshot.get("turn_status"),
            "activity": snapshot.get("activity"),
            "live_activity": live_activity,
            "phase": snapshot.get("phase"),
            "phase_source": snapshot.get("phase_source"),
            "guidance": snapshot.get("guidance"),
            "last_tool": snapshot.get("last_tool"),
            "elapsed_seconds": snapshot.get("elapsed_seconds"),
            "idle_seconds": snapshot.get("idle_seconds"),
            "started_at": snapshot.get("started_at"),
            "finished_at": snapshot.get("finished_at"),
            "lifecycle_events": snapshot.get("lifecycle_events"),
            "token_usage": snapshot.get("token_usage"),
        },
        "live_activity": live_activity,
        "token_usage": snapshot.get("token_usage"),
        "queue_depth": queue_depth,
        "queued_turns": [
            {
                "managed_turn_id": item.get("managed_turn_id"),
                "request_kind": item.get("request_kind"),
                "state": item.get("state"),
                "enqueued_at": item.get("enqueued_at"),
                "prompt_preview": truncate_text(item.get("prompt") or "", 120),
            }
            for item in queued_turns
        ],
        "recent_progress": live_activity.get("events") or [],
        "latest_turn_id": serialized_thread.get("latest_turn_id"),
        "latest_turn_status": serialized_thread.get("latest_turn_status"),
        "latest_assistant_text": serialized_thread.get("latest_assistant_text"),
        "latest_output_excerpt": serialized_thread.get("latest_output_excerpt"),
        "stream_available": bool(snapshot.get("stream_available")),
        "work_status": lifecycle["work_status"],
        "terminal": lifecycle["terminal"],
        "stream_should_close": lifecycle["stream_should_close"],
        "stream_close_reason": lifecycle["stream_close_reason"],
        "stream_lifecycle": lifecycle,
        "active_turn_diagnostics": snapshot.get("active_turn_diagnostics"),
    }


__all__ = [
    "LIVE_ACTIVITY_CONTRACT_VERSION",
    "LIVE_ACTIVITY_EVENT_WINDOW",
    "build_live_activity_projection",
    "build_managed_thread_status_response",
    "build_managed_thread_stream_lifecycle",
    "coerce_dict",
    "iso_from_event_ms",
    "parse_iso_datetime",
    "truncate_text",
    "_derive_active_turn_diagnostics",
    "_derive_last_tool",
    "_derive_progress_phase",
    "_event_received_at_iso",
    "_parse_inline_sse",
    "_runtime_raw_payload",
    "_record_serialized_tail_event",
    "_redacted_prompt_preview",
    "_redact_nested",
    "_refresh_active_turn_diagnostics",
    "_run_event_from_timeline_entry",
    "_runtime_method_and_params",
    "_runtime_raw_payload",
    "_runtime_terminal_tail_event",
    "_running_turn_stall_flags",
    "_latest_token_usage_from_timeline_entries",
    "_serialize_persisted_timeline_tail_events",
    "_serialize_runtime_raw_tail_events",
    "_tail_event_from_run_event",
    "_truncate_tool_name",
]
