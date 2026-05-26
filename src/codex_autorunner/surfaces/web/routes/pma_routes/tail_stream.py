from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .....agents.base import harness_supports_event_streaming
from .....core.managed_thread_store import ManagedThreadStore
from .....core.orchestration.managed_thread_timeline import (
    timeline_item_from_tail_event,
)
from .....core.orchestration.managed_thread_transcript import (
    build_managed_thread_transcript,
    transcript_row_from_tail_event,
)
from .....core.orchestration.progress_projection import ProgressProjectionState
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
)
from .....core.orchestration.turn_timeline import list_turn_timeline
from ...services.pma import get_pma_request_context
from ...services.pma.common import normalize_optional_text
from ..shared import SSE_HEADERS
from .managed_thread_tail_serializers import (
    _canonical_turn_request_metadata,
    _derive_active_turn_diagnostics,
    _derive_progress_phase,
    _event_received_at_iso,
    _latest_token_usage_from_timeline_entries,
    _running_turn_stall_flags,
    _serialize_persisted_timeline_tail_events,
    _serialize_runtime_raw_tail_events,
    build_live_activity_projection,
    build_managed_thread_status_response,
    build_managed_thread_stream_lifecycle,
    parse_iso_datetime,
)
from .managed_threads import (
    _attach_latest_execution_fields,
    _serialize_thread_target,
    build_managed_thread_orchestration_service,
)

_PERSISTED_TAIL_POLL_SECONDS = 1.0
_PERSISTED_TAIL_HEARTBEAT_SECONDS = 15.0
_TRANSCRIPT_STREAM_LIMIT = 200
_TRANSCRIPT_APPEND_ROW_LIMIT = 80


