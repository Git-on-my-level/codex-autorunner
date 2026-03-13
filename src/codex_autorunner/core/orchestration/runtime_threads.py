from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

from ..sse import parse_sse_lines
from .models import ExecutionRecord, MessageRequest, ThreadTarget
from .service import HarnessBackedOrchestrationService

RuntimeThreadOutcomeStatus = Literal["ok", "error", "interrupted"]


@dataclass(frozen=True)
class RuntimeThreadExecution:
    """Started runtime-thread execution bound to one concrete harness instance."""

    service: HarnessBackedOrchestrationService
    harness: Any
    thread: ThreadTarget
    execution: ExecutionRecord
    workspace_root: Path
    request: MessageRequest


@dataclass(frozen=True)
class RuntimeThreadOutcome:
    """Collected outcome of one runtime-thread execution before persistence."""

    status: RuntimeThreadOutcomeStatus
    assistant_text: str
    error: Optional[str]
    backend_thread_id: str
    backend_turn_id: Optional[str]


@dataclass
class _CollectedStreamState:
    output_chunks: list[str]
    completed_message: Optional[str] = None
    error: Optional[str] = None


def _normalize_message_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, dict):
                part_text = part.get("text")
                if isinstance(part_text, str):
                    parts.append(part_text)
        joined = "".join(parts).strip()
        return joined or None
    if isinstance(value, dict):
        for key in ("text", "message", "content"):
            nested_text = _normalize_message_text(value.get(key))
            if nested_text:
                return nested_text
        return None
    return None


def _extract_delta_text(params: dict[str, Any]) -> Optional[str]:
    for key in ("content", "delta", "text"):
        text = _normalize_message_text(params.get(key))
        if text:
            return text
    message = params.get("message")
    if isinstance(message, dict):
        return _extract_delta_text(message)
    item = params.get("item")
    if isinstance(item, dict):
        return _extract_delta_text(item)
    return None


def _extract_completed_text(params: dict[str, Any]) -> Optional[str]:
    item = params.get("item")
    if isinstance(item, dict):
        return _normalize_message_text(item.get("content")) or _normalize_message_text(
            item
        )
    result = params.get("result")
    if isinstance(result, dict):
        return _normalize_message_text(result)
    return _normalize_message_text(params)


def _extract_error_text(params: dict[str, Any]) -> Optional[str]:
    error = params.get("error")
    if isinstance(error, dict):
        return _normalize_message_text(error)
    return _normalize_message_text(params.get("message")) or _normalize_message_text(
        params.get("detail")
    )


def _unwrap_harness_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if isinstance(payload.get("message"), dict):
        message = payload["message"]
        method = message.get("method")
        params = message.get("params")
        if isinstance(method, str) and isinstance(params, dict):
            return method, params
    method = payload.get("method")
    params = payload.get("params")
    if isinstance(method, str) and isinstance(params, dict):
        return method, params
    return "", {}


def _update_stream_state_from_payload(
    state: _CollectedStreamState, payload: dict[str, Any]
) -> None:
    method, params = _unwrap_harness_payload(payload)
    method_lower = method.lower()

    if method in {"message.delta", "message.updated", "message.completed"}:
        text = _extract_delta_text(params) or _extract_completed_text(params)
        if text:
            if method == "message.delta":
                state.output_chunks.append(text)
            else:
                state.completed_message = text
        return

    if method == "item/agentMessage/delta" or method == "turn/streamDelta":
        text = _extract_delta_text(params)
        if text:
            state.output_chunks.append(text)
        return

    if "outputdelta" in method_lower:
        text = _extract_delta_text(params)
        if text:
            state.output_chunks.append(text)
        return

    if method == "item/completed":
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            text = _extract_completed_text(params)
            if text:
                state.completed_message = text
        return

    if method in {"turn/error", "error"}:
        error = _extract_error_text(params)
        if error:
            state.error = error
        return


async def begin_runtime_thread_execution(
    service: HarnessBackedOrchestrationService,
    request: MessageRequest,
    *,
    client_request_id: Optional[str] = None,
    sandbox_policy: Optional[Any] = None,
) -> RuntimeThreadExecution:
    """Start a runtime-backed thread execution via the orchestration service."""

    if request.target_kind != "thread":
        raise ValueError("Runtime thread execution only supports thread targets")
    thread = service.get_thread_target(request.target_id)
    if thread is None:
        raise KeyError(f"Unknown thread target '{request.target_id}'")
    if not thread.workspace_root:
        raise RuntimeError("Thread target is missing workspace_root")
    harness = service.harness_factory(thread.agent_id)
    execution = await service.send_message(
        request,
        client_request_id=client_request_id,
        sandbox_policy=sandbox_policy,
        harness=harness,
    )
    refreshed_thread = service.get_thread_target(request.target_id)
    if refreshed_thread is None:
        raise KeyError(f"Unknown thread target '{request.target_id}' after send")
    return RuntimeThreadExecution(
        service=service,
        harness=harness,
        thread=refreshed_thread,
        execution=execution,
        workspace_root=Path(refreshed_thread.workspace_root or thread.workspace_root),
        request=request,
    )


