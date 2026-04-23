from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from .....core.orchestration.cold_trace_store import ColdTraceWriter
from .....core.orchestration.turn_timeline import (
    append_turn_events_to_cold_trace,
    persist_turn_timeline,
)
from .....core.pma_audit import PmaActionType
from .....core.pma_context import PMA_MAX_TEXT
from .....core.pma_state import PmaStateStore
from .....core.pma_transcripts import PmaTranscriptStore
from .....core.ports.run_event import RunEvent
from .....core.text_utils import _normalize_optional_text, _truncate_text
from .....core.time_utils import now_iso
from ...services.pma import get_pma_request_context
from .runtime_state import PmaRuntimeState

logger = logging.getLogger(__name__)


def format_last_result(
    result: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    status = result.get("status") or "error"
    message = result.get("message")
    detail = result.get("detail")
    text = message if isinstance(message, str) and message else detail
    summary = _truncate_text(text or "", PMA_MAX_TEXT)
    payload: dict[str, Any] = {
        "status": status,
        "message": summary,
        "detail": (
            _truncate_text(detail or "", PMA_MAX_TEXT)
            if isinstance(detail, str)
            else None
        ),
        "client_turn_id": result.get("client_turn_id") or "",
        "agent": current.get("agent"),
        "profile": current.get("profile"),
        "thread_id": result.get("thread_id") or current.get("thread_id"),
        "turn_id": result.get("turn_id") or current.get("turn_id"),
        "started_at": current.get("started_at"),
        "finished_at": now_iso(),
    }
    delivery_status = _normalize_optional_text(result.get("delivery_status"))
    if delivery_status:
        payload["delivery_status"] = delivery_status
    delivery_outcome = result.get("delivery_outcome")
    if isinstance(delivery_outcome, dict):
        payload["delivery_outcome"] = dict(delivery_outcome)
    return payload


def resolve_transcript_turn_id(result: dict[str, Any], current: dict[str, Any]) -> str:
    for candidate in (
        result.get("turn_id"),
        current.get("turn_id"),
        current.get("client_turn_id"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return f"local-{uuid.uuid4()}"


def resolve_transcript_text(result: dict[str, Any]) -> str:
    message = result.get("message")
    if isinstance(message, str) and message.strip():
        return message
    detail = result.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail
    return ""


def build_transcript_metadata(
    *,
    result: dict[str, Any],
    current: dict[str, Any],
    prompt_message: Optional[str],
    lifecycle_event: Optional[dict[str, Any]],
    profile: Optional[str],
    model: Optional[str],
    reasoning: Optional[str],
    duration_ms: Optional[int],
    finished_at: str,
) -> dict[str, Any]:
    trigger = "lifecycle_event" if lifecycle_event else "user_prompt"
    metadata: dict[str, Any] = {
        "status": result.get("status") or "error",
        "agent": current.get("agent"),
        "profile": profile,
        "thread_id": result.get("thread_id") or current.get("thread_id"),
        "turn_id": resolve_transcript_turn_id(result, current),
        "client_turn_id": current.get("client_turn_id") or "",
        "lane_id": current.get("lane_id") or "",
        "trigger": trigger,
        "model": model,
        "reasoning": reasoning,
        "started_at": current.get("started_at"),
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "user_prompt": prompt_message or "",
    }
    if lifecycle_event:
        metadata["lifecycle_event"] = dict(lifecycle_event)
        metadata["event_id"] = lifecycle_event.get("event_id")
        metadata["event_type"] = lifecycle_event.get("event_type")
        metadata["repo_id"] = lifecycle_event.get("repo_id")
        metadata["run_id"] = lifecycle_event.get("run_id")
        metadata["event_timestamp"] = lifecycle_event.get("timestamp")
    return metadata


def persist_timeline(
    *,
    hub_root: Path,
    execution_id: str,
    metadata: dict[str, Any],
    events: list[RunEvent],
) -> int:
    timeline_events = list(events)
    trace_writer = ColdTraceWriter(
        hub_root=hub_root,
        execution_id=execution_id,
        backend_thread_id=_normalize_optional_text(metadata.get("thread_id")),
        backend_turn_id=_normalize_optional_text(metadata.get("turn_id")),
    ).open()
    trace_manifest_id: Optional[str] = None
    try:
        append_turn_events_to_cold_trace(trace_writer, events=timeline_events)
        trace_manifest_id = trace_writer.finalize().trace_id
    finally:
        trace_writer.close()
    return persist_turn_timeline(
        hub_root,
        execution_id=execution_id,
        target_kind="lane",
        target_id=str(metadata.get("lane_id") or "").strip() or "pma:default",
        repo_id=_normalize_optional_text(metadata.get("repo_id")),
        run_id=_normalize_optional_text(metadata.get("run_id")),
        metadata={
            **metadata,
            **({"trace_manifest_id": trace_manifest_id} if trace_manifest_id else {}),
        },
        events=timeline_events,
    )


async def persist_transcript(
    *,
    hub_root: Path,
    result: dict[str, Any],
    current: dict[str, Any],
    prompt_message: Optional[str],
    lifecycle_event: Optional[dict[str, Any]],
    profile: Optional[str],
    model: Optional[str],
    reasoning: Optional[str],
    duration_ms: Optional[int],
    finished_at: str,
    timeline_events: Optional[list[RunEvent]] = None,
) -> Optional[dict[str, Any]]:
    store = PmaTranscriptStore(hub_root)
    assistant_text = resolve_transcript_text(result)
    metadata = build_transcript_metadata(
        result=result,
        current=current,
        prompt_message=prompt_message,
        lifecycle_event=lifecycle_event,
        profile=profile,
        model=model,
        reasoning=reasoning,
        duration_ms=duration_ms,
        finished_at=finished_at,
    )
    try:
        timeline_event_count = 0
        if timeline_events:
            timeline_event_count = persist_timeline(
                hub_root=hub_root,
                execution_id=metadata["turn_id"],
                metadata=metadata,
                events=timeline_events,
            )
            if timeline_event_count:
                metadata["timeline_event_count"] = timeline_event_count
        pointer = store.write_transcript(
            turn_id=metadata["turn_id"],
            metadata=metadata,
            assistant_text=assistant_text,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        logger.exception("Failed to write PMA transcript")
        return None
    return {
        "turn_id": pointer.turn_id,
        "metadata_path": pointer.metadata_path,
        "content_path": pointer.content_path,
        "created_at": pointer.created_at,
    }


async def finalize_result(
    runtime: PmaRuntimeState,
    result: dict[str, Any],
    *,
    request: Any,
    store: Optional[PmaStateStore] = None,
    prompt_message: Optional[str] = None,
    lifecycle_event: Optional[dict[str, Any]] = None,
    profile: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    timeline_events: Optional[list[RunEvent]] = None,
) -> None:
    from datetime import datetime, timezone

    async with await runtime.get_pma_lock():
        current_snapshot = dict(runtime.pma_current or {})
        runtime.pma_last_result = format_last_result(result or {}, current_snapshot)
        runtime.pma_current = None
        runtime.pma_active = False
        runtime.pma_event = None
        runtime.pma_event_loop = None

    status = result.get("status") or "error"
    started_at = current_snapshot.get("started_at")
    duration_ms = None
    finished_at = now_iso()
    if started_at:
        try:
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            duration_ms = int(
                (datetime.now(timezone.utc) - start_dt).total_seconds() * 1000
            )
        except ValueError:
            logger.debug("Failed to compute PMA turn duration", exc_info=True)

    hub_root = get_pma_request_context(request).hub_root
    transcript_pointer = await persist_transcript(
        hub_root=hub_root,
        result=result,
        current=current_snapshot,
        prompt_message=prompt_message,
        lifecycle_event=lifecycle_event,
        profile=profile or _normalize_optional_text(current_snapshot.get("profile")),
        model=model,
        reasoning=reasoning,
        duration_ms=duration_ms,
        finished_at=finished_at,
        timeline_events=timeline_events,
    )
    if transcript_pointer is not None:
        runtime.pma_last_result = dict(runtime.pma_last_result or {})
        runtime.pma_last_result["transcript"] = transcript_pointer
        if not runtime.pma_last_result.get("turn_id"):
            runtime.pma_last_result["turn_id"] = transcript_pointer.get("turn_id")

    from .....core.logging_utils import log_event

    log_event(
        logger,
        logging.INFO,
        "pma.turn.completed",
        status=status,
        duration_ms=duration_ms,
        agent=current_snapshot.get("agent"),
        client_turn_id=current_snapshot.get("client_turn_id"),
        thread_id=runtime.pma_last_result.get("thread_id"),
        turn_id=runtime.pma_last_result.get("turn_id"),
        error=result.get("detail") if status == "error" else None,
    )

    safety_checker = runtime.get_safety_checker(hub_root, request)
    if status == "ok":
        action_type = PmaActionType.CHAT_COMPLETED
    elif status == "interrupted":
        action_type = PmaActionType.CHAT_INTERRUPTED
    else:
        action_type = PmaActionType.CHAT_FAILED

    safety_checker.record_action(
        action_type=action_type,
        agent=current_snapshot.get("agent"),
        thread_id=runtime.pma_last_result.get("thread_id"),
        turn_id=runtime.pma_last_result.get("turn_id"),
        client_turn_id=current_snapshot.get("client_turn_id"),
        details={"status": status, "duration_ms": duration_ms},
        status=status,
        error=result.get("detail") if status == "error" else None,
    )

    safety_checker.record_chat_result(
        agent=current_snapshot.get("agent") or "",
        status=status,
        error=result.get("detail") if status == "error" else None,
    )
    if lifecycle_event:
        safety_checker.record_reactive_result(
            status=status,
            error=result.get("detail") if status == "error" else None,
        )

    if store is not None:
        await runtime._persist_state(store)
