from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Optional

from .models import ExecutionRecord, MessageRequest, ThreadTarget
from .service import HarnessBackedOrchestrationService

RuntimeThreadOutcomeStatus = Literal["ok", "error", "interrupted"]
_INTERRUPT_POLL_INTERVAL_SECONDS = 0.05
RUNTIME_THREAD_TIMEOUT_ERROR = "Runtime thread timed out"
RUNTIME_THREAD_INTERRUPTED_ERROR = "Runtime thread interrupted"


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
    harness = service.harness_factory(thread.agent_id)
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
        yield event


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
        if not collector_task.done():
            cleanup_tasks.append(collector_task)
        if interrupt_task is not None:
            cleanup_tasks.append(interrupt_task)
        for task in cleanup_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    status = (result.status or "").strip().lower()
    if result.errors:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=execution_error_message,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
        )
    if status in {"interrupted", "cancelled", "canceled", "aborted"}:
        return RuntimeThreadOutcome(
            status="interrupted",
            assistant_text="",
            error=RUNTIME_THREAD_INTERRUPTED_ERROR,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
        )
    if status and status not in {"ok", "completed", "complete", "done", "success"}:
        return RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=execution_error_message,
            backend_thread_id=backend_thread_id,
            backend_turn_id=backend_turn_id,
        )
    return RuntimeThreadOutcome(
        status="ok",
        assistant_text=result.assistant_text,
        error=None,
        backend_thread_id=backend_thread_id,
        backend_turn_id=backend_turn_id,
    )


async def _wait_for_interrupt(interrupt_event: asyncio.Event) -> None:
    while not interrupt_event.is_set():
        await asyncio.sleep(_INTERRUPT_POLL_INTERVAL_SECONDS)


__all__ = [
    "RUNTIME_THREAD_INTERRUPTED_ERROR",
    "RUNTIME_THREAD_TIMEOUT_ERROR",
    "RuntimeThreadExecution",
    "RuntimeThreadOutcome",
    "await_runtime_thread_outcome",
    "begin_next_queued_runtime_thread_execution",
    "begin_runtime_thread_execution",
    "stream_runtime_thread_events",
]
