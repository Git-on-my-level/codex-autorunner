from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .....agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
)
from .....core.orchestration.turn_timeline import list_turn_timeline
from .....core.pma_thread_store import PmaThreadStore
from .....core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from .....core.redaction import redact_text
from ..shared import SSE_HEADERS
from .automation_adapter import normalize_optional_text
from .managed_threads import (
    _attach_latest_execution_fields,
    _serialize_thread_target,
    build_managed_thread_orchestration_service,
)


def coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_tail_duration_seconds(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    raw = value.strip().lower()
    if not raw:
        raise HTTPException(status_code=400, detail="since must not be empty")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total_seconds = 0
    idx = 0
    size = len(raw)
    while idx < size:
        start = idx
        while idx < size and raw[idx].isdigit():
            idx += 1
        if start == idx or idx >= size:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid since duration. Use forms like 30s, 5m, 2h, 1d, "
                    "or combined 1h30m."
                ),
            )
        amount_text = raw[start:idx]
        if len(amount_text) > 9:
            raise HTTPException(
                status_code=400, detail="since duration component is too large"
            )
        multiplier = multipliers.get(raw[idx])
        if multiplier is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid since duration. Use forms like 30s, 5m, 2h, 1d, "
                    "or combined 1h30m."
                ),
            )
        idx += 1
        total_seconds += int(amount_text) * multiplier
    if total_seconds <= 0:
        raise HTTPException(status_code=400, detail="since must be > 0")
    return total_seconds


def since_ms_from_duration(value: Optional[str]) -> Optional[int]:
    seconds = parse_tail_duration_seconds(value)
    if seconds is None:
        return None
    return int((datetime.now(timezone.utc).timestamp() - seconds) * 1000)


def normalize_tail_level(level: Optional[str]) -> str:
    normalized = (level or "info").strip().lower() or "info"
    if normalized not in {"info", "debug"}:
        raise HTTPException(status_code=400, detail="level must be info or debug")
    return normalized


def resolve_resume_after(
    request: Request, since_event_id: Optional[int]
) -> Optional[int]:
    if since_event_id is not None:
        if since_event_id < 0:
            raise HTTPException(status_code=400, detail="since_event_id must be >= 0")
        return since_event_id
    last_event_id = request.headers.get("Last-Event-ID")
    if not last_event_id:
        return None
    try:
        parsed = int(last_event_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid Last-Event-ID header"
        ) from exc
    if parsed < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be >= 0")
    return parsed


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


def _should_suppress_tail_event(message: Any) -> bool:
    payload = coerce_dict(message)
    method = str(payload.get("method") or "").strip().lower()
    params = coerce_dict(payload.get("params"))
    item = coerce_dict(params.get("item"))

    if method in {
        "item/agentmessage/delta",
        "item/plan/delta",
        "turn/plan/updated",
    }:
        return True
    if method == "item/completed":
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "agentmessage":
            return True
    return False


_NO_STREAM_AVAILABLE_IDLE_SECONDS = 15
_LIKELY_HUNG_IDLE_SECONDS = 90
_STALL_IDLE_SECONDS = 30


