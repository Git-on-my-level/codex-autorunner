from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from ...harness_capabilities import harness_supports_event_streaming
from ..sse import format_sse
from .models import ExecutionRecord, MessageRequest, ThreadTarget
from .runtime_turn_terminal_state import (
    RuntimeThreadOutcome,
    RuntimeTurnTerminalStateMachine,
)
from .service import HarnessBackedOrchestrationService

_INTERRUPT_POLL_INTERVAL_SECONDS = 0.05
_STALL_POLL_INTERVAL_SECONDS = 0.25
_STALL_RECOVERY_PROBE_INTERVAL_SECONDS = 15.0
_STALL_COMPLETION_GRACE_SECONDS = 2.0
RUNTIME_THREAD_TIMEOUT_ERROR = "Runtime thread timed out"
RUNTIME_THREAD_INTERRUPTED_ERROR = "Runtime thread interrupted"
RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR = (
    "Runtime thread execution is missing backend ids"
)


async def _recover_stalled_turn(
    execution: RuntimeThreadExecution,
) -> Optional[Any]:
    recover = getattr(execution.harness, "recover_stalled_turn", None)
    if not callable(recover):
        return None
    try:
        result = await recover(
            execution.workspace_root,
            execution.thread.backend_thread_id or "",
            execution.execution.backend_id or "",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if not _looks_like_terminal_turn_result(result):
        return None
    return result


def _looks_like_terminal_turn_result(result: Any) -> bool:
    if result is None:
        return False
    status = getattr(result, "status", None)
    assistant_text = getattr(result, "assistant_text", None)
    raw_events = getattr(result, "raw_events", None)
    errors = getattr(result, "errors", None)
    return (
        isinstance(status, str)
        and isinstance(assistant_text, str)
        and isinstance(raw_events, list)
        and isinstance(errors, list)
    )


def _harness_supports_stall_recovery(harness: Any) -> bool:
    return callable(getattr(harness, "recover_stalled_turn", None))


async def _wait_for_late_collector_result(
    collector_task: asyncio.Task[Any],
    grace_seconds: float,
) -> Optional[Any]:
    if collector_task.done():
        return await collector_task
    timeout_seconds = max(float(grace_seconds), 0.0)
    if timeout_seconds <= 0.0:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.shield(collector_task),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return None


@dataclass(frozen=True)
class RuntimeThreadExecution:
    """Started runtime-thread execution bound to one concrete harness instance."""

    service: HarnessBackedOrchestrationService
    harness: Any
    thread: ThreadTarget
    execution: ExecutionRecord
    workspace_root: Path
    request: MessageRequest


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

    claimed = service.claim_next_queued_execution_context(thread_target_id)
    if claimed is None:
        return None
    refreshed, harness = await service._start_claimed_execution_request(
        claimed.thread,
        claimed.request,
        claimed.execution,
        workspace_root=(
            Path(claimed.thread.workspace_root)
            if claimed.thread.workspace_root
            else None
        ),
        sandbox_policy=claimed.sandbox_policy,
    )
    refreshed_thread = service.get_thread_target(thread_target_id)
    if refreshed_thread is None:
        raise KeyError(f"Unknown thread target '{thread_target_id}' after queue start")
    resolved_workspace_root = (
        refreshed_thread.workspace_root or claimed.thread.workspace_root
    )
    if not resolved_workspace_root:
        raise RuntimeError("Thread target is missing workspace_root after queue start")
    return RuntimeThreadExecution(
        service=service,
        harness=harness,
        thread=refreshed_thread,
        execution=refreshed,
        workspace_root=Path(resolved_workspace_root),
        request=claimed.request,
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
    timeout_seconds: Optional[float],
    stall_timeout_seconds: Optional[float] = None,
    stall_timeout_replaces_wall_clock_timeout: bool = False,
    execution_error_message: str,
    terminal_state: Optional[RuntimeTurnTerminalStateMachine] = None,
    observe_progress_events: bool = True,
) -> RuntimeThreadOutcome:
    """Wait for a started runtime-thread execution to reach a terminal outcome.

    By default, callers that provide both ``timeout_seconds`` and
    ``stall_timeout_seconds`` get both protections: a hard wall-clock cap plus a
    progress-stall guard. PMA managed-thread callers can opt into idle-only
    semantics by setting ``stall_timeout_replaces_wall_clock_timeout=True``.
    """

    backend_thread_id = execution.thread.backend_thread_id or ""
    backend_turn_id = execution.execution.backend_id
    state = terminal_state or RuntimeTurnTerminalStateMachine(
        backend_thread_id=backend_thread_id,
        backend_turn_id=backend_turn_id,
    )
    if not backend_thread_id or not backend_turn_id:
        return state.build_missing_backend_ids_outcome(
            RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR
        )
    collector_task = asyncio.create_task(
        execution.harness.wait_for_turn(
            execution.workspace_root,
            backend_thread_id,
            backend_turn_id,
            timeout=None,
        )
    )
    resolved_timeout_seconds = (
        float(timeout_seconds)
        if timeout_seconds is not None and float(timeout_seconds) > 0.0
        else None
    )
    resolved_stall_timeout_seconds = (
        float(stall_timeout_seconds)
        if stall_timeout_seconds is not None and float(stall_timeout_seconds) > 0.0
        else None
    )
    timeout_task = None
    if resolved_timeout_seconds is not None and (
        resolved_stall_timeout_seconds is None
        or not stall_timeout_replaces_wall_clock_timeout
    ):
        timeout_task = asyncio.create_task(asyncio.sleep(resolved_timeout_seconds))
    interrupt_task = (
        asyncio.create_task(_wait_for_interrupt(interrupt_event))
        if interrupt_event is not None
        else None
    )
    terminal_wait_task = asyncio.create_task(state.terminal_signal_waiter().wait())
    stall_task = (
        asyncio.create_task(
            _wait_for_progress_stall(state, resolved_stall_timeout_seconds)
        )
        if resolved_stall_timeout_seconds is not None
        else None
    )
    recovery_probe_task = None
    recovery_probe_interval = _stall_recovery_probe_interval_seconds(
        resolved_stall_timeout_seconds
    )
    if recovery_probe_interval is not None and _harness_supports_stall_recovery(
        execution.harness
    ):
        recovery_probe_task = asyncio.create_task(
            _wait_for_progress_recovery_probe(state, recovery_probe_interval)
        )
    stream_task = None
    if observe_progress_events and harness_supports_event_streaming(execution.harness):
        stream_task = asyncio.create_task(
            _observe_runtime_terminal_state(execution, state)
        )

    try:
        wait_tasks = {collector_task, terminal_wait_task}
        if timeout_task is not None:
            wait_tasks.add(timeout_task)
        if interrupt_task is not None:
            wait_tasks.add(interrupt_task)
        if stall_task is not None:
            wait_tasks.add(stall_task)
        if recovery_probe_task is not None:
            wait_tasks.add(recovery_probe_task)
        while True:
            done, _ = await asyncio.wait(
                wait_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if collector_task in done:
                result = await collector_task
                state.note_transport_result(result)
                return state.build_outcome(execution_error_message)
            if terminal_wait_task in done:
                return state.build_outcome(execution_error_message)
            if interrupt_task is not None and interrupt_task in done:
                await execution.harness.interrupt(
                    execution.workspace_root,
                    backend_thread_id,
                    backend_turn_id,
                )
                return state.build_interrupted_outcome(RUNTIME_THREAD_INTERRUPTED_ERROR)
            if timeout_task is not None and timeout_task in done:
                await execution.harness.interrupt(
                    execution.workspace_root,
                    backend_thread_id,
                    backend_turn_id,
                )
                return state.build_timeout_outcome(RUNTIME_THREAD_TIMEOUT_ERROR)
            if recovery_probe_task is not None and recovery_probe_task in done:
                wait_tasks.discard(recovery_probe_task)
                recovered = await _recover_stalled_turn(execution)
                if recovered is not None:
                    state.note_transport_result(recovered)
                    return state.build_outcome(execution_error_message)
                assert recovery_probe_interval is not None
                recovery_probe_task = asyncio.create_task(
                    _wait_for_progress_recovery_probe(state, recovery_probe_interval)
                )
                wait_tasks.add(recovery_probe_task)
                continue
            if stall_task is not None and stall_task in done:
                recovered = await _recover_stalled_turn(execution)
                if recovered is not None:
                    state.note_transport_result(recovered)
                    return state.build_outcome(execution_error_message)
                late_result = await _wait_for_late_collector_result(
                    collector_task,
                    _STALL_COMPLETION_GRACE_SECONDS,
                )
                if late_result is not None:
                    state.note_transport_result(late_result)
                    return state.build_outcome(execution_error_message)
                await execution.harness.interrupt(
                    execution.workspace_root,
                    backend_thread_id,
                    backend_turn_id,
                )
                return state.build_timeout_outcome(RUNTIME_THREAD_TIMEOUT_ERROR)
    except Exception as exc:  # intentional: harness runtime errors are unpredictable
        detail = str(exc or "").strip()
        return state.build_transport_exception_outcome(
            detail or execution_error_message
        )
    finally:
        cleanup_tasks: list[asyncio.Task[Any]] = [terminal_wait_task]
        if timeout_task is not None:
            cleanup_tasks.append(timeout_task)
        if not collector_task.done():
            cleanup_tasks.append(collector_task)
        if interrupt_task is not None:
            cleanup_tasks.append(interrupt_task)
        if stall_task is not None:
            cleanup_tasks.append(stall_task)
        if recovery_probe_task is not None:
            cleanup_tasks.append(recovery_probe_task)
        if stream_task is not None:
            cleanup_tasks.append(stream_task)
        for task in cleanup_tasks:
            task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)


async def _wait_for_interrupt(interrupt_event: asyncio.Event) -> None:
    while not interrupt_event.is_set():
        await asyncio.sleep(_INTERRUPT_POLL_INTERVAL_SECONDS)


async def _wait_for_progress_stall(
    state: RuntimeTurnTerminalStateMachine,
    stall_timeout_seconds: float,
) -> None:
    timeout_seconds = max(float(stall_timeout_seconds), 0.0)
    deadline = time.monotonic() + timeout_seconds
    while True:
        last_progress = state.last_progress_monotonic
        if last_progress is not None:
            deadline = max(deadline, last_progress + timeout_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, _STALL_POLL_INTERVAL_SECONDS))


