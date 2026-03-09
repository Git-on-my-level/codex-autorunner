from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

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

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

MANAGED_THREAD_PUBLIC_EXECUTION_ERROR = "Managed thread execution failed"
MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR = "Failed to interrupt backend turn"
PMA_TIMEOUT_SECONDS = 7200
PMA_MAX_TEXT = 5000
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _truncate_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _is_within_root(path: Path, root: Path) -> bool:
    from .....core.state_roots import is_within_allowed_root

    return is_within_allowed_root(path, allowed_roots=[root], resolve=True)


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


def _normalize_workspace_root_input(workspace_root: str) -> Any:
    # Uses PurePosixPath from pathlib at top of file

    cleaned = (workspace_root or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    if "\\" in cleaned or "\x00" in cleaned or _DRIVE_PREFIX_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    normalized = PurePosixPath(cleaned)
    if ".." in normalized.parts:
        raise HTTPException(status_code=400, detail="workspace_root is invalid")
    return normalized


def _resolve_managed_thread_workspace(hub_root: Path, workspace_root: Any) -> Path:
    raw_workspace = normalize_optional_text(workspace_root)
    if raw_workspace is None:
        raise HTTPException(
            status_code=500, detail="Managed thread has invalid workspace_root"
        )
    resolved_workspace = Path(raw_workspace).absolute()
    if not _is_within_root(resolved_workspace, hub_root):
        raise HTTPException(
            status_code=400, detail="Managed thread workspace_root is invalid"
        )
    return resolved_workspace


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
        defaults: dict[str, Any] = {"enabled": True, "max_text_chars": 5000}
        if not isinstance(raw, dict):
            return defaults
        pma = raw.get("pma")
        if not isinstance(pma, dict):
            return defaults
        return {**defaults, **pma}

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
        from .....agents.codex.harness import CodexHarness

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
            except Exception:
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
        codex_harness = CodexHarness(supervisor, events)
        if on_meta is not None:
            try:
                maybe = on_meta(thread_id, handle.turn_id)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                logger.exception("pma meta callback failed")

        if interrupt_event.is_set():
            try:
                await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
            except Exception:
                logger.exception("Failed to interrupt Codex turn")
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

        turn_task = asyncio.create_task(handle.wait(timeout=None))
        timeout_task = asyncio.create_task(asyncio.sleep(PMA_TIMEOUT_SECONDS))
        interrupt_task = asyncio.create_task(interrupt_event.wait())

        runtime_state = get_runtime_state()

        try:
            done, _ = await asyncio.wait(
                {turn_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                try:
                    await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
                except Exception:
                    logger.exception("Failed to interrupt Codex turn")
                runtime_state.cancel_background_task(
                    turn_task, name="pma.app_server.turn.wait"
                )
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                try:
                    await codex_harness.interrupt(hub_root, thread_id, handle.turn_id)
                except Exception:
                    logger.exception("Failed to interrupt Codex turn")
                runtime_state.cancel_background_task(
                    turn_task, name="pma.app_server.turn.wait"
                )
                return {"status": "interrupted", "detail": "PMA chat interrupted"}
            turn_result = await turn_task
        finally:
            runtime_state.cancel_background_task(
                timeout_task, name="pma.app_server.timeout.wait"
            )
            runtime_state.cancel_background_task(
                interrupt_task, name="pma.app_server.interrupt.wait"
            )

        if getattr(turn_result, "errors", None):
            errors = turn_result.errors
            raise HTTPException(status_code=502, detail=errors[-1] if errors else "")

        output = "\n".join(getattr(turn_result, "agent_messages", []) or []).strip()
        raw_events = getattr(turn_result, "raw_events", []) or []
        return {
            "status": "ok",
            "message": output,
            "thread_id": thread_id,
            "backend_thread_id": thread_id,
            "turn_id": handle.turn_id,
            "raw_events": raw_events,
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
    ) -> dict[str, Any]:
        from .....agents.opencode.harness import OpenCodeHarness
        from .....agents.opencode.runtime import (
            PERMISSION_ALLOW,
            build_turn_id,
            collect_opencode_output,
            extract_session_id,
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
            except Exception:
                logger.exception("pma meta callback failed")

        opencode_harness = OpenCodeHarness(supervisor)
        if interrupt_event.is_set():
            await opencode_harness.interrupt(hub_root, session_id, None)
            return {"status": "interrupted", "detail": "PMA chat interrupted"}

        model_payload = split_model_id(model)
        await supervisor.mark_turn_started(hub_root)

        ready_event = asyncio.Event()
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
                stall_timeout_seconds=stall_timeout_seconds,
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

        runtime_state = get_runtime_state()

        try:
            prompt_response = None
            try:
                prompt_response = await prompt_task
            except Exception as exc:
                interrupt_event.set()
                runtime_state.cancel_background_task(
                    output_task, name="pma.opencode.output.collect"
                )
                await opencode_harness.interrupt(hub_root, session_id, None)
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            done, _ = await asyncio.wait(
                {output_task, timeout_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if timeout_task in done:
                runtime_state.cancel_background_task(
                    output_task, name="pma.opencode.output.collect"
                )
                await opencode_harness.interrupt(hub_root, session_id, None)
                return {"status": "error", "detail": "PMA chat timed out"}
            if interrupt_task in done:
                runtime_state.cancel_background_task(
                    output_task, name="pma.opencode.output.collect"
                )
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
            runtime_state.cancel_background_task(
                timeout_task, name="pma.opencode.timeout.wait"
            )
            runtime_state.cancel_background_task(
                interrupt_task, name="pma.opencode.interrupt.wait"
            )
            await supervisor.mark_turn_finished(hub_root)

        if output_result.error:
            raise HTTPException(status_code=502, detail=output_result.error)
        return {
            "status": "ok",
            "message": output_result.text,
            "thread_id": session_id,
            "backend_thread_id": session_id,
            "turn_id": build_turn_id(session_id),
        }

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
        known_backend_thread_id = stored_backend_id
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
        try:
            turn = thread_store.create_turn(
                managed_thread_id,
                prompt=message,
                model=model,
                reasoning=reasoning,
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
                    "backend_thread_id": known_backend_thread_id or "",
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
                    "backend_thread_id": known_backend_thread_id or "",
                    "assistant_text": "",
                    "error": "Managed thread already has a running turn",
                },
            )
        managed_turn_id = str(turn.get("managed_turn_id") or "")
        if not managed_turn_id:
            raise HTTPException(status_code=500, detail="Failed to create managed turn")

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
        workspace_root = _resolve_managed_thread_workspace(
            hub_root, thread.get("workspace_root")
        )
        agent = str(thread.get("agent") or "").strip().lower()
        interrupt_event = asyncio.Event()

        async def _finalize_error(
            detail: str, *, backend_turn_id: Optional[str] = None
        ) -> dict[str, Any]:
            thread_store.mark_turn_finished(
                managed_turn_id,
                status="error",
                assistant_text="",
                error=detail,
                backend_turn_id=backend_turn_id,
                transcript_turn_id=None,
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
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": known_backend_thread_id or "",
                "assistant_text": "",
                "error": detail,
            }

        async def _on_managed_turn_meta(
            backend_thread_id: Optional[str],
            backend_turn_id: Optional[str],
        ) -> None:
            nonlocal known_backend_thread_id
            resolved_backend_turn_id = normalize_optional_text(backend_turn_id)
            if resolved_backend_turn_id:
                thread_store.set_turn_backend_turn_id(
                    managed_turn_id, resolved_backend_turn_id
                )
            resolved_backend_thread_id = normalize_optional_text(backend_thread_id)
            if resolved_backend_thread_id != known_backend_thread_id:
                thread_store.set_thread_backend_id(
                    managed_thread_id, resolved_backend_thread_id
                )
                known_backend_thread_id = resolved_backend_thread_id

        async def _run_turn() -> dict[str, Any]:
            nonlocal known_backend_thread_id
            try:
                if agent == "opencode":
                    supervisor = getattr(request.app.state, "opencode_supervisor", None)
                    if supervisor is None:
                        return await _finalize_error("OpenCode unavailable")
                    stall_timeout_seconds = None
                    try:
                        stall_timeout_seconds = (
                            request.app.state.config.opencode.session_stall_timeout_seconds
                        )
                    except Exception:
                        stall_timeout_seconds = None
                    result = await _execute_opencode(
                        supervisor,
                        workspace_root,
                        execution_prompt,
                        interrupt_event,
                        model=model,
                        reasoning=reasoning,
                        backend_session_id=stored_backend_id,
                        stall_timeout_seconds=stall_timeout_seconds,
                        on_meta=_on_managed_turn_meta,
                    )
                elif agent == "codex":
                    supervisor = getattr(
                        request.app.state, "app_server_supervisor", None
                    )
                    events = getattr(request.app.state, "app_server_events", None)
                    if supervisor is None or events is None:
                        return await _finalize_error("App-server unavailable")
                    result = await _execute_app_server(
                        supervisor,
                        events,
                        workspace_root,
                        execution_prompt,
                        interrupt_event,
                        model=model,
                        reasoning=reasoning,
                        backend_thread_id=stored_backend_id,
                        on_meta=_on_managed_turn_meta,
                    )
                else:
                    return await _finalize_error(
                        f"Unknown managed thread agent: {agent}"
                    )
            except HTTPException:
                logger.exception(
                    "Managed thread execution failed (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
                return await _finalize_error(MANAGED_THREAD_PUBLIC_EXECUTION_ERROR)
            except Exception:
                logger.exception(
                    "Managed thread execution raised unexpected error (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
                return await _finalize_error(MANAGED_THREAD_PUBLIC_EXECUTION_ERROR)

            result = dict(result or {})
            if str(result.get("status") or "") != "ok":
                detail = _sanitize_managed_thread_result_error(result.get("detail"))
                backend_turn_id = normalize_optional_text(result.get("turn_id"))
                return await _finalize_error(detail, backend_turn_id=backend_turn_id)

            assistant_text = str(result.get("message") or "")
            backend_turn_id = normalize_optional_text(result.get("turn_id"))
            backend_thread_id = normalize_optional_text(
                result.get("backend_thread_id") or result.get("thread_id")
            )
            if backend_thread_id != known_backend_thread_id:
                thread_store.set_thread_backend_id(managed_thread_id, backend_thread_id)
                known_backend_thread_id = backend_thread_id

            transcript_metadata = {
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "repo_id": thread.get("repo_id"),
                "workspace_root": str(workspace_root),
                "agent": agent,
                "backend_thread_id": backend_thread_id,
                "backend_turn_id": backend_turn_id,
                "model": model,
                "reasoning": reasoning,
                "status": "ok",
            }
            transcript_turn_id: Optional[str] = None
            try:
                transcripts.write_transcript(
                    turn_id=managed_turn_id,
                    metadata=transcript_metadata,
                    assistant_text=assistant_text,
                )
                transcript_turn_id = managed_turn_id
            except Exception:
                logger.exception(
                    "Failed to persist managed-thread transcript (managed_thread_id=%s, managed_turn_id=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )

            thread_store.mark_turn_finished(
                managed_turn_id,
                status="ok",
                assistant_text=assistant_text,
                error=None,
                backend_turn_id=backend_turn_id,
                transcript_turn_id=transcript_turn_id,
            )
            finalized_turn = thread_store.get_turn(managed_thread_id, managed_turn_id)
            finalized_status = str((finalized_turn or {}).get("status") or "").strip()
            if finalized_status != "ok":
                detail = MANAGED_THREAD_PUBLIC_EXECUTION_ERROR
                response_status = "error"
                if finalized_status == "interrupted":
                    detail = "PMA chat interrupted"
                    response_status = "interrupted"
                elif finalized_status == "error":
                    detail = _sanitize_managed_thread_result_error(
                        (finalized_turn or {}).get("error")
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
                    "backend_thread_id": backend_thread_id or "",
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
                "backend_thread_id": backend_thread_id or "",
                "assistant_text": assistant_text,
                "error": None,
            }

        accepted_payload: dict[str, Any] = {
            "status": "ok",
            "send_state": "accepted",
            "execution_state": "running",
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "backend_thread_id": known_backend_thread_id or "",
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
                        thread_store.mark_turn_finished(
                            managed_turn_id,
                            status="error",
                            assistant_text="",
                            error=detail,
                            backend_turn_id=None,
                            transcript_turn_id=None,
                        )
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
        hub_root = request.app.state.config.root
        store = PmaThreadStore(hub_root)
        thread = store.get_thread(managed_thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail="Managed thread not found")

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
        backend_interrupt_attempted = False

        if agent == "codex":
            supervisor = getattr(request.app.state, "app_server_supervisor", None)
            if supervisor is None:
                backend_error = "App-server unavailable"
            elif not backend_thread_id or not backend_turn_id:
                backend_error = (
                    "Codex interrupt requires backend_thread_id and backend_turn_id"
                )
            else:
                backend_interrupt_attempted = True
                try:
                    client = await supervisor.get_client(hub_root)
                    await client.turn_interrupt(
                        backend_turn_id, thread_id=backend_thread_id
                    )
                except Exception:
                    logger.exception(
                        "Failed to interrupt Codex managed-thread turn (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )
                    backend_error = MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR
        elif agent == "opencode":
            supervisor = getattr(request.app.state, "opencode_supervisor", None)
            if supervisor is None:
                backend_error = "OpenCode unavailable"
            elif not backend_thread_id:
                backend_error = "OpenCode interrupt requires backend_thread_id"
            else:
                backend_interrupt_attempted = True
                try:
                    client = await supervisor.get_client(hub_root)
                    await client.abort(backend_thread_id)
                except Exception:
                    logger.exception(
                        "Failed to interrupt OpenCode managed-thread turn (managed_thread_id=%s, managed_turn_id=%s)",
                        managed_thread_id,
                        managed_turn_id,
                    )
                    backend_error = MANAGED_THREAD_PUBLIC_INTERRUPT_ERROR
        else:
            backend_error = f"Unknown managed thread agent: {agent}"

        interrupted = store.mark_turn_interrupted(managed_turn_id)
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
