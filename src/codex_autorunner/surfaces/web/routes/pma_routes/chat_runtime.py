from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .....agents.base import (
    harness_allows_parallel_event_stream,
    harness_progress_event_stream,
)
from .....agents.codex.harness import CodexHarness
from .....agents.opencode.harness import OpenCodeHarness
from .....agents.registry import (
    get_registered_agents,
    resolve_agent_runtime,
    wrap_requested_agent_context,
)
from .....core.orchestration import (
    FreshConversationRequiredError,
    SurfaceThreadMessageRequest,
    build_surface_orchestration_ingress,
)
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    merge_runtime_thread_raw_events,
    normalize_runtime_thread_message,
    normalize_runtime_thread_raw_event,
)
from .....core.orchestration.turn_timeline import (
    iso_from_epoch_millis,
    persist_turn_timeline,
)
from .....core.pma_audit import PmaActionType
from .....core.pma_context import (
    PMA_MAX_TEXT,
    build_hub_snapshot,
    format_pma_prompt,
    load_pma_prompt,
)
from .....core.pma_lifecycle import PmaLifecycleRouter
from .....core.pma_queue import QueueItemState
from .....core.pma_state import PmaStateStore
from .....core.pma_transcripts import PmaTranscriptStore
from .....core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    Completed,
    OutputDelta,
    RunEvent,
)
from .....core.sse import format_sse
from .....core.text_utils import _normalize_optional_text, _truncate_text
from .....core.time_utils import now_iso
from .....integrations.app_server import is_missing_thread_error
from .....integrations.app_server.threads import pma_base_key
from .....integrations.github.context_injection import maybe_inject_github_context
from ...services.pma.common import (
    build_idempotency_key as service_build_idempotency_key,
)
from ...services.pma.common import pma_config_from_raw
from ..agents import _available_agents
from ..shared import SSE_HEADERS
from .publish import publish_automation_result
from .runtime_state import PmaRuntimeState
from .tail_stream import resolve_resume_after

logger = logging.getLogger(__name__)

PMA_TIMEOUT_SECONDS = 7200
_SUCCESSFUL_COMPLETION_STATUSES = frozenset(
    {"ok", "completed", "complete", "done", "success"}
)


def _requires_fresh_pma_conversation(exc: Exception) -> bool:
    if isinstance(exc, FreshConversationRequiredError):
        return True
    return is_missing_thread_error(exc)


def _get_pma_config(request: Request) -> dict[str, Any]:
    raw = getattr(request.app.state.config, "raw", {})
    return pma_config_from_raw(raw)


async def _resolve_terminal_queue_item_result(
    queue: Any,
    *,
    lane_id: str,
    item_id: str,
) -> Optional[dict[str, Any]]:
    try:
        items = await queue.list_items(lane_id)
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
    ):
        logger.debug(
            "Failed to read PMA queue item state for late result delivery",
            exc_info=True,
        )
        return None

    for queued_item in items:
        if getattr(queued_item, "item_id", None) != item_id:
            continue
        state = getattr(queued_item, "state", None)
        if state == QueueItemState.COMPLETED:
            result = getattr(queued_item, "result", None)
            return dict(result) if isinstance(result, dict) else {"status": "ok"}
        if state == QueueItemState.FAILED:
            return {
                "status": "error",
                "detail": str(getattr(queued_item, "error", "") or "").strip()
                or "PMA chat failed",
            }
        if state == QueueItemState.CANCELLED:
            return {
                "status": "error",
                "detail": str(getattr(queued_item, "error", "") or "").strip()
                or "PMA chat cancelled",
            }
        break
    return None


async def _register_pma_result_future(
    runtime: PmaRuntimeState,
    queue: Any,
    *,
    lane_id: str,
    item_id: str,
) -> asyncio.Future[dict[str, Any]]:
    result_future = asyncio.get_running_loop().create_future()
    runtime.item_futures[item_id] = result_future
    late_result = await _resolve_terminal_queue_item_result(
        queue,
        lane_id=lane_id,
        item_id=item_id,
    )
    if late_result is not None and not result_future.done():
        result_future.set_result(late_result)
    return result_future


def _resolve_agent_profile(
    request: Request,
    agent_id: str,
    requested_profile: Optional[str],
    *,
    default_profile: Optional[str] = None,
) -> Optional[str]:
    config = getattr(request.app.state, "config", None)
    profile_getter = getattr(config, "agent_profiles", None)
    default_profile_getter = getattr(config, "agent_default_profile", None)
    available_profiles: dict[str, Any] = {}
    if callable(profile_getter):
        try:
            available_profiles = profile_getter(agent_id) or {}
        except (ValueError, TypeError, AttributeError):
            available_profiles = {}
    resolved_profile = _normalize_optional_text(requested_profile)
    if resolved_profile is not None:
        if agent_id == "hermes":
            hermes_valid = set(available_profiles.keys())
            try:
                from .....integrations.chat.agents import chat_hermes_profile_options

                hermes_valid |= {
                    opt.profile
                    for opt in chat_hermes_profile_options(request.app.state)
                }
            except (
                ImportError,
                AttributeError,
                RuntimeError,
            ):  # intentional: import and call of optional integration
                logger.debug("Failed to resolve hermes profile options", exc_info=True)
            if resolved_profile not in hermes_valid:
                raise HTTPException(status_code=400, detail="profile is invalid")
        elif resolved_profile not in available_profiles:
            raise HTTPException(status_code=400, detail="profile is invalid")
        return resolved_profile

    fallback_profiles: list[Optional[str]] = [
        _normalize_optional_text(default_profile),
    ]
    if callable(default_profile_getter):
        try:
            fallback_profiles.append(
                _normalize_optional_text(default_profile_getter(agent_id))
            )
        except (ValueError, TypeError, AttributeError):
            fallback_profiles.append(None)

    fallback_keys: set[str] = set(available_profiles.keys())
    if agent_id == "hermes":
        try:
            from .....integrations.chat.agents import chat_hermes_profile_options

            fallback_keys |= {
                opt.profile for opt in chat_hermes_profile_options(request.app.state)
            }
        except (
            ImportError,
            AttributeError,
            RuntimeError,
        ):  # intentional: import and call of optional integration
            logger.debug(
                "Failed to resolve hermes fallback profile options", exc_info=True
            )

    for fallback_profile in fallback_profiles:
        if fallback_profile is not None:
            if agent_id == "hermes":
                if fallback_profile in fallback_keys:
                    return fallback_profile
            elif fallback_profile in available_profiles:
                return fallback_profile

    return None