def _transcript_append_frames(
    rows: list[dict[str, Any]],
    append_event_id: int,
    *,
    chunk_limit: int = _TRANSCRIPT_APPEND_ROW_LIMIT,
) -> Iterator[str]:
    if not rows:
        return
    effective_limit = max(1, chunk_limit)
    for offset in range(0, len(rows), effective_limit):
        chunk = rows[offset : offset + effective_limit]
        is_final_chunk = offset + effective_limit >= len(rows)
        append_id_line = (
            f"id: {append_event_id}\n" if is_final_chunk and append_event_id > 0 else ""
        )
        yield (
            "event: transcript.append\n"
            f"{append_id_line}"
            f"data: {json.dumps({'rows': chunk}, ensure_ascii=True)}\n\n"
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


def _managed_thread_harness(
    service: Any, agent_id: str, profile: Optional[str] = None
) -> Any:
    factory = getattr(service, "harness_factory", None)
    if not callable(factory):
        return None
    try:
        if profile:
            try:
                return factory(agent_id, profile)
            except TypeError as exc:
                if "positional argument" not in str(exc):
                    raise
                return factory(agent_id)
        return factory(agent_id)
    except Exception:  # intentional: dynamic harness factory - exception types unknown
        return None


def _managed_thread_agent_profile(thread: Any) -> Optional[str]:
    profile = normalize_optional_text(getattr(thread, "agent_profile", None))
    if profile:
        return profile
    metadata = getattr(thread, "metadata", None)
    if isinstance(metadata, dict):
        return normalize_optional_text(metadata.get("agent_profile"))
    return None


def _managed_thread_harness_for_thread(service: Any, thread: Any) -> Any:
    agent_id = str(getattr(thread, "agent_id", "") or "")
    profile = _managed_thread_agent_profile(thread)
    if profile:
        return _managed_thread_harness(service, agent_id, profile)
    return _managed_thread_harness(service, agent_id)


def _load_managed_thread_tail_store_state(
    *,
    hub_root: Path,
    thread_store: ManagedThreadStore,
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
    thread_store: ManagedThreadStore,
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
    serialized_thread["status"] = thread.status
    serialized_thread["normalized_status"] = thread.status
    queued_turns = thread_store.list_pending_turn_queue_items(
        managed_thread_id,
        limit=min(limit, 50),
    )
    queue_depth = service.get_queue_depth(managed_thread_id)
    return thread, serialized_thread, queued_turns, queue_depth


def _stream_lifecycle_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "work_status": payload.get("work_status"),
        "operator_status": payload.get("operator_status"),
        "terminal": payload.get("terminal"),
        "stream_should_close": payload.get("stream_should_close"),
        "stream_close_reason": payload.get("stream_close_reason"),
        "stream_lifecycle": payload.get("stream_lifecycle"),
    }


def _timeline_stream_frame(
    *,
    managed_thread_id: str,
    managed_turn_id: Any,
    tail_event: dict[str, Any],
) -> str | None:
    item = timeline_item_from_tail_event(
        managed_thread_id=managed_thread_id,
        managed_turn_id=str(managed_turn_id or ""),
        tail_event=tail_event,
    )
    if item is None:
        return None
    event_id = tail_event.get("event_id")
    event_id_line = (
        f"id: {event_id}\n" if isinstance(event_id, int) and event_id > 0 else ""
    )
    return (
        f"event: timeline\n"
        f"{event_id_line}"
        f"data: {json.dumps(item, ensure_ascii=True)}\n\n"
    )


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
    resume_after_managed_turn_id: Optional[str] = None,
    include_runtime_overlay: bool = True,
    runtime_projection_state: ProgressProjectionState | None = None,
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
    requested_resume_turn_id = normalize_optional_text(resume_after_managed_turn_id)
    if turn is None:
        effective_resume_after = (
            0 if requested_resume_turn_id else int(resume_after or 0)
        )
        lifecycle = build_managed_thread_stream_lifecycle(
            managed_turn_id=None,
            turn_status=None,
            thread_status=getattr(thread, "status", None),
            lifecycle_status=getattr(thread, "lifecycle_status", None),
            stream_available=False,
            queue_depth=0,
        )
        return {
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": None,
            "agent": thread.agent_id,
            "turn_status": None,
            "thread_status": getattr(thread, "status", None),
            "thread_lifecycle_status": getattr(thread, "lifecycle_status", None),
            "lifecycle_events": [],
            "events": [],
            "last_event_id": effective_resume_after,
            "elapsed_seconds": None,
            "idle_seconds": None,
            "activity": "idle",
            "stream_available": False,
            "work_status": lifecycle["work_status"],
            "operator_status": lifecycle["operator_status"],
            "terminal": lifecycle["terminal"],
            "stream_should_close": lifecycle["stream_should_close"],
            "stream_close_reason": lifecycle["stream_close_reason"],
            "stream_lifecycle": lifecycle,
            "live_activity": build_live_activity_projection(
                snapshot={
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": None,
                    "activity": "idle",
                    "events": [],
                    "elapsed_seconds": None,
                    "idle_seconds": None,
                    "stream_available": False,
                    "terminal": lifecycle["terminal"],
                }
            ),
        }

    managed_turn_id = str(turn.execution_id or "")
    effective_resume_after = int(resume_after or 0)
    if (
        requested_resume_turn_id is not None
        and requested_resume_turn_id != managed_turn_id
    ):
        effective_resume_after = 0
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
    elif turn_status in {"error", "failed"}:
        lifecycle_events.append("turn_failed")
    elif turn_status == "interrupted":
        lifecycle_events.append("turn_interrupted")

    backend_thread_id = normalize_optional_text(thread.backend_thread_id)
    backend_turn_id = normalize_optional_text(turn.backend_id)
    if harness is None:
        harness = _managed_thread_harness_for_thread(service, thread)
    has_backend_binding = bool(backend_thread_id) and bool(backend_turn_id)
    stream_available = bool(
        harness is not None
        and has_backend_binding
        and harness_supports_event_streaming(harness)
    )
    tail_events: list[dict[str, Any]] = []
    raw_last_activity_at: Optional[str] = None
    token_usage = _latest_token_usage_from_timeline_entries(persisted_timeline_entries)
    tail_events, raw_last_activity_at = _serialize_persisted_timeline_tail_events(
        persisted_timeline_entries,
        level=level,
        since_ms=since_ms,
        resume_after=effective_resume_after,
    )
    turn_running = str(turn_status or "").strip().lower() == "running"
    persisted_floor_id = int(effective_resume_after or 0)
    persisted_max_event_id = persisted_floor_id
    if tail_events:
        persisted_max_event_id = max(int(e.get("event_id") or 0) for e in tail_events)

    runtime_overlay_eligible = bool(
        include_runtime_overlay
        and has_backend_binding
        and harness is not None
        and (not tail_events or (turn_running and stream_available))
    )
    if runtime_overlay_eligible:
        list_fn = getattr(harness, "list_progress_events", None)
        if callable(list_fn):
            try:
                raw_events = await list_fn(
                    str(backend_thread_id),
                    str(backend_turn_id),
                    after_id=persisted_max_event_id,
                    limit=limit,
                )
            except (
                Exception
            ):  # intentional: dynamic harness method - exception types depend on backend
                raw_events = []
            state = RuntimeThreadRunEventState()
            projection_state = runtime_projection_state or ProgressProjectionState()
            event_id_start = persisted_max_event_id
            overlay_floor = persisted_max_event_id
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
                    projection_state=projection_state,
                    default_received_at=finished_at,
                )
                if isinstance(state.token_usage, dict) and state.token_usage:
                    token_usage = dict(state.token_usage)
                for entry in serialized_entries:
                    eid = int(entry.get("event_id") or 0)
                    if eid <= overlay_floor:
                        continue
                    tail_events.append(entry)
                    event_id_start = int(entry.get("event_id") or event_id_start)
            if len(tail_events) > limit:
                tail_events = tail_events[-limit:]

    last_event_id = effective_resume_after
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
            idle_seconds=idle_seconds,
            last_event_at=last_activity_at,
            agent_id=getattr(thread, "agent_id", None),
            has_visible_events=bool(tail_events),
        )
        activity = "stalled" if is_stalled else "running"
    elif turn_status == "ok":
        activity = "completed"
    elif turn_status == "interrupted":
        activity = "interrupted"
    elif turn_status in {"error", "failed"}:
        activity = "failed"

    phase, phase_source, guidance, last_tool = _derive_progress_phase(
        turn_status=turn_status,
        stream_available=stream_available,
        events=tail_events,
        idle_seconds=idle_seconds,
        agent_id=getattr(thread, "agent_id", None),
    )
    snapshot: dict[str, Any] = {
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "agent": thread.agent_id,
        "backend_thread_id": backend_thread_id,
        "backend_turn_id": backend_turn_id,
        "turn_status": turn_status,
        "canonical_request": _canonical_turn_request_metadata(turn_record),
        "thread_status": getattr(thread, "status", None),
        "thread_lifecycle_status": getattr(thread, "lifecycle_status", None),
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
        "token_usage": token_usage,
    }
    lifecycle = build_managed_thread_stream_lifecycle(
        managed_turn_id=managed_turn_id,
        turn_status=turn_status,
        thread_status=getattr(thread, "status", None),
        lifecycle_status=getattr(thread, "lifecycle_status", None),
        stream_available=stream_available,
        queue_depth=0,
    )
    snapshot.update(
        {
            "work_status": lifecycle["work_status"],
            "operator_status": lifecycle["operator_status"],
            "terminal": lifecycle["terminal"],
            "stream_should_close": lifecycle["stream_should_close"],
            "stream_close_reason": lifecycle["stream_close_reason"],
            "stream_lifecycle": lifecycle,
        }
    )
    snapshot["live_activity"] = build_live_activity_projection(snapshot=snapshot)
    snapshot["active_turn_diagnostics"] = _derive_active_turn_diagnostics(
        snapshot=snapshot,
        turn_record=turn_record,
    )
    return snapshot


