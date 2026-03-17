from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .....core.app_server_logging import AppServerEventFormatter
from .....core.pma_thread_store import PmaThreadStore
from .....core.redaction import redact_text
from ..shared import SSE_HEADERS
from .automation_adapter import normalize_optional_text
from .managed_threads import (
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


def _tail_event_type_from_message(message: Any) -> str:
    payload = coerce_dict(message)
    method = str(payload.get("method") or "").strip().lower()
    params = coerce_dict(payload.get("params"))
    item = coerce_dict(params.get("item"))

    if method == "turn/completed":
        status = str(params.get("status") or "").strip().lower()
        if status in {"interrupt", "interrupted"}:
            return "turn_interrupted"
        if status in {"error", "failed"}:
            return "turn_failed"
        return "turn_completed"
    if method == "error":
        return "turn_failed"
    if method in {
        "item/commandexecution/requestapproval",
        "item/filechange/requestapproval",
    }:
        return "tool_started"
    if method == "item/toolcall/start":
        return "tool_started"
    if method == "item/toolcall/end":
        success = params.get("success")
        if success is False:
            return "tool_failed"
        return "tool_completed"
    if method == "item/completed":
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"commandexecution", "filechange", "tool"}:
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                return "tool_failed"
            return "tool_completed"
    if "reasoning" in method:
        return "assistant_update"
    return "progress"


_NO_STREAM_AVAILABLE_IDLE_SECONDS = 15
_LIKELY_HUNG_IDLE_SECONDS = 90


def _truncate_tool_name(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if text is None:
        return None
    return truncate_text(text, 80)


def _tool_metadata_from_message(message: Any) -> tuple[str | None, str | None]:
    payload = coerce_dict(message)
    method = str(payload.get("method") or "").strip().lower()
    params = coerce_dict(payload.get("params"))
    item = coerce_dict(params.get("item"))
    tool_name = _truncate_tool_name(
        item.get("name")
        or item.get("toolName")
        or item.get("command")
        or params.get("tool_name")
        or params.get("toolName")
        or params.get("command")
        or params.get("name")
    )
    tool_state: str | None = None
    if method in {
        "item/commandexecution/requestapproval",
        "item/filechange/requestapproval",
        "item/toolcall/start",
    }:
        tool_state = "started"
    elif method == "item/toolcall/end":
        tool_state = "failed" if params.get("success") is False else "completed"
    elif method == "item/completed":
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"commandexecution", "filechange", "tool"}:
            exit_code = item.get("exitCode")
            tool_state = (
                "failed"
                if isinstance(exit_code, int) and exit_code != 0
                else "completed"
            )
    return tool_name, tool_state


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


def _zeroclaw_tail_event_from_raw(
    *,
    event_id: int,
    raw_event: str,
    published_at: str | None,
    since_ms: Optional[int],
) -> dict[str, Any] | None:
    event_name, payload = _parse_inline_sse(raw_event)
    message = coerce_dict(payload.get("message"))
    method = str(message.get("method") or "").strip().lower()
    params = coerce_dict(message.get("params"))
    text = redact_text(str(params.get("text") or "")).strip()
    if not method:
        return None
    received_at = normalize_optional_text(published_at)
    received_at_ms = None
    if received_at is not None:
        dt = parse_iso_datetime(received_at)
        if dt is not None:
            received_at_ms = int(dt.timestamp() * 1000)
    if (
        since_ms is not None
        and received_at_ms is not None
        and received_at_ms < since_ms
    ):
        return None

    event_type = "progress"
    tool_name: str | None = None
    tool_state: str | None = None
    summary = text or method or event_name
    normalized_text = text.strip()
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
    elif method == "message.completed":
        event_type = "turn_completed"
        summary = truncate_text(text or "Turn completed", 220)
    else:
        event_type = "assistant_update"
        summary = truncate_text(summary, 220)

    return {
        "event_id": event_id,
        "event_type": event_type,
        "summary": truncate_text(summary, 220),
        "lines": [summary] if summary else [],
        "received_at_ms": received_at_ms,
        "received_at": received_at,
        "tool_name": tool_name,
        "tool_state": tool_state,
    }


