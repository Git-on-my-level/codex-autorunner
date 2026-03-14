from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
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
    payload: dict[str, Any] = {
        "event_id": event_id,
        "event_type": _tail_event_type_from_message(message),
        "summary": summary,
        "lines": lines[:8],
        "received_at_ms": received_at_ms if received_at_ms > 0 else None,
        "received_at": iso_from_event_ms(received_at_ms),
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
    can_stream_codex = (
        str(thread.agent_id or "").strip().lower() == "codex"
        and app_server_events is not None
        and bool(backend_thread_id)
        and bool(backend_turn_id)
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
    elif turn_status in {"error", "interrupted"}:
        activity = "failed"

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
        "stream_available": bool(can_stream_codex),
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
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps({'managed_thread_id': managed_thread_id, 'managed_turn_id': snapshot.get('managed_turn_id'), 'turn_status': 'running', 'elapsed_seconds': elapsed}, ensure_ascii=True)}\n\n"
                    )

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
                    yield (
                        "event: progress\ndata: "
                        f"{json.dumps({'managed_thread_id': managed_thread_id, 'managed_turn_id': snapshot.get('managed_turn_id'), 'turn_status': status or 'running', 'elapsed_seconds': elapsed, 'idle_seconds': idle}, ensure_ascii=True)}\n\n"
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