def _tail_event_sse_frames(
    *,
    managed_thread_id: str,
    managed_turn_id: Any,
    events: list[Any],
) -> list[str]:
    frames: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        event_id_line = (
            f"id: {event_id}\n" if isinstance(event_id, int) and event_id > 0 else ""
        )
        frames.append(
            f"event: tail\n"
            f"{event_id_line}"
            f"data: {json.dumps(event, ensure_ascii=True)}\n\n"
        )
        timeline_frame = _timeline_stream_frame(
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            tail_event=event,
        )
        if timeline_frame is not None:
            frames.append(timeline_frame)
    return frames


def _sse_initial_replay_requested(
    *,
    request: Request,
    replay: bool,
    since: Optional[str],
    since_event_id: Optional[int],
    since_managed_turn_id: Optional[str],
) -> bool:
    if replay:
        return True
    if since is not None or since_event_id is not None:
        return True
    if normalize_optional_text(since_managed_turn_id) is not None:
        return True
    return bool(request.headers.get("Last-Event-ID"))


def _tail_snapshot_without_replay(snapshot: dict[str, Any]) -> dict[str, Any]:
    cheap_snapshot = dict(snapshot)
    events = snapshot.get("events")
    high_watermark = int(snapshot.get("last_event_id") or 0)
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            high_watermark = max(high_watermark, int(event.get("event_id") or 0))
    cheap_snapshot["events"] = []
    cheap_snapshot["last_event_id"] = high_watermark
    cheap_snapshot["last_event_at"] = None
    live_activity = cheap_snapshot.get("live_activity")
    if isinstance(live_activity, dict):
        live_activity = dict(live_activity)
        live_activity["summary"] = None
        live_activity["current_tool"] = None
        live_activity["events"] = []
        live_activity["visible_event_count"] = 0
        live_activity["coalesced_event_count"] = 0
        cheap_snapshot["live_activity"] = live_activity
    diagnostics = cheap_snapshot.get("active_turn_diagnostics")
    if isinstance(diagnostics, dict):
        diagnostics = dict(diagnostics)
        diagnostics["last_event_type"] = None
        diagnostics["last_event_summary"] = None
        cheap_snapshot["active_turn_diagnostics"] = diagnostics
    return cheap_snapshot


