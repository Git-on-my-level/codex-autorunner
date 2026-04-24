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
)
from .....core.orchestration.turn_timeline import list_turn_timeline
from .....core.pma_thread_store import PmaThreadStore
from ...services.pma import get_pma_request_context
from ..shared import SSE_HEADERS
from .automation_adapter import normalize_optional_text
from .managed_thread_tail_serializers import (
    _derive_active_turn_diagnostics,
    _derive_progress_phase,
    _event_received_at_iso,
    _record_serialized_tail_event,
    _refresh_active_turn_diagnostics,
    _running_turn_stall_flags,
    _serialize_persisted_timeline_tail_events,
    _serialize_runtime_raw_tail_events,
    build_managed_thread_status_response,
    parse_iso_datetime,
)
from .managed_threads import (
    _attach_latest_execution_fields,
    _serialize_thread_target,
    build_managed_thread_orchestration_service,
)


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
    thread_store: PmaThreadStore,
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
        turn_record = thread_store.get_turn(
            managed_thread_id,
            managed_turn_id,
        )
    return thread, turn, persisted_timeline_entries, turn_record


def _load_managed_thread_status_state(
    *,
    service: Any,
    thread_store: PmaThreadStore,
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
    queued_turns = thread_store.list_pending_turn_queue_items(
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
    context = get_pma_request_context(request)
    thread, turn, persisted_timeline_entries, turn_record = await asyncio.to_thread(
        _load_managed_thread_tail_store_state,
        hub_root=context.hub_root,
        thread_store=context.thread_store(),
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
        context = get_pma_request_context(request)
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
            service=service,
            thread_store=context.thread_store(),
            managed_thread_id=managed_thread_id,
            limit=limit,
        )
        if thread is None or serialized_thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")
        return build_managed_thread_status_response(
            managed_thread_id=managed_thread_id,
            serialized_thread=serialized_thread,
            snapshot=snapshot,
            queued_turns=queued_turns,
            queue_depth=queue_depth,
        )

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
