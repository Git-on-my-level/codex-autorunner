from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Optional, Protocol

from ...agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
from ...core.orchestration.models import MessageRequest
from ...core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
    recover_post_completion_outcome,
    terminal_run_event_from_outcome,
)
from ...core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    RuntimeThreadOutcome,
    await_runtime_thread_outcome,
    begin_next_queued_runtime_thread_execution,
    begin_runtime_thread_execution,
)
from ...core.orchestration.turn_timeline import persist_turn_timeline
from ...core.pma_thread_store import PmaThreadStore
from ...core.pma_transcripts import PmaTranscriptStore
from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
)
from .runtime_thread_errors import resolve_runtime_thread_error_detail

ProgressEventHandler = Callable[[Any], Awaitable[None]]
RunWithIndicator = Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]
ManagedThreadStatus = Literal["ok", "error", "interrupted"]
ManagedThreadLifecycleHook = Callable[[RuntimeThreadExecution], object]
SpawnTask = Callable[[Awaitable[Any]], asyncio.Task[Any]]
MessagePreviewBuilder = Callable[[str], str]


class ManagedThreadExecutionStarter(Protocol):
    async def __call__(
        self,
        orchestration_service: Any,
        request: MessageRequest,
        *,
        client_request_id: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> RuntimeThreadExecution: ...


class ManagedThreadQueuedExecutionStarter(Protocol):
    async def __call__(
        self,
        orchestration_service: Any,
        managed_thread_id: str,
    ) -> Optional[RuntimeThreadExecution]: ...


@dataclass(frozen=True)
class ManagedThreadFinalizationResult:
    status: ManagedThreadStatus
    assistant_text: str
    error: Optional[str]
    managed_thread_id: str
    managed_turn_id: str
    backend_thread_id: Optional[str]
    token_usage: Optional[dict[str, Any]] = None


FinalizeQueuedExecution = Callable[
    [RuntimeThreadExecution], Awaitable[ManagedThreadFinalizationResult]
]
DeliverQueuedResult = Callable[[ManagedThreadFinalizationResult], Awaitable[None]]

_QUEUE_WORKER_FAILURE_ERROR = "Queue worker terminated unexpectedly"
logger = logging.getLogger(__name__)

_DIRECT_RUN_EVENT_TYPES = (
    OutputDelta,
    ToolCall,
    ApprovalRequested,
    RunNotice,
    TokenUsage,
    Completed,
    Failed,
    Started,
)


@dataclass(frozen=True)
class ManagedThreadErrorMessages:
    public_execution_error: str
    timeout_error: str
    interrupted_error: str
    timeout_seconds: float


@dataclass(frozen=True)
class ManagedThreadSurfaceInfo:
    log_label: str
    surface_kind: str
    surface_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedThreadSubmissionResult:
    started_execution: RuntimeThreadExecution
    queued: bool


@dataclass(frozen=True)
class ManagedThreadExecutionFlowResult:
    started_execution: RuntimeThreadExecution
    queued: bool
    finalized: Optional[ManagedThreadFinalizationResult] = None


@dataclass(frozen=True)
class ManagedThreadExecutionHooks:
    on_execution_started: Optional[ManagedThreadLifecycleHook] = None
    on_execution_finished: Optional[ManagedThreadLifecycleHook] = None
    on_progress_event: Optional[ProgressEventHandler] = None


@dataclass(frozen=True)
class ManagedThreadQueueWorkerHooks:
    deliver_result: DeliverQueuedResult
    run_with_indicator: Optional[RunWithIndicator] = None
    execution_hooks: ManagedThreadExecutionHooks = field(
        default_factory=ManagedThreadExecutionHooks
    )


@dataclass(frozen=True)
class ManagedThreadCoordinatorHooks:
    on_execution_started: Optional[ManagedThreadLifecycleHook] = None
    on_execution_finished: Optional[ManagedThreadLifecycleHook] = None
    on_progress_event: Optional[ProgressEventHandler] = None
    deliver_result: Optional[DeliverQueuedResult] = None
    run_with_indicator: Optional[RunWithIndicator] = None

    def execution_hooks(self) -> ManagedThreadExecutionHooks:
        return ManagedThreadExecutionHooks(
            on_execution_started=self.on_execution_started,
            on_execution_finished=self.on_execution_finished,
            on_progress_event=self.on_progress_event,
        )

    def queue_worker_hooks(self) -> ManagedThreadQueueWorkerHooks:
        if self.deliver_result is None:
            raise ValueError("Queue-worker hooks require deliver_result")
        return ManagedThreadQueueWorkerHooks(
            deliver_result=self.deliver_result,
            run_with_indicator=self.run_with_indicator,
            execution_hooks=self.execution_hooks(),
        )


@dataclass(frozen=True)
class ManagedThreadTargetRequest:
    surface_kind: str
    surface_key: str
    mode: str
    agent: str
    workspace_root: Path
    display_name: str
    agent_profile: Optional[str] = None
    repo_id: Optional[str] = None
    resource_kind: Optional[str] = None
    resource_id: Optional[str] = None
    binding_metadata: Mapping[str, Any] = field(default_factory=dict)
    thread_metadata: Mapping[str, Any] = field(default_factory=dict)
    reusable_agent_ids: tuple[str, ...] = ()
    allow_new_thread: bool = True
    backend_thread_id: Optional[str] = None
    backend_runtime_instance_id: Optional[str] = None
    existing_binding: Optional[Any] = None
    existing_thread: Optional[Any] = None


def _normalized_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _matching_backend_thread_target(
    orchestration_service: Any,
    *,
    request: ManagedThreadTargetRequest,
    reusable_agent_ids: tuple[str, ...],
    canonical_workspace: str,
    backend_thread_id: str,
) -> Optional[Any]:
    list_thread_targets = getattr(orchestration_service, "list_thread_targets", None)
    if not callable(list_thread_targets):
        return None
    list_kwargs: dict[str, Any] = {"limit": 500}
    if len(reusable_agent_ids) == 1:
        list_kwargs["agent_id"] = reusable_agent_ids[0]
    if request.repo_id is not None:
        list_kwargs["repo_id"] = request.repo_id
    if request.resource_kind is not None:
        list_kwargs["resource_kind"] = request.resource_kind
    if request.resource_id is not None:
        list_kwargs["resource_id"] = request.resource_id
    try:
        candidates = list_thread_targets(**list_kwargs)
    except TypeError:
        candidates = list_thread_targets(limit=list_kwargs["limit"])
    for candidate in candidates or ():
        if candidate is None:
            continue
        if (
            str(getattr(candidate, "agent_id", "") or "").strip()
            not in reusable_agent_ids
        ):
            continue
        if (getattr(candidate, "agent_profile", None) or None) != (
            request.agent_profile or None
        ):
            continue
        if (
            str(getattr(candidate, "workspace_root", "") or "").strip()
            != canonical_workspace
        ):
            continue
        if request.repo_id is not None and (
            _normalized_optional_text(getattr(candidate, "repo_id", None))
            != _normalized_optional_text(request.repo_id)
        ):
            continue
        if request.resource_kind is not None and (
            _normalized_optional_text(getattr(candidate, "resource_kind", None))
            != _normalized_optional_text(request.resource_kind)
        ):
            continue
        if request.resource_id is not None and (
            _normalized_optional_text(getattr(candidate, "resource_id", None))
            != _normalized_optional_text(request.resource_id)
        ):
            continue
        if _normalized_optional_text(getattr(candidate, "backend_thread_id", None)) != (
            backend_thread_id
        ):
            continue
        return candidate
    return None


def _load_managed_thread_binding(
    orchestration_service: Any,
    *,
    request: ManagedThreadTargetRequest,
) -> Any:
    if request.existing_binding is not None:
        return request.existing_binding
    get_binding = getattr(orchestration_service, "get_binding", None)
    if not callable(get_binding):
        return None
    return get_binding(
        surface_kind=request.surface_kind,
        surface_key=request.surface_key,
    )


def _load_bound_thread_target(
    orchestration_service: Any,
    *,
    request: ManagedThreadTargetRequest,
    binding: Any,
) -> Any:
    if request.existing_thread is not None:
        return request.existing_thread
    normalized_mode = request.mode.strip().lower()
    thread_target_id = (
        binding.thread_target_id
        if binding is not None
        and str(getattr(binding, "mode", "") or "").strip().lower() == normalized_mode
        else None
    )
    get_thread_target = getattr(orchestration_service, "get_thread_target", None)
    if (
        not callable(get_thread_target)
        or not isinstance(thread_target_id, str)
        or not thread_target_id
    ):
        return None
    return get_thread_target(thread_target_id)


def _managed_thread_reusable_agent_ids(
    request: ManagedThreadTargetRequest,
) -> tuple[str, ...]:
    return tuple(dict.fromkeys((request.agent, *tuple(request.reusable_agent_ids))))


def _thread_matches_reuse_constraints(
    thread: Any,
    *,
    request: ManagedThreadTargetRequest,
    reusable_agent_ids: tuple[str, ...],
    canonical_workspace: str,
) -> bool:
    return (
        thread is not None
        and str(getattr(thread, "agent_id", "") or "").strip() in reusable_agent_ids
        and (getattr(thread, "agent_profile", None) or None)
        == (request.agent_profile or None)
        and str(getattr(thread, "workspace_root", "") or "").strip()
        == canonical_workspace
    )


def _resume_managed_thread_target(
    orchestration_service: Any,
    thread: Any,
    *,
    desired_backend_thread_id: Optional[str],
    current_backend_thread_id: Optional[str],
    desired_runtime_instance_id: Optional[str],
) -> Any:
    resume_kwargs: dict[str, Any] = {}
    if desired_backend_thread_id is not None or current_backend_thread_id is not None:
        resume_kwargs["backend_thread_id"] = desired_backend_thread_id
    resume_kwargs["backend_runtime_instance_id"] = desired_runtime_instance_id
    return orchestration_service.resume_thread_target(
        thread.thread_target_id,
        **resume_kwargs,
    )


def _create_managed_thread_target(
    orchestration_service: Any,
    *,
    request: ManagedThreadTargetRequest,
    desired_backend_thread_id: Optional[str],
    desired_runtime_instance_id: Optional[str],
) -> Any:
    thread_metadata = dict(request.thread_metadata)
    if request.agent_profile and "agent_profile" not in thread_metadata:
        thread_metadata["agent_profile"] = request.agent_profile
    if desired_runtime_instance_id is not None:
        thread_metadata.setdefault(
            "backend_runtime_instance_id",
            desired_runtime_instance_id,
        )
    return orchestration_service.create_thread_target(
        request.agent,
        request.workspace_root,
        repo_id=request.repo_id,
        resource_kind=request.resource_kind,
        resource_id=request.resource_id,
        display_name=request.display_name,
        backend_thread_id=desired_backend_thread_id,
        metadata=thread_metadata or None,
    )


def _persist_managed_thread_binding(
    orchestration_service: Any,
    *,
    request: ManagedThreadTargetRequest,
    thread: Any,
) -> None:
    orchestration_service.upsert_binding(
        surface_kind=request.surface_kind,
        surface_key=request.surface_key,
        thread_target_id=thread.thread_target_id,
        agent_id=request.agent,
        repo_id=request.repo_id,
        resource_kind=request.resource_kind,
        resource_id=request.resource_id,
        mode=request.mode,
        metadata=dict(request.binding_metadata),
    )


async def _invoke_lifecycle_hook(
    hook: Optional[ManagedThreadLifecycleHook],
    started: RuntimeThreadExecution,
) -> None:
    if hook is None:
        return
    result = hook(started)
    if inspect.isawaitable(result):
        await result


def _coerce_execution_hooks(
    hooks: Optional[ManagedThreadExecutionHooks | ManagedThreadCoordinatorHooks],
) -> ManagedThreadExecutionHooks:
    if hooks is None:
        return ManagedThreadExecutionHooks()
    if isinstance(hooks, ManagedThreadCoordinatorHooks):
        return hooks.execution_hooks()
    return hooks


def _coerce_queue_worker_hooks(
    hooks: ManagedThreadQueueWorkerHooks | ManagedThreadCoordinatorHooks,
) -> ManagedThreadQueueWorkerHooks:
    if isinstance(hooks, ManagedThreadCoordinatorHooks):
        return hooks.queue_worker_hooks()
    return hooks


def _build_finalization_result(
    *,
    status: ManagedThreadStatus,
    assistant_text: str,
    error: Optional[str],
    managed_thread_id: str,
    managed_turn_id: str,
    backend_thread_id: Optional[str],
    token_usage: Optional[dict[str, Any]],
) -> ManagedThreadFinalizationResult:
    return ManagedThreadFinalizationResult(
        status=status,
        assistant_text=assistant_text,
        error=error,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id=backend_thread_id,
        token_usage=token_usage,
    )


def _coerce_managed_thread_status(value: Any) -> ManagedThreadStatus:
    normalized = str(value or "").strip().lower()
    if normalized == "ok":
        return "ok"
    if normalized == "interrupted":
        return "interrupted"
    return "error"


def coerce_managed_thread_finalization_result(
    finalized: Optional[ManagedThreadFinalizationResult | Mapping[str, Any]],
) -> Optional[ManagedThreadFinalizationResult]:
    if finalized is None or isinstance(finalized, ManagedThreadFinalizationResult):
        return finalized
    token_usage = finalized.get("token_usage")
    normalized_token_usage = (
        dict(token_usage) if isinstance(token_usage, Mapping) else None
    )
    return ManagedThreadFinalizationResult(
        status=_coerce_managed_thread_status(finalized.get("status")),
        assistant_text=str(finalized.get("assistant_text") or ""),
        error=(
            str(finalized.get("error") or "").strip() or None
            if finalized.get("error") is not None
            else None
        ),
        managed_thread_id=str(finalized.get("managed_thread_id") or ""),
        managed_turn_id=str(finalized.get("managed_turn_id") or ""),
        backend_thread_id=(
            str(finalized.get("backend_thread_id") or "").strip() or None
        ),
        token_usage=normalized_token_usage,
    )


def resolve_managed_thread_target(
    orchestration_service: Any,
    *,
    request: ManagedThreadTargetRequest,
) -> tuple[Any, Optional[Any]]:
    binding = _load_managed_thread_binding(
        orchestration_service,
        request=request,
    )
    thread = _load_bound_thread_target(
        orchestration_service,
        request=request,
        binding=binding,
    )
    canonical_workspace = str(request.workspace_root.resolve())
    desired_backend_thread_id = _normalized_optional_text(request.backend_thread_id)
    desired_runtime_instance_id = _normalized_optional_text(
        request.backend_runtime_instance_id
    )
    reusable_agent_ids = _managed_thread_reusable_agent_ids(request)
    current_backend_thread_id = _normalized_optional_text(
        getattr(thread, "backend_thread_id", None)
    )
    current_runtime_instance_id = _normalized_optional_text(
        getattr(thread, "backend_runtime_instance_id", None)
    )
    reusable_thread = _thread_matches_reuse_constraints(
        thread,
        request=request,
        reusable_agent_ids=reusable_agent_ids,
        canonical_workspace=canonical_workspace,
    )
    if (
        desired_backend_thread_id is not None
        and current_backend_thread_id != desired_backend_thread_id
    ):
        matched_thread = _matching_backend_thread_target(
            orchestration_service,
            request=request,
            reusable_agent_ids=reusable_agent_ids,
            canonical_workspace=canonical_workspace,
            backend_thread_id=desired_backend_thread_id,
        )
        if matched_thread is not None:
            thread = matched_thread
            current_backend_thread_id = _normalized_optional_text(
                getattr(thread, "backend_thread_id", None)
            )
            current_runtime_instance_id = _normalized_optional_text(
                getattr(thread, "backend_runtime_instance_id", None)
            )
            reusable_thread = _thread_matches_reuse_constraints(
                thread,
                request=request,
                reusable_agent_ids=reusable_agent_ids,
                canonical_workspace=canonical_workspace,
            )
    should_resume_reusable = reusable_thread and (
        str(getattr(thread, "lifecycle_status", "") or "").strip().lower() != "active"
        or current_backend_thread_id != desired_backend_thread_id
        or (
            desired_runtime_instance_id is not None
            and current_runtime_instance_id != desired_runtime_instance_id
        )
    )
    if should_resume_reusable:
        assert thread is not None
        thread = _resume_managed_thread_target(
            orchestration_service,
            thread,
            desired_backend_thread_id=desired_backend_thread_id,
            current_backend_thread_id=current_backend_thread_id,
            desired_runtime_instance_id=desired_runtime_instance_id,
        )
    elif not reusable_thread:
        if not request.allow_new_thread and desired_backend_thread_id is None:
            return orchestration_service, None
        thread = _create_managed_thread_target(
            orchestration_service,
            request=request,
            desired_backend_thread_id=desired_backend_thread_id,
            desired_runtime_instance_id=desired_runtime_instance_id,
        )
    if thread is None:
        return orchestration_service, None
    _persist_managed_thread_binding(
        orchestration_service,
        request=request,
        thread=thread,
    )
    return orchestration_service, thread


@dataclass
class ManagedThreadTurnCoordinator:
    orchestration_service: Any
    state_root: Path
    surface: ManagedThreadSurfaceInfo
    errors: ManagedThreadErrorMessages
    logger: logging.Logger
    turn_preview: str
    preview_builder: Optional[MessagePreviewBuilder] = None

    async def submit_execution(
        self,
        request: MessageRequest,
        *,
        client_request_id: Optional[str],
        sandbox_policy: Optional[Any],
        begin_execution: Optional[ManagedThreadExecutionStarter] = None,
    ) -> ManagedThreadSubmissionResult:
        return await submit_managed_thread_execution(
            self.orchestration_service,
            request,
            client_request_id=client_request_id,
            sandbox_policy=sandbox_policy,
            begin_execution=begin_execution,
        )

    async def run_started_execution(
        self,
        started: RuntimeThreadExecution,
        *,
        hooks: Optional[
            ManagedThreadExecutionHooks | ManagedThreadCoordinatorHooks
        ] = None,
        runtime_event_state: Optional[RuntimeThreadRunEventState] = None,
    ) -> ManagedThreadFinalizationResult:
        resolved_hooks = _coerce_execution_hooks(hooks)
        turn_preview = self.turn_preview
        if self.preview_builder is not None:
            try:
                turn_preview = self.preview_builder(started.request.message_text)
            except (
                RuntimeError,
                ValueError,
                TypeError,
                AttributeError,
            ):
                self.logger.debug(
                    "%s preview builder failed; falling back to coordinator preview",
                    self.surface.log_label,
                    exc_info=True,
                )
        await _invoke_lifecycle_hook(resolved_hooks.on_execution_started, started)
        try:
            return await finalize_managed_thread_execution(
                orchestration_service=self.orchestration_service,
                started=started,
                state_root=self.state_root,
                surface=self.surface,
                errors=self.errors,
                logger=self.logger,
                turn_preview=turn_preview,
                runtime_event_state=runtime_event_state,
                on_progress_event=resolved_hooks.on_progress_event,
            )
        finally:
            await _invoke_lifecycle_hook(resolved_hooks.on_execution_finished, started)

    def ensure_queue_worker(
        self,
        *,
        task_map: dict[str, asyncio.Task[Any]],
        managed_thread_id: str,
        spawn_task: SpawnTask,
        hooks: ManagedThreadQueueWorkerHooks | ManagedThreadCoordinatorHooks,
        poll_interval_seconds: float = 0.1,
        begin_next_execution: Optional[ManagedThreadQueuedExecutionStarter] = None,
    ) -> None:
        resolved_hooks = _coerce_queue_worker_hooks(hooks)
        ensure_managed_thread_queue_worker(
            task_map=task_map,
            managed_thread_id=managed_thread_id,
            orchestration_service=self.orchestration_service,
            spawn_task=spawn_task,
            finalize_started_execution=lambda started: self.run_started_execution(
                started,
                hooks=resolved_hooks.execution_hooks,
            ),
            deliver_result=resolved_hooks.deliver_result,
            run_with_indicator=resolved_hooks.run_with_indicator,
            poll_interval_seconds=poll_interval_seconds,
            begin_next_execution=begin_next_execution,
        )


def build_managed_thread_input_items(
    runtime_prompt: str,
    input_items: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    if not input_items:
        return None
    normalized: list[dict[str, Any]] = []
    replaced_text = False
    for item in input_items:
        if not isinstance(item, dict):
            continue
        item_copy = dict(item)
        if not replaced_text and str(item_copy.get("type") or "").strip() == "text":
            item_copy["text"] = runtime_prompt
            replaced_text = True
        normalized.append(item_copy)
    if not replaced_text:
        normalized.insert(0, {"type": "text", "text": runtime_prompt})
    return normalized or None


def get_thread_runtime_binding(
    orchestration_service: Any, thread_target_id: str
) -> Any:
    getter = getattr(orchestration_service, "get_thread_runtime_binding", None)
    if not callable(getter) or not thread_target_id:
        return None
    try:
        return getter(thread_target_id)
    except (AttributeError, KeyError, TypeError, RuntimeError):
        return None


async def submit_managed_thread_execution(
    orchestration_service: Any,
    request: MessageRequest,
    *,
    client_request_id: Optional[str],
    sandbox_policy: Optional[Any],
    ensure_queue_worker: Optional[Callable[[], None]] = None,
    begin_execution: Optional[ManagedThreadExecutionStarter] = None,
) -> ManagedThreadSubmissionResult:
    begin = begin_execution or begin_runtime_thread_execution
    started_execution = await begin(
        orchestration_service,
        request,
        client_request_id=client_request_id,
        sandbox_policy=sandbox_policy,
    )
    queued = str(getattr(started_execution.execution, "status", "") or "").strip() == (
        "queued"
    )
    if queued and ensure_queue_worker is not None:
        ensure_queue_worker()
    return ManagedThreadSubmissionResult(
        started_execution=started_execution,
        queued=queued,
    )


async def complete_managed_thread_execution(
    coordinator: ManagedThreadTurnCoordinator,
    submission: ManagedThreadSubmissionResult,
    *,
    ensure_queue_worker: Optional[Callable[[], None]] = None,
    direct_hooks: Optional[
        ManagedThreadExecutionHooks | ManagedThreadCoordinatorHooks
    ] = None,
    runtime_event_state: Optional[RuntimeThreadRunEventState] = None,
) -> ManagedThreadExecutionFlowResult:
    if submission.queued:
        if ensure_queue_worker is not None:
            ensure_queue_worker()
        return ManagedThreadExecutionFlowResult(
            started_execution=submission.started_execution,
            queued=True,
        )
    finalized = await coordinator.run_started_execution(
        submission.started_execution,
        hooks=direct_hooks,
        runtime_event_state=runtime_event_state,
    )
    if ensure_queue_worker is not None:
        ensure_queue_worker()
    return ManagedThreadExecutionFlowResult(
        started_execution=submission.started_execution,
        queued=False,
        finalized=finalized,
    )


def ensure_managed_thread_queue_worker(
    *,
    task_map: dict[str, asyncio.Task[Any]],
    managed_thread_id: str,
    orchestration_service: Any,
    spawn_task: SpawnTask,
    finalize_started_execution: FinalizeQueuedExecution,
    deliver_result: DeliverQueuedResult,
    run_with_indicator: Optional[RunWithIndicator] = None,
    poll_interval_seconds: float = 0.1,
    begin_next_execution: Optional[ManagedThreadQueuedExecutionStarter] = None,
) -> None:
    existing = task_map.get(managed_thread_id)
    if isinstance(existing, asyncio.Task) and not existing.done():
        return

    worker_task: Optional[asyncio.Task[Any]] = None
    begin_next = begin_next_execution or begin_next_queued_runtime_thread_execution

    async def _record_queue_worker_failure(
        started_execution: RuntimeThreadExecution,
        *,
        error: str,
    ) -> ManagedThreadFinalizationResult:
        detail = error.strip() or _QUEUE_WORKER_FAILURE_ERROR
        try:
            orchestration_service.record_execution_result(
                started_execution.thread.thread_target_id,
                started_execution.execution.execution_id,
                status="error",
                assistant_text="",
                error=detail,
                backend_turn_id=None,
                transcript_turn_id=None,
            )
        except KeyError:
            pass
        return _build_finalization_result(
            status="error",
            assistant_text="",
            error=detail,
            managed_thread_id=started_execution.thread.thread_target_id,
            managed_turn_id=started_execution.execution.execution_id,
            backend_thread_id=getattr(
                started_execution.thread,
                "backend_thread_id",
                None,
            ),
            token_usage=None,
        )

    async def _process_started_execution(
        started_execution: RuntimeThreadExecution,
    ) -> None:
        finalized: Optional[ManagedThreadFinalizationResult] = None
        try:
            if run_with_indicator is None:
                finalized = coerce_managed_thread_finalization_result(
                    await finalize_started_execution(started_execution)
                )
            else:

                async def _finalize_work() -> None:
                    nonlocal finalized
                    finalized = coerce_managed_thread_finalization_result(
                        await finalize_started_execution(started_execution)
                    )

                await run_with_indicator(_finalize_work)
        except BaseException as exc:
            if finalized is not None:
                logger.exception(
                    "Managed-thread queue worker indicator failed after execution finalized "
                    "(thread=%s execution=%s)",
                    started_execution.thread.thread_target_id,
                    started_execution.execution.execution_id,
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
            else:
                logger.exception(
                    "Managed-thread queued execution failed "
                    "(thread=%s execution=%s)",
                    started_execution.thread.thread_target_id,
                    started_execution.execution.execution_id,
                )
                finalized = await _record_queue_worker_failure(
                    started_execution,
                    error=str(exc) or _QUEUE_WORKER_FAILURE_ERROR,
                )
                try:
                    await deliver_result(finalized)
                except (
                    RuntimeError,
                    OSError,
                    ValueError,
                    TypeError,
                    ConnectionError,
                ):
                    logger.exception(
                        "Managed-thread queued execution failure delivery failed "
                        "(thread=%s execution=%s)",
                        started_execution.thread.thread_target_id,
                        started_execution.execution.execution_id,
                    )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return

        if finalized is None:
            return
        try:
            await deliver_result(finalized)
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            ConnectionError,
        ):
            logger.exception(
                "Managed-thread queued execution delivery failed "
                "(thread=%s execution=%s)",
                started_execution.thread.thread_target_id,
                started_execution.execution.execution_id,
            )

    async def _queue_worker() -> None:
        try:
            while True:
                if (
                    orchestration_service.get_running_execution(managed_thread_id)
                    is not None
                ):
                    await asyncio.sleep(poll_interval_seconds)
                    continue
                started = await begin_next(
                    orchestration_service,
                    managed_thread_id,
                )
                if started is None:
                    break
                await _process_started_execution(started)
        except BaseException:
            logger.exception(
                "Managed-thread queue worker failed (thread=%s)",
                managed_thread_id,
            )
            try:
                running = orchestration_service.get_running_execution(managed_thread_id)
                if running is not None:
                    orchestration_service.record_execution_result(
                        managed_thread_id,
                        running.execution_id,
                        status="error",
                        assistant_text="",
                        error=_QUEUE_WORKER_FAILURE_ERROR,
                        backend_turn_id=None,
                        transcript_turn_id=None,
                    )
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                AttributeError,
            ):
                logger.exception(
                    "Managed-thread queue worker cleanup failed (thread=%s)",
                    managed_thread_id,
                )
            raise
        finally:
            if (
                worker_task is not None
                and task_map.get(managed_thread_id) is worker_task
            ):
                task_map.pop(managed_thread_id, None)

    worker_task = spawn_task(_queue_worker())
    task_map[managed_thread_id] = worker_task


def _note_runtime_event_state(
    event_state: RuntimeThreadRunEventState,
    run_event: Any,
) -> None:
    if isinstance(run_event, OutputDelta):
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM:
            event_state.note_stream_text(str(run_event.content or ""))
            return
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE:
            event_state.note_message_text(str(run_event.content or ""))
            return
        return
    if isinstance(run_event, TokenUsage) and isinstance(run_event.usage, dict):
        event_state.token_usage = dict(run_event.usage)
        return
    if isinstance(run_event, Completed):
        event_state.completed_seen = True
        if isinstance(run_event.final_message, str):
            event_state.note_message_text(run_event.final_message)
        return
    if isinstance(run_event, Failed):
        error_message = str(run_event.error_message or "").strip()
        if error_message:
            event_state.last_error_message = error_message


async def _normalize_runtime_progress_event(
    raw_event: Any,
    event_state: RuntimeThreadRunEventState,
) -> list[Any]:
    if isinstance(raw_event, _DIRECT_RUN_EVENT_TYPES):
        _note_runtime_event_state(event_state, raw_event)
        return [raw_event]
    return await normalize_runtime_thread_raw_event(raw_event, event_state)


def _surface_metadata(
    started: RuntimeThreadExecution,
    surface: ManagedThreadSurfaceInfo,
    *,
    backend_thread_id: Optional[str],
    backend_turn_id: Optional[str],
    status: str,
) -> dict[str, Any]:
    metadata = {
        "agent": getattr(started.thread, "agent_id", None),
        "execution_id": started.execution.execution_id,
        "thread_target_id": started.thread.thread_target_id,
        "backend_thread_id": backend_thread_id,
        "backend_turn_id": backend_turn_id,
        "model": started.request.model,
        "reasoning": started.request.reasoning,
        "request_kind": getattr(started.request, "kind", None),
        "status": status,
        "surface_kind": surface.surface_kind,
        "surface_key": surface.surface_key,
    }
    metadata.update(dict(surface.metadata))
    return metadata


async def finalize_managed_thread_execution(
    *,
    orchestration_service: Any,
    started: RuntimeThreadExecution,
    state_root: Path,
    surface: ManagedThreadSurfaceInfo,
    errors: ManagedThreadErrorMessages,
    logger: logging.Logger,
    turn_preview: str,
    runtime_event_state: Optional[RuntimeThreadRunEventState] = None,
    on_progress_event: Optional[ProgressEventHandler] = None,
) -> ManagedThreadFinalizationResult:
    thread_store = PmaThreadStore(state_root)
    transcripts = PmaTranscriptStore(state_root)
    managed_thread_id = started.thread.thread_target_id
    managed_turn_id = started.execution.execution_id
    current_thread_row = thread_store.get_thread(managed_thread_id) or {}
    runtime_binding = get_thread_runtime_binding(
        orchestration_service, managed_thread_id
    )
    current_backend_thread_id = str(
        getattr(runtime_binding, "backend_thread_id", None)
        or started.thread.backend_thread_id
        or ""
    ).strip()
    started_execution_status = str(
        getattr(started.execution, "status", "") or ""
    ).strip()
    started_execution_error = str(getattr(started.execution, "error", "") or "").strip()
    event_state = runtime_event_state or RuntimeThreadRunEventState()
    stream_task: Optional[asyncio.Task[None]] = None
    timeline_events: list[Any] = []
    live_timeline_count = 0
    live_timeline_error_logged = False

    def _persist_live_timeline_events(events: list[Any]) -> None:
        nonlocal live_timeline_count
        nonlocal live_timeline_error_logged
        if not events:
            return
        try:
            persist_turn_timeline(
                state_root,
                execution_id=managed_turn_id,
                target_kind="thread_target",
                target_id=managed_thread_id,
                repo_id=str(current_thread_row.get("repo_id") or "").strip() or None,
                resource_kind=(
                    str(current_thread_row.get("resource_kind") or "").strip() or None
                ),
                resource_id=(
                    str(current_thread_row.get("resource_id") or "").strip() or None
                ),
                metadata=_surface_metadata(
                    started,
                    surface,
                    backend_thread_id=current_backend_thread_id or None,
                    backend_turn_id=started.execution.backend_id,
                    status="running",
                ),
                events=events,
                start_index=live_timeline_count + 1,
            )
        except Exception:
            if not live_timeline_error_logged:
                live_timeline_error_logged = True
                logger.exception(
                    "Failed to persist live %s thread timeline (thread=%s turn=%s)",
                    surface.log_label,
                    managed_thread_id,
                    managed_turn_id,
                )
        else:
            live_timeline_count += len(events)

    stream_backend_thread_id = current_backend_thread_id
    stream_backend_turn_id = str(started.execution.backend_id or "").strip()
    if not stream_backend_turn_id:
        stream_backend_turn_id = str(started.execution.execution_id or "").strip()
        logger.warning(
            "%s finalize: backend_id missing, falling back to execution_id=%s for thread=%s",
            surface.log_label,
            stream_backend_turn_id,
            managed_thread_id,
        )

    if (
        harness_supports_progress_event_stream(started.harness)
        and stream_backend_thread_id
        and stream_backend_turn_id
    ):

        async def _pump_runtime_events() -> None:
            raw_events_received = 0
            run_events_dispatched = 0
            try:
                async for raw_event in harness_progress_event_stream(
                    started.harness,
                    started.workspace_root,
                    stream_backend_thread_id,
                    stream_backend_turn_id,
                ):
                    raw_events_received += 1
                    run_events = await _normalize_runtime_progress_event(
                        raw_event,
                        event_state,
                    )
                    timeline_events.extend(run_events)
                    _persist_live_timeline_events(run_events)
                    if on_progress_event is None:
                        continue
                    for run_event in run_events:
                        run_events_dispatched += 1
                        try:
                            await on_progress_event(run_event)
                        except (
                            RuntimeError,
                            ValueError,
                            TypeError,
                            ConnectionError,
                            OSError,
                            AttributeError,
                        ):
                            logger.debug(
                                "%s progress event handler failed for %s",
                                surface.log_label,
                                type(run_event).__name__,
                                exc_info=True,
                            )
                            continue
            except Exception:
                logger.warning(
                    "%s progress event pump failed",
                    surface.log_label,
                    exc_info=True,
                )
            finally:
                logger.info(
                    "%s progress pump finished thread=%s turn=%s raw_events=%d run_events_dispatched=%d",
                    surface.log_label,
                    managed_thread_id,
                    managed_turn_id,
                    raw_events_received,
                    run_events_dispatched,
                )

        stream_task = asyncio.create_task(_pump_runtime_events())

    try:
        if started_execution_status == "error":
            outcome = RuntimeThreadOutcome(
                status="error",
                assistant_text="",
                error=started_execution_error or errors.public_execution_error,
                backend_thread_id=current_backend_thread_id,
                backend_turn_id=started.execution.backend_id,
            )
        elif started_execution_status == "interrupted":
            outcome = RuntimeThreadOutcome(
                status="interrupted",
                assistant_text="",
                error=errors.interrupted_error,
                backend_thread_id=current_backend_thread_id,
                backend_turn_id=started.execution.backend_id,
            )
        else:
            outcome = await await_runtime_thread_outcome(
                started,
                interrupt_event=None,
                timeout_seconds=errors.timeout_seconds,
                execution_error_message=errors.public_execution_error,
            )
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        ConnectionError,
    ):
        outcome = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=started_execution_error or errors.public_execution_error,
            backend_thread_id=current_backend_thread_id,
            backend_turn_id=started.execution.backend_id,
        )
    finally:
        if stream_task is not None:
            drain_cancel: Optional[BaseException] = None
            try:
                await asyncio.wait_for(stream_task, timeout=0.5)
            except asyncio.TimeoutError:
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
            except asyncio.CancelledError as exc:
                drain_cancel = exc
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
            if drain_cancel is not None:
                raise drain_cancel

    recovered_outcome = recover_post_completion_outcome(outcome, event_state)
    if recovered_outcome is not outcome:
        logger.warning(
            "%s runtime turn recovered from post-completion error: thread=%s turn=%s error=%s",
            surface.log_label,
            managed_thread_id,
            managed_turn_id,
            outcome.error,
        )
        outcome = recovered_outcome

    terminal_event = terminal_run_event_from_outcome(outcome, event_state)
    timeline_events.append(terminal_event)
    if on_progress_event is not None:
        try:
            await on_progress_event(terminal_event)
        except (
            RuntimeError,
            ValueError,
            TypeError,
            ConnectionError,
            OSError,
            AttributeError,
        ):
            logger.debug(
                "%s terminal progress event failed",
                surface.log_label,
                exc_info=True,
            )

    try:
        persist_turn_timeline(
            state_root,
            execution_id=managed_turn_id,
            target_kind="thread_target",
            target_id=managed_thread_id,
            repo_id=str(current_thread_row.get("repo_id") or "").strip() or None,
            resource_kind=(
                str(current_thread_row.get("resource_kind") or "").strip() or None
            ),
            resource_id=(
                str(current_thread_row.get("resource_id") or "").strip() or None
            ),
            metadata=_surface_metadata(
                started,
                surface,
                backend_thread_id=current_backend_thread_id or None,
                backend_turn_id=outcome.backend_turn_id or started.execution.backend_id,
                status=outcome.status,
            ),
            events=timeline_events,
        )
    except Exception:
        logger.exception(
            "Failed to persist %s thread timeline (thread=%s turn=%s)",
            surface.log_label,
            managed_thread_id,
            managed_turn_id,
        )

    resolved_assistant_text = (
        outcome.assistant_text or event_state.best_assistant_text()
    )
    finalized_thread = orchestration_service.get_thread_target(managed_thread_id)
    finalized_runtime_binding = get_thread_runtime_binding(
        orchestration_service,
        managed_thread_id,
    )
    resolved_backend_thread_id = (
        str(
            getattr(finalized_runtime_binding, "backend_thread_id", None)
            or getattr(finalized_thread, "backend_thread_id", None)
            or ""
        ).strip()
        or outcome.backend_thread_id
        or current_backend_thread_id
    )

    if outcome.status == "ok":
        transcript_turn_id: Optional[str] = None
        transcript_metadata = {
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "repo_id": current_thread_row.get("repo_id"),
            "workspace_root": str(started.workspace_root),
            "agent": current_thread_row.get("agent"),
            "backend_thread_id": resolved_backend_thread_id,
            "backend_turn_id": outcome.backend_turn_id,
            "model": started.request.model,
            "reasoning": started.request.reasoning,
            "status": "ok",
            "surface_kind": surface.surface_kind,
            "surface_key": surface.surface_key,
        }
        transcript_metadata.update(dict(surface.metadata))
        try:
            transcripts.write_transcript(
                turn_id=managed_turn_id,
                metadata=transcript_metadata,
                assistant_text=resolved_assistant_text,
            )
            transcript_turn_id = managed_turn_id
        except OSError as exc:
            logger.warning(
                "Failed to persist %s transcript (thread=%s turn=%s): %s",
                surface.log_label,
                managed_thread_id,
                managed_turn_id,
                exc,
            )
        try:
            finalized_execution = orchestration_service.record_execution_result(
                managed_thread_id,
                managed_turn_id,
                status="ok",
                assistant_text=resolved_assistant_text,
                error=outcome.error,
                backend_turn_id=outcome.backend_turn_id,
                transcript_turn_id=transcript_turn_id,
            )
        except KeyError:
            finalized_execution = orchestration_service.get_execution(
                managed_thread_id,
                managed_turn_id,
            )
        finalized_status = str(
            getattr(finalized_execution, "status", "") if finalized_execution else ""
        ).strip()
        if finalized_status != "ok":
            detail = errors.public_execution_error
            if finalized_status == "interrupted":
                detail = errors.interrupted_error
            elif finalized_status == "error" and finalized_execution is not None:
                detail = resolve_runtime_thread_error_detail(
                    execution_error=getattr(finalized_execution, "error", None),
                    event_error=event_state.last_error_message,
                    public_error=errors.public_execution_error,
                    timeout_error=errors.timeout_error,
                    interrupted_error=errors.interrupted_error,
                )
            return _build_finalization_result(
                status="error",
                assistant_text="",
                error=detail,
                managed_thread_id=managed_thread_id,
                managed_turn_id=managed_turn_id,
                backend_thread_id=resolved_backend_thread_id,
                token_usage=event_state.token_usage,
            )
        thread_store.update_thread_after_turn(
            managed_thread_id,
            last_turn_id=managed_turn_id,
            last_message_preview=turn_preview,
        )
        return _build_finalization_result(
            status="ok",
            assistant_text=resolved_assistant_text,
            error=None,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=resolved_backend_thread_id,
            token_usage=event_state.token_usage,
        )

    if outcome.status == "interrupted":
        try:
            orchestration_service.record_execution_interrupted(
                managed_thread_id,
                managed_turn_id,
            )
        except KeyError:
            pass
        return _build_finalization_result(
            status="interrupted",
            assistant_text="",
            error=errors.interrupted_error,
            managed_thread_id=managed_thread_id,
            managed_turn_id=managed_turn_id,
            backend_thread_id=resolved_backend_thread_id,
            token_usage=event_state.token_usage,
        )

    detail = resolve_runtime_thread_error_detail(
        outcome_error=outcome.error,
        event_error=event_state.last_error_message,
        public_error=errors.public_execution_error,
        timeout_error=errors.timeout_error,
        interrupted_error=errors.interrupted_error,
    )
    try:
        orchestration_service.record_execution_result(
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
    return _build_finalization_result(
        status="error",
        assistant_text="",
        error=detail,
        managed_thread_id=managed_thread_id,
        managed_turn_id=managed_turn_id,
        backend_thread_id=resolved_backend_thread_id,
        token_usage=event_state.token_usage,
    )