def _build_idempotency_key(
    *,
    lane_id: str,
    agent: Optional[str],
    profile: Optional[str],
    model: Optional[str],
    reasoning: Optional[str],
    client_turn_id: Optional[str],
    message: str,
) -> str:
    return service_build_idempotency_key(
        lane_id=lane_id,
        agent=agent,
        profile=profile,
        model=model,
        reasoning=reasoning,
        client_turn_id=client_turn_id,
        message=message,
    )


def _format_last_result(
    result: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    status = result.get("status") or "error"
    message = result.get("message")
    detail = result.get("detail")
    text = message if isinstance(message, str) and message else detail
    summary = _truncate_text(text or "", PMA_MAX_TEXT)
    payload = {
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


def _resolve_transcript_turn_id(result: dict[str, Any], current: dict[str, Any]) -> str:
    for candidate in (
        result.get("turn_id"),
        current.get("turn_id"),
        current.get("client_turn_id"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return f"local-{uuid.uuid4()}"


def _resolve_transcript_text(result: dict[str, Any]) -> str:
    message = result.get("message")
    if isinstance(message, str) and message.strip():
        return message
    detail = result.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail
    return ""


def _build_transcript_metadata(
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
        "turn_id": _resolve_transcript_turn_id(result, current),
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


def _persist_timeline(
    *,
    hub_root: Path,
    execution_id: str,
    metadata: dict[str, Any],
    events: list[RunEvent],
) -> int:
    return persist_turn_timeline(
        hub_root,
        execution_id=execution_id,
        target_kind="lane",
        target_id=str(metadata.get("lane_id") or "").strip() or "pma:default",
        repo_id=_normalize_optional_text(metadata.get("repo_id")),
        run_id=_normalize_optional_text(metadata.get("run_id")),
        metadata=metadata,
        events=events,
    )


async def _persist_transcript(
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
    assistant_text = _resolve_transcript_text(result)
    metadata = _build_transcript_metadata(
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
            timeline_event_count = _persist_timeline(
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
    except OSError:
        logger.exception("Failed to write PMA transcript")
        return None
    return {
        "turn_id": pointer.turn_id,
        "metadata_path": pointer.metadata_path,
        "content_path": pointer.content_path,
        "created_at": pointer.created_at,
    }


async def _finalize_result(
    runtime: PmaRuntimeState,
    result: dict[str, Any],
    *,
    request: Request,
    store: Optional[PmaStateStore] = None,
    prompt_message: Optional[str] = None,
    lifecycle_event: Optional[dict[str, Any]] = None,
    profile: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    timeline_events: Optional[list[RunEvent]] = None,
) -> None:
    async with await runtime.get_pma_lock():
        current_snapshot = dict(runtime.pma_current or {})
        runtime.pma_last_result = _format_last_result(result or {}, current_snapshot)
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

    hub_root = request.app.state.config.root
    transcript_pointer = await _persist_transcript(
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


async def _interrupt_active(
    runtime: PmaRuntimeState,
    request: Request,
    *,
    reason: str,
    source: str = "unknown",
) -> dict[str, Any]:
    event = await runtime.get_interrupt_event()
    event.set()
    current = await runtime.get_current_snapshot()
    agent_id = (current.get("agent") or "").strip().lower()
    profile = _normalize_optional_text(current.get("profile"))
    thread_id = current.get("thread_id")
    turn_id = current.get("turn_id")
    client_turn_id = current.get("client_turn_id")
    hub_root = request.app.state.config.root

    from .....core.logging_utils import log_event

    log_event(
        logger,
        logging.INFO,
        "pma.turn.interrupted",
        agent=agent_id or None,
        client_turn_id=client_turn_id or None,
        thread_id=thread_id,
        turn_id=turn_id,
        reason=reason,
        source=source,
    )

    if agent_id and thread_id:
        try:
            harness = _build_runtime_harness(request, agent_id, profile)
        except HTTPException:
            harness = None
        if harness is not None and callable(getattr(harness, "supports", None)):
            if harness.supports("interrupt"):
                try:
                    await harness.interrupt(hub_root, thread_id, turn_id)
                except (
                    RuntimeError,
                    OSError,
                    BrokenPipeError,
                    ProcessLookupError,
                    ConnectionResetError,
                ):  # intentional: best-effort interrupt
                    logger.exception("Failed to interrupt PMA turn")
    return {
        "status": "ok",
        "interrupted": bool(event.is_set()),
        "detail": reason,
        "agent": agent_id or None,
        "profile": profile,
        "thread_id": thread_id,
        "turn_id": turn_id,
    }


def _cancel_background_task(task: asyncio.Task[Any], *, name: str) -> None:
    def _on_done(done_task: asyncio.Future[Any]) -> None:
        if isinstance(done_task, asyncio.Task):
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception:  # intentional: top-level error handler
                logger.exception("PMA task failed: %s", name)

    if task.done():
        _on_done(task)
        return

    task.add_done_callback(_on_done)
    task.cancel()


def _timeline_has_assistant_output(events: list[RunEvent]) -> bool:
    return any(
        isinstance(event, OutputDelta)
        and event.delta_type
        in {
            RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
            RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        }
        for event in events
    )


def _raw_events_show_completion(raw_events: tuple[Any, ...]) -> bool:
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        method = str(raw_event.get("method") or "").strip().lower()
        if not method:
            message = raw_event.get("message")
            if isinstance(message, dict):
                method = str(message.get("method") or "").strip().lower()
        if method in {
            "turn/completed",
            "prompt/completed",
            "session.idle",
        }:
            return True
    return False


def _build_runtime_harness(
    request: Request, agent_id: str, profile: Optional[str] = None
) -> Any:
    try:
        descriptors = get_registered_agents(request.app.state)
    except TypeError as exc:
        if "positional argument" not in str(exc):
            raise
        descriptors = get_registered_agents()
    resolution = resolve_agent_runtime(agent_id, profile, context=request.app.state)
    effective_agent_id = resolution.runtime_agent_id
    effective_profile = resolution.runtime_profile
    descriptor = descriptors.get(effective_agent_id)
    if descriptor is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown agent: {effective_agent_id}"
        )
    try:
        return descriptor.make_harness(
            wrap_requested_agent_context(
                request.app.state,
                agent_id=effective_agent_id,
                profile=effective_profile,
            )
        )
    except (
        RuntimeError,
        TypeError,
        ValueError,
        AttributeError,
        OSError,
        ConnectionError,
    ) as exc:  # intentional: harness creation can fail in arbitrary ways
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _timeline_from_raw_events(
    raw_events: tuple[Any, ...],
) -> tuple[list[RunEvent], RuntimeThreadRunEventState]:
    state = RuntimeThreadRunEventState()
    timeline_events: list[RunEvent] = []
    for raw_event in raw_events:
        timeline_events.extend(
            await normalize_runtime_thread_raw_event(
                raw_event, state, timestamp=now_iso()
            )
        )
    return timeline_events, state


async def _execute_harness_turn(
    harness: Any,
    hub_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    on_meta: Optional[Any] = None,
) -> dict[str, Any]:
    await harness.ensure_ready(hub_root)

    conversation_id = backend_thread_id
    if conversation_id is None and thread_registry is not None and thread_key:
        conversation_id = thread_registry.get_thread_id(thread_key)
    had_existing_conversation = bool(conversation_id)

    if conversation_id:
        try:
            resumed = await harness.resume_conversation(hub_root, conversation_id)
        except (
            Exception
        ) as exc:  # intentional: resume may fail, fallback to new conversation
            if _requires_fresh_pma_conversation(exc) or isinstance(
                exc,
                (
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    ConnectionError,
                ),
            ):
                conversation_id = None
            else:
                raise
        else:
            resolved_conversation_id = getattr(resumed, "id", None)
            if isinstance(resolved_conversation_id, str) and resolved_conversation_id:
                conversation_id = resolved_conversation_id
                had_existing_conversation = True

    async def _create_fresh_conversation() -> str:
        conversation = await harness.new_conversation(hub_root, title="PMA")
        fresh_conversation_id = str(getattr(conversation, "id", "") or "").strip()
        if not fresh_conversation_id:
            raise HTTPException(
                status_code=502,
                detail="Runtime did not return a conversation id",
            )
        return fresh_conversation_id

    if not conversation_id:
        conversation_id = await _create_fresh_conversation()

    if thread_registry is not None and thread_key and conversation_id:
        thread_registry.set_thread_id(thread_key, conversation_id)

    try:
        turn = await harness.start_turn(
            hub_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode="on-request",
            sandbox_policy="dangerFullAccess",
        )
    except (
        Exception
    ) as exc:  # intentional: start_turn may fail, checks for retryable errors
        if not had_existing_conversation or not _requires_fresh_pma_conversation(exc):
            raise
        if thread_registry is not None and thread_key:
            try:
                thread_registry.reset_thread(thread_key)
            except OSError:
                logger.debug(
                    "Failed to clear stale PMA conversation binding for %s",
                    thread_key,
                    exc_info=True,
                )
        conversation_id = await _create_fresh_conversation()
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, conversation_id)
        turn = await harness.start_turn(
            hub_root,
            conversation_id,
            prompt,
            model,
            reasoning,
            approval_mode="on-request",
            sandbox_policy="dangerFullAccess",
        )
    resolved_conversation_id = str(
        getattr(turn, "conversation_id", None) or conversation_id or ""
    ).strip()
    if not resolved_conversation_id:
        raise HTTPException(
            status_code=502,
            detail="Runtime did not return a conversation id",
        )
    turn_id = str(getattr(turn, "turn_id", "") or "").strip()
    if not turn_id:
        raise HTTPException(status_code=502, detail="Runtime did not return a turn id")

    if thread_registry is not None and thread_key:
        thread_registry.set_thread_id(thread_key, resolved_conversation_id)

    if on_meta is not None:
        try:
            maybe = on_meta(resolved_conversation_id, turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
        ):  # intentional: user-provided callback
            logger.exception("pma meta callback failed")

    if interrupt_event.is_set():
        try:
            await harness.interrupt(hub_root, resolved_conversation_id, turn_id)
        except (
            RuntimeError,
            OSError,
            BrokenPipeError,
            ProcessLookupError,
            ConnectionResetError,
        ):  # intentional: best-effort interrupt
            logger.exception("Failed to interrupt PMA runtime turn")
        return {"status": "interrupted", "detail": "PMA chat interrupted"}

    streamed_raw_events: list[Any] = []
    stream_task: Optional[asyncio.Task[None]] = None
    if harness_allows_parallel_event_stream(harness):

        async def _collect_events() -> None:
            async for raw_event in harness_progress_event_stream(
                harness,
                hub_root,
                resolved_conversation_id,
                turn_id,
            ):
                streamed_raw_events.append(raw_event)

        stream_task = asyncio.create_task(_collect_events())

    turn_task = asyncio.create_task(
        harness.wait_for_turn(
            hub_root,
            resolved_conversation_id,
            turn_id,
            timeout=None,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        done, _ = await asyncio.wait(
            {turn_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            try:
                await harness.interrupt(hub_root, resolved_conversation_id, turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):  # intentional: best-effort interrupt
                logger.exception("Failed to interrupt PMA runtime turn")
            _cancel_background_task(turn_task, name="pma.runtime.turn.wait")
            return {"status": "error", "detail": "PMA chat timed out"}
        if interrupt_task in done:
            try:
                await harness.interrupt(hub_root, resolved_conversation_id, turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):  # intentional: best-effort interrupt
                logger.exception("Failed to interrupt PMA runtime turn")
            _cancel_background_task(turn_task, name="pma.runtime.turn.wait")
            return {"status": "interrupted", "detail": "PMA chat interrupted"}
        turn_result = await turn_task
    except Exception as exc:  # intentional: top-level error handler
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        _cancel_background_task(timeout_task, name="pma.runtime.timeout.wait")
        _cancel_background_task(interrupt_task, name="pma.runtime.interrupt.wait")
        if stream_task is not None:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass

    raw_events = tuple(getattr(turn_result, "raw_events", ()) or ())
    merged_raw_events = tuple(
        merge_runtime_thread_raw_events(streamed_raw_events, raw_events)
    )
    timeline_events, timeline_state = await _timeline_from_raw_events(merged_raw_events)

    assistant_text = str(getattr(turn_result, "assistant_text", "") or "").strip()
    if not assistant_text:
        assistant_text = timeline_state.best_assistant_text().strip()

    status = str(getattr(turn_result, "status", "") or "").strip().lower()
    errors = tuple(getattr(turn_result, "errors", ()) or ())
    successful_completion = status in _SUCCESSFUL_COMPLETION_STATUSES
    if errors:
        detail = next(
            (str(error or "").strip() for error in errors if str(error or "").strip()),
            "",
        )
        if (
            assistant_text
            and (successful_completion or timeline_state.completed_seen)
            and (
                timeline_state.completed_seen
                or _raw_events_show_completion(merged_raw_events)
            )
        ):
            timeline_events.append(
                Completed(timestamp=now_iso(), final_message=assistant_text)
            )
            return {
                "status": "ok",
                "message": assistant_text,
                "thread_id": resolved_conversation_id,
                "backend_thread_id": resolved_conversation_id,
                "turn_id": turn_id,
                "raw_events": list(merged_raw_events),
                "timeline_events": timeline_events,
            }
        return {"status": "error", "detail": detail or "PMA chat failed"}

    if status in {"interrupted", "cancelled", "canceled", "aborted"}:
        return {"status": "interrupted", "detail": "PMA chat interrupted"}
    if status and not successful_completion:
        return {"status": "error", "detail": "PMA chat failed"}

    if assistant_text:
        timeline_events.append(
            Completed(timestamp=now_iso(), final_message=assistant_text)
        )
    return {
        "status": "ok",
        "message": assistant_text,
        "thread_id": resolved_conversation_id,
        "backend_thread_id": resolved_conversation_id,
        "turn_id": turn_id,
        "raw_events": list(merged_raw_events),
        "timeline_events": timeline_events,
    }


async def _execute_app_server(
    supervisor: Any,
    events: Any,
    hub_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    backend_thread_id: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    on_meta: Optional[Any] = None,
) -> dict[str, Any]:
    client = await supervisor.get_client(hub_root)

    if backend_thread_id:
        thread_id = backend_thread_id
    elif thread_registry is not None and thread_key:
        thread_id = thread_registry.get_thread_id(thread_key)
    else:
        thread_id = None
    if thread_id:
        try:
            await client.thread_resume(thread_id)
        except (
            OSError,
            RuntimeError,
            ValueError,
        ):  # intentional: resume may fail, fallback to new thread
            thread_id = None

    if not thread_id:
        thread = await client.thread_start(str(hub_root))
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise HTTPException(
                status_code=502, detail="App-server did not return a thread id"
            )
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, thread_id)

    turn_kwargs: dict[str, Any] = {}
    if model:
        turn_kwargs["model"] = model
    if reasoning:
        turn_kwargs["effort"] = reasoning

    handle = await client.turn_start(
        thread_id,
        prompt,
        approval_policy="on-request",
        sandbox_policy="dangerFullAccess",
        **turn_kwargs,
    )
    register_turn = getattr(events, "register_turn", None)
    if callable(register_turn):
        try:
            await register_turn(thread_id, handle.turn_id)
        except (
            OSError,
            RuntimeError,
            ValueError,
        ):  # intentional: optional event registration
            logger.debug("pma chat register_turn failed", exc_info=True)
    codex_harness = CodexHarness(supervisor, events)
    if on_meta is not None:
        try:
            maybe = on_meta(thread_id, handle.turn_id)
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
        ):  # intentional: user-provided callback
            logger.exception("pma meta callback failed")

    if interrupt_event.is_set():
        try:
            await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
        except (
            RuntimeError,
            OSError,
            BrokenPipeError,
            ProcessLookupError,
            ConnectionResetError,
        ):  # intentional: best-effort interrupt
            logger.exception("Failed to interrupt Codex turn")
        return {"status": "interrupted", "detail": "PMA chat interrupted"}

    turn_task = asyncio.create_task(handle.wait(timeout=None))
    timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        done, _ = await asyncio.wait(
            {turn_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            try:
                await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):  # intentional: best-effort interrupt
                logger.exception("Failed to interrupt Codex turn")
            _cancel_background_task(turn_task, name="pma.app_server.turn.wait")
            return {"status": "error", "detail": "PMA chat timed out"}
        if interrupt_task in done:
            try:
                await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
            except (
                RuntimeError,
                OSError,
                BrokenPipeError,
                ProcessLookupError,
                ConnectionResetError,
            ):  # intentional: best-effort interrupt
                logger.exception("Failed to interrupt Codex turn")
            _cancel_background_task(turn_task, name="pma.app_server.turn.wait")
            return {"status": "interrupted", "detail": "PMA chat interrupted"}
        turn_result = await turn_task
    finally:
        _cancel_background_task(timeout_task, name="pma.app_server.timeout.wait")
        _cancel_background_task(interrupt_task, name="pma.app_server.interrupt.wait")

    if getattr(turn_result, "errors", None):
        errors = turn_result.errors
        raise HTTPException(status_code=502, detail=errors[-1] if errors else "")

    output = str(getattr(turn_result, "final_message", "") or "").strip()
    if not output:
        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
    raw_events = getattr(turn_result, "raw_events", []) or []
    timeline_events: list[RunEvent] = []
    list_events = getattr(events, "list_events", None)
    if callable(list_events):
        try:
            buffered = await list_events(thread_id, handle.turn_id)
        except (
            OSError,
            RuntimeError,
            ValueError,
        ):  # intentional: optional event listing
            buffered = []
        state = RuntimeThreadRunEventState()
        for entry in buffered:
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            params = message.get("params")
            timeline_events.extend(
                normalize_runtime_thread_message(
                    str(message.get("method") or ""),
                    params if isinstance(params, dict) else {},
                    state,
                    timestamp=iso_from_epoch_millis(entry.get("received_at")),
                )
            )
    if not timeline_events and raw_events:
        state = RuntimeThreadRunEventState()
        for raw_event in raw_events:
            timeline_events.extend(
                await normalize_runtime_thread_raw_event(raw_event, state)
            )
    timeline_events.append(Completed(timestamp=now_iso(), final_message=output))
    return {
        "status": "ok",
        "message": output,
        "thread_id": thread_id,
        "backend_thread_id": thread_id,
        "turn_id": handle.turn_id,
        "raw_events": raw_events,
        "timeline_events": timeline_events,
    }


async def _execute_opencode(
    supervisor: Any,
    hub_root: Path,
    prompt: str,
    interrupt_event: asyncio.Event,
    *,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    backend_session_id: Optional[str] = None,
    thread_registry: Optional[Any] = None,
    thread_key: Optional[str] = None,
    stall_timeout_seconds: Optional[float] = None,
    on_meta: Optional[Any] = None,
    part_handler: Optional[Any] = None,
) -> dict[str, Any]:
    from .....agents.opencode.runtime import (
        PERMISSION_ALLOW,
        build_turn_id,
        collect_opencode_output,
        extract_session_id,
        opencode_stream_timeouts,
        parse_message_response,
        split_model_id,
    )

    client = await supervisor.get_client(hub_root)
    session_id = backend_session_id
    if session_id is None and thread_registry is not None and thread_key:
        session_id = thread_registry.get_thread_id(thread_key)
    if not session_id:
        session = await client.create_session(directory=str(hub_root))
        session_id = extract_session_id(session, allow_fallback_id=True)
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(
                status_code=502, detail="OpenCode did not return a session id"
            )
        if thread_registry is not None and thread_key:
            thread_registry.set_thread_id(thread_key, session_id)
    if on_meta is not None:
        try:
            maybe = on_meta(session_id, build_turn_id(session_id))
            if asyncio.iscoroutine(maybe):
                await maybe
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
        ):  # intentional: user-provided callback
            logger.exception("pma meta callback failed")

    opencode_harness = OpenCodeHarness(supervisor)
    if interrupt_event.is_set():
        await opencode_harness.interrupt(hub_root, session_id, None)
        return {"status": "interrupted", "detail": "PMA chat interrupted"}

    model_payload = split_model_id(model)
    await supervisor.mark_turn_started(hub_root)

    ready_event = asyncio.Event()
    timeline_state = RuntimeThreadRunEventState()
    timeline_events: list[RunEvent] = []

    async def _timeline_part_handler(
        part_type: str,
        part: dict[str, Any],
        delta_text: Optional[str],
    ) -> None:
        params: dict[str, Any] = {"part": dict(part or {})}
        if delta_text:
            params["delta"] = {"text": delta_text}
        timeline_events.extend(
            normalize_runtime_thread_message(
                "message.part.updated",
                params,
                timeline_state,
                timestamp=now_iso(),
            )
        )
        if part_handler is not None:
            maybe = part_handler(part_type, part, delta_text)
            if asyncio.iscoroutine(maybe):
                await maybe

    stall_timeout, first_event_timeout = opencode_stream_timeouts(
        stall_timeout_seconds,
    )
    output_task = asyncio.create_task(
        collect_opencode_output(
            client,
            session_id=session_id,
            workspace_path=str(hub_root),
            model_payload=model_payload,
            permission_policy=PERMISSION_ALLOW,
            question_policy="auto_first_option",
            should_stop=interrupt_event.is_set,
            ready_event=ready_event,
            part_handler=_timeline_part_handler,
            stall_timeout_seconds=stall_timeout,
            first_event_timeout_seconds=first_event_timeout,
            logger=logger,
        )
    )
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass

    prompt_task = asyncio.create_task(
        client.prompt_async(
            session_id,
            message=prompt,
            model=model_payload,
            variant=reasoning,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
    interrupt_task = asyncio.create_task(interrupt_event.wait())
    try:
        prompt_response = None
        try:
            prompt_response = await prompt_task
        except (RuntimeError, OSError, ConnectionError) as exc:
            interrupt_event.set()
            _cancel_background_task(output_task, name="pma.opencode.output.collect")
            await opencode_harness.interrupt(hub_root, session_id, None)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        done, _ = await asyncio.wait(
            {output_task, timeout_task, interrupt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            _cancel_background_task(output_task, name="pma.opencode.output.collect")
            await opencode_harness.interrupt(hub_root, session_id, None)
            return {"status": "error", "detail": "PMA chat timed out"}
        if interrupt_task in done:
            _cancel_background_task(output_task, name="pma.opencode.output.collect")
            await opencode_harness.interrupt(hub_root, session_id, None)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}
        output_result = await output_task
        if (not output_result.text) and prompt_response is not None:
            fallback = parse_message_response(prompt_response)
            if fallback.text:
                output_result = type(output_result)(
                    text=fallback.text, error=fallback.error
                )
    finally:
        _cancel_background_task(timeout_task, name="pma.opencode.timeout.wait")
        _cancel_background_task(interrupt_task, name="pma.opencode.interrupt.wait")
        await supervisor.mark_turn_finished(hub_root)

    if output_result.error:
        raise HTTPException(status_code=502, detail=output_result.error)
    if (
        output_result.text
        and prompt_response is not None
        and not _timeline_has_assistant_output(timeline_events)
    ):
        completion_payload = (
            dict(prompt_response) if isinstance(prompt_response, dict) else {}
        )
        info = completion_payload.get("info")
        if not isinstance(info, dict):
            info = {}
        info = dict(info)
        if not isinstance(info.get("role"), str) or not str(info.get("role")).strip():
            info["role"] = "assistant"
        completion_payload["info"] = info
        if not isinstance(completion_payload.get("parts"), list):
            completion_payload["parts"] = [{"type": "text", "text": output_result.text}]
        timeline_events.extend(
            normalize_runtime_thread_message(
                "message.completed",
                completion_payload,
                timeline_state,
                timestamp=now_iso(),
            )
        )
    timeline_events.append(
        Completed(timestamp=now_iso(), final_message=output_result.text)
    )
    return {
        "status": "ok",
        "message": output_result.text,
        "thread_id": session_id,
        "backend_thread_id": session_id,
        "turn_id": build_turn_id(session_id),
        "timeline_events": timeline_events,
    }


async def _execute_queue_item(
    runtime: PmaRuntimeState,
    item: Any,
    request: Any,
) -> dict[str, Any]:
    hub_root = request.app.state.config.root
    payload = item.payload

    client_turn_id = payload.get("client_turn_id")
    message = payload.get("message", "")
    agent = payload.get("agent")
    profile = _normalize_optional_text(payload.get("profile"))
    model = _normalize_optional_text(payload.get("model"))
    reasoning = _normalize_optional_text(payload.get("reasoning"))
    lifecycle_event = payload.get("lifecycle_event")
    if not isinstance(lifecycle_event, dict):
        lifecycle_event = None
    wake_up = payload.get("wake_up")
    if not isinstance(wake_up, dict):
        wake_up = None
    automation_trigger = lifecycle_event is not None or wake_up is not None

    store = runtime.get_state_store(hub_root)
    defaults = _get_pma_config(request)
    started = False

    async def _finalize_queue_result_payload(
        result_payload: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        payload_result = dict(result_payload or {})
        timeline_events = (
            payload_result.get("timeline_events")
            if isinstance(payload_result.get("timeline_events"), list)
            else None
        )
        payload_result.pop("timeline_events", None)
        if automation_trigger:
            try:
                payload_result.update(
                    await publish_automation_result(
                        request=request,
                        result=payload_result,
                        client_turn_id=(
                            _normalize_optional_text(client_turn_id) or None
                        ),
                        lifecycle_event=lifecycle_event,
                        wake_up=wake_up,
                    )
                )
            except (
                RuntimeError,
                OSError,
                ConnectionError,
            ) as exc:  # intentional: publishing to external services
                logger.exception(
                    "Failed publishing PMA automation result: client_turn_id=%s",
                    client_turn_id,
                )
                payload_result["delivery_status"] = "failed"
                payload_result["delivery_outcome"] = {
                    "published": 0,
                    "duplicates": 0,
                    "failed": 1,
                    "targets": 0,
                    "repo_id": None,
                    "correlation_id": (
                        _normalize_optional_text(client_turn_id)
                        or f"pma-{uuid.uuid4().hex[:12]}"
                    ),
                    "errors": [str(exc)],
                }
        if persist and started:
            await _finalize_result(
                runtime,
                payload_result,
                request=request,
                store=store,
                prompt_message=message,
                lifecycle_event=lifecycle_event,
                profile=profile,
                model=model,
                reasoning=reasoning,
                timeline_events=timeline_events,
            )
        return payload_result

    def _resolve_default_agent(available_ids: set[str], available_default: str) -> str:
        configured_default = defaults.get("default_agent")
        from .....agents.registry import validate_agent_id

        try:
            candidate = validate_agent_id(configured_default or "", request.app.state)
        except ValueError:
            candidate = None
        if candidate and candidate in available_ids:
            return candidate
        return available_default

    from .....agents.registry import validate_agent_id

    agents, available_default = _available_agents(request)
    available_ids = {
        agent_id
        for entry in agents
        if isinstance(entry, dict)
        for agent_id in [entry.get("id")]
        if isinstance(agent_id, str)
    }

    try:
        agent_id = validate_agent_id(agent or "", request.app.state)
    except ValueError:
        agent_id = _resolve_default_agent(available_ids, available_default)

    profile = _resolve_agent_profile(
        request,
        agent_id,
        profile,
        default_profile=_normalize_optional_text(defaults.get("profile")),
    )

    safety_checker = runtime.get_safety_checker(hub_root, request)
    safety_check = safety_checker.check_chat_start(agent_id, message, client_turn_id)
    if not safety_check.allowed:
        detail = safety_check.reason or "PMA action blocked by safety check"
        if safety_check.details:
            detail = f"{detail}: {safety_check.details}"
        return await _finalize_queue_result_payload(
            {"status": "error", "detail": detail},
            persist=False,
        )

    started = await runtime.begin_turn(
        client_turn_id, store=store, lane_id=getattr(item, "lane_id", None)
    )
    if not started:
        detail = "Another PMA turn is already active; queue item was not started"
        logger.warning("PMA queue item rejected: %s", detail)
        return await _finalize_queue_result_payload(
            {
                "status": "error",
                "detail": detail,
                "client_turn_id": client_turn_id or "",
            },
            persist=False,
        )

    if not model and defaults.get("model"):
        model = defaults["model"]
    if not reasoning and defaults.get("reasoning"):
        reasoning = defaults["reasoning"]

    try:
        prompt_base = load_pma_prompt(hub_root)
        supervisor = getattr(request.app.state, "hub_supervisor", None)
        from .. import pma as pma_routes

        snapshot_builder = getattr(pma_routes, "build_hub_snapshot", build_hub_snapshot)
        github_context_injector = getattr(
            pma_routes,
            "maybe_inject_github_context",
            maybe_inject_github_context,
        )

        snapshot = await snapshot_builder(supervisor, hub_root=hub_root)
        prompt_state_key = pma_base_key(agent_id, profile)
        prompt = format_pma_prompt(
            prompt_base,
            snapshot,
            message,
            hub_root=hub_root,
            prompt_state_key=prompt_state_key,
        )
        prompt, _ = await github_context_injector(
            prompt_text=prompt,
            link_source_text=message,
            workspace_root=hub_root,
            logger=logger,
            event_prefix="web.pma.github_context",
            allow_cross_repo=True,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:  # intentional: top-level error handler for prompt construction
        error_result = {
            "status": "error",
            "detail": str(exc),
            "client_turn_id": client_turn_id or "",
        }
        return await _finalize_queue_result_payload(error_result)

    interrupt_event = await runtime.get_interrupt_event()
    if interrupt_event.is_set():
        result = {"status": "interrupted", "detail": "PMA chat interrupted"}
        return await _finalize_queue_result_payload(result)

    async def _meta(thread_id: str, turn_id: str) -> None:
        await runtime.update_current(
            store=store,
            client_turn_id=client_turn_id or "",
            status="running",
            agent=agent_id,
            profile=profile,
            thread_id=thread_id,
            turn_id=turn_id,
        )

        safety_checker.record_action(
            action_type=PmaActionType.CHAT_STARTED,
            agent=agent_id,
            thread_id=thread_id,
            turn_id=turn_id,
            client_turn_id=client_turn_id,
            details={"message": message[:200]},
        )

        from .....core.logging_utils import log_event

        log_event(
            logger,
            logging.INFO,
            "pma.turn.started",
            agent=agent_id,
            client_turn_id=client_turn_id or None,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    registry = getattr(request.app.state, "app_server_threads", None)

    ingress = build_surface_orchestration_ingress(
        event_sink=lambda orchestration_event: logger.info(
            "web.pma.%s surface=%s target_kind=%s target_id=%s status=%s meta=%s",
            orchestration_event.event_type,
            orchestration_event.surface_kind,
            orchestration_event.target_kind,
            orchestration_event.target_id,
            orchestration_event.status,
            orchestration_event.metadata,
        )
    )

    async def _resolve_no_flow(
        _request: SurfaceThreadMessageRequest,
    ) -> None:
        return None

    async def _submit_flow_reply(
        _request: SurfaceThreadMessageRequest, _flow_target: Any
    ) -> dict[str, Any]:
        raise RuntimeError("PMA web ingress does not route ticket_flow replies")

    async def _submit_thread_message(
        _request: SurfaceThreadMessageRequest,
    ) -> dict[str, Any]:
        try:
            harness = _build_runtime_harness(request, agent_id, profile)
        except HTTPException as exc:
            return {"status": "error", "detail": str(exc.detail)}
        if not callable(getattr(harness, "supports", None)):
            return {"status": "error", "detail": "Runtime harness unavailable"}
        if not harness.supports("durable_threads") or not harness.supports(
            "message_turns"
        ):
            return {
                "status": "error",
                "detail": f"Agent '{agent_id}' does not support PMA message turns",
            }
        return await _execute_harness_turn(
            harness,
            hub_root,
            prompt,
            interrupt_event,
            model=model,
            reasoning=reasoning,
            thread_registry=registry,
            thread_key=pma_base_key(agent_id, profile),
            on_meta=_meta,
        )

    try:
        ingress_result = await ingress.submit_message(
            SurfaceThreadMessageRequest(
                surface_kind="web",
                workspace_root=hub_root,
                prompt_text=message,
                agent_id=agent_id,
                pma_enabled=True,
                metadata={
                    "client_turn_id": client_turn_id or "",
                    "profile": profile or "",
                },
            ),
            resolve_paused_flow_target=_resolve_no_flow,
            submit_flow_reply=_submit_flow_reply,
            submit_thread_message=_submit_thread_message,
        )
        result = dict(ingress_result.thread_result or {})
    except (
        Exception
    ) as exc:  # intentional: top-level error handler for orchestration ingress
        error_result = {
            "status": "error",
            "detail": str(exc),
            "client_turn_id": client_turn_id or "",
        }
        await _finalize_queue_result_payload(error_result)
        raise

    result = dict(result or {})
    result["client_turn_id"] = client_turn_id or ""
    return await _finalize_queue_result_payload(result)


def _require_pma_enabled(request: Request) -> None:
    pma_config = _get_pma_config(request)
    if not pma_config.get("enabled", True):
        raise HTTPException(status_code=404, detail="PMA is disabled")


class _AppRequest:
    def __init__(self, app: Any) -> None:
        self.app = app


async def _ensure_lane_worker_for_app(
    runtime: PmaRuntimeState, app: Any, lane_id: str
) -> None:
    await runtime.ensure_lane_worker(
        lane_id,
        _AppRequest(app),
        lambda item: _execute_queue_item(runtime, item, _AppRequest(app)),
    )


async def _stop_lane_worker_for_app(
    runtime: PmaRuntimeState, app: Any, lane_id: str
) -> None:
    _ = app
    await runtime.stop_lane_worker(lane_id)


async def _stop_all_lane_workers_for_app(runtime: PmaRuntimeState, app: Any) -> None:
    _ = app
    await runtime.stop_all_lane_workers()


def build_chat_runtime_router(
    router: APIRouter,
    get_runtime_state: Any,
) -> None:
    """Build PMA chat runtime routes.

    This includes:
    - /active - Get current PMA status
    - /chat - Submit a PMA chat message
    - /interrupt - Interrupt running PMA turn
    - /stop - Stop a PMA lane
    - /new - Create new PMA session
    - /reset - Reset PMA state
    - /compact - Compact PMA history
    - /thread/reset - Reset PMA thread
    - /queue - Get queue summary
    - /queue/{lane_id} - Get lane queue items
    - /turns/{turn_id}/events - Stream turn events
    """

    @router.get("/active")
    async def pma_active_status(
        request: Request, client_turn_id: Optional[str] = None
    ) -> dict[str, Any]:
        runtime = get_runtime_state()
        async with await runtime.get_pma_lock():
            current = dict(runtime.pma_current or {})
            last_result = dict(runtime.pma_last_result or {})
            active = bool(runtime.pma_active)
        store = runtime.get_state_store(request.app.state.config.root)
        disk_state = store.load(ensure_exists=True)
        if isinstance(disk_state, dict):
            disk_current = (
                disk_state.get("current")
                if isinstance(disk_state.get("current"), dict)
                else {}
            )
            disk_last = (
                disk_state.get("last_result")
                if isinstance(disk_state.get("last_result"), dict)
                else {}
            )
            if not current and disk_current:
                current = dict(disk_current)
            if not last_result and disk_last:
                last_result = dict(disk_last)
            if not active and disk_state.get("active"):
                active = True
        if client_turn_id:
            if last_result.get("client_turn_id") != client_turn_id:
                last_result = {}
            if current.get("client_turn_id") != client_turn_id:
                current = {}
        return {"active": active, "current": current, "last_result": last_result}

    @router.post("/chat")
    async def pma_chat(request: Request):
        pma_config = _get_pma_config(request)
        body = await request.json()
        message = (body.get("message") or "").strip()
        stream = bool(body.get("stream", False))
        agent = _normalize_optional_text(body.get("agent"))
        profile = _normalize_optional_text(body.get("profile"))
        model = _normalize_optional_text(body.get("model"))
        reasoning = _normalize_optional_text(body.get("reasoning"))
        client_turn_id = (body.get("client_turn_id") or "").strip() or None

        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        max_text_chars = int(pma_config.get("max_text_chars", 0) or 0)
        if max_text_chars > 0 and len(message) > max_text_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"message exceeds max_text_chars ({max_text_chars} characters)"
                ),
            )

        runtime = get_runtime_state()
        hub_root = request.app.state.config.root
        queue = runtime.get_pma_queue(hub_root)

        lane_id = "pma:default"
        idempotency_key = _build_idempotency_key(
            lane_id=lane_id,
            agent=agent,
            profile=profile,
            model=model,
            reasoning=reasoning,
            client_turn_id=client_turn_id,
            message=message,
        )

        payload = {
            "message": message,
            "agent": agent,
            "profile": profile,
            "model": model,
            "reasoning": reasoning,
            "client_turn_id": client_turn_id,
            "stream": stream,
            "hub_root": str(hub_root),
        }

        item, dupe_reason = await queue.enqueue(lane_id, idempotency_key, payload)
        if dupe_reason:
            logger.info("Duplicate PMA turn: %s", dupe_reason)

        if item.state == QueueItemState.DEDUPED:
            return {
                "status": "ok",
                "message": "Duplicate request - already processing",
                "deduped": True,
            }

        result_future = await _register_pma_result_future(
            runtime, queue, lane_id=lane_id, item_id=item.item_id
        )
        await runtime.ensure_lane_worker(
            lane_id, request, lambda item: _execute_queue_item(runtime, item, request)
        )

        try:
            result = await asyncio.wait_for(result_future, timeout=PMA_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"status": "error", "detail": "PMA chat timed out"}
        except Exception:  # intentional: top-level error handler
            logger.exception("PMA chat error")
            return {
                "status": "error",
                "detail": "An error occurred processing your request",
            }
        finally:
            runtime.item_futures.pop(item.item_id, None)

        return result

    @router.post("/interrupt")
    async def pma_interrupt(request: Request) -> dict[str, Any]:
        runtime = get_runtime_state()
        return await _interrupt_active(
            runtime, request, reason="PMA chat interrupted", source="user_request"
        )

    @router.post("/stop")
    async def pma_stop(request: Request) -> dict[str, Any]:
        body = await request.json() if request.headers.get("content-type") else {}
        lane_id = (body.get("lane_id") or "pma:default").strip()
        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        runtime = get_runtime_state()
        result = await lifecycle_router.stop(lane_id=lane_id)

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        await runtime.stop_lane_worker(lane_id)

        await _interrupt_active(
            runtime, request, reason="Lane stopped", source="user_request"
        )

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/new")
    async def new_pma_session(request: Request) -> dict[str, Any]:
        body = await request.json()
        agent = _normalize_optional_text(body.get("agent"))
        profile = _normalize_optional_text(body.get("profile"))
        lane_id = (body.get("lane_id") or "pma:default").strip()

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.new(
            agent=agent,
            profile=profile,
            lane_id=lane_id,
        )

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/reset")
    async def reset_pma_session(request: Request) -> dict[str, Any]:
        body = await request.json() if request.headers.get("content-type") else {}
        raw_agent = (body.get("agent") or "").strip().lower()
        agent = raw_agent or None
        profile = _normalize_optional_text(body.get("profile"))

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.reset(agent=agent, profile=profile)

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/compact")
    async def compact_pma_history(request: Request) -> dict[str, Any]:
        body = await request.json()
        summary = (body.get("summary") or "").strip()
        agent = _normalize_optional_text(body.get("agent"))
        thread_id = _normalize_optional_text(body.get("thread_id"))

        if not summary:
            raise HTTPException(status_code=400, detail="summary is required")

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.compact(
            summary=summary, agent=agent, thread_id=thread_id
        )

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "message": result.message,
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
            "details": result.details,
        }

    @router.post("/thread/reset")
    async def reset_pma_thread(request: Request) -> dict[str, Any]:
        body = await request.json()
        raw_agent = (body.get("agent") or "").strip().lower()
        agent = raw_agent or None
        profile = _normalize_optional_text(body.get("profile"))

        hub_root = request.app.state.config.root
        lifecycle_router = PmaLifecycleRouter(hub_root)

        result = await lifecycle_router.reset(agent=agent, profile=profile)

        if result.status != "ok":
            raise HTTPException(status_code=500, detail=result.error)

        return {
            "status": result.status,
            "cleared": result.details.get("cleared_threads", []),
            "artifact_path": (
                str(result.artifact_path) if result.artifact_path else None
            ),
        }

    @router.get("/queue")
    async def pma_queue_status(request: Request) -> dict[str, Any]:
        runtime = get_runtime_state()
        queue = runtime.get_pma_queue(request.app.state.config.root)
        summary = await queue.get_queue_summary()
        return cast(dict[str, Any], summary)

    @router.get("/queue/{lane_id:path}")
    async def pma_lane_queue_status(request: Request, lane_id: str) -> dict[str, Any]:
        runtime = get_runtime_state()
        queue = runtime.get_pma_queue(request.app.state.config.root)
        items = await queue.list_items(lane_id)
        return {
            "lane_id": lane_id,
            "items": [
                {
                    "item_id": item.item_id,
                    "state": item.state.value,
                    "enqueued_at": item.enqueued_at,
                    "started_at": item.started_at,
                    "finished_at": item.finished_at,
                    "error": item.error,
                    "dedupe_reason": item.dedupe_reason,
                }
                for item in items
            ],
        }

    @router.get("/turns/{turn_id}/events")
    async def stream_pma_turn_events(
        turn_id: str,
        request: Request,
        thread_id: str,
        agent: str = "codex",
        profile: Optional[str] = None,
        since_event_id: Optional[int] = None,
    ):
        agent_id = (agent or "").strip().lower()
        profile = _normalize_optional_text(profile)
        resume_after = resolve_resume_after(request, since_event_id)
        if not thread_id:
            raise HTTPException(status_code=400, detail="thread_id is required")
        harness = _build_runtime_harness(request, agent_id, profile)
        events = getattr(request.app.state, "app_server_events", None)
        if isinstance(harness, CodexHarness) and events is not None:
            return StreamingResponse(
                events.stream(thread_id, turn_id, after_id=(resume_after or 0)),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )
        if not harness_allows_parallel_event_stream(harness):
            raise HTTPException(
                status_code=409,
                detail="Live turn events unavailable for this agent",
            )

        async def _stream_events() -> Any:
            async for raw_event in harness.stream_events(
                request.app.state.config.root, thread_id, turn_id
            ):
                payload = (
                    raw_event if isinstance(raw_event, dict) else {"value": raw_event}
                )
                yield format_sse("event", {"message": payload})

        return StreamingResponse(
            _stream_events(),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )
