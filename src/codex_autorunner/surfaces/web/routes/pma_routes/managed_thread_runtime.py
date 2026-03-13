from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .....core.config import PMA_DEFAULT_MAX_TEXT_CHARS
from .....core.orchestration import MessageRequest
from .....core.orchestration.runtime_threads import (
    RuntimeThreadOutcome,
    await_runtime_thread_outcome,
    begin_runtime_thread_execution,
)
from .....core.pma_context import format_pma_discoverability_preamble
from .....core.pma_thread_store import (
    ManagedThreadAlreadyHasRunningTurnError,
    ManagedThreadNotActiveError,
    PmaThreadStore,
)
from .....core.pma_transcripts import PmaTranscriptStore
from ...schemas import PmaManagedThreadMessageRequest
from .automation_adapter import (
    call_store_create_with_payload,
    first_callable,
    get_automation_store,
    normalize_optional_text,
)
from .managed_threads import (
    build_managed_thread_orchestration_service as _shared_managed_thread_orchestration_service,
)

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

MANAGED_THREAD_PUBLIC_EXECUTION_ERROR = "Managed thread execution failed"
MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR = "Failed to interrupt backend turn"
PMA_TIMEOUT_SECONDS = 7200
PMA_MAX_TEXT = PMA_DEFAULT_MAX_TEXT_CHARS


def _build_managed_thread_orchestration_service(
    request: Request, *, thread_store: Optional[PmaThreadStore] = None
):
    _ = thread_store
    return _shared_managed_thread_orchestration_service(request)


def _truncate_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _compose_compacted_prompt(compact_seed: str, message: str) -> str:
    return (
        "Context summary (from compaction):\n"
        f"{compact_seed}\n\n"
        "User message:\n"
        f"{message}"
    )


def _sanitize_managed_thread_result_error(detail: Any) -> str:
    sanitized = normalize_optional_text(detail)
    if sanitized in {"PMA chat timed out", "PMA chat interrupted"}:
        return sanitized
    return MANAGED_THREAD_PUBLIC_EXECUTION_ERROR


async def notify_managed_thread_terminal_transition(
    request: Request,
    *,
    thread: dict[str, Any],
    managed_thread_id: str,
    managed_turn_id: str,
    to_state: str,
    reason: str,
) -> None:
    normalized_to_state = (to_state or "").strip().lower() or "failed"
    await _notify_hub_automation_transition(
        request,
        repo_id=normalize_optional_text(thread.get("repo_id")),
        run_id=None,
        thread_id=managed_thread_id,
        from_state="running",
        to_state=normalized_to_state,
        reason=reason,
        extra={
            "event_type": f"managed_thread_{normalized_to_state}",
            "transition_id": f"managed_turn:{managed_turn_id}:{normalized_to_state}",
            "idempotency_key": (
                f"managed_turn:{managed_turn_id}:{normalized_to_state}"
            ),
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "agent": normalize_optional_text(thread.get("agent")) or "",
        },
    )


