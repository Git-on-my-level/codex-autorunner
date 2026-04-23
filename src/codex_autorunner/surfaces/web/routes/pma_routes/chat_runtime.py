from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .....agents.base import (
    harness_allows_parallel_event_stream,
)
from .....agents.codex.harness import CodexHarness
from .....core.pma_context import build_hub_snapshot as build_hub_snapshot
from .....core.pma_context import format_pma_prompt as format_pma_prompt
from .....core.pma_context import load_pma_prompt as load_pma_prompt
from .....core.pma_lifecycle import PmaLifecycleRouter
from .....core.pma_queue import QueueItemState
from .....core.sse import format_sse
from .....core.text_utils import _normalize_optional_text
from .....integrations.github.context_injection import (
    maybe_inject_github_context as maybe_inject_github_context,
)
from ...schemas import (
    PmaChatRequest,
    PmaHistoryCompactRequest,
    PmaNewSessionRequest,
    PmaSessionResetRequest,
    PmaStopRequest,
    PmaThreadResetRequest,
)
from ...services.pma.common import (
    build_idempotency_key as service_build_idempotency_key,
)
from ...services.pma.common import pma_config_from_raw
from ..shared import SSE_HEADERS
from .chat_queue_execution import execute_queue_item
from .chat_runtime_execution import (
    DEFAULT_PMA_TIMEOUT_SECONDS,
    build_runtime_harness,
)
from .chat_session_management import (
    new_pma_session_response,
    resolve_preclear_hermes_fork_thread_id,
    serialize_lifecycle_result,
)
from .runtime_state import PmaRuntimeState
from .tail_stream import resolve_resume_after

logger = logging.getLogger(__name__)

PMA_TURN_IDLE_TIMEOUT_SECONDS = 1800


def _get_pma_config(request: Request) -> dict[str, Any]:
    raw = getattr(request.app.state.config, "raw", {})
    return pma_config_from_raw(raw)


def _pma_turn_idle_timeout_seconds(request: Request) -> float:
    overridden_timeout = globals().get(
        "PMA_TURN_IDLE_TIMEOUT_SECONDS",
        DEFAULT_PMA_TIMEOUT_SECONDS,
    )
    if overridden_timeout != DEFAULT_PMA_TIMEOUT_SECONDS:
        return float(overridden_timeout)
    configured_timeout = getattr(
        getattr(request.app.state.config, "pma", None),
        "turn_idle_timeout_seconds",
        None,
    )
    if configured_timeout is None:
        return float(DEFAULT_PMA_TIMEOUT_SECONDS)
    return float(configured_timeout)


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
            harness = build_runtime_harness(request, agent_id, profile)
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
                ):
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
        lambda item: execute_queue_item(
            runtime,
            item,
            _AppRequest(app),
            turn_timeout_seconds=_pma_turn_idle_timeout_seconds_from_app(app),
        ),
    )


def _pma_turn_idle_timeout_seconds_from_app(app: Any) -> float:
    overridden_timeout = globals().get(
        "PMA_TURN_IDLE_TIMEOUT_SECONDS",
        DEFAULT_PMA_TIMEOUT_SECONDS,
    )
    if overridden_timeout != DEFAULT_PMA_TIMEOUT_SECONDS:
        return float(overridden_timeout)
    config = getattr(app, "state", None)
    if config is None:
        return float(DEFAULT_PMA_TIMEOUT_SECONDS)
    configured_timeout = getattr(
        getattr(config, "config", None),
        "pma",
        None,
    )
    configured_timeout = getattr(configured_timeout, "turn_idle_timeout_seconds", None)
    if configured_timeout is None:
        return float(DEFAULT_PMA_TIMEOUT_SECONDS)
    return float(configured_timeout)


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
    async def pma_chat(request: Request, payload: PmaChatRequest):
        pma_config = _get_pma_config(request)
        message = (payload.message or "").strip()
        stream = bool(payload.stream)
        agent = _normalize_optional_text(payload.agent)
        profile = _normalize_optional_text(payload.profile)
        model = _normalize_optional_text(payload.model)
        reasoning = _normalize_optional_text(payload.reasoning)
        client_turn_id = (payload.client_turn_id or "").strip() or None

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

        queue_payload = {
            "message": message,
            "agent": agent,
            "profile": profile,
            "model": model,
            "reasoning": reasoning,
            "client_turn_id": client_turn_id,
            "stream": stream,
            "hub_root": str(hub_root),
        }

        item, dupe_reason = await queue.enqueue(lane_id, idempotency_key, queue_payload)
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
            lane_id,
            request,
            lambda item: execute_queue_item(
                runtime,
                item,
                request,
                turn_timeout_seconds=_pma_turn_idle_timeout_seconds(request),
            ),
        )

        try:
            result = await asyncio.wait_for(
                result_future,
                timeout=_pma_turn_idle_timeout_seconds(request),
            )
        except asyncio.TimeoutError:
            return {"status": "error", "detail": "PMA chat timed out"}
        except Exception:
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
    async def pma_stop(
        request: Request, payload: Optional[PmaStopRequest] = None
    ) -> dict[str, Any]:
        lane_id = ((payload.lane_id if payload else None) or "pma:default").strip()
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

        return serialize_lifecycle_result(result)

    @router.post("/new")
    async def new_pma_session(
        request: Request, payload: Optional[PmaNewSessionRequest] = None
    ) -> dict[str, Any]:
        agent = _normalize_optional_text(payload.agent if payload else None)
        profile = _normalize_optional_text(payload.profile if payload else None)
        runtime = get_runtime_state()
        current = await runtime.get_current_snapshot()
        preclear_thread_id = resolve_preclear_hermes_fork_thread_id(
            request,
            current=current,
            agent=agent,
            profile=profile,
        )
        return await new_pma_session_response(
            request,
            payload,
            current=current,
            preclear_thread_id=preclear_thread_id,
        )

    @router.post("/reset")
    async def reset_pma_session(
        request: Request, payload: Optional[PmaSessionResetRequest] = None
    ) -> dict[str, Any]:
        raw_agent = ((payload.agent if payload else None) or "").strip().lower()
        agent = raw_agent or None
        profile = _normalize_optional_text(payload.profile if payload else None)

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
    async def compact_pma_history(
        request: Request, payload: PmaHistoryCompactRequest
    ) -> dict[str, Any]:
        summary = (payload.summary or "").strip()
        agent = _normalize_optional_text(payload.agent)
        thread_id = _normalize_optional_text(payload.thread_id)

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
    async def reset_pma_thread(
        request: Request, payload: Optional[PmaThreadResetRequest] = None
    ) -> dict[str, Any]:
        raw_agent = ((payload.agent if payload else None) or "").strip().lower()
        agent = raw_agent or None
        profile = _normalize_optional_text(payload.profile if payload else None)

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
        harness = build_runtime_harness(request, agent_id, profile)
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
