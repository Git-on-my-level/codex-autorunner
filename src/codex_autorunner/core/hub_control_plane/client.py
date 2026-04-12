from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class HubControlPlaneClient(Protocol):
    """Transport-neutral client interface for hub-owned shared state."""

    async def handshake(self, request: HandshakeRequest) -> HandshakeResponse: ...

    async def get_notification_record(
        self, request: NotificationLookupRequest
    ) -> NotificationRecordResponse: ...

    async def bind_notification_continuation(
        self, request: NotificationContinuationBindRequest
    ) -> NotificationRecordResponse: ...

    async def mark_notification_delivered(
        self, request: NotificationDeliveryMarkRequest
    ) -> NotificationRecordResponse: ...

    async def get_surface_binding(
        self, request: SurfaceBindingLookupRequest
    ) -> SurfaceBindingResponse: ...

    async def upsert_surface_binding(
        self, request: SurfaceBindingUpsertRequest
    ) -> SurfaceBindingResponse: ...

    async def get_thread_target(
        self, request: ThreadTargetLookupRequest
    ) -> ThreadTargetResponse: ...

    async def list_thread_targets(
        self, request: ThreadTargetListRequest
    ) -> ThreadTargetListResponse: ...

    async def resume_thread_target(
        self, request: ThreadTargetResumeRequest
    ) -> ThreadTargetResponse: ...

    async def archive_thread_target(
        self, request: ThreadTargetArchiveRequest
    ) -> ThreadTargetResponse: ...

    async def update_thread_compact_seed(
        self, request: ThreadCompactSeedUpdateRequest
    ) -> ThreadTargetResponse: ...

    async def get_agent_workspace(
        self, request: AgentWorkspaceLookupRequest
    ) -> AgentWorkspaceResponse: ...

    async def list_agent_workspaces(
        self, request: AgentWorkspaceListRequest
    ) -> AgentWorkspaceListResponse: ...

    async def run_workspace_setup_commands(
        self, request: WorkspaceSetupCommandRequest
    ) -> WorkspaceSetupCommandResult: ...

    async def request_automation(
        self, request: AutomationRequest
    ) -> AutomationResult: ...


__all__ = ["HubControlPlaneClient"]