def _running_turn_stall_flags(
    *,
    idle_seconds: Optional[int],
    last_event_at: Optional[str],
) -> tuple[bool, Optional[str]]:
    """Return (stalled, stall_reason) for a running turn."""
    stalled = idle_seconds is not None and idle_seconds >= _STALL_IDLE_SECONDS
    if not stalled:
        return (False, None)
    reason = (
        "no_events_yet"
        if last_event_at is None
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
            idle_seconds=idle_seconds, last_event_at=last_event_at
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
            idle_seconds=resolved_idle, last_event_at=resolved_last_event_at
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


def _managed_thread_harness(service: Any, agent_id: str) -> Any:
    factory = getattr(service, "harness_factory", None)
    if not callable(factory):
        return None
    try:
        return factory(agent_id)
    except Exception:  # intentional: dynamic harness factory - exception types unknown
        return None


def _load_managed_thread_tail_store_state(
    *,
    hub_root: Path,
    service: Any,
    managed_thread_id: str,
) -> tuple[Any, Any, list[dict[str, Any]], Any]:
    thread = service.get_thread_target(managed_thread_id)
    if thread is None:
        return None, None, [], None
    turn = service.get_running_execution(
        managed_thread_id
    ) or service.get_latest_execution(managed_thread_id)
    if turn is None:
        return thread, None, [], None
    managed_turn_id = str(turn.execution_id or "")
    persisted_timeline_entries = list_turn_timeline(
        hub_root,
        execution_id=managed_turn_id,
    )
    turn_record = None
    if managed_turn_id:
        turn_record = PmaThreadStore(hub_root).get_turn(
            managed_thread_id,
            managed_turn_id,
        )
    return thread, turn, persisted_timeline_entries, turn_record


def _load_managed_thread_status_state(
    *,
    hub_root: Path,
    service: Any,
    managed_thread_id: str,
    limit: int,
) -> tuple[Any, dict[str, Any] | None, list[dict[str, Any]], int]:
    thread = service.get_thread_target(managed_thread_id)
    if thread is None:
        return None, None, [], 0
    serialized_thread = _attach_latest_execution_fields(
        _serialize_thread_target(thread),
        service=service,
        managed_thread_id=managed_thread_id,
    )
    queue_store = PmaThreadStore(hub_root)
    queued_turns = queue_store.list_pending_turn_queue_items(
        managed_thread_id,
        limit=min(limit, 50),
    )
    queue_depth = service.get_queue_depth(managed_thread_id)
    return thread, serialized_thread, queued_turns, queue_depth


def _poll_managed_thread_execution_state(
    *,
    service: Any,
    managed_thread_id: str,
    managed_turn_id: str,
) -> tuple[Any, str, Any]:
    turn = service.get_execution(managed_thread_id, managed_turn_id)
    status = str((turn.status if turn is not None else "") or "").strip().lower()
    refreshed_thread = (
        service.get_thread_target(managed_thread_id) if status == "running" else None
    )
    return turn, status, refreshed_thread


async def _build_managed_thread_orchestration_service_async(request: Request) -> Any:
    return await asyncio.to_thread(build_managed_thread_orchestration_service, request)


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
        }
    return None


def _tail_event_from_run_event(
    run_event: Any,
    *,
    event_id: int,
    received_at: str,
) -> dict[str, Any] | None:
    tool_name: str | None = None
    tool_state: str | None = None
    event_type = "progress"
    summary = ""
    if isinstance(run_event, OutputDelta):
        normalized_text = (run_event.content or "").strip()
        if normalized_text.startswith("🤔"):
            event_type = "assistant_update"
            summary = "Thinking"
        elif normalized_text.startswith("⏳"):
            event_type = "tool_started"
            tool_name = _truncate_tool_name(normalized_text.lstrip("⏳").strip())
            tool_state = "started"
            summary = tool_name or "Tool started"
        elif normalized_text.startswith("✅"):
            event_type = "tool_completed"
            tool_name = _truncate_tool_name(normalized_text.lstrip("✅").strip())
            tool_state = "completed"
            summary = tool_name or "Tool completed"
        elif normalized_text.startswith("❌"):
            event_type = "tool_failed"
            tool_name = _truncate_tool_name(normalized_text.lstrip("❌").strip())
            tool_state = "failed"
            summary = tool_name or "Tool failed"
        else:
            event_type = "assistant_update"
            summary = run_event.content or "Assistant update"
    elif isinstance(run_event, ToolCall):
        event_type = "tool_started"
        tool_name = _truncate_tool_name(run_event.tool_name)
        tool_state = "started"
        summary = f"tool: {tool_name or 'unknown'}"
    elif isinstance(run_event, ToolResult):
        event_type = "tool_failed" if run_event.status == "error" else "tool_completed"
        tool_name = _truncate_tool_name(run_event.tool_name)
        tool_state = "failed" if run_event.status == "error" else "completed"
        summary = f"tool: {tool_name or 'unknown'}"
    elif isinstance(run_event, ApprovalRequested):
        event_type = "progress"
        summary = run_event.description or "Approval requested"
    elif isinstance(run_event, TokenUsage):
        event_type = "progress"
        summary = "Token usage updated"
    elif isinstance(run_event, RunNotice):
        event_type = "assistant_update" if run_event.kind == "thinking" else "progress"
        summary = run_event.message or run_event.kind.replace("_", " ").title()
    elif isinstance(run_event, Completed):
        event_type = "turn_completed"
        summary = run_event.final_message or "Turn completed"
    elif isinstance(run_event, Failed):
        detail = run_event.error_message or "Turn failed"
        lowered = detail.lower()
        if "interrupt" in lowered or "cancel" in lowered or "abort" in lowered:
            event_type = "turn_interrupted"
            summary = "Turn interrupted"
        else:
            event_type = "turn_failed"
            summary = detail
    else:
        return None

    summary = truncate_text(redact_text(summary), 220)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "summary": summary,
        "lines": [summary] if summary else [],
        "received_at_ms": None,
        "received_at": received_at,
        "tool_name": tool_name,
        "tool_state": tool_state,
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
        payload = _tail_event_from_run_event(
            run_event,
            event_id=event_id,
            received_at=timestamp,
        )
        if payload is None:
            continue
        if level == "debug":
            payload["raw"] = _redact_nested(entry)
        serialized.append(payload)
        last_activity_at = timestamp
    return serialized, last_activity_at