async def stream_runtime_thread_events(
    execution: RuntimeThreadExecution,
) -> AsyncIterator[str]:
    """Stream raw runtime events for an already-started execution."""

    backend_thread_id = execution.thread.backend_thread_id
    backend_turn_id = execution.execution.backend_id
    if not backend_thread_id or not backend_turn_id:
        raise RuntimeError("Runtime thread execution is missing backend ids")
    async for event in execution.harness.stream_events(
        execution.workspace_root,
        backend_thread_id,
        backend_turn_id,
    ):
        yield event


async def _collect_streamed_output(
    execution: RuntimeThreadExecution,
) -> tuple[str, Optional[str]]:
    state = _CollectedStreamState(output_chunks=[])

    async def _iter_lines(raw_event_text: str) -> AsyncIterator[str]:
        for line in raw_event_text.splitlines():
            yield line
        yield ""

    async for raw_event in stream_runtime_thread_events(execution):
        async for sse_event in parse_sse_lines(_iter_lines(str(raw_event))):
            try:
                payload = json.loads(sse_event.data) if sse_event.data else {}
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                _update_stream_state_from_payload(state, payload)
    assistant_text = (state.completed_message or "".join(state.output_chunks)).strip()
    return assistant_text, state.error


async def await_runtime_thread_outcome(
    execution: RuntimeThreadExecution,
    *,
    interrupt_event: Optional[asyncio.Event],
    timeout_seconds: float,
    execution_error_message: str,
) -> RuntimeThreadOutcome:
    """Wait for a started runtime-thread execution to reach a terminal outcome."""

    backend_thread_id = execution.thread.backend_thread_id or ""
    backend_turn_id = execution.execution.backend_id
    wait_for_turn = getattr(execution.harness, "wait_for_turn", None)

    if callable(wait_for_turn):
        collector_task = asyncio.create_task(
            wait_for_turn(
                execution.workspace_root,
                backend_thread_id,
                backend_turn_id,
                timeout=None,
            )
        )
    else:
        collector_task = asyncio.create_task(_collect_streamed_output(execution))
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
    interrupt_task = (
        asyncio.create_task(interrupt_event.wait())
        if interrupt_event is not None
        else None
    )

    try:
        wait_tasks = {collector_task, timeout_task}
        if interrupt_task is not None:
            wait_tasks.add(interrupt_task)
        done, _ = await asyncio.wait(
            wait_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_task in done:
            await execution.harness.interrupt(
                execution.workspace_root,
                backend_thread_id,
                backend_turn_id,
            )
            return RuntimeThreadOutcome(
                status="error",
                assistant_text="",
                error="PMA chat timed out",
                backend_thread_id=backend_thread_id,
                backend_turn_id=backend_turn_id,
            )
        if interrupt_task is not None and interrupt_task in done:
            await execution.harness.interrupt(
                execution.workspace_root,
                backend_thread_id,
                backend_turn_id,
            )
            return RuntimeThreadOutcome(
                status="interrupted",
                assistant_text="",
                error="PMA chat interrupted",
                backend_thread_id=backend_thread_id,
                backend_turn_id=backend_turn_id,
            )

        result = await collector_task
    except Exception:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=execution_error_message,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
        )
    finally:
        cleanup_tasks: list[asyncio.Task[Any]] = [timeout_task]
        if interrupt_task is not None:
            cleanup_tasks.append(interrupt_task)
        for task in cleanup_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    if callable(wait_for_turn):
        assistant_text = "\n".join(getattr(result, "agent_messages", []) or []).strip()
        errors = getattr(result, "errors", []) or []
        if errors:
            return RuntimeThreadOutcome(
                status="error",
                assistant_text="",
                error=execution_error_message,
                backend_thread_id=backend_thread_id,
                backend_turn_id=backend_turn_id,
            )
        return RuntimeThreadOutcome(
            status="ok",
            assistant_text=assistant_text,
            error=None,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
        )

    assistant_text, error = result
    if error:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=execution_error_message,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
        )
    return RuntimeThreadOutcome(
        status="ok",
        assistant_text=assistant_text,
        error=None,
        backend_thread_id=backend_thread_id,
        backend_turn_id=backend_turn_id,
    )


__all__ = [
    "RuntimeThreadExecution",
    "RuntimeThreadOutcome",
    "await_runtime_thread_outcome",
    "begin_runtime_thread_execution",
    "stream_runtime_thread_events",
]