async def _notify_hub_automation_transition(
    request: Request,
    *,
    repo_id: Optional[str] = None,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    from_state: str,
    to_state: str,
    reason: Optional[str] = None,
    timestamp: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    from .....core.time_utils import now_iso

    payload: dict[str, Any] = {
        "from_state": (from_state or "").strip(),
        "to_state": (to_state or "").strip(),
        "reason": normalize_optional_text(reason) or "",
        "timestamp": normalize_optional_text(timestamp) or now_iso(),
    }
    normalized_repo_id = normalize_optional_text(repo_id)
    normalized_run_id = normalize_optional_text(run_id)
    normalized_thread_id = normalize_optional_text(thread_id)
    if normalized_repo_id:
        payload["repo_id"] = normalized_repo_id
    if normalized_run_id:
        payload["run_id"] = normalized_run_id
    if normalized_thread_id:
        payload["thread_id"] = normalized_thread_id
    if isinstance(extra, dict):
        payload.update(extra)

    supervisor = getattr(request.app.state, "hub_supervisor", None)
    store = await get_automation_store(request, None)
    if store is None:
        return

    method = first_callable(
        store,
        (
            "notify_transition",
            "record_transition",
            "handle_transition",
            "on_transition",
            "process_transition",
        ),
    )
    if method is None:
        return

    async def await_if_needed(value: Any) -> Any:
        if asyncio.iscoroutine(value):
            return await value
        return value

    async def call_with_fallbacks(
        method: Any, attempts: list[tuple[tuple[Any, ...], dict[str, Any]]]
    ) -> Any:
        last_type_error: Optional[TypeError] = None
        for args, kwargs in attempts:
            try:
                return await await_if_needed(method(*args, **kwargs))
            except TypeError as exc:
                last_type_error = exc
                continue
        if last_type_error is not None:
            raise last_type_error
        raise RuntimeError("No automation method call attempts were provided")

    try:
        await call_with_fallbacks(
            method,
            [
                ((dict(payload),), {}),
                ((), {"payload": dict(payload)}),
                ((), dict(payload)),
            ],
        )
    except Exception:
        logger.exception("Failed to notify hub automation transition")
        return

    process_now = (
        getattr(supervisor, "process_pma_automation_now", None)
        if supervisor is not None
        else None
    )
    if not callable(process_now):
        return
    try:
        await await_if_needed(process_now(include_timers=False))
    except TypeError:
        try:
            await await_if_needed(process_now())
        except Exception:
            logger.exception("Failed immediate PMA automation processing")
    except Exception:
        logger.exception("Failed immediate PMA automation processing")


def _build_terminal_notify_subscription_payload(
    *,
    managed_thread_id: str,
    lane_id: Optional[str],
    notify_once: bool,
    idempotency_key: Optional[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_types": ["managed_thread_completed", "managed_thread_failed"],
        "thread_id": managed_thread_id,
        "lane_id": lane_id,
        "notify_once": notify_once,
        "metadata": {"notify_once": notify_once},
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    return payload


async def register_managed_thread_terminal_notify(
    request: Request,
    *,
    managed_thread_id: str,
    lane_id: Optional[str],
    notify_once: bool,
    idempotency_key: Optional[str],
) -> Optional[dict[str, Any]]:
    store = await get_automation_store(request, None)
    if store is None:
        return None
    created = await call_store_create_with_payload(
        store,
        (
            "create_subscription",
            "add_subscription",
            "upsert_subscription",
        ),
        _build_terminal_notify_subscription_payload(
            managed_thread_id=managed_thread_id,
            lane_id=lane_id,
            notify_once=notify_once,
            idempotency_key=idempotency_key,
        ),
    )
    if isinstance(created, dict) and "subscription" in created:
        return created
    return {"subscription": created}


def build_managed_thread_runtime_routes(
    router: APIRouter,
    get_runtime_state,
) -> None:
    """Build managed-thread runtime routes (send message, interrupt)."""

    def _get_pma_config(request: Request) -> dict[str, Any]:
        raw = getattr(request.app.state.config, "raw", {})
        return _pma_config_from_raw(raw)

    def _pma_config_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "enabled": True,
            "max_text_chars": PMA_DEFAULT_MAX_TEXT_CHARS,
        }
        if not isinstance(raw, dict):
            return defaults
        pma = raw.get("pma")
        if not isinstance(pma, dict):
            return defaults
        return {**defaults, **pma}

    @router.post("/threads/{managed_thread_id}/messages")
    async def send_managed_thread_message(
        managed_thread_id: str,
        request: Request,
        payload: PmaManagedThreadMessageRequest,
    ) -> Any:
        parsed = payload.model_dump(exclude_none=True)

        message = (parsed.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")

        defaults = _get_pma_config(request)
        max_text_chars = int(defaults.get("max_text_chars", 0) or 0)
        if max_text_chars > 0 and len(message) > max_text_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"message exceeds max_text_chars ({max_text_chars} characters)"
                ),
            )

        hub_root = request.app.state.config.root
        thread_store = PmaThreadStore(hub_root)
        transcripts = PmaTranscriptStore(hub_root)
        thread = thread_store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        notify_on = normalize_optional_text(parsed.get("notify_on"))
        if notify_on and notify_on != "terminal":
            raise HTTPException(
                status_code=400, detail="notify_on must be 'terminal' when provided"
            )
        notify_on = None if not notify_on else notify_on

        notify_lane = normalize_optional_text(parsed.get("notify_lane"))
        raw_notify_once = parsed.get("notify_once")
        if raw_notify_once is not None:
            notify_once = bool(raw_notify_once)
        defer_execution = bool(parsed.get("defer_execution"))

        if (thread.get("status") or "") == "archived":
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "rejected",
                    "reason": "thread_archived",
                    "detail": "Managed thread is archived and read-only",
                    "next_step": "Use `car pma thread resume` or spawn a new thread.",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": None,
                    "backend_thread_id": normalize_optional_text(
                        thread.get("backend_thread_id")
                    )
                    or "",
                    "assistant_text": "",
                    "error": "Managed thread is archived and read-only",
                },
            )
        model = normalize_optional_text(parsed.get("model")) or defaults.get("model")
        reasoning = normalize_optional_text(parsed.get("reasoning")) or defaults.get(
            "reasoning"
        )
        stored_backend_id = normalize_optional_text(thread.get("backend_thread_id"))
        compact_seed = normalize_optional_text(thread.get("compact_seed"))
        execution_message = message
        if not stored_backend_id and compact_seed:
            execution_message = _compose_compacted_prompt(compact_seed, message)
        execution_prompt = (
            f"{format_pma_discoverability_preamble(hub_root=hub_root)}"
            "<user_message>\n"
            f"{execution_message}\n"
            "</user_message>\n"
        )
        service = _build_managed_thread_orchestration_service(
            request,
            thread_store=thread_store,
        )
        try:
            started_execution = await begin_runtime_thread_execution(
                service,
                MessageRequest(
                    target_id=managed_thread_id,
                    target_kind="thread",
                    message_text=message,
                    model=model,
                    reasoning=reasoning,
                    approval_mode="on-request",
                    metadata={
                        "runtime_prompt": execution_prompt,
                        "execution_error_message": (
                            MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                        ),
                    },
                ),
                sandbox_policy="dangerFullAccess",
            )
        except ManagedThreadNotActiveError as exc:
            if exc.status == "archived":
                detail = "Managed thread is archived and read-only"
            else:
                detail = "Managed thread is not active"
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "rejected",
                    "reason": "thread_not_active",
                    "detail": detail,
                    "next_step": "Resume the thread or create a new active thread.",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": None,
                    "backend_thread_id": stored_backend_id or "",
                    "assistant_text": "",
                    "error": detail,
                },
            )
        except ManagedThreadAlreadyHasRunningTurnError:
            running_turn = thread_store.get_running_turn(managed_thread_id)
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "send_state": "already_in_flight",
                    "reason": "running_turn_exists",
                    "detail": (
                        f"Managed thread {managed_thread_id} already has a running turn"
                    ),
                    "next_step": (
                        "Wait for the running turn to finish, use --watch, "
                        "or subscribe with --notify-on terminal."
                    ),
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": str(
                        (running_turn or {}).get("managed_turn_id") or ""
                    )
                    or None,
                    "backend_thread_id": stored_backend_id or "",
                    "assistant_text": "",
                    "error": "Managed thread already has a running turn",
                },
            )
        except Exception:
            logger.exception(
                "Managed thread execution setup failed (managed_thread_id=%s)",
                managed_thread_id,
            )
            return {
                "status": "error",
                "send_state": "accepted",
                "execution_state": "completed",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": None,
                "backend_thread_id": stored_backend_id or "",
                "assistant_text": "",
                "error": MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
            }
        managed_turn_id = started_execution.execution.execution_id
        if not managed_turn_id:
            raise HTTPException(status_code=500, detail="Failed to create managed turn")
        backend_thread_id = (
            normalize_optional_text(started_execution.thread.backend_thread_id)
            or stored_backend_id
            or ""
        )
        if getattr(started_execution.execution, "status", "running") != "running":
            detail = _sanitize_managed_thread_result_error(
                started_execution.execution.error
            )
            await notify_managed_thread_terminal_transition(
                request,
                thread=thread,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                to_state="failed",
                reason=detail,
            )
            return {
                "status": "error",
                "send_state": "accepted",
                "execution_state": "completed",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": backend_thread_id or "",
                "assistant_text": "",
                "error": detail,
            }

        notification: Optional[dict[str, Any]] = None
        if notify_on == "terminal":
            notification = await register_managed_thread_terminal_notify(
                request,
                managed_thread_id=managed_thread_id,
                lane_id=notify_lane,
                notify_once=notify_once,
                idempotency_key=(
                    f"managed-thread-send-notify:{managed_turn_id}"
                    if notify_once
                    else None
                ),
            )

        preview = _truncate_text(message, 120)

        async def _run_turn() -> dict[str, Any]:
            try:
                outcome = await await_runtime_thread_outcome(
                    started_execution,
                    interrupt_event=None,
                    timeout_seconds=PMA_TIMEOUT_SECONDS,
                    execution_error_message=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                )
            except Exception:
                logger.exception(
                    "Managed thread execution raised unexpected error (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
                outcome = RuntimeThreadOutcome(
                    status="error",
                    assistant_text="",
                    error=MANAGED_THREAD_PUBLIC_EXECUTION_ERROR,
                    backend_thread_id=backend_thread_id,
                    backend_turn_id=started_execution.execution.backend_id,
                )

            finalized_thread = service.get_thread_target(managed_thread_id)
            resolved_backend_thread_id = (
                normalize_optional_text(
                    finalized_thread.backend_thread_id if finalized_thread else None
                )
                or outcome.backend_thread_id
                or backend_thread_id
            )
            if outcome.status == "ok":
                transcript_metadata = {
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": managed_turn_id,
                    "repo_id": thread.get("repo_id"),
                    "workspace_root": str(started_execution.workspace_root),
                    "agent": thread.get("agent"),
                    "backend_thread_id": resolved_backend_thread_id,
                    "backend_turn_id": outcome.backend_turn_id,
                    "model": model,
                    "reasoning": reasoning,
                    "status": "ok",
                }
                transcript_turn_id: Optional[str] = None
                try:
                    transcripts.write_transcript(
                        turn_id=managed_turn_id,
                        metadata=transcript_metadata,
                        assistant_text=outcome.assistant_text,
                    )
                    transcript_turn_id = managed_turn_id
                except Exception:
                    logger.exception(
                        "Failed to persist managed-thread transcript (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )

                try:
                    finalized_execution = service.record_execution_result(
                        managed_thread_id,
                        managed_turn_id,
                        status="ok",
                        assistant_text=outcome.assistant_text,
                        error=None,
                        backend_turn_id=outcome.backend_turn_id,
                        transcript_turn_id=transcript_turn_id,
                    )
                except KeyError:
                    finalized_execution = service.get_execution(
                        managed_thread_id, managed_turn_id
                    )
                finalized_status = str(
                    (finalized_execution.status if finalized_execution else "")
                ).strip()
                if finalized_status != "ok":
                    detail = MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                    response_status = "error"
                    if finalized_status == "interrupted":
                        detail = "PMA chat interrupted"
                        response_status = "interrupted"
                    elif (
                        finalized_status == "error" and finalized_execution is not None
                    ):
                        detail = _sanitize_managed_thread_result_error(
                            finalized_execution.error
                        )
                    await notify_managed_thread_terminal_transition(
                        request,
                        thread=thread,
                        managed_thread_id=managed_thread_id,
                        managed_turn_id=managed_turn_id,
                        to_state="failed",
                        reason=detail,
                    )
                    return {
                        "status": response_status,
                        "managed_thread_id": managed_thread_id,
                        "managed_turn_id": managed_turn_id,
                        "backend_thread_id": resolved_backend_thread_id or "",
                        "assistant_text": "",
                        "error": detail,
                    }
                thread_store.update_thread_after_turn(
                    managed_thread_id,
                    last_turn_id=managed_turn_id,
                    last_message_preview=preview,
                )
                await notify_managed_thread_terminal_transition(
                    request,
                    thread=thread,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    to_state="completed",
                    reason="managed_turn_completed",
                )
                return {
                    "status": "ok",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": managed_turn_id,
                    "backend_thread_id": resolved_backend_thread_id or "",
                    "assistant_text": outcome.assistant_text,
                    "error": None,
                }

            if outcome.status == "interrupted":
                try:
                    service.record_execution_interrupted(
                        managed_thread_id, managed_turn_id
                    )
                except KeyError:
                    pass
                detail = "PMA chat interrupted"
                await notify_managed_thread_terminal_transition(
                    request,
                    thread=thread,
                    managed_thread_id=managed_thread_id,
                    managed_turn_id=managed_turn_id,
                    to_state="failed",
                    reason=detail,
                )
                return {
                    "status": "interrupted",
                    "managed_thread_id": managed_thread_id,
                    "managed_turn_id": managed_turn_id,
                    "backend_thread_id": resolved_backend_thread_id or "",
                    "assistant_text": "",
                    "error": detail,
                }

            detail = _sanitize_managed_thread_result_error(outcome.error)
            try:
                service.record_execution_result(
                    managed_thread_id,
                    managed_turn_id,
                    status="error",
                    assistant_text="",
                    error=detail,
                    backend_turn_id=outcome.backend_turn_id,
                    transcript_turn_id=None,
                )
            except KeyError:
                pass
            await notify_managed_thread_terminal_transition(
                request,
                thread=thread,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                to_state="failed",
                reason=detail,
            )
            return {
                "status": "error",
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": resolved_backend_thread_id or "",
                "assistant_text": "",
                "error": detail,
            }

        accepted_payload: dict[str, Any] = {
            "status": "ok",
            "send_state": "accepted",
            "execution_state": "running",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "backend_thread_id": backend_thread_id or "",
            "assistant_text": "",
            "error": None,
        }
        if notification is not None:
            accepted_payload["notification"] = notification

        if defer_execution:
            task_pool = getattr(request.app.state, "pma_managed_thread_tasks", None)
            if not isinstance(task_pool, set):
                task_pool = set()
                request.app.state.pma_managed_thread_tasks = task_pool

            async def _background_run() -> None:
                try:
                    await _run_turn()
                except Exception:
                    logger.exception(
                        "Managed-thread background execution failed (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )
                    turn = thread_store.get_turn(managed_thread_id, managed_turn_id)
                    if (
                        str((turn or {}).get("status") or "").strip().lower()
                        == "running"
                    ):
                        detail = MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                        try:
                            service.record_execution_result(
                                managed_thread_id,
                                managed_turn_id,
                                status="error",
                                assistant_text="",
                                error=detail,
                                backend_turn_id=None,
                                transcript_turn_id=None,
                            )
                        except KeyError:
                            pass
                        await notify_managed_thread_terminal_transition(
                            request,
                            thread=thread,
                            managed_thread_id=managed_thread_id,
                            managed_turn_id=managed_turn_id,
                            to_state="failed",
                            reason=detail,
                        )

            task = asyncio.create_task(_background_run())
            task_pool.add(task)
            task.add_done_callback(lambda done: task_pool.discard(done))
            return accepted_payload

        response = await _run_turn()
        response["send_state"] = "accepted"
        response["execution_state"] = "completed"
        if notification is not None:
            response["notification"] = notification
        return response

    @router.post("/threads/{managed_thread_id}/interrupt")
    async def interrupt_managed_thread(
        managed_thread_id: str,
        request: Request,
    ) -> dict[str, Any]:
        from .....agents.registry import get_available_agents
        from .....core.orchestration.catalog import map_agent_capabilities

        hub_root = request.app.state.config.root
        store = PmaThreadStore(hub_root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

        agent = str(thread.get("agent") or "").strip().lower()
        if agent:
            available = get_available_agents(request.app.state)
            descriptor = available.get(agent)
            if descriptor is not None:
                capabilities = map_agent_capabilities(descriptor.capabilities)
                if "interrupt" not in capabilities:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Agent '{agent}' does not support interrupt (missing capability: interrupt)",
                    )

        running_turn = store.get_running_turn(managed_thread_id)
        if running_turn is None:
            raise HTTPException(
                status_code=409, detail="Managed thread has no running turn"
            )
        managed_turn_id = str(running_turn.get("managed_turn_id") or "")
        if not managed_turn_id:
            raise HTTPException(status_code=500, detail="Running turn is missing id")

        agent = str(thread.get("agent") or "").strip().lower()
        backend_thread_id = normalize_optional_text(thread.get("backend_thread_id"))
        backend_turn_id = normalize_optional_text(running_turn.get("backend_turn_id"))
        backend_error: Optional[str] = None
        backend_interrupt_attempted = True
        service = _build_managed_thread_orchestration_service(
            request,
            thread_store=store,
        )
        try:
            interrupted_execution = await service.interrupt_thread(managed_thread_id)
        except Exception:
            logger.exception(
                "Failed to interrupt managed-thread turn via orchestration service (managed_thread_id=%s, managed_turn_id=%s)",
                managed_thread_id,
                managed_turn_id,
            )
            interrupted_execution = service.get_execution(
                managed_thread_id,
                managed_turn_id,
            )
            if (
                interrupted_execution is None
                or interrupted_execution.status == "running"
            ):
                backend_error = MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR
                try:
                    interrupted_execution = service.record_execution_interrupted(
                        managed_thread_id,
                        managed_turn_id,
                    )
                except KeyError:
                    interrupted_execution = service.get_execution(
                        managed_thread_id,
                        managed_turn_id,
                    )

        interrupted = interrupted_execution is not None and (
            interrupted_execution.status == "interrupted"
        )
        if interrupted:
            await notify_managed_thread_terminal_transition(
                request,
                thread=thread,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                to_state="failed",
                reason=backend_error or "managed_turn_interrupted",
            )
            store.append_action(
                "managed_thread_interrupt",
                managed_thread_id=managed_thread_id,
                payload_json=json.dumps(
                    {
                        "agent": agent,
                        "managed_turn_id": managed_turn_id,
                        "backend_thread_id": backend_thread_id,
                        "backend_turn_id": backend_turn_id,
                        "backend_interrupt_attempted": backend_interrupt_attempted,
                        "backend_error": backend_error,
                    },
                    ensure_ascii=True,
                ),
            )
        updated_turn = store.get_turn(managed_thread_id, managed_turn_id)
        return {
            "status": "ok",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "turn": updated_turn,
            "backend_error": backend_error,
        }


__all__ = [
    "build_managed_thread_runtime_routes",
    "notify_managed_thread_terminal_transition",
    "register_managed_thread_terminal_notify",
]