async def _list_zeroclaw_tail_events(
    *,
    request: Request,
    workspace_root: str,
    session_id: str,
    turn_id: str,
    resume_after: Optional[int],
    since_ms: Optional[int],
    limit: int,
) -> list[dict[str, Any]]:
    supervisor = getattr(request.app.state, "zeroclaw_supervisor", None)
    list_events = getattr(supervisor, "list_turn_events", None)
    if not callable(list_events):
        return []
    try:
        raw_entries = await list_events(Path(workspace_root), session_id, turn_id)
    except Exception:
        return []
    start_after = int(resume_after or 0)
    serialized: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_entries, start=1):
        if index <= start_after:
            continue
        payload = entry if isinstance(entry, dict) else {}
        serialized_entry = _zeroclaw_tail_event_from_raw(
            event_id=index,
            raw_event=str(payload.get("raw_event") or ""),
            published_at=normalize_optional_text(payload.get("published_at")),
            since_ms=since_ms,
        )
        if serialized_entry is not None:
            serialized.append(serialized_entry)
    if len(serialized) > limit:
        serialized = serialized[-limit:]
    return serialized


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


def _serialize_tail_event(
    event: dict[str, Any],
    *,
    level: str,
    formatter: AppServerEventFormatter,
    since_ms: Optional[int],
) -> Optional[dict[str, Any]]:
    event_id = int(event.get("id") or 0)
    if event_id <= 0:
        return None
    received_at_ms = int(event.get("received_at") or 0)
    if since_ms is not None and received_at_ms and received_at_ms < since_ms:
        return None
    message = coerce_dict(event.get("message"))
    lines = [
        redact_text(str(line).strip())
        for line in formatter.format_event(message)
        if isinstance(line, str) and line.strip()
    ]
    if not lines:
        fallback = str(message.get("method") or "").strip()
        if fallback:
            lines = [fallback]
    summary = truncate_text(lines[0], 220) if lines else ""
    tool_name, tool_state = _tool_metadata_from_message(message)
    payload: dict[str, Any] = {
        "event_id": event_id,
        "event_type": _tail_event_type_from_message(message),
        "summary": summary,
        "lines": lines[:8],
        "received_at_ms": received_at_ms if received_at_ms > 0 else None,
        "received_at": iso_from_event_ms(received_at_ms),
        "tool_name": tool_name,
        "tool_state": tool_state,
    }
    if level == "debug":
        payload["raw"] = _redact_nested(message)
    return payload