async def _serialize_runtime_raw_tail_events(
    raw_event: Any,
    state: RuntimeThreadRunEventState,
    *,
    level: str,
    event_id_start: int,
    since_ms: Optional[int] = None,
) -> list[dict[str, Any]]:
    received_at_ms = 0
    received_at = datetime.now(timezone.utc).isoformat()
    if isinstance(raw_event, dict):
        msg = raw_event.get("message")
        if isinstance(msg, dict) and _should_suppress_tail_event(msg):
            return []
        rim = int(raw_event.get("received_at") or 0)
        if rim > 0:
            received_at_ms = rim
            iso = iso_from_event_ms(rim)
            if iso:
                received_at = iso
        else:
            published = normalize_optional_text(raw_event.get("published_at"))
            if published:
                dt = parse_iso_datetime(published)
                if dt is not None:
                    received_at_ms = int(dt.timestamp() * 1000)
                    received_at = dt.isoformat()
    if since_ms is not None and received_at_ms > 0 and received_at_ms < since_ms:
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


async def _build_managed_thread_tail_snapshot(
    *,
    request: Request,
    service: Any,
    managed_thread_id: str,
    harness: Any | None = None,
    limit: int,
    level: str,
    since_ms: Optional[int],
    resume_after: Optional[int],
) -> dict[str, Any]:
    thread, turn, persisted_timeline_entries, turn_record = await asyncio.to_thread(
        _load_managed_thread_tail_store_state,
        hub_root=request.app.state.config.root,
        service=service,
        managed_thread_id=managed_thread_id,
    )
    if thread is None:
        raise HTTPException(status_code=404, detail="Managed thread not found")
    if turn is None:
        return {
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": None,
            "agent": thread.agent_id,
            "turn_status": None,
            "lifecycle_events": [],
            "events": [],
            "last_event_id": int(resume_after or 0),
            "elapsed_seconds": None,
            "idle_seconds": None,
            "activity": "idle",
            "stream_available": False,
        }

    managed_turn_id = str(turn.execution_id or "")
    turn_status = str(turn.status or "").strip().lower()
    started_at = normalize_optional_text(turn.started_at)
    finished_at = normalize_optional_text(turn.finished_at)
    started_dt = parse_iso_datetime(started_at)
    finished_dt = parse_iso_datetime(finished_at)
    now_dt = datetime.now(timezone.utc)
    effective_finished = finished_dt or (None if turn_status == "running" else now_dt)
    elapsed_seconds: Optional[int] = None
    if started_dt is not None:
        end_dt = effective_finished or now_dt
        elapsed_seconds = max(0, int((end_dt - started_dt).total_seconds()))

    lifecycle_events = ["turn_started"]
    if turn_status == "ok":
        lifecycle_events.append("turn_completed")
    elif turn_status == "error":
        lifecycle_events.append("turn_failed")
    elif turn_status == "interrupted":
        lifecycle_events.append("turn_interrupted")

    backend_thread_id = normalize_optional_text(thread.backend_thread_id)
    backend_turn_id = normalize_optional_text(turn.backend_id)
    if harness is None:
        harness = _managed_thread_harness(service, str(thread.agent_id or ""))
    has_backend_binding = bool(backend_thread_id) and bool(backend_turn_id)
    stream_available = bool(
        harness is not None
        and has_backend_binding
        and harness_supports_progress_event_stream(harness)
    )
    tail_events: list[dict[str, Any]] = []
    raw_last_activity_at: Optional[str] = None
    tail_events, raw_last_activity_at = _serialize_persisted_timeline_tail_events(
        persisted_timeline_entries,
        level=level,
        since_ms=since_ms,
        resume_after=resume_after,
    )
    if not tail_events and has_backend_binding and harness is not None:
        list_fn = getattr(harness, "list_progress_events", None)
        if callable(list_fn):
            try:
                raw_events = await list_fn(
                    str(backend_thread_id),
                    str(backend_turn_id),
                    after_id=int(resume_after or 0),
                    limit=limit,
                )
            except (
                Exception
            ):  # intentional: dynamic harness method - exception types depend on backend
                raw_events = []
            state = RuntimeThreadRunEventState()
            event_id_start = int(resume_after or 0)
            for raw_event in raw_events:
                if isinstance(raw_event, dict):
                    activity_at = _event_received_at_iso(raw_event)
                    if activity_at is None:
                        activity_at = normalize_optional_text(
                            raw_event.get("published_at")
                        )
                    if activity_at:
                        raw_last_activity_at = activity_at
                serialized_entries = await _serialize_runtime_raw_tail_events(
                    raw_event,
                    state,
                    level=level,
                    event_id_start=event_id_start,
                    since_ms=since_ms,
                )
                for entry in serialized_entries:
                    tail_events.append(entry)
                    event_id_start = int(entry.get("event_id") or event_id_start)
            if len(tail_events) > limit:
                tail_events = tail_events[-limit:]

    last_event_id = int(resume_after or 0)
    last_activity_at: Optional[str] = raw_last_activity_at
    if tail_events:
        last_event_id = int(tail_events[-1].get("event_id") or last_event_id)
        tail_last_activity_at = normalize_optional_text(
            tail_events[-1].get("received_at")
        )
        raw_last_dt = parse_iso_datetime(raw_last_activity_at)
        tail_last_dt = parse_iso_datetime(tail_last_activity_at)
        if raw_last_dt is None:
            last_activity_at = tail_last_activity_at
        elif tail_last_dt is None:
            last_activity_at = raw_last_activity_at
        else:
            last_activity_at = (
                raw_last_activity_at
                if raw_last_dt >= tail_last_dt
                else tail_last_activity_at
            )
    last_event_ms = tail_events[-1].get("received_at_ms") if tail_events else None
    idle_seconds: Optional[int] = None
    if turn_status == "running":
        if last_activity_at:
            last_activity_dt = parse_iso_datetime(last_activity_at)
            if last_activity_dt is not None:
                idle_seconds = max(0, int((now_dt - last_activity_dt).total_seconds()))
        elif isinstance(last_event_ms, (int, float)) and last_event_ms > 0:
            idle_seconds = max(
                0, int((now_dt.timestamp() * 1000 - last_event_ms) / 1000)
            )
        elif started_dt is not None:
            idle_seconds = max(0, int((now_dt - started_dt).total_seconds()))

    activity = "idle"
    if turn_status == "running":
        is_stalled, _ = _running_turn_stall_flags(
            idle_seconds=idle_seconds, last_event_at=None
        )
        activity = "stalled" if is_stalled else "running"
    elif turn_status == "ok":
        activity = "completed"
    elif turn_status == "interrupted":
        activity = "interrupted"
    elif turn_status == "error":
        activity = "failed"

    phase, phase_source, guidance, last_tool = _derive_progress_phase(
        turn_status=turn_status,
        stream_available=stream_available,
        events=tail_events,
        idle_seconds=idle_seconds,
    )
    snapshot: dict[str, Any] = {
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "agent": thread.agent_id,
        "backend_thread_id": backend_thread_id,
        "backend_turn_id": backend_turn_id,
        "turn_status": turn_status,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": elapsed_seconds,
        "idle_seconds": idle_seconds,
        "activity": activity,
        "lifecycle_events": lifecycle_events,
        "events": tail_events,
        "last_event_id": last_event_id,
        "last_event_at": tail_events[-1].get("received_at") if tail_events else None,
        "last_activity_at": last_activity_at,
        "stream_available": stream_available,
        "phase": phase,
        "phase_source": phase_source,
        "guidance": guidance,
        "last_tool": last_tool,
    }
    snapshot["active_turn_diagnostics"] = _derive_active_turn_diagnostics(
        snapshot=snapshot,
        turn_record=turn_record,
    )
    return snapshot


