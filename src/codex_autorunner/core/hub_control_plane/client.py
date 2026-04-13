from __future__ import annotations

from typing import Protocol, runtime_checkable

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
    ExecutionCreateRequest,
    ExecutionInterruptRecordRequest,
    ExecutionListResponse,
    ExecutionLookupRequest,
    ExecutionPromoteRequest,
    ExecutionPromoteResponse,
    ExecutionResponse,
    ExecutionResultRecordRequest,
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
    SurfaceBindingLookupRequest,
    SurfaceBindingResponse,
    SurfaceBindingUpsertRequest,
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

    async def get_notification_reply_target(
        self, request: NotificationReplyTargetLookupRequest
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

    async def create_thread_target(
        self, request: ThreadTargetCreateRequest
    ) -> ThreadTargetResponse: ...

    async def create_execution(
        self, request: ExecutionCreateRequest
    ) -> ExecutionResponse: ...

    async def get_execution(
        self, request: ExecutionLookupRequest
    ) -> ExecutionResponse: ...

    async def get_running_execution(
        self, request: RunningExecutionLookupRequest
    ) -> ExecutionResponse: ...

    async def get_latest_execution(
        self, request: LatestExecutionLookupRequest
    ) -> ExecutionResponse: ...

    async def list_queued_executions(
        self, request: QueuedExecutionListRequest
    ) -> ExecutionListResponse: ...

    async def get_queue_depth(
        self, request: QueueDepthRequest
    ) -> QueueDepthResponse: ...

    async def cancel_queued_execution(
        self, request: ExecutionCancelRequest
    ) -> ExecutionCancelResponse: ...

    async def promote_queued_execution(
        self, request: ExecutionPromoteRequest
    ) -> ExecutionPromoteResponse: ...

    async def record_execution_result(
        self, request: ExecutionResultRecordRequest
    ) -> ExecutionResponse: ...

    async def record_execution_interrupted(
        self, request: ExecutionInterruptRecordRequest
    ) -> ExecutionResponse: ...

    async def cancel_queued_executions(
        self, request: ExecutionCancelAllRequest
    ) -> ExecutionCancelAllResponse: ...

    async def set_execution_backend_id(
        self, request: ExecutionBackendIdUpdateRequest
    ) -> None: ...

    async def claim_next_queued_execution(
        self, request: ExecutionClaimNextRequest
    ) -> ExecutionClaimNextResponse: ...

    async def resume_thread_target(
        self, request: ThreadTargetResumeRequest
    ) -> ThreadTargetResponse: ...

    async def archive_thread_target(
        self, request: ThreadTargetArchiveRequest
    ) -> ThreadTargetResponse: ...

    async def update_thread_compact_seed(
        self, request: ThreadCompactSeedUpdateRequest
    ) -> ThreadTargetResponse: ...

    async def get_transcript_history(
        self, request: TranscriptHistoryRequest
    ) -> TranscriptHistoryResponse: ...

    async def get_pma_snapshot(self) -> PmaSnapshotResponse: ...

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