def _apply_sse_lifetime_to_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Rewrite per-turn close hints to reflect the SSE-lifetime contract.

    The per-turn lifecycle reports `stream_should_close=true` for every terminal
    turn. The SSE subscription, by contrast, lives across turn boundaries — see
    `_sse_stream_should_terminate`. Emitting the raw snapshot (in the `state`
    frame) would tear down the client subscription the moment it sees a chat
    that ended its previous turn, which is the steady state for most chats.
    """
    sse_close, sse_close_reason = _sse_stream_should_terminate(snapshot)
    snapshot["stream_should_close"] = sse_close
    snapshot["stream_close_reason"] = sse_close_reason
    nested = snapshot.get("stream_lifecycle")
    if isinstance(nested, dict):
        nested = dict(nested)
        nested["stream_should_close"] = sse_close
        nested["stream_close_reason"] = sse_close_reason
        snapshot["stream_lifecycle"] = nested
    return snapshot


def _progress_stream_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    lifecycle_fields = _stream_lifecycle_fields(snapshot)
    sse_close, sse_close_reason = _sse_stream_should_terminate(snapshot)
    lifecycle_fields["stream_should_close"] = sse_close
    lifecycle_fields["stream_close_reason"] = sse_close_reason
    return {
        "managed_thread_id": snapshot.get("managed_thread_id"),
        "managed_turn_id": snapshot.get("managed_turn_id"),
        "turn_status": snapshot.get("turn_status") or "unknown",
        "started_at": snapshot.get("started_at"),
        "elapsed_seconds": snapshot.get("elapsed_seconds"),
        "idle_seconds": snapshot.get("idle_seconds"),
        "phase": snapshot.get("phase"),
        "phase_source": snapshot.get("phase_source"),
        "guidance": snapshot.get("guidance"),
        "last_tool": snapshot.get("last_tool"),
        "live_activity": snapshot.get("live_activity"),
        "active_turn_diagnostics": snapshot.get("active_turn_diagnostics"),
        **lifecycle_fields,
    }


def _sse_stream_should_terminate(
    snapshot: dict[str, Any],
) -> tuple[bool, str | None]:
    """Whether the SSE stream should end for this snapshot.

    The SSE subscription represents a viewer attached to a thread, not to a
    single turn. A finished turn does NOT end the stream — the user may queue
    another turn and expects to see it stream live. The stream ends only when
    the thread itself will produce no further activity (archived or missing).
    """
    thread_status = (snapshot.get("thread_status") or "").strip().lower()
    lifecycle_status = (snapshot.get("thread_lifecycle_status") or "").strip().lower()
    if thread_status == "archived" or lifecycle_status == "archived":
        return True, "thread_archived"
    return False, None


def _successful_terminal_turn_id(snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("terminal") is not True:
        return None
    if str(snapshot.get("turn_status") or "").strip().lower() not in {
        "ok",
        "done",
        "completed",
        "complete",
    }:
        return None
    return normalize_optional_text(snapshot.get("managed_turn_id"))


def _transcript_has_assistant_row(snapshot: dict[str, Any], turn_id: str) -> bool:
    rows = snapshot.get("rows")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") != "message" or row.get("turn_id") != turn_id:
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        if (
            message.get("role") == "assistant"
            and str(message.get("text") or "").strip()
        ):
            return True
    return False


async def _build_managed_thread_transcript_snapshot(
    *,
    request: Request,
    service: Any,
    managed_thread_id: str,
    harness: Any | None = None,
    limit: int = _TRANSCRIPT_STREAM_LIMIT,
    level: str = "info",
    include_runtime_overlay: bool = True,
    runtime_projection_state: ProgressProjectionState | None = None,
) -> dict[str, Any]:
    context = get_pma_request_context(request)
    progress_snapshot = await _build_managed_thread_tail_snapshot(
        request=request,
        service=service,
        managed_thread_id=managed_thread_id,
        harness=harness,
        limit=min(limit, _TRANSCRIPT_STREAM_LIMIT),
        level=level,
        since_ms=None,
        resume_after=None,
        resume_after_managed_turn_id=None,
        include_runtime_overlay=include_runtime_overlay,
        runtime_projection_state=runtime_projection_state,
    )
    _apply_sse_lifetime_to_snapshot(progress_snapshot)
    return await asyncio.to_thread(
        build_managed_thread_transcript,
        context.hub_root,
        thread_store=context.thread_store(),
        managed_thread_id=managed_thread_id,
        limit=min(limit, _TRANSCRIPT_STREAM_LIMIT),
        progress_snapshot=progress_snapshot,
    )


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
        since_managed_turn_id: Optional[str] = None,
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
            resume_after_managed_turn_id=since_managed_turn_id,
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
        since_managed_turn_id: Optional[str] = None,
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
            resume_after_managed_turn_id=since_managed_turn_id,
        )

    @router.get("/threads/{managed_thread_id}/transcript")
    async def get_managed_thread_transcript(
        managed_thread_id: str,
        request: Request,
        limit: int = _TRANSCRIPT_STREAM_LIMIT,
        level: str = "info",
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        service = await _build_managed_thread_orchestration_service_async(request)
        thread_target = await asyncio.to_thread(
            service.get_thread_target,
            managed_thread_id,
        )
        harness = (
            _managed_thread_harness_for_thread(service, thread_target)
            if thread_target is not None
            else None
        )
        return await _build_managed_thread_transcript_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
            harness=harness,
            limit=min(limit, _TRANSCRIPT_STREAM_LIMIT),
            level=normalize_tail_level(level),
        )

    @router.get("/threads/{managed_thread_id}/transcript/events")
    async def stream_managed_thread_transcript(
        managed_thread_id: str,
        request: Request,
        limit: int = _TRANSCRIPT_STREAM_LIMIT,
        since_event_id: Optional[int] = None,
        since_managed_turn_id: Optional[str] = None,
        level: str = "info",
        once: bool = False,
        replay: bool = False,
    ):
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be greater than 0")
        normalized_level = normalize_tail_level(level)
        service = await _build_managed_thread_orchestration_service_async(request)
        thread_target = await asyncio.to_thread(
            service.get_thread_target,
            managed_thread_id,
        )
        harness = (
            _managed_thread_harness_for_thread(service, thread_target)
            if thread_target is not None
            else None
        )
        resume_after = resolve_resume_after(request, since_event_id)
        replay_initial_events = _sse_initial_replay_requested(
            request=request,
            replay=replay,
            since=None,
            since_event_id=since_event_id,
            since_managed_turn_id=since_managed_turn_id,
        )
        runtime_projection_state = ProgressProjectionState()
        initial = await _build_managed_thread_transcript_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
            harness=harness,
            limit=min(limit, _TRANSCRIPT_STREAM_LIMIT),
            level=normalized_level,
            runtime_projection_state=runtime_projection_state,
        )

        async def _stream() -> Any:
            raw_initial_progress = initial.get("status")
            initial_progress: dict[str, Any] = (
                raw_initial_progress if isinstance(raw_initial_progress, dict) else {}
            )
            last_event_id = int(
                initial_progress.get("last_event_id") or resume_after or 0
            )
            last_managed_turn_id = normalize_optional_text(
                initial_progress.get("managed_turn_id") or since_managed_turn_id
            )
            snapshot_id_line = f"id: {last_event_id}\n" if last_event_id > 0 else ""
            yield (
                "event: transcript.snapshot\n"
                f"{snapshot_id_line}"
                "data: "
                f"{json.dumps(initial, ensure_ascii=True)}\n\n"
            )
            if replay_initial_events:
                for row in initial.get("rows", []):
                    if isinstance(row, dict):
                        replay_id_line = (
                            f"id: {last_event_id}\n" if last_event_id > 0 else ""
                        )
                        yield (
                            "event: transcript.append\n"
                            f"{replay_id_line}"
                            "data: "
                            f"{json.dumps({'rows': [row]}, ensure_ascii=True)}\n\n"
                        )
            if once:
                return
            sse_close, _ = _sse_stream_should_terminate(initial_progress)
            if sse_close:
                return

            last_heartbeat_at = asyncio.get_running_loop().time()
            terminal_transcript_snapshots_sent: set[str] = set()
            while True:
                await asyncio.sleep(_PERSISTED_TAIL_POLL_SECONDS)
                if await request.is_disconnected():
                    return
                refreshed = await _build_managed_thread_tail_snapshot(
                    request=request,
                    service=service,
                    managed_thread_id=managed_thread_id,
                    harness=harness,
                    limit=min(limit, _TRANSCRIPT_STREAM_LIMIT),
                    level=normalized_level,
                    since_ms=None,
                    resume_after=last_event_id,
                    resume_after_managed_turn_id=last_managed_turn_id,
                    include_runtime_overlay=True,
                    runtime_projection_state=runtime_projection_state,
                )
                _apply_sse_lifetime_to_snapshot(refreshed)
                rows: list[dict[str, Any]] = []
                for event in refreshed.get("events", []):
                    if not isinstance(event, dict):
                        continue
                    rows.extend(
                        transcript_row_from_tail_event(
                            managed_thread_id=managed_thread_id,
                            managed_turn_id=str(refreshed.get("managed_turn_id") or ""),
                            tail_event=event,
                        )
                    )
                if rows:
                    append_event_id = int(
                        refreshed.get("last_event_id") or last_event_id
                    )
                    for frame in _transcript_append_frames(rows, append_event_id):
                        yield frame
                last_event_id = int(refreshed.get("last_event_id") or last_event_id)
                last_managed_turn_id = normalize_optional_text(
                    refreshed.get("managed_turn_id")
                )
                terminal_turn_id = _successful_terminal_turn_id(refreshed)
                if (
                    terminal_turn_id
                    and terminal_turn_id not in terminal_transcript_snapshots_sent
                ):
                    terminal_snapshot = await _build_managed_thread_transcript_snapshot(
                        request=request,
                        service=service,
                        managed_thread_id=managed_thread_id,
                        harness=harness,
                        limit=min(limit, _TRANSCRIPT_STREAM_LIMIT),
                        level=normalized_level,
                    )
                    if _transcript_has_assistant_row(
                        terminal_snapshot,
                        terminal_turn_id,
                    ):
                        terminal_id_line = (
                            f"id: {last_event_id}\n" if last_event_id > 0 else ""
                        )
                        yield (
                            "event: transcript.snapshot\n"
                            f"{terminal_id_line}"
                            "data: "
                            f"{json.dumps(terminal_snapshot, ensure_ascii=True)}\n\n"
                        )
                        terminal_transcript_snapshots_sent.add(terminal_turn_id)
                patch_id_line = f"id: {last_event_id}\n" if last_event_id > 0 else ""
                yield (
                    "event: transcript.patch\n"
                    f"{patch_id_line}"
                    "data: "
                    f"{json.dumps({'status': _progress_stream_payload(refreshed)}, ensure_ascii=True)}\n\n"
                )
                sse_close, _ = _sse_stream_should_terminate(refreshed)
                if sse_close:
                    return
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat_at >= _PERSISTED_TAIL_HEARTBEAT_SECONDS:
                    yield ": keep-alive\n\n"
                    last_heartbeat_at = now

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @router.get("/threads/{managed_thread_id}/tail/events")
    async def stream_managed_thread_tail(
        managed_thread_id: str,
        request: Request,
        limit: int = 50,
        since: Optional[str] = None,
        since_event_id: Optional[int] = None,
        since_managed_turn_id: Optional[str] = None,
        level: str = "info",
        once: bool = False,
        replay: bool = False,
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
            harness = _managed_thread_harness_for_thread(service, thread_target)
        resume_after = resolve_resume_after(request, since_event_id)
        replay_initial_events = _sse_initial_replay_requested(
            request=request,
            replay=replay,
            since=since,
            since_event_id=since_event_id,
            since_managed_turn_id=since_managed_turn_id,
        )
        snapshot = await _build_managed_thread_tail_snapshot(
            request=request,
            service=service,
            managed_thread_id=managed_thread_id,
            harness=harness,
            limit=min(limit, 200),
            level=normalized_level,
            since_ms=since_ms,
            resume_after=resume_after,
            resume_after_managed_turn_id=since_managed_turn_id,
            # Live UI needs harness-buffered deltas (OpenCode) while the durable
            # turn journal may lag; JSON GET /tail already uses the default True.
            include_runtime_overlay=True,
        )

        async def _stream() -> Any:
            initial_snapshot = (
                snapshot
                if replay_initial_events
                else _tail_snapshot_without_replay(snapshot)
            )
            _apply_sse_lifetime_to_snapshot(initial_snapshot)
            yield (
                "event: state\ndata: "
                f"{json.dumps(initial_snapshot, ensure_ascii=True)}\n\n"
            )
            if replay_initial_events:
                for frame in _tail_event_sse_frames(
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=initial_snapshot.get("managed_turn_id"),
                    events=initial_snapshot.get("events", []),
                ):
                    yield frame
            last_event_id = int(initial_snapshot.get("last_event_id") or 0)
            last_managed_turn_id = normalize_optional_text(
                initial_snapshot.get("managed_turn_id")
            )
            yield (
                "event: progress\ndata: "
                f"{json.dumps(_progress_stream_payload(initial_snapshot), ensure_ascii=True)}\n\n"
            )
            if once:
                return
            sse_close, _ = _sse_stream_should_terminate(initial_snapshot)
            if sse_close:
                return

            last_heartbeat_at = asyncio.get_running_loop().time()
            while True:
                await asyncio.sleep(_PERSISTED_TAIL_POLL_SECONDS)
                if await request.is_disconnected():
                    return
                refreshed = await _build_managed_thread_tail_snapshot(
                    request=request,
                    service=service,
                    managed_thread_id=managed_thread_id,
                    harness=harness,
                    limit=min(limit, 200),
                    level=normalized_level,
                    since_ms=since_ms,
                    resume_after=last_event_id,
                    resume_after_managed_turn_id=last_managed_turn_id,
                    include_runtime_overlay=True,
                )
                _apply_sse_lifetime_to_snapshot(refreshed)
                for frame in _tail_event_sse_frames(
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=refreshed.get("managed_turn_id"),
                    events=refreshed.get("events", []),
                ):
                    yield frame
                last_event_id = int(refreshed.get("last_event_id") or last_event_id)
                last_managed_turn_id = normalize_optional_text(
                    refreshed.get("managed_turn_id")
                )
                yield (
                    "event: progress\ndata: "
                    f"{json.dumps(_progress_stream_payload(refreshed), ensure_ascii=True)}\n\n"
                )
                sse_close, _ = _sse_stream_should_terminate(refreshed)
                if sse_close:
                    return
                now = asyncio.get_running_loop().time()
                if now - last_heartbeat_at >= _PERSISTED_TAIL_HEARTBEAT_SECONDS:
                    yield ": keep-alive\n\n"
                    last_heartbeat_at = now

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