def build_managed_thread_tail_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build managed-thread status and tail routes."""
    _ = get_runtime_state

    @router.get("/threads/{managed_thread_id}/status")
    async def get_managed_thread_status(
        managed_thread_id: str,
        request: Request,
        limit: int = 20,
        since: Optional[str] = None,
        since_event_id: Optional[int] = None,
        level: str = "info",
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        service = await _build_managed_thread_orchestration_service_async(request)
        snapshot = await _build_managed_thread_tail_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
            limit=min(limit, 200),
            level=normalize_tail_level(level),
            since_ms=since_ms_from_duration(since),
            resume_after=resolve_resume_after(request, since_event_id),
        )
        thread, serialized_thread, queued_turns, queue_depth = await asyncio.to_thread(
            _load_managed_thread_status_state,
            hub_root=request.app.state.config.root,
            service=service,
            managed_thread_id=managed_thread_id,
            limit=limit,
        )
        if thread is None or serialized_thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        turn_status = str(snapshot.get("turn_status") or "")
        return {
            "managed_thread_id": managed_thread_id,
            "thread": serialized_thread,
            "is_alive": bool(
                (serialized_thread.get("lifecycle_status") or "") == "active"
                and turn_status == "running"
            ),
            "status": str(serialized_thread.get("status") or ""),
            "operator_status": str(serialized_thread.get("operator_status") or ""),
            "is_reusable": bool(serialized_thread.get("is_reusable")),
            "status_reason": normalize_optional_text(
                serialized_thread.get("status_reason")
            )
            or "",
            "status_changed_at": normalize_optional_text(
                serialized_thread.get("status_changed_at")
            ),
            "status_terminal": bool(serialized_thread.get("status_terminal")),
            "turn": {
                "managed_turn_id": snapshot.get("managed_turn_id"),
                "status": snapshot.get("turn_status"),
                "activity": snapshot.get("activity"),
                "phase": snapshot.get("phase"),
                "phase_source": snapshot.get("phase_source"),
                "guidance": snapshot.get("guidance"),
                "last_tool": snapshot.get("last_tool"),
                "elapsed_seconds": snapshot.get("elapsed_seconds"),
                "idle_seconds": snapshot.get("idle_seconds"),
                "started_at": snapshot.get("started_at"),
                "finished_at": snapshot.get("finished_at"),
                "lifecycle_events": snapshot.get("lifecycle_events"),
            },
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
            "recent_progress": snapshot.get("events") or [],
            "latest_turn_id": serialized_thread.get("latest_turn_id"),
            "latest_turn_status": serialized_thread.get("latest_turn_status"),
            "latest_assistant_text": serialized_thread.get("latest_assistant_text"),
            "latest_output_excerpt": serialized_thread.get("latest_output_excerpt"),
            "stream_available": bool(snapshot.get("stream_available")),
            "active_turn_diagnostics": snapshot.get("active_turn_diagnostics"),
        }

    @router.get("/threads/{managed_thread_id}/tail")
    async def get_managed_thread_tail(
        managed_thread_id: str,
        request: Request,
        limit: int = 50,
        since: Optional[str] = None,
        since_event_id: Optional[int] = None,
        level: str = "info",
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        service = await _build_managed_thread_orchestration_service_async(request)
        return await _build_managed_thread_tail_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
            limit=min(limit, 200),
            level=normalize_tail_level(level),
            since_ms=since_ms_from_duration(since),
            resume_after=resolve_resume_after(request, since_event_id),
        )

    @router.get("/threads/{managed_thread_id}/tail/events")
    async def stream_managed_thread_tail(
        managed_thread_id: str,
        request: Request,
        limit: int = 50,
        since: Optional[str] = None,
        since_event_id: Optional[int] = None,
        level: str = "info",
    ):
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        normalized_level = normalize_tail_level(level)
        since_ms = since_ms_from_duration(since)
        service = await _build_managed_thread_orchestration_service_async(request)
        thread_target = await asyncio.to_thread(
            service.get_thread_target,
            managed_thread_id,
        )
        harness = None
        if thread_target is not None:
            harness = _managed_thread_harness(
                service, str(thread_target.agent_id or "")
            )
        snapshot = await _build_managed_thread_tail_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
            harness=harness,
            limit=min(limit, 200),
            level=normalized_level,
            since_ms=since_ms,
            resume_after=resolve_resume_after(request, since_event_id),
        )

        async def _stream() -> Any:
            yield f"event: state\ndata: {json.dumps(snapshot, ensure_ascii=True)}\n\n"
            for event in snapshot.get("events", []):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("event_id")
                event_id_line = (
                    f"id: {event_id}\n"
                    if isinstance(event_id, int) and event_id > 0
                    else ""
                )
                yield (
                    f"event: tail\n"
                    f"{event_id_line}"
                    f"data: {json.dumps(event, ensure_ascii=True)}\n\n"
                )

            if snapshot.get("turn_status") != "running":
                return
            if not snapshot.get("stream_available"):
                while True:
                    await asyncio.sleep(5.0)
                    turn, status, refreshed_thread = await asyncio.to_thread(
                        _poll_managed_thread_execution_state,
                        service=service,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=str(snapshot.get("managed_turn_id") or ""),
                    )
                    if status != "running":
                        yield (
                            "event: state\ndata: "
                            f"{json.dumps({'turn_status': status or 'unknown'}, ensure_ascii=True)}\n\n"
                        )
                        return

                    refreshed_backend_thread_id = (
                        normalize_optional_text(
                            getattr(refreshed_thread, "backend_thread_id", None)
                        )
                        if refreshed_thread is not None
                        else None
                    )
                    refreshed_backend_turn_id = normalize_optional_text(
                        turn.backend_id if turn is not None else None
                    )
                    if refreshed_backend_thread_id and refreshed_backend_turn_id:
                        snapshot["stream_available"] = True
                        snapshot["backend_thread_id"] = refreshed_backend_thread_id
                        snapshot["backend_turn_id"] = refreshed_backend_turn_id
                        refreshed_snapshot = await _build_managed_thread_tail_snapshot(
                            request=request,
                            service=service,
                            managed_thread_id=managed_thread_id,
                            harness=harness,
                            limit=min(limit, 200),
                            level=normalized_level,
                            since_ms=since_ms,
                            resume_after=resolve_resume_after(request, since_event_id),
                        )
                        for key in (
                            "events",
                            "last_event_id",
                            "phase",
                            "phase_source",
                            "guidance",
                            "last_tool",
                            "idle_seconds",
                            "active_turn_diagnostics",
                        ):
                            if key in refreshed_snapshot:
                                snapshot[key] = refreshed_snapshot[key]
                        for event in snapshot.get("events", []):
                            if not isinstance(event, dict):
                                continue
                            event_id = event.get("event_id")
                            event_id_line = (
                                f"id: {event_id}\n"
                                if isinstance(event_id, int) and event_id > 0
                                else ""
                            )
                            yield (
                                f"event: tail\n"
                                f"{event_id_line}"
                                f"data: {json.dumps(event, ensure_ascii=True)}\n\n"
                            )
                        break

                    now = datetime.now(timezone.utc)
                    started_dt = parse_iso_datetime(snapshot.get("started_at"))
                    elapsed = None
                    if started_dt is not None:
                        elapsed = max(0, int((now - started_dt).total_seconds()))
                    phase, phase_source, guidance, last_tool = _derive_progress_phase(
                        turn_status="running",
                        stream_available=False,
                        events=[
                            event
                            for event in snapshot.get("events", [])
                            if isinstance(event, dict)
                        ],
                        idle_seconds=elapsed,
                    )
                    active_turn_diagnostics = _refresh_active_turn_diagnostics(
                        snapshot,
                        turn_status="running",
                        idle_seconds=elapsed,
                        phase=phase,
                        guidance=guidance,
                    )
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps({'managed_thread_id': managed_thread_id, 'managed_turn_id': snapshot.get('managed_turn_id'), 'turn_status': 'running', 'elapsed_seconds': elapsed, 'phase': phase, 'phase_source': phase_source, 'guidance': guidance, 'last_tool': last_tool, 'active_turn_diagnostics': active_turn_diagnostics}, ensure_ascii=True)}\n\n"
                    )

            workspace_root = (
                str(thread_target.workspace_root or "")
                if thread_target is not None
                else ""
            )
            backend_thread_id = str(snapshot.get("backend_thread_id") or "")
            backend_turn_id = str(snapshot.get("backend_turn_id") or "")
            if not backend_thread_id or not backend_turn_id or harness is None:
                return

            workspace_path = Path(workspace_root) if workspace_root else Path(".")
            state = RuntimeThreadRunEventState()
            initial_last_event_id = int(snapshot.get("last_event_id") or 0)
            last_event_id = initial_last_event_id
            async for raw_event in harness_progress_event_stream(
                harness,
                workspace_path,
                backend_thread_id,
                backend_turn_id,
            ):
                if isinstance(raw_event, dict):
                    activity_at = _event_received_at_iso(raw_event)
                    if activity_at is None:
                        activity_at = normalize_optional_text(
                            raw_event.get("published_at")
                        )
                    if activity_at:
                        snapshot["last_activity_at"] = activity_at
                serialized_entries = await _serialize_runtime_raw_tail_events(
                    raw_event,
                    state,
                    level=normalized_level,
                    event_id_start=last_event_id,
                    since_ms=since_ms,
                )
                for serialized_entry in serialized_entries:
                    entry_id = int(serialized_entry.get("event_id") or 0)
                    if entry_id > 0 and entry_id <= initial_last_event_id:
                        continue
                    event_id = _record_serialized_tail_event(snapshot, serialized_entry)
                    if event_id > 0:
                        last_event_id = event_id
                    yield (
                        "event: tail\n"
                        f"id: {event_id}\n"
                        f"data: {json.dumps(serialized_entry, ensure_ascii=True)}\n\n"
                    )

            refreshed = await _build_managed_thread_tail_snapshot(
                request=request,
                service=service,
                managed_thread_id=managed_thread_id,
                harness=harness,
                limit=min(limit, 200),
                level=normalized_level,
                since_ms=since_ms,
                resume_after=None,
            )
            yield (
                "event: progress\ndata: "
                f"{json.dumps({'managed_thread_id': managed_thread_id, 'managed_turn_id': refreshed.get('managed_turn_id'), 'turn_status': refreshed.get('turn_status') or 'running', 'elapsed_seconds': refreshed.get('elapsed_seconds'), 'idle_seconds': refreshed.get('idle_seconds'), 'phase': refreshed.get('phase'), 'phase_source': refreshed.get('phase_source'), 'guidance': refreshed.get('guidance'), 'last_tool': refreshed.get('last_tool'), 'active_turn_diagnostics': refreshed.get('active_turn_diagnostics')}, ensure_ascii=True)}\n\n"
            )
            return

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