async def _build_managed_thread_tail_snapshot(
    *,
    request: Request,
    service: Any,
    managed_thread_id: str,
    limit: int,
    level: str,
    since_ms: Optional[int],
    resume_after: Optional[int],
) -> dict[str, Any]:
    thread = service.get_thread_target(managed_thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Managed thread not found")
    turn = service.get_running_execution(
        managed_thread_id
    ) or service.get_latest_execution(managed_thread_id)
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
    app_server_events = getattr(request.app.state, "app_server_events", None)
    agent_id = str(thread.agent_id or "").strip().lower()
    can_stream_codex = (
        agent_id == "codex"
        and app_server_events is not None
        and bool(backend_thread_id)
        and bool(backend_turn_id)
    )
    can_stream_zeroclaw = (
        agent_id == "zeroclaw"
        and bool(thread.workspace_root)
        and bool(backend_thread_id)
        and bool(backend_turn_id)
        and callable(
            getattr(
                getattr(request.app.state, "zeroclaw_supervisor", None),
                "list_turn_events",
                None,
            )
        )
    )
    formatter = AppServerEventFormatter(redact_enabled=True)
    tail_events: list[dict[str, Any]] = []
    if can_stream_codex:
        raw_events = await app_server_events.list_events(
            str(backend_thread_id),
            str(backend_turn_id),
            after_id=int(resume_after or 0),
            limit=limit,
        )
        for event in raw_events:
            serialized = _serialize_tail_event(
                event,
                level=level,
                formatter=formatter,
                since_ms=since_ms,
            )
            if serialized is not None:
                tail_events.append(serialized)
    elif can_stream_zeroclaw and thread.workspace_root:
        tail_events = await _list_zeroclaw_tail_events(
            request=request,
            workspace_root=str(thread.workspace_root),
            session_id=str(backend_thread_id),
            turn_id=str(backend_turn_id),
            resume_after=resume_after,
            since_ms=since_ms,
            limit=limit,
        )

    last_event_id = int(resume_after or 0)
    if tail_events:
        last_event_id = int(tail_events[-1].get("event_id") or last_event_id)
    last_event_ms = tail_events[-1].get("received_at_ms") if tail_events else None
    idle_seconds: Optional[int] = None
    if turn_status == "running":
        if isinstance(last_event_ms, (int, float)) and last_event_ms > 0:
            idle_seconds = max(
                0, int((now_dt.timestamp() * 1000 - last_event_ms) / 1000)
            )
        elif started_dt is not None:
            idle_seconds = max(0, int((now_dt - started_dt).total_seconds()))

    activity = "idle"
    if turn_status == "running":
        activity = (
            "stalled" if idle_seconds is not None and idle_seconds >= 30 else "running"
        )
    elif turn_status == "ok":
        activity = "completed"
    elif turn_status == "interrupted":
        activity = "interrupted"
    elif turn_status == "error":
        activity = "failed"

    stream_available = bool(can_stream_codex or can_stream_zeroclaw)
    phase, phase_source, guidance, last_tool = _derive_progress_phase(
        turn_status=turn_status,
        stream_available=stream_available,
        events=tail_events,
        idle_seconds=idle_seconds,
    )

    return {
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
        "stream_available": stream_available,
        "phase": phase,
        "phase_source": phase_source,
        "guidance": guidance,
        "last_tool": last_tool,
    }


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
        service = build_managed_thread_orchestration_service(request)
        snapshot = await _build_managed_thread_tail_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
            limit=min(limit, 200),
            level=normalize_tail_level(level),
            since_ms=since_ms_from_duration(since),
            resume_after=resolve_resume_after(request, since_event_id),
        )
        thread = service.get_thread_target(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        serialized_thread = _serialize_thread_target(thread)
        turn = service.get_running_execution(
            managed_thread_id
        ) or service.get_latest_execution(managed_thread_id)
        queue_store = PmaThreadStore(request.app.state.config.root)
        queued_turns = queue_store.list_pending_turn_queue_items(
            managed_thread_id, limit=min(limit, 50)
        )
        latest_output_excerpt = ""
        if turn is not None:
            latest_output_excerpt = truncate_text(turn.output_text or "", 240)
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
            "queue_depth": service.get_queue_depth(managed_thread_id),
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
            "latest_output_excerpt": latest_output_excerpt,
            "stream_available": bool(snapshot.get("stream_available")),
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
        service = build_managed_thread_orchestration_service(request)
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
        service = build_managed_thread_orchestration_service(request)
        snapshot = await _build_managed_thread_tail_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
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
                    await asyncio.sleep(10.0)
                    turn = service.get_execution(
                        managed_thread_id,
                        str(snapshot.get("managed_turn_id") or ""),
                    )
                    status = (
                        str((turn.status if turn is not None else "") or "")
                        .strip()
                        .lower()
                    )
                    if status != "running":
                        yield (
                            "event: state\ndata: "
                            f"{json.dumps({'turn_status': status or 'unknown'}, ensure_ascii=True)}\n\n"
                        )
                        return
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
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps({'managed_thread_id': managed_thread_id, 'managed_turn_id': snapshot.get('managed_turn_id'), 'turn_status': 'running', 'elapsed_seconds': elapsed, 'phase': phase, 'phase_source': phase_source, 'guidance': guidance, 'last_tool': last_tool}, ensure_ascii=True)}\n\n"
                    )

            if str(snapshot.get("agent") or "").strip().lower() == "zeroclaw":
                thread_target = service.get_thread_target(managed_thread_id)
                workspace_root = (
                    str(thread_target.workspace_root or "")
                    if thread_target is not None
                    else ""
                )
                if (
                    workspace_root
                    and snapshot.get("backend_thread_id")
                    and snapshot.get("backend_turn_id")
                ):
                    last_event_id = int(snapshot.get("last_event_id") or 0)
                    while True:
                        await asyncio.sleep(2.0)
                        incremental_events = await _list_zeroclaw_tail_events(
                            request=request,
                            workspace_root=workspace_root,
                            session_id=str(snapshot.get("backend_thread_id") or ""),
                            turn_id=str(snapshot.get("backend_turn_id") or ""),
                            resume_after=last_event_id,
                            since_ms=since_ms,
                            limit=min(limit, 200),
                        )
                        for serialized in incremental_events:
                            event_id = int(serialized.get("event_id") or 0)
                            if event_id > 0:
                                last_event_id = event_id
                            snapshot_events = snapshot.get("events")
                            if isinstance(snapshot_events, list):
                                snapshot_events.append(serialized)
                            snapshot["last_event_at"] = serialized.get("received_at")
                            yield (
                                "event: tail\n"
                                f"id: {event_id}\n"
                                f"data: {json.dumps(serialized, ensure_ascii=True)}\n\n"
                            )

                        refreshed = await _build_managed_thread_tail_snapshot(
                            request=request,
                            service=service,
                            managed_thread_id=managed_thread_id,
                            limit=min(limit, 200),
                            level=normalized_level,
                            since_ms=since_ms,
                            resume_after=None,
                        )
                        yield (
                            "event: progress\ndata: "
                            f"{json.dumps({'managed_thread_id': managed_thread_id, 'managed_turn_id': refreshed.get('managed_turn_id'), 'turn_status': refreshed.get('turn_status') or 'running', 'elapsed_seconds': refreshed.get('elapsed_seconds'), 'idle_seconds': refreshed.get('idle_seconds'), 'phase': refreshed.get('phase'), 'phase_source': refreshed.get('phase_source'), 'guidance': refreshed.get('guidance'), 'last_tool': refreshed.get('last_tool')}, ensure_ascii=True)}\n\n"
                        )
                        if (
                            str(refreshed.get("turn_status") or "").strip().lower()
                            != "running"
                        ):
                            return
                return

            app_server_events = getattr(request.app.state, "app_server_events", None)
            if app_server_events is None:
                return
            backend_thread_id = str(snapshot.get("backend_thread_id") or "")
            backend_turn_id = str(snapshot.get("backend_turn_id") or "")
            if not backend_thread_id or not backend_turn_id:
                return

            formatter = AppServerEventFormatter(redact_enabled=True)
            last_event_id = int(snapshot.get("last_event_id") or 0)
            async for entry in app_server_events.stream_entries(
                backend_thread_id,
                backend_turn_id,
                after_id=last_event_id,
                heartbeat_interval=10.0,
            ):
                if entry is None:
                    turn = service.get_execution(
                        managed_thread_id,
                        str(snapshot.get("managed_turn_id") or ""),
                    )
                    status = (
                        str((turn.status if turn is not None else "") or "")
                        .strip()
                        .lower()
                    )
                    now = datetime.now(timezone.utc)
                    started_dt = parse_iso_datetime(snapshot.get("started_at"))
                    elapsed = None
                    if started_dt is not None:
                        elapsed = max(0, int((now - started_dt).total_seconds()))
                    idle = None
                    if snapshot.get("last_event_at"):
                        last_event_dt = parse_iso_datetime(
                            snapshot.get("last_event_at")
                        )
                        if last_event_dt is not None:
                            idle = max(0, int((now - last_event_dt).total_seconds()))
                    phase, phase_source, guidance, last_tool = _derive_progress_phase(
                        turn_status=status or "running",
                        stream_available=True,
                        events=[
                            event
                            for event in snapshot.get("events", [])
                            if isinstance(event, dict)
                        ],
                        idle_seconds=idle,
                    )
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps({'managed_thread_id': managed_thread_id, 'managed_turn_id': snapshot.get('managed_turn_id'), 'turn_status': status or 'running', 'elapsed_seconds': elapsed, 'idle_seconds': idle, 'phase': phase, 'phase_source': phase_source, 'guidance': guidance, 'last_tool': last_tool}, ensure_ascii=True)}\n\n"
                    )
                    if status != "running":
                        return
                    continue

                serialized = _serialize_tail_event(
                    entry,
                    level=normalized_level,
                    formatter=formatter,
                    since_ms=since_ms,
                )
                if serialized is None:
                    continue
                event_id = int(serialized.get("event_id") or 0)
                if event_id > 0:
                    last_event_id = event_id
                snapshot_events = snapshot.get("events")
                if isinstance(snapshot_events, list):
                    snapshot_events.append(serialized)
                snapshot["last_event_at"] = serialized.get("received_at")
                yield (
                    "event: tail\n"
                    f"id: {event_id}\n"
                    f"data: {json.dumps(serialized, ensure_ascii=True)}\n\n"
                )

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