def _stall_recovery_probe_interval_seconds(
    stall_timeout_seconds: Optional[float],
) -> Optional[float]:
    interval = float(_STALL_RECOVERY_PROBE_INTERVAL_SECONDS)
    if interval <= 0.0:
        return None
    if stall_timeout_seconds is None:
        return interval
    timeout_seconds = float(stall_timeout_seconds)
    if timeout_seconds <= 0.0:
        return None
    if timeout_seconds <= interval:
        return None
    return interval


async def _wait_for_progress_recovery_probe(
    state: RuntimeTurnTerminalStateMachine,
    probe_interval_seconds: float,
) -> None:
    timeout_seconds = max(float(probe_interval_seconds), 0.0)
    deadline = time.monotonic() + timeout_seconds
    while True:
        last_progress = state.last_progress_monotonic
        if last_progress is not None:
            deadline = max(deadline, last_progress + timeout_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, _STALL_POLL_INTERVAL_SECONDS))


async def _observe_runtime_terminal_state(
    execution: RuntimeThreadExecution,
    state: RuntimeTurnTerminalStateMachine,
) -> None:
    try:
        async for raw_event in execution.harness.stream_events(
            execution.workspace_root,
            execution.thread.backend_thread_id or "",
            execution.execution.backend_id or "",
        ):
            state.note_raw_event(raw_event)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


__all__ = [
    "RUNTIME_THREAD_INTERRUPTED_ERROR",
    "RUNTIME_THREAD_MISSING_BACKEND_IDS_ERROR",
    "RUNTIME_THREAD_TIMEOUT_ERROR",
    "RuntimeThreadExecution",
    "RuntimeThreadOutcome",
    "RuntimeTurnTerminalStateMachine",
    "await_runtime_thread_outcome",
    "begin_next_queued_runtime_thread_execution",
    "begin_runtime_thread_execution",
    "stream_runtime_thread_events",
]
