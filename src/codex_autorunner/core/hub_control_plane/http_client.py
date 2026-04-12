from __future__ import annotations

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
    HandshakeRequest,
    HandshakeResponse,
    NotificationContinuationBindRequest,
    NotificationDeliveryMarkRequest,
    NotificationLookupRequest,
    NotificationRecordResponse,
    NotificationReplyTargetLookupRequest,
    SurfaceBindingLookupRequest,
    SurfaceBindingResponse,
    SurfaceBindingUpsertRequest,
    ThreadCompactSeedUpdateRequest,
    ThreadTargetArchiveRequest,
    ThreadTargetListRequest,
    ThreadTargetListResponse,
    ThreadTargetLookupRequest,
    ThreadTargetResponse,
    ThreadTargetResumeRequest,
    WorkspaceSetupCommandRequest,
    WorkspaceSetupCommandResult,
)


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
        self._owns_client = http_client is None
        if http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
                headers=dict(headers or {}),
            )
        else:
            self._http_client = http_client

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
        if self._owns_client:
            await self._http_client.aclose()

    async def _request(
        self,
        *,
        method: str,
        path: str,
        json_payload: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http_client.request(
                method,
                path,
                json=dict(json_payload) if json_payload is not None else None,
                params=dict(params) if params is not None else None,
            )
        except httpx.RequestError as exc:
            raise HubControlPlaneError(
                "transport_failure",
                f"Hub control-plane transport request failed: {exc}",
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

    async def update_thread_compact_seed(
        self, request: ThreadCompactSeedUpdateRequest
    ) -> ThreadTargetResponse:
        payload = await self._request(
            method="POST",
            path=f"/hub/api/control-plane/thread-targets/{request.thread_target_id}/compact-seed",
            json_payload=request.to_dict(),
        )
        return ThreadTargetResponse.from_mapping(payload)

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
