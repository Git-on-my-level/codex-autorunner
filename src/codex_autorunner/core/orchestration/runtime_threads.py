from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

from ..sse import format_sse
from .models import ExecutionRecord, MessageRequest, ThreadTarget
from .service import HarnessBackedOrchestrationService

RuntimeThreadOutcomeStatus = Literal["ok", "error", "interrupted"]
_INTERRUPT_POLL_INTERVAL_SECONDS = 0.05
RUNTIME_THREAD_TIMEOUT_ERROR = "Runtime thread timed out"
RUNTIME_THREAD_INTERRUPTED_ERROR = "Runtime thread interrupted"
RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR = (
    "Runtime thread execution is missing backend ids"
)
_SUCCESSFUL_COMPLETION_STATUSES = frozenset(
    {"ok", "completed", "complete", "done", "success"}
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
        if method in {"turn/completed", "prompt/completed", "session.idle"}:
            return True
    return False


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
    raw_events: tuple[Any, ...] = ()


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
    harness = service._harness_for_thread(thread)
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


async def begin_next_queued_runtime_thread_execution(
    service: HarnessBackedOrchestrationService,
    thread_target_id: str,
) -> Optional[RuntimeThreadExecution]:
    """Claim and start the next queued execution for a thread target."""

    claimed = service.claim_next_queued_execution_request(thread_target_id)
    if claimed is None:
        return None
    thread, execution, request, _client_request_id, sandbox_policy = claimed
    if not thread.workspace_root:
        raise RuntimeError("Thread target is missing workspace_root")
    harness = service._harness_for_thread(thread)
    refreshed = await service._start_execution(
        thread,
        request,
        execution,
        harness=harness,
        workspace_root=Path(thread.workspace_root),
        sandbox_policy=sandbox_policy,
    )
    refreshed_thread = service.get_thread_target(thread_target_id)
    if refreshed_thread is None:
        raise KeyError(f"Unknown thread target '{thread_target_id}' after queue start")
    return RuntimeThreadExecution(
        service=service,
        harness=harness,
        thread=refreshed_thread,
        execution=refreshed,
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
        if isinstance(event, dict):
            yield format_sse("app-server", event)
        else:
            yield format_sse("app-server", {"value": event})


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
    if not backend_thread_id or not backend_turn_id:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            raw_events=(),
        )
    collector_task = asyncio.create_task(
        execution.harness.wait_for_turn(
            execution.workspace_root,
            backend_thread_id,
            backend_turn_id,
            timeout=None,
        )
    )
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
    interrupt_task = (
        asyncio.create_task(_wait_for_interrupt(interrupt_event))
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
                error=RUNTIME_THREAD_TIMEOUT_ERROR,
                backend_thread_id=backend_thread_id,
                backend_turn_id=backend_turn_id,
                raw_events=(),
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
                error=RUNTIME_THREAD_INTERRUPTED_ERROR,
                backend_thread_id=backend_thread_id,
                backend_turn_id=backend_turn_id,
                raw_events=(),
            )

        result = await collector_task
    except Exception as exc:  # intentional: harness runtime errors are unpredictable
        detail = str(exc or "").strip()
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=detail or execution_error_message,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            raw_events=(),
        )
    finally:
        cleanup_tasks: list[asyncio.Task[Any]] = [timeout_task]
        if not collector_task.done():
            cleanup_tasks.append(collector_task)
        if interrupt_task is not None:
            cleanup_tasks.append(interrupt_task)
        for task in cleanup_tasks:
            task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    status = str(getattr(result, "status", "") or "").strip().lower()
    assistant_text = str(getattr(result, "assistant_text", "") or "")
    errors = tuple(getattr(result, "errors", ()) or ())
    raw_events = tuple(getattr(result, "raw_events", ()) or ())
    successful_completion = status in _SUCCESSFUL_COMPLETION_STATUSES
    if errors:
        detail = next(
            (str(error or "").strip() for error in errors if str(error or "").strip()),
            "",
        )
        # Some runtimes can emit a trailing transport error after a completed turn.
        # Only prefer the final text when the runtime explicitly reported success
        # and the raw event stream confirms completion was already observed.
        if (
            assistant_text.strip()
            and successful_completion
            and _raw_events_show_completion(raw_events)
        ):
            return RuntimeThreadOutcome(
                status="ok",
                assistant_text=assistant_text,
                error=None,
                backend_thread_id=backend_thread_id,
                backend_turn_id=backend_turn_id,
                raw_events=raw_events,
            )
        # OpenCode sets output.error while still returning assistant text; preserve text
        # for Telegram/Discord even when the harness reports a secondary error list.
        if assistant_text.strip():
            return RuntimeThreadOutcome(
                status="ok",
                assistant_text=assistant_text,
                error=detail or None,
                backend_thread_id=backend_thread_id,
                backend_turn_id=backend_turn_id,
                raw_events=raw_events,
            )
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=detail or execution_error_message,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            raw_events=raw_events,
        )
    if status in {"interrupted", "cancelled", "canceled", "aborted"}:
        return RuntimeThreadOutcome(
            status="interrupted",
            assistant_text="",
            error=RUNTIME_THREAD_INTERRUPTED_ERROR,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            raw_events=raw_events,
        )
    if status and not successful_completion:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=execution_error_message,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
            raw_events=raw_events,
        )
    return RuntimeThreadOutcome(
        status="ok",
        assistant_text=assistant_text,
        error=None,
        backend_thread_id=backend_thread_id,
        backend_turn_id=backend_turn_id,
        raw_events=raw_events,
    )


async def _wait_for_interrupt(interrupt_event: asyncio.Event) -> None:
    while not interrupt_event.is_set():
        await asyncio.sleep(_INTERRUPT_POLL_INTERVAL_SECONDS)


__all__ = [
    "RUNTIME_THREAD_INTERRUPTED_ERROR",
    "RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR",
    "RUNTIME_THREAD_TIMEOUT_ERROR",
    "RuntimeThreadExecution",
    "RuntimeThreadOutcome",
    "await_runtime_thread_outcome",
    "begin_next_queued_runtime_thread_execution",
    "begin_runtime_thread_execution",
    "stream_runtime_thread_events",
]
