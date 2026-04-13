from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, TypeVar

from ..car_context import CarContextProfile, normalize_car_context_profile
from ..orchestration.interfaces import ThreadExecutionStore
from ..orchestration.models import (
    BusyThreadPolicy,
    ExecutionRecord,
    MessageRequestKind,
    ThreadTarget,
)
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
    QueueDepthRequest,
    QueuedExecutionListRequest,
    RunningExecutionLookupRequest,
    ThreadActivityRecordRequest,
    ThreadBackendIdUpdateRequest,
    ThreadTargetArchiveRequest,
    ThreadTargetCreateRequest,
    ThreadTargetListRequest,
    ThreadTargetLookupRequest,
    ThreadTargetResumeRequest,
)

ResultT = TypeVar("ResultT")


class RemoteThreadExecutionStore(ThreadExecutionStore):
    """ThreadExecutionStore adapter backed by the hub control plane."""

    def __init__(
        self,
        client: HubControlPlaneClient,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds

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
        action: Callable[[], Coroutine[Any, Any, ResultT]],
    ) -> ResultT:
        def _invoke() -> ResultT:
            return asyncio.run(action())

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_invoke)
                return future.result(timeout=self._timeout_seconds)
        except FuturesTimeoutError as exc:
            raise self._hub_unavailable(
                operation=operation,
                message=f"request timed out after {self._timeout_seconds:g}s",
                details={"timeout_seconds": self._timeout_seconds},
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
        display_name: Optional[str] = None,
        backend_thread_id: Optional[str] = None,
        context_profile: Optional[CarContextProfile] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ThreadTarget:
        metadata_payload = dict(metadata or {})
        normalized_context_profile = normalize_car_context_profile(context_profile)
        if normalized_context_profile is not None:
            metadata_payload["context_profile"] = normalized_context_profile
        response = self._run(
            operation="create_thread_target",
            action=lambda: self._client.create_thread_target(
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
        return self._require_thread(
            response.thread,
            operation="create_thread_target",
        )

    def get_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        response = self._run(
            operation="get_thread_target",
            action=lambda: self._client.get_thread_target(
                ThreadTargetLookupRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.thread

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
            action=lambda: self._client.list_thread_targets(
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
        return list(response.threads)

    def resume_thread_target(
        self,
        thread_target_id: str,
        *,
        backend_thread_id: Optional[str] = None,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> Optional[ThreadTarget]:
        response = self._run(
            operation="resume_thread_target",
            action=lambda: self._client.resume_thread_target(
                ThreadTargetResumeRequest(
                    thread_target_id=thread_target_id,
                    backend_thread_id=backend_thread_id,
                    backend_runtime_instance_id=backend_runtime_instance_id,
                )
            ),
        )
        return response.thread

    def archive_thread_target(self, thread_target_id: str) -> Optional[ThreadTarget]:
        response = self._run(
            operation="archive_thread_target",
            action=lambda: self._client.archive_thread_target(
                ThreadTargetArchiveRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.thread

    def set_thread_backend_id(
        self,
        thread_target_id: str,
        backend_thread_id: Optional[str],
        *,
        backend_runtime_instance_id: Optional[str] = None,
    ) -> None:
        self._run(
            operation="set_thread_backend_id",
            action=lambda: self._client.set_thread_backend_id(
                ThreadBackendIdUpdateRequest(
                    thread_target_id=thread_target_id,
                    backend_thread_id=backend_thread_id,
                    backend_runtime_instance_id=backend_runtime_instance_id,
                )
            ),
        )

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
        queue_payload: Optional[dict[str, Any]] = None,
    ) -> ExecutionRecord:
        response = self._run(
            operation="create_execution",
            action=lambda: self._client.create_execution(
                ExecutionCreateRequest(
                    thread_target_id=thread_target_id,
                    prompt=prompt,
                    request_kind=request_kind,
                    busy_policy=busy_policy,
                    model=model,
                    reasoning=reasoning,
                    client_request_id=client_request_id,
                    queue_payload=dict(queue_payload or {}),
                )
            ),
        )
        return self._require_execution(
            response.execution,
            operation="create_execution",
        )

    def get_execution(
        self, thread_target_id: str, execution_id: str
    ) -> Optional[ExecutionRecord]:
        response = self._run(
            operation="get_execution",
            action=lambda: self._client.get_execution(
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
            action=lambda: self._client.get_running_execution(
                RunningExecutionLookupRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.execution

    def get_latest_execution(self, thread_target_id: str) -> Optional[ExecutionRecord]:
        response = self._run(
            operation="get_latest_execution",
            action=lambda: self._client.get_latest_execution(
                LatestExecutionLookupRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.execution

    def list_queued_executions(
        self, thread_target_id: str, *, limit: int = 200
    ) -> list[ExecutionRecord]:
        response = self._run(
            operation="list_queued_executions",
            action=lambda: self._client.list_queued_executions(
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
            action=lambda: self._client.get_queue_depth(
                QueueDepthRequest(thread_target_id=thread_target_id)
            ),
        )
        return response.queue_depth

    def cancel_queued_execution(self, thread_target_id: str, execution_id: str) -> bool:
        response = self._run(
            operation="cancel_queued_execution",
            action=lambda: self._client.cancel_queued_execution(
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
            action=lambda: self._client.promote_queued_execution(
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
            action=lambda: self._client.claim_next_queued_execution(
                ExecutionClaimNextRequest(thread_target_id=thread_target_id)
            ),
        )
        if response.execution is None:
            return None
        return response.execution, dict(response.queue_payload)

    def set_execution_backend_id(
        self, execution_id: str, backend_turn_id: Optional[str]
    ) -> None:
        self._run(
            operation="set_execution_backend_id",
            action=lambda: self._client.set_execution_backend_id(
                ExecutionBackendIdUpdateRequest(
                    execution_id=execution_id,
                    backend_turn_id=backend_turn_id,
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
        error: Optional[str] = None,
        backend_turn_id: Optional[str] = None,
        transcript_turn_id: Optional[str] = None,
    ) -> ExecutionRecord:
        response = self._run(
            operation="record_execution_result",
            action=lambda: self._client.record_execution_result(
                ExecutionResultRecordRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                    status=status,
                    assistant_text=assistant_text,
                    error=error,
                    backend_turn_id=backend_turn_id,
                    transcript_turn_id=transcript_turn_id,
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
            action=lambda: self._client.record_execution_interrupted(
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
            action=lambda: self._client.cancel_queued_executions(
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
            action=lambda: self._client.record_thread_activity(
                ThreadActivityRecordRequest(
                    thread_target_id=thread_target_id,
                    execution_id=execution_id,
                    message_preview=message_preview,
                )
            ),
        )


__all__ = ["RemoteThreadExecutionStore"]
