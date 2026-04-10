from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, Mapping, Optional, TypeVar, cast

from ...core.orchestration.runtime_thread_events import RuntimeThreadRunEventState
from .managed_thread_turns import (
    ManagedThreadCoordinatorHooks,
    ManagedThreadExecutionFlowResult,
    ManagedThreadExecutionStarter,
    ManagedThreadFinalizationResult,
    ManagedThreadQueuedExecutionStarter,
    ManagedThreadSubmissionResult,
    ManagedThreadTurnCoordinator,
    SpawnTask,
    coerce_managed_thread_finalization_result,
    complete_managed_thread_execution,
)

SurfaceResultT = TypeVar("SurfaceResultT")


@dataclass(frozen=True)
class ManagedSurfaceQueueConfig:
    task_map: dict[str, asyncio.Task[Any]]
    managed_thread_id: str
    spawn_task: SpawnTask
    begin_next_execution: Optional[ManagedThreadQueuedExecutionStarter] = None
    poll_interval_seconds: float = 0.1


@dataclass(frozen=True)
class ManagedSurfaceRunnerConfig(Generic[SurfaceResultT]):
    coordinator: ManagedThreadTurnCoordinator
    client_request_id: Optional[str]
    sandbox_policy: Optional[Any]
    hooks: ManagedThreadCoordinatorHooks = field(
        default_factory=ManagedThreadCoordinatorHooks
    )
    queue: Optional[ManagedSurfaceQueueConfig] = None
    begin_execution: Optional[ManagedThreadExecutionStarter] = None
    complete_execution: Optional[
        Callable[..., Awaitable[ManagedThreadExecutionFlowResult]]
    ] = None
    submission_timeout_seconds: Optional[float] = None
    runtime_event_state: Optional[RuntimeThreadRunEventState] = None
    after_submission: Optional[Callable[[ManagedThreadSubmissionResult], object]] = None
    on_submission_error: Optional[Callable[[BaseException], object]] = None
    on_after_submission_error: Optional[
        Callable[[ManagedThreadSubmissionResult, Exception], object]
    ] = None
    on_queued: Optional[Callable[[ManagedThreadExecutionFlowResult], object]] = None
    on_finalized: Optional[
        Callable[
            [ManagedThreadExecutionFlowResult, ManagedThreadFinalizationResult],
            object,
        ]
    ] = None
    after_completion: Optional[
        Callable[[Optional[ManagedThreadExecutionFlowResult]], object]
    ] = None


async def _resolve_callback_result(callback: Callable[..., object], *args: Any) -> Any:
    result = callback(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _coerce_execution_flow_result(
    value: Any,
    *,
    submission: ManagedThreadSubmissionResult,
) -> ManagedThreadExecutionFlowResult:
    if isinstance(value, ManagedThreadExecutionFlowResult):
        return value
    finalized: Optional[ManagedThreadFinalizationResult | Mapping[str, Any]] = None
    if isinstance(value, Mapping):
        finalized = value
    elif hasattr(value, "finalized"):
        finalized = value.finalized
    return ManagedThreadExecutionFlowResult(
        started_execution=submission.started_execution,
        queued=submission.queued,
        finalized=coerce_managed_thread_finalization_result(finalized),
    )


async def run_managed_surface_turn(
    request: Any,
    *,
    config: ManagedSurfaceRunnerConfig[SurfaceResultT],
) -> SurfaceResultT:
    finalized_flow: Optional[ManagedThreadExecutionFlowResult] = None

    def _ensure_queue_worker() -> None:
        queue = config.queue
        if queue is None:
            return
        try:
            ensure_queue_worker = config.coordinator.ensure_queue_worker
        except AttributeError:
            return
        if not callable(ensure_queue_worker):
            return
        ensure_queue_worker(
            task_map=queue.task_map,
            managed_thread_id=queue.managed_thread_id,
            spawn_task=queue.spawn_task,
            hooks=config.hooks,
            poll_interval_seconds=queue.poll_interval_seconds,
            begin_next_execution=queue.begin_next_execution,
        )

    try:
        try:
            submit_coro = config.coordinator.submit_execution(
                request,
                client_request_id=config.client_request_id,
                sandbox_policy=config.sandbox_policy,
                begin_execution=config.begin_execution,
            )
            if config.submission_timeout_seconds is not None:
                submission = await asyncio.wait_for(
                    submit_coro,
                    timeout=config.submission_timeout_seconds,
                )
            else:
                submission = await submit_coro
        except BaseException as exc:
            if config.on_submission_error is None:
                raise
            return cast(
                SurfaceResultT,
                await _resolve_callback_result(config.on_submission_error, exc),
            )
        if config.after_submission is not None:
            try:
                await _resolve_callback_result(config.after_submission, submission)
            except Exception as exc:
                if config.on_after_submission_error is None:
                    raise
                await _resolve_callback_result(
                    config.on_after_submission_error,
                    submission,
                    exc,
                )
        if submission.queued:
            if config.queue is not None:
                _ensure_queue_worker()
            finalized_flow = ManagedThreadExecutionFlowResult(
                started_execution=submission.started_execution,
                queued=True,
            )
        else:
            complete_execution = (
                config.complete_execution or complete_managed_thread_execution
            )
            finalized_flow = _coerce_execution_flow_result(
                await complete_execution(
                    config.coordinator,
                    submission,
                    ensure_queue_worker=(
                        _ensure_queue_worker if config.queue is not None else None
                    ),
                    direct_hooks=config.hooks,
                    runtime_event_state=config.runtime_event_state,
                ),
                submission=submission,
            )
        if finalized_flow.queued:
            if config.on_queued is None:
                raise RuntimeError("Queued managed-surface turn requires on_queued")
            return cast(
                SurfaceResultT,
                await _resolve_callback_result(config.on_queued, finalized_flow),
            )
        finalized = coerce_managed_thread_finalization_result(finalized_flow.finalized)
        if finalized is None:
            raise RuntimeError("Managed-thread turn finalized without a result")
        if config.on_finalized is None:
            raise RuntimeError("Managed-surface turn requires on_finalized")
        return cast(
            SurfaceResultT,
            await _resolve_callback_result(
                config.on_finalized,
                finalized_flow,
                finalized,
            ),
        )
    finally:
        if config.after_completion is not None:
            await _resolve_callback_result(config.after_completion, finalized_flow)
