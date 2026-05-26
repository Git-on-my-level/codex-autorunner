from __future__ import annotations

import asyncio
import inspect
import logging
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Callable, Coroutine, Mapping, Optional, TypeVar

from ..car_context import CarContextProfile, normalize_car_context_profile
from ..domain.refs import ScopeRef
from ..logging_utils import log_event
from ..orchestration.interfaces import ThreadExecutionStore
from ..orchestration.models import (
    BusyThreadPolicy,
    ExecutionRecord,
    MessageRequestKind,
    ThreadTarget,
    owner_fields_from_scope_ref,
)
from ..orchestration.runtime_bindings import RuntimeThreadBinding
from ..orchestration.turn_assistant_output import TurnAssistantOutput
from ..orchestration.turn_execution_contract import (
    TurnExecutionRecord,
    TurnExecutionRequest,
)
from ..orchestration.turn_execution_storage import (
    build_turn_execution_record_from_storage,
)
from .background_runner import BackgroundRunnerSaturated, BoundedBackgroundRunner
from .client import HubControlPlaneClient
from .errors import HubControlPlaneError
from .models import (
    ExecutionBackendIdUpdateRequest,
    ExecutionCancelAllRequest,
    ExecutionCancelRequest,
    ExecutionClaimNextRequest,
    ExecutionCreateRequest,
    ExecutionInterruptRecordRequest,
    ExecutionLookupRequest,
    ExecutionPromoteRequest,
    ExecutionResultRecordRequest,
    LatestExecutionLookupRequest,
    PreviousCompletedExecutionLookupRequest,
    QueueDepthRequest,
    QueuedExecutionListRequest,
    RunningExecutionLookupRequest,
    RunningThreadTargetIdsRequest,
    ThreadActivityRecordRequest,
    ThreadBackendBindingUpdateRequest,
    ThreadTargetArchiveRequest,
    ThreadTargetCreateRequest,
    ThreadTargetListRequest,
    ThreadTargetLookupRequest,
    ThreadTargetResumeRequest,
)

ResultT = TypeVar("ResultT")
_LOGGER = logging.getLogger(__name__)
_THREAD_TARGET_CREATE_TIMEOUT_SECONDS = 30.0
_EXECUTION_BACKEND_ID_TIMEOUT_SECONDS = 30.0
_EXECUTION_RESULT_TIMEOUT_SECONDS = 30.0
_EXECUTION_CREATE_RETRY_DEADLINE_SECONDS = 180.0
_EXECUTION_CREATE_RETRY_INITIAL_DELAY_SECONDS = 1.0
_EXECUTION_CREATE_RETRY_MAX_DELAY_SECONDS = 10.0
_BACKGROUND_RUNNER = BoundedBackgroundRunner(
    max_workers=8,
    saturation_wait_seconds=0.05,
    thread_name_prefix="hub-execution",
)


def _assistant_output_payload(value: Any) -> Optional[dict[str, Any]]:
    if isinstance(value, TurnAssistantOutput):
        return value.to_durable_dict()
    if isinstance(value, dict):
        return dict(value)
    return None


