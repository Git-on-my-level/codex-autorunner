from __future__ import annotations

import asyncio
import contextlib
from types import TracebackType
from typing import Any, Mapping, Optional

import httpx

from .client import HubControlPlaneClient
from .errors import HubControlPlaneError, HubControlPlaneErrorInfo
from .models import (
    AgentWorkspaceListRequest,
    AgentWorkspaceListResponse,
    AgentWorkspaceLookupRequest,
    AgentWorkspaceResponse,
    AutomationRequest,
    AutomationResult,
    ExecutionBackendIdUpdateRequest,
    ExecutionCancelAllRequest,
    ExecutionCancelAllResponse,
    ExecutionCancelRequest,
    ExecutionCancelResponse,
    ExecutionClaimNextRequest,
    ExecutionClaimNextResponse,
    ExecutionColdTraceFinalizeRequest,
    ExecutionColdTraceFinalizeResponse,
    ExecutionCreateRequest,
    ExecutionInterruptRecordRequest,
    ExecutionListResponse,
    ExecutionLookupRequest,
    ExecutionPromoteRequest,
    ExecutionPromoteResponse,
    ExecutionResponse,
    ExecutionResultRecordRequest,
    ExecutionTimelinePersistRequest,
    ExecutionTimelinePersistResponse,
    HandshakeRequest,
    HandshakeResponse,
    LatestExecutionLookupRequest,
    NotificationContinuationBindRequest,
    NotificationDeliveryMarkRequest,
    NotificationLookupRequest,
    NotificationRecordResponse,
    NotificationReplyTargetLookupRequest,
    PmaSnapshotResponse,
    QueueDepthRequest,
    QueueDepthResponse,
    QueuedExecutionListRequest,
    RunningExecutionLookupRequest,
    SurfaceBindingListRequest,
    SurfaceBindingListResponse,
    SurfaceBindingLookupRequest,
    SurfaceBindingResponse,
    SurfaceBindingUpsertRequest,
    ThreadActivityRecordRequest,
    ThreadBackendIdUpdateRequest,
    ThreadCompactSeedUpdateRequest,
    ThreadTargetArchiveRequest,
    ThreadTargetCreateRequest,
    ThreadTargetListRequest,
    ThreadTargetListResponse,
    ThreadTargetLookupRequest,
    ThreadTargetResponse,
    ThreadTargetResumeRequest,
    TranscriptHistoryRequest,
    TranscriptHistoryResponse,
    TranscriptWriteRequest,
    TranscriptWriteResponse,
    WorkspaceSetupCommandRequest,
    WorkspaceSetupCommandResult,
)

_USE_CLIENT_DEFAULT_TIMEOUT = object()


def _normalize_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("base_url is required")
    return normalized


class HttpHubControlPlaneClient(HubControlPlaneClient):
    """HTTP transport over the hub-owned shared-state control plane."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 10.0,
        headers: Mapping[str, str] | None = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._timeout = timeout
        self._headers = dict(headers or {})
        self._owns_client = http_client is None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._http_client: Optional[httpx.AsyncClient] = (
            self._build_http_client() if http_client is None else http_client
        )
        with contextlib.suppress(RuntimeError):
            self._client_loop = asyncio.get_running_loop()

    def _build_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=dict(self._headers),
        )

    async def _get_http_client(self) -> httpx.AsyncClient:
        current_loop = asyncio.get_running_loop()
        client = self._http_client
        if client is None or (
            self._owns_client and getattr(client, "is_closed", False)
        ):
            client = self._build_http_client()
            self._http_client = client
            self._client_loop = current_loop
            return client
        if (
            self._owns_client
            and self._client_loop is not None
            and self._client_loop is not current_loop
        ):
            stale_client = client
            client = self._build_http_client()
            self._http_client = client
            self._client_loop = current_loop
            with contextlib.suppress(RuntimeError, OSError):
                await stale_client.aclose()
            return client
        if self._owns_client and self._client_loop is None:
            self._client_loop = current_loop
        return client

    def clone_for_background_loop(self) -> "HttpHubControlPlaneClient":
        """Return a client copy with its own AsyncClient for a separate event loop."""
        return HttpHubControlPlaneClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._headers,
        )

    async def __aenter__(self) -> "HttpHubControlPlaneClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if not self._owns_client:
            return
        client = self._http_client
        self._client_loop = None
        if client is None:
            return
        with contextlib.suppress(RuntimeError, OSError):
            await client.aclose()

    @staticmethod
    def _format_transport_error(exc: httpx.RequestError) -> str:
        message = str(exc).strip()
        if message:
            return message
        return exc.__class__.__name__

    async def _request(
        self,
        *,
        method: str,
        path: str,
        json_payload: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: object = _USE_CLIENT_DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "json": dict(json_payload) if json_payload is not None else None,
            "params": dict(params) if params is not None else None,
        }
        if timeout is not _USE_CLIENT_DEFAULT_TIMEOUT:
            request_kwargs["timeout"] = timeout
        try:
            response = await (await self._get_http_client()).request(
                method,
                path,
                **request_kwargs,
            )
        except httpx.RequestError as exc:
            raise HubControlPlaneError(
                "transport_failure",
                "Hub control-plane transport request failed: "
                f"{self._format_transport_error(exc)}",
                details={"path": path, "method": method},
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise HubControlPlaneError(
                "protocol_failure",
                "Hub control-plane response was not valid JSON",
                retryable=False,
                details={"path": path, "status_code": response.status_code},
            ) from exc
        if not isinstance(payload, dict):
            raise HubControlPlaneError(
                "protocol_failure",
                "Hub control-plane response payload was not a JSON object",
                retryable=False,
                details={"path": path, "status_code": response.status_code},
            )
        if response.is_success:
            return payload
        error_payload = payload.get("error")
        if isinstance(error_payload, Mapping):
            info = HubControlPlaneErrorInfo.from_mapping(error_payload)
            raise HubControlPlaneError.from_info(info)
        raise HubControlPlaneError(
            "protocol_failure",
            f"Hub control-plane request failed with status {response.status_code}",
            retryable=False,
            details={"path": path, "status_code": response.status_code},
        )

    async def _request_no_content(
        self,
        *,
        method: str,
        path: str,
        json_payload: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        timeout: object = _USE_CLIENT_DEFAULT_TIMEOUT,
    ) -> None:
        request_kwargs: dict[str, Any] = {
            "json": dict(json_payload) if json_payload is not None else None,
            "params": dict(params) if params is not None else None,
        }
        if timeout is not _USE_CLIENT_DEFAULT_TIMEOUT:
            request_kwargs["timeout"] = timeout
        try:
            response = await (await self._get_http_client()).request(
                method,
                path,
                **request_kwargs,
            )
        except httpx.RequestError as exc:
            raise HubControlPlaneError(
                "transport_failure",
                "Hub control-plane transport request failed: "
                f"{self._format_transport_error(exc)}",
                details={"path": path, "method": method},
            ) from exc
        if response.is_success:
            return
        try:
            payload = response.json()
        except ValueError as exc:
            raise HubControlPlaneError(
                "protocol_failure",
                "Hub control-plane response was not valid JSON",
                retryable=False,
                details={"path": path, "status_code": response.status_code},
            ) from exc
        error_payload = payload.get("error") if isinstance(payload, Mapping) else None
        if isinstance(error_payload, Mapping):
            info = HubControlPlaneErrorInfo.from_mapping(error_payload)
            raise HubControlPlaneError.from_info(info)
        raise HubControlPlaneError(
            "protocol_failure",
            f"Hub control-plane request failed with status {response.status_code}",
            retryable=False,
            details={"path": path, "status_code": response.status_code},
        )

    async def handshake(self, request: HandshakeRequest) -> HandshakeResponse:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/handshake",
            json_payload=request.to_dict(),
        )
        return HandshakeResponse.from_mapping(payload)

    async def get_notification_record(
        self, request: NotificationLookupRequest
    ) -> NotificationRecordResponse:
        payload = await self._request(
            method="GET",
            path=f"/hub/api/control-plane/notifications/{request.notification_id}",
        )
        return NotificationRecordResponse.from_mapping(payload)

    async def get_notification_reply_target(
        self, request: NotificationReplyTargetLookupRequest
    ) -> NotificationRecordResponse:
        payload = await self._request(
            method="GET",
            path="/hub/api/control-plane/notification-reply-target",
            params=request.to_dict(),
        )
        return NotificationRecordResponse.from_mapping(payload)

    async def bind_notification_continuation(
        self, request: NotificationContinuationBindRequest
    ) -> NotificationRecordResponse:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/notifications/continuation",
            json_payload=request.to_dict(),
        )
        return NotificationRecordResponse.from_mapping(payload)

    async def mark_notification_delivered(
        self, request: NotificationDeliveryMarkRequest
    ) -> NotificationRecordResponse:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/notifications/delivery",
            json_payload=request.to_dict(),
        )
        return NotificationRecordResponse.from_mapping(payload)

    async def get_surface_binding(
        self, request: SurfaceBindingLookupRequest
    ) -> SurfaceBindingResponse:
        payload = await self._request(
            method="GET",
            path="/hub/api/control-plane/surface-bindings",
            params=request.to_dict(),
        )
        return SurfaceBindingResponse.from_mapping(payload)

    async def upsert_surface_binding(
        self, request: SurfaceBindingUpsertRequest
    ) -> SurfaceBindingResponse:
        payload = await self._request(
            method="PUT",
            path="/hub/api/control-plane/surface-bindings",
            json_payload=request.to_dict(),
        )
        return SurfaceBindingResponse.from_mapping(payload)

    async def list_surface_bindings(
        self, request: SurfaceBindingListRequest
    ) -> SurfaceBindingListResponse:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/surface-bindings/query",
            json_payload=request.to_dict(),
        )
        return SurfaceBindingListResponse.from_mapping(payload)

    async def get_thread_target(
        self, request: ThreadTargetLookupRequest
    ) -> ThreadTargetResponse:
        payload = await self._request(
            method="GET",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}",
        )
        return ThreadTargetResponse.from_mapping(payload)

    async def list_thread_targets(
        self, request: ThreadTargetListRequest
    ) -> ThreadTargetListResponse:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/thread-targets/query",
            json_payload=request.to_dict(),
        )
        return ThreadTargetListResponse.from_mapping(payload)

    async def resume_thread_target(
        self, request: ThreadTargetResumeRequest
    ) -> ThreadTargetResponse:
        payload = await self._request(
            method="POST",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}/resume",
            json_payload=request.to_dict(),
        )
        return ThreadTargetResponse.from_mapping(payload)

    async def archive_thread_target(
        self, request: ThreadTargetArchiveRequest
    ) -> ThreadTargetResponse:
        payload = await self._request(
            method="POST",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}/archive",
            json_payload=request.to_dict(),
        )
        return ThreadTargetResponse.from_mapping(payload)

    async def set_thread_backend_id(
        self, request: ThreadBackendIdUpdateRequest
    ) -> None:
        await self._request_no_content(
            method="POST",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}/backend-id",
            json_payload=request.to_dict(),
        )

    async def record_thread_activity(
        self, request: ThreadActivityRecordRequest
    ) -> None:
        await self._request_no_content(
            method="POST",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}/activity",
            json_payload=request.to_dict(),
        )

    async def update_thread_compact_seed(
        self, request: ThreadCompactSeedUpdateRequest
    ) -> ThreadTargetResponse:
        payload = await self._request(
            method="POST",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}/compact-seed",
            json_payload=request.to_dict(),
        )
        return ThreadTargetResponse.from_mapping(payload)

    async def create_thread_target(
        self, request: ThreadTargetCreateRequest
    ) -> ThreadTargetResponse:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/thread-targets",
            json_payload=request.to_dict(),
        )
        return ThreadTargetResponse.from_mapping(payload)

    async def create_execution(
        self, request: ExecutionCreateRequest
    ) -> ExecutionResponse:
        payload = await self._request(
            method="POST",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}/executions",
            json_payload=request.to_dict(),
        )
        return ExecutionResponse.from_mapping(payload)

    async def get_execution(self, request: ExecutionLookupRequest) -> ExecutionResponse:
        payload = await self._request(
            method="GET",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/{request.execution_id}"
            ),
        )
        return ExecutionResponse.from_mapping(payload)

    async def get_running_execution(
        self, request: RunningExecutionLookupRequest
    ) -> ExecutionResponse:
        payload = await self._request(
            method="GET",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/running"
            ),
        )
        return ExecutionResponse.from_mapping(payload)

    async def get_latest_execution(
        self, request: LatestExecutionLookupRequest
    ) -> ExecutionResponse:
        payload = await self._request(
            method="GET",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/latest"
            ),
        )
        return ExecutionResponse.from_mapping(payload)

    async def list_queued_executions(
        self, request: QueuedExecutionListRequest
    ) -> ExecutionListResponse:
        payload = await self._request(
            method="GET",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/queued"
            ),
            params={"limit": request.limit},
        )
        return ExecutionListResponse.from_mapping(payload)

    async def get_queue_depth(self, request: QueueDepthRequest) -> QueueDepthResponse:
        payload = await self._request(
            method="GET",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/queue-depth"
            ),
        )
        return QueueDepthResponse.from_mapping(payload)

    async def cancel_queued_execution(
        self, request: ExecutionCancelRequest
    ) -> ExecutionCancelResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/{request.execution_id}/cancel"
            ),
        )
        return ExecutionCancelResponse.from_mapping(payload)

    async def promote_queued_execution(
        self, request: ExecutionPromoteRequest
    ) -> ExecutionPromoteResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/{request.execution_id}/promote"
            ),
        )
        return ExecutionPromoteResponse.from_mapping(payload)

    async def record_execution_result(
        self, request: ExecutionResultRecordRequest
    ) -> ExecutionResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/{request.execution_id}/result"
            ),
            json_payload=request.to_dict(),
        )
        return ExecutionResponse.from_mapping(payload)

    async def record_execution_interrupted(
        self, request: ExecutionInterruptRecordRequest
    ) -> ExecutionResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/{request.execution_id}/interrupt"
            ),
        )
        return ExecutionResponse.from_mapping(payload)

    async def cancel_queued_executions(
        self, request: ExecutionCancelAllRequest
    ) -> ExecutionCancelAllResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/cancel-all"
            ),
        )
        return ExecutionCancelAllResponse.from_mapping(payload)

    async def set_execution_backend_id(
        self, request: ExecutionBackendIdUpdateRequest
    ) -> None:
        await self._request_no_content(
            method="PUT",
            path=(
                "/hub/api/control-plane/thread-executions/"
                f"{request.execution_id}/backend-id"
            ),
            json_payload=request.to_dict(),
        )

    async def claim_next_queued_execution(
        self, request: ExecutionClaimNextRequest
    ) -> ExecutionClaimNextResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-targets/"
                f"{request.thread_target_id}/executions/claim-next"
            ),
        )
        return ExecutionClaimNextResponse.from_mapping(payload)

    async def persist_execution_timeline(
        self, request: ExecutionTimelinePersistRequest
    ) -> ExecutionTimelinePersistResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-executions/"
                f"{request.execution_id}/timeline"
            ),
            json_payload=request.to_dict(),
        )
        return ExecutionTimelinePersistResponse.from_mapping(payload)

    async def finalize_execution_cold_trace(
        self, request: ExecutionColdTraceFinalizeRequest
    ) -> ExecutionColdTraceFinalizeResponse:
        payload = await self._request(
            method="POST",
            path=(
                "/hub/api/control-plane/thread-executions/"
                f"{request.execution_id}/cold-trace/finalize"
            ),
            json_payload=request.to_dict(),
        )
        return ExecutionColdTraceFinalizeResponse.from_mapping(payload)

    async def get_transcript_history(
        self, request: TranscriptHistoryRequest
    ) -> TranscriptHistoryResponse:
        payload = await self._request(
            method="GET",
            path="/hub/api/control-plane/transcript-history",
            params=request.to_dict(),
        )
        return TranscriptHistoryResponse.from_mapping(payload)

    async def write_transcript(
        self, request: TranscriptWriteRequest
    ) -> TranscriptWriteResponse:
        payload = await self._request(
            method="POST",
            path=f"/hub/api/control-plane/transcripts/{request.turn_id}",
            json_payload=request.to_dict(),
        )
        return TranscriptWriteResponse.from_mapping(payload)

    async def get_pma_snapshot(self) -> PmaSnapshotResponse:
        payload = await self._request(
            method="GET",
            path="/hub/api/control-plane/pma-snapshot",
        )
        return PmaSnapshotResponse.from_mapping(payload)

    async def get_agent_workspace(
        self, request: AgentWorkspaceLookupRequest
    ) -> AgentWorkspaceResponse:
        payload = await self._request(
            method="GET",
            path=f"/hub/api/control-plane/agent-workspaces/{request.workspace_id}",
        )
        return AgentWorkspaceResponse.from_mapping(payload)

    async def list_agent_workspaces(
        self, request: AgentWorkspaceListRequest
    ) -> AgentWorkspaceListResponse:
        payload = await self._request(
            method="GET",
            path="/hub/api/control-plane/agent-workspaces",
            params=request.to_dict(),
        )
        return AgentWorkspaceListResponse.from_mapping(payload)

    async def run_workspace_setup_commands(
        self, request: WorkspaceSetupCommandRequest
    ) -> WorkspaceSetupCommandResult:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/workspace-setup-commands",
            json_payload=request.to_dict(),
            # Setup commands may run for an unbounded sequence of user-defined steps.
            timeout=None,
        )
        return WorkspaceSetupCommandResult.from_mapping(payload)

    async def request_automation(self, request: AutomationRequest) -> AutomationResult:
        payload = await self._request(
            method="POST",
            path="/hub/api/control-plane/automation",
            json_payload=request.to_dict(),
        )
        return AutomationResult.from_mapping(payload)


__all__ = ["HttpHubControlPlaneClient"]