def _runtime_payload(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return dict(payload)
    return None


class RemoteThreadExecutionStore(ThreadExecutionStore):
    """ThreadExecutionStore adapter backed by the hub control plane."""

    def __init__(
        self,
        client: HubControlPlaneClient,
        *,
        timeout_seconds: float = 10.0,
        background_runner: Optional[BoundedBackgroundRunner] = None,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._background_runner = background_runner or _BACKGROUND_RUNNER
        self._thread_cache: dict[str, ThreadTarget] = {}
        self._turn_request_cache: dict[tuple[str, str], TurnExecutionRequest] = {}
        self._turn_record_cache: dict[tuple[str, str], TurnExecutionRecord] = {}

    def _hub_unavailable(
        self,
        *,
        operation: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> HubControlPlaneError:
        payload = {"operation": operation}
        if isinstance(details, dict):
            payload.update(details)
        return HubControlPlaneError(
            "hub_unavailable",
            f"Hub control-plane unavailable during {operation}: {message}",
            retryable=True,
            details=payload,
        )

    def _run(
        self,
        *,
        operation: str,
        action: Callable[[HubControlPlaneClient], Coroutine[Any, Any, ResultT]],
        timeout_seconds: Optional[float] = None,
    ) -> ResultT:
        def _invoke() -> ResultT:
            background_client = self._client
            clone = getattr(type(self._client), "clone_for_background_loop", None)
            if callable(clone) and not inspect.iscoroutinefunction(clone):
                cloned_client = clone(self._client)
                if cloned_client is not None and not inspect.isawaitable(cloned_client):
                    background_client = cloned_client

            async def _run_action() -> ResultT:
                try:
                    return await action(background_client)
                finally:
                    close = getattr(background_client, "aclose", None)
                    if callable(close) and background_client is not self._client:
                        result = close()
                        if inspect.isawaitable(result):
                            await result

            return asyncio.run(_run_action())

        effective_timeout_seconds = (
            self._timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )

        try:
            future = self._background_runner.submit(
                _invoke,
                timeout_seconds=effective_timeout_seconds,
            )
        except BackgroundRunnerSaturated as exc:
            raise self._hub_unavailable(
                operation=operation,
                message="background worker pool saturated",
                details={
                    "max_workers": exc.max_workers,
                    "acquire_timeout_seconds": exc.acquire_timeout_seconds,
                },
            ) from exc
        try:
            return future.result(timeout=effective_timeout_seconds)
        except FuturesTimeoutError as exc:
            future.cancel()
            raise self._hub_unavailable(
                operation=operation,
                message=f"request timed out after {effective_timeout_seconds:g}s",
                details={"timeout_seconds": effective_timeout_seconds},
            ) from exc
        except HubControlPlaneError as exc:
            if exc.code in {"hub_unavailable", "transport_failure"}:
                raise self._hub_unavailable(
                    operation=operation,
                    message=str(exc),
                    details={
                        "cause_code": exc.code,
                        **dict(exc.details),
                    },
                ) from exc
            raise
        except (ConnectionError, OSError) as exc:
            raise self._hub_unavailable(
                operation=operation,
                message=str(exc) or exc.__class__.__name__,
                details={"cause_type": exc.__class__.__name__},
            ) from exc

    def _run_create_execution_with_retry(
        self,
        *,
        action: Callable[[HubControlPlaneClient], Coroutine[Any, Any, ResultT]],
    ) -> ResultT:
        deadline = time.monotonic() + _EXECUTION_CREATE_RETRY_DEADLINE_SECONDS
        delay = _EXECUTION_CREATE_RETRY_INITIAL_DELAY_SECONDS
        attempt = 0
        last_error: HubControlPlaneError | None = None
        while True:
            attempt += 1
            try:
                return self._run(operation="create_execution", action=action)
            except HubControlPlaneError as exc:
                if not self._should_retry_create_execution(exc):
                    raise
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    details = dict(exc.details)
                    details.update(
                        {
                            "attempts": attempt,
                            "retry_deadline_seconds": (
                                _EXECUTION_CREATE_RETRY_DEADLINE_SECONDS
                            ),
                        }
                    )
                    raise self._hub_unavailable(
                        operation="create_execution",
                        message=(
                            f"retry deadline exhausted after {attempt} attempts: {exc}"
                        ),
                        details=details,
                    ) from exc
                sleep_seconds = min(delay, remaining)
                log_event(
                    _LOGGER,
                    logging.WARNING,
                    "hub_control_plane.create_execution.retrying",
                    attempt=attempt,
                    delay_seconds=sleep_seconds,
                    remaining_deadline_seconds=max(0.0, remaining),
                    cause_code=exc.details.get("cause_code", exc.code),
                    wrapped_code=exc.code,
                    cause_message=str(exc),
                )
                time.sleep(sleep_seconds)
                delay = min(
                    delay * 2.0,
                    _EXECUTION_CREATE_RETRY_MAX_DELAY_SECONDS,
                )
        assert last_error is not None  # pragma: no cover
        raise last_error

    @staticmethod
    def _should_retry_create_execution(exc: HubControlPlaneError) -> bool:
        if not bool(getattr(exc, "retryable", False)):
            return False
        return exc.code in {"hub_unavailable", "transport_failure"}

    @staticmethod
    def _require_thread(
        thread: Optional[ThreadTarget], *, operation: str
    ) -> ThreadTarget:
        if thread is None:
            raise HubControlPlaneError(
                "hub_rejected",
                f"Hub control-plane returned no thread for {operation}",
                retryable=False,
                details={"operation": operation},
            )
        return thread

    @staticmethod
    def _require_execution(
        execution: Optional[ExecutionRecord], *, operation: str
    ) -> ExecutionRecord:
        if execution is None:
            raise HubControlPlaneError(
                "hub_rejected",
                f"Hub control-plane returned no execution for {operation}",
                retryable=False,
                details={"operation": operation},
            )
        return execution

    def create_thread_target(
        self,
        agent_id: str,
        workspace_root: Path,
        *,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        scope: Optional[ScopeRef] = None,
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        if scope is not None:
            if (
                repo_id is not None
                or resource_kind is not None
                or resource_id is not None
            ):
                raise ValueError(
                    "scope cannot be combined with repo_id/resource_kind/resource_id"
                )
            scope_repo_id, scope_resource_kind, scope_resource_id, scope_workspace = (
                owner_fields_from_scope_ref(scope)
            )
            repo_id = scope_repo_id
            resource_kind = scope_resource_kind
            resource_id = scope_resource_id
            if scope_workspace is not None:
                workspace_root = Path(scope_workspace)
        metadata_payload = dict(metadata or {})
        normalized_context_profile = normalize_car_context_profile(context_profile)
        if normalized_context_profile is not None:
            metadata_payload["context_profile"] = normalized_context_profile
        response = self._run(
            operation="create_thread_target",
            timeout_seconds=_THREAD_TARGET_CREATE_TIMEOUT_SECONDS,
            action=lambda client: client.create_thread_target(
                ThreadTargetCreateRequest(
                    agent_id=agent_id,
                    workspace_root=str(workspace_root),
                    repo_id=repo_id,
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    display_name=display_name,
                    backend_thread_id=backend_thread_id,
                    metadata=metadata_payload,
                )
            ),
        )
        thread = self._require_thread(
            response.thread,
            operation="create_thread_target",
        )
        self._thread_cache[thread.thread_target_id] = thread
        return thread

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        response = self._run(
            operation="get_thread_target",
            action=lambda client: client.get_thread_target(
                ThreadTargetLookupRequest(thread_target_id=thread_target_id)
            ),
        )
        if response.thread is not None:
            self._thread_cache[response.thread.thread_target_id] = response.thread
        return response.thread

    def get_thread_runtime_binding(
        self, thread_target_id: str
    ) -> Optional[RuntimeThreadBinding]:
        thread = self.get_thread_target(thread_target_id)
        if thread is None:
            return None
        backend_thread_id = str(getattr(thread, "backend_thread_id", "") or "").strip()
        backend_runtime_instance_id = str(
            getattr(thread, "backend_runtime_instance_id", "") or ""
        ).strip()
        backend_binding_state = str(
            getattr(thread, "backend_binding_state", "") or ""
        ).strip()
        backend_binding_state_reason = str(
            getattr(thread, "backend_binding_state_reason", "") or ""
        ).strip()
        if not backend_thread_id and not backend_runtime_instance_id:
            return None
        return RuntimeThreadBinding(
            backend_thread_id=backend_thread_id or None,
            backend_runtime_instance_id=backend_runtime_instance_id or None,
            binding_state=backend_binding_state or "bound",
            state_reason=backend_binding_state_reason or None,
        )

    def list_thread_targets(
        self,
        *,
        agent_id: Optional[str] = None,
        lifecycle_status: Optional[str] = None,
        runtime_status: Optional[str] = None,
        repo_id: Optional[str] = None,
        resource_kind: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[ThreadTarget]:
        response = self._run(
            operation="list_thread_targets",
            action=lambda client: client.list_thread_targets(
                ThreadTargetListRequest(
                    agent_id=agent_id,
                    lifecycle_status=lifecycle_status,
                    runtime_status=runtime_status,
                    repo_id=repo_id,
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    limit=limit,
                )
            ),
        )
        threads = list(response.threads)
        for thread in threads:
            self._thread_cache[thread.thread_target_id] = thread
        return threads

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] = None,
        backend_runtime_instance_id: Optional[str] = None,
        binding_state: Optional[str] = None,
        state_reason: Optional[str] = None,
    ) -> Optional[ThreadTarget]:
        response = self._run(
            operation="resume_thread_target",
            action=lambda client: client.resume_thread_target(
                ThreadTargetResumeRequest(
                    thread_target_id=thread_target_id,
                    backend_thread_id=backend_thread_id,
                    backend_runtime_instance_id=backend_runtime_instance_id,
                    backend_binding_state=binding_state,
                    backend_binding_state_reason=state_reason,
                )
            ),
        )
        if response.thread is not None:
            self._thread_cache[response.thread.thread_target_id] = response.thread
        return response.thread

    def archive_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        response = self._run(
            operation="archive_thread_target",
            action=lambda client: client.archive_thread_target(
                ThreadTargetArchiveRequest(thread_target_id=thread_target_id)
            ),
        )
        if response.thread is not None:
            self._thread_cache[response.thread.thread_target_id] = response.thread
        return response.thread

    def set_thread_backend_binding(
        self,
        thread_target_id: str,
        backend_thread_id: Optional[str],
        *,
        backend_runtime_instance_id: Optional[str] = None,
        binding_state: str = "bound",
        state_reason: Optional[str] = None,
    ) -> None:
        self._run(
            operation="set_thread_backend_binding",
            action=lambda client: client.set_thread_backend_binding(
                ThreadBackendBindingUpdateRequest(
                    thread_target_id=thread_target_id,
                    backend_thread_id=backend_thread_id,
                    backend_runtime_instance_id=backend_runtime_instance_id,
                    backend_binding_state=binding_state,
                    backend_binding_state_reason=state_reason,
                    backend_thread_id_provided=True,
                    backend_runtime_instance_id_provided=True,
                    backend_binding_state_provided=True,
                    backend_binding_state_reason_provided=True,
                )
            ),
        )

    def mark_thread_runtime_binding_state(
        self,
        thread_target_id: str,
        *,
        binding_state: str,
        state_reason: Optional[str] = None,
    ) -> Optional[RuntimeThreadBinding]:
        current = self.get_thread_runtime_binding(thread_target_id)
        if current is None or current.backend_thread_id is None:
            return current
        self.set_thread_backend_binding(
            thread_target_id,
            current.backend_thread_id,
            backend_runtime_instance_id=current.backend_runtime_instance_id,
            binding_state=binding_state,
            state_reason=state_reason,
        )
        return self.get_thread_runtime_binding(thread_target_id)

    def create_execution(
        self,
        thread_target_id: str,
        *,
        prompt: str,
        request_kind: MessageRequestKind = "message",
        busy_policy: BusyThreadPolicy = "reject",
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
        client_request_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        queue_payload: Optional[dict[str, Any]] = None,
        turn_request: Optional[TurnExecutionRequest] = None,
    ) -> ExecutionRecord:
        if turn_request is None:
            raise RuntimeError(
                "remote execution creation requires a canonical TurnExecutionRequest"
            )
        response = self._run_create_execution_with_retry(
            action=lambda client: client.create_execution(
                ExecutionCreateRequest(
                    thread_target_id=thread_target_id,
                    prompt=prompt,
                    request_kind=request_kind,
                    busy_policy=busy_policy,
                    model=model,
                    reasoning=reasoning,
                    client_request_id=client_request_id,
                    metadata=dict(metadata or {}),
                    queue_payload=dict(queue_payload or {}),
                    turn_request=turn_request,
                )
            ),
        )
        execution = self._require_execution(
            response.execution,
            operation="create_execution",
        )
        thread = self._thread_cache.get(thread_target_id)
        if thread is None:
            thread = self.get_thread_target(thread_target_id)
        if thread is not None:
            request = turn_request
            record = build_turn_execution_record_from_storage(
                execution=execution.to_dict(),
                thread=thread.to_dict(),
                request=request,
                queue_item={},
            )
            cache_key = (thread_target_id, execution.execution_id)
            self._turn_request_cache[cache_key] = request
            self._turn_record_cache[cache_key] = record
        return execution

    def get_turn_execution_request(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[TurnExecutionRequest]:
        return self._turn_request_cache.get((thread_target_id, execution_id))

    def get_turn_execution_record(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[TurnExecutionRecord]:
        return self._turn_record_cache.get((thread_target_id, execution_id))

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        response = self._run(
            operation="get_execution",
            action=lambda client: client.get_execution(
                ExecutionLookupRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                )
            ),
        )
        return response.execution

    def get_running_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        response = self._run(
            operation="get_running_execution",
            action=lambda client: client.get_running_execution(
                RunningExecutionLookupRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.execution

    def list_thread_ids_with_running_executions(
        self, *, limit: Optional[int] = 200
    ) -> list[str]:
        response = self._run(
            operation="list_thread_ids_with_running_executions",
            action=lambda client: client.list_thread_target_ids_with_running_executions(
                RunningThreadTargetIdsRequest(limit=limit),
            ),
        )
        return list(response.thread_target_ids)

    def get_latest_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        response = self._run(
            operation="get_latest_execution",
            action=lambda client: client.get_latest_execution(
                LatestExecutionLookupRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.execution

    def get_previous_completed_execution(
        self,
        thread_target_id: str,
        *,
        exclude_execution_id: Optional[str] = None,
    ) -> Optional[ExecutionRecord]:
        response = self._run(
            operation="get_previous_completed_execution",
            action=lambda client: client.get_previous_completed_execution(
                PreviousCompletedExecutionLookupRequest(
                    thread_target_id=thread_target_id,
                    exclude_execution_id=exclude_execution_id,
                )
            ),
        )
        return response.execution

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]:
        response = self._run(
            operation="list_queued_executions",
            action=lambda client: client.list_queued_executions(
                QueuedExecutionListRequest(
                    thread_target_id=thread_target_id,
                    limit=limit,
                )
            ),
        )
        return list(response.executions)

    def get_queue_depth(self, thread_target_id: str) -> int:
        response = self._run(
            operation="get_queue_depth",
            action=lambda client: client.get_queue_depth(
                QueueDepthRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.queue_depth

    def cancel_queued_execution(self, thread_target_id: str, execution_id: str) -> bool:
        response = self._run(
            operation="cancel_queued_execution",
            action=lambda client: client.cancel_queued_execution(
                ExecutionCancelRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                )
            ),
        )
        return response.cancelled

    def promote_queued_execution(
        self, thread_target_id: str, execution_id: str
    ) -> bool:
        response = self._run(
            operation="promote_queued_execution",
            action=lambda client: client.promote_queued_execution(
                ExecutionPromoteRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                )
            ),
        )
        return response.promoted

    def claim_next_queued_execution(
        self, thread_target_id: str
    ) -> Optional[tuple[ExecutionRecord, dict[str, Any]]]:
        response = self._run(
            operation="claim_next_queued_execution",
            action=lambda client: client.claim_next_queued_execution(
                ExecutionClaimNextRequest(thread_target_id=thread_target_id)
            ),
        )
        if response.execution is None:
            return None
        return response.execution, dict(response.queue_payload)

    def set_execution_backend_id(
        self,
        execution_id: str,
        backend_turn_id: Optional[str],
        *,
        confirmed_start: bool = True,
    ) -> None:
        self._run(
            operation="set_execution_backend_id",
            timeout_seconds=_EXECUTION_BACKEND_ID_TIMEOUT_SECONDS,
            action=lambda client: client.set_execution_backend_id(
                ExecutionBackendIdUpdateRequest(
                    execution_id=execution_id,
                    backend_turn_id=backend_turn_id,
                    confirmed_start=confirmed_start,
                )
            ),
        )

    def record_execution_result(
        self,
        thread_target_id: str,
        execution_id: str,
        *,
        status: str,
        assistant_text: Optional[str] = None,
        assistant_output: Optional[Any] = None,
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
        effective_runtime: Optional[Any] = None,
    ) -> ExecutionRecord:
        response = self._run(
            operation="record_execution_result",
            timeout_seconds=_EXECUTION_RESULT_TIMEOUT_SECONDS,
            action=lambda client: client.record_execution_result(
                ExecutionResultRecordRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                    status=status,
                    assistant_text=assistant_text,
                    assistant_output=_assistant_output_payload(assistant_output),
                    error=error,
                    backend_turn_id=backend_turn_id,
                    transcript_turn_id=transcript_turn_id,
                    effective_runtime=_runtime_payload(effective_runtime),
                )
            ),
        )
        return self._require_execution(
            response.execution,
            operation="record_execution_result",
        )

    def record_execution_interrupted(
        self, thread_target_id: str, execution_id: str
    ) -> ExecutionRecord:
        response = self._run(
            operation="record_execution_interrupted",
            action=lambda client: client.record_execution_interrupted(
                ExecutionInterruptRecordRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                )
            ),
        )
        return self._require_execution(
            response.execution,
            operation="record_execution_interrupted",
        )

    def cancel_queued_executions(self, thread_target_id: str) -> int:
        response = self._run(
            operation="cancel_queued_executions",
            action=lambda client: client.cancel_queued_executions(
                ExecutionCancelAllRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.cancelled_count

    def record_thread_activity(
        self,
        thread_target_id: str,
        *,
        execution_id: Optional[str],
        message_preview: Optional[str],
    ) -> None:
        self._run(
            operation="record_thread_activity",
            action=lambda client: client.record_thread_activity(
                ThreadActivityRecordRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                    message_preview=message_preview,
                )
            ),
        )


__all__ = ["RemoteThreadExecutionStore"]
