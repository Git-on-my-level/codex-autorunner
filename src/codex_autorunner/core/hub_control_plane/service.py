from __future__ import annotations

import logging
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Iterable, Optional

from ..hub import AgentWorkspaceSnapshot
from ..orchestration.bindings import OrchestrationBindingStore
from ..orchestration.models import ExecutionRecord, ThreadTarget
from ..orchestration.service import PmaThreadExecutionStore
from ..orchestration.sqlite import read_orchestration_compatibility_metadata
from ..pma_notification_store import NotificationConversation, PmaNotificationStore
from ..pma_thread_store import PmaThreadStore
from .errors import HubControlPlaneError
from .models import (
    AgentWorkspaceDescriptor,
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
    NotificationRecord,
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
    WorkspaceSetupCommandRequest,
    WorkspaceSetupCommandResult,
)

CONTROL_PLANE_API_VERSION = "1.0.0"
CONTROL_PLANE_MINIMUM_CLIENT_API_VERSION = "1.0.0"
CONTROL_PLANE_CAPABILITIES: tuple[str, ...] = (
    "agent_workspaces",
    "automation_requests",
    "compact_seed_updates",
    "compatibility_handshake",
    "notification_continuations",
    "notification_delivery_ack",
    "notification_records",
    "notification_reply_targets",
    "pma_snapshot",
    "surface_bindings",
    "thread_activity_updates",
    "thread_backend_updates",
    "thread_execution_lifecycle",
    "thread_target_creation",
    "thread_targets",
    "transcript_history",
    "workspace_setup_commands",
)


def _resolve_hub_build_version() -> str:
    for distribution_name in ("codex-autorunner", "codex_autorunner"):
        try:
            version = importlib_metadata.version(distribution_name).strip()
        except importlib_metadata.PackageNotFoundError:
            continue
        if version:
            return version
    return "unknown"


def _coerce_positive_int(
    value: Any, *, field_name: str, default: int, minimum: int = 1
) -> int:
    if value is None:
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise HubControlPlaneError(
            "hub_rejected",
            f"{field_name} must be an integer",
            details={"field": field_name},
        ) from exc
    if normalized < minimum:
        raise HubControlPlaneError(
            "hub_rejected",
            f"{field_name} must be >= {minimum}",
            details={"field": field_name, "minimum": minimum},
        )
    return normalized


def _thread_target_from_row(record: dict[str, Any] | None) -> ThreadTarget | None:
    if record is None:
        return None
    return ThreadTarget.from_mapping(record)


def _execution_from_record(record: ExecutionRecord | None) -> ExecutionRecord | None:
    return record


def _error_message(exc: BaseException) -> str:
    if exc.args and isinstance(exc.args[0], str):
        return exc.args[0]
    return str(exc)


def _notification_record_from_conversation(
    conversation: NotificationConversation | None,
) -> NotificationRecord | None:
    if conversation is None:
        return None
    return NotificationRecord.from_mapping(conversation.to_dict())


def _workspace_descriptor_from_snapshot(
    snapshot: AgentWorkspaceSnapshot,
) -> AgentWorkspaceDescriptor:
    return AgentWorkspaceDescriptor(
        workspace_id=snapshot.id,
        runtime_kind=snapshot.runtime,
        workspace_root=str(snapshot.path),
        display_name=snapshot.display_name,
        enabled=bool(snapshot.enabled),
        exists_on_disk=bool(snapshot.exists_on_disk),
        resource_kind=snapshot.resource_kind,
    )


class HubSharedStateService:
    """Hub-owned owner of shared-state control-plane operations."""

    def __init__(
        self,
        *,
        hub_root: Path,
        supervisor: Any,
        hub_asset_version: Optional[str] = None,
        hub_build_version: Optional[str] = None,
        durable_writes: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._hub_root = Path(hub_root)
        self._supervisor = supervisor
        self._hub_asset_version = (
            str(hub_asset_version).strip() if hub_asset_version else None
        )
        resolved_build_version = (
            str(hub_build_version).strip() if hub_build_version else ""
        )
        self._hub_build_version = resolved_build_version or _resolve_hub_build_version()
        self._durable_writes = bool(durable_writes)
        self._logger = logger or logging.getLogger(__name__)
        self._notification_store = PmaNotificationStore(self._hub_root)
        self._binding_store = OrchestrationBindingStore(self._hub_root)
        self._thread_store = PmaThreadStore(
            self._hub_root,
            durable=self._durable_writes,
            bootstrap_on_init=False,
        )
        self._execution_store = PmaThreadExecutionStore(self._thread_store)

    @property
    def capabilities(self) -> tuple[str, ...]:
        return CONTROL_PLANE_CAPABILITIES

    def handshake(self, request: HandshakeRequest) -> HandshakeResponse:
        metadata = read_orchestration_compatibility_metadata(self._hub_root)
        if metadata is None:
            raise HubControlPlaneError(
                "hub_unavailable",
                "hub shared-state compatibility metadata is unavailable",
                details={"hub_root": str(self._hub_root)},
            )
        return HandshakeResponse(
            api_version=CONTROL_PLANE_API_VERSION,
            minimum_client_api_version=CONTROL_PLANE_MINIMUM_CLIENT_API_VERSION,
            schema_generation=metadata.schema_generation,
            capabilities=self.capabilities,
            hub_build_version=self._hub_build_version,
            hub_asset_version=self._hub_asset_version,
        )

    def get_notification_record(
        self, request: NotificationLookupRequest
    ) -> NotificationRecordResponse:
        return NotificationRecordResponse(
            record=self._lookup_notification_record(request.notification_id)
        )

    def _lookup_notification_record(
        self, notification_id: str
    ) -> NotificationRecord | None:
        from ..orchestration.sqlite import open_orchestration_sqlite

        with open_orchestration_sqlite(
            self._hub_root,
            durable=self._durable_writes,
            migrate=False,
        ) as conn:
            row = conn.execute(
                """
                SELECT *
                  FROM orch_notification_conversations
                 WHERE notification_id = ?
                 LIMIT 1
                """,
                (notification_id,),
            ).fetchone()
        if row is None:
            return None
        return NotificationRecord.from_mapping(dict(row))

    def get_notification_reply_target(
        self, request: NotificationReplyTargetLookupRequest
    ) -> NotificationRecordResponse:
        conversation = self._notification_store.get_reply_target(
            surface_kind=request.surface_kind,
            surface_key=request.surface_key,
            delivered_message_id=request.delivered_message_id,
        )
        return NotificationRecordResponse(
            record=_notification_record_from_conversation(conversation)
        )

    def bind_notification_continuation(
        self, request: NotificationContinuationBindRequest
    ) -> NotificationRecordResponse:
        conversation = self._notification_store.bind_continuation_thread(
            notification_id=request.notification_id,
            thread_target_id=request.thread_target_id,
        )
        return NotificationRecordResponse(
            record=_notification_record_from_conversation(conversation)
        )

    def mark_notification_delivered(
        self, request: NotificationDeliveryMarkRequest
    ) -> NotificationRecordResponse:
        conversation = self._notification_store.mark_delivered(
            delivery_record_id=request.delivery_record_id,
            delivered_message_id=request.delivered_message_id,
        )
        return NotificationRecordResponse(
            record=_notification_record_from_conversation(conversation)
        )

    def get_surface_binding(
        self, request: SurfaceBindingLookupRequest
    ) -> SurfaceBindingResponse:
        binding = self._binding_store.get_binding(
            surface_kind=request.surface_kind,
            surface_key=request.surface_key,
            include_disabled=request.include_disabled,
        )
        return SurfaceBindingResponse(binding=binding)

    def upsert_surface_binding(
        self, request: SurfaceBindingUpsertRequest
    ) -> SurfaceBindingResponse:
        try:
            binding = self._binding_store.upsert_binding(
                surface_kind=request.surface_kind,
                surface_key=request.surface_key,
                thread_target_id=request.thread_target_id,
                agent_id=request.agent_id,
                repo_id=request.repo_id,
                resource_kind=request.resource_kind,
                resource_id=request.resource_id,
                mode=request.mode,
                metadata=request.metadata,
            )
        except ValueError as exc:
            raise HubControlPlaneError(
                "hub_rejected",
                str(exc),
                details={"operation": "upsert_surface_binding"},
            ) from exc
        return SurfaceBindingResponse(binding=binding)

    def get_thread_target(
        self, request: ThreadTargetLookupRequest
    ) -> ThreadTargetResponse:
        thread = _thread_target_from_row(
            self._thread_store.get_thread(request.thread_target_id)
        )
        return ThreadTargetResponse(thread=thread)

    def list_thread_targets(
        self, request: ThreadTargetListRequest
    ) -> ThreadTargetListResponse:
        rows = self._thread_store.list_threads(
            agent=request.agent_id,
            status=request.lifecycle_status,
            normalized_status=request.runtime_status,
            repo_id=request.repo_id,
            resource_kind=request.resource_kind,
            resource_id=request.resource_id,
            limit=request.limit,
        )
        threads = tuple(
            thread
            for thread in (_thread_target_from_row(row) for row in rows)
            if thread is not None
        )
        return ThreadTargetListResponse(threads=threads)

    def resume_thread_target(
        self, request: ThreadTargetResumeRequest
    ) -> ThreadTargetResponse:
        if self._thread_store.get_thread(request.thread_target_id) is None:
            return ThreadTargetResponse(thread=None)
        self._thread_store.set_thread_backend_id(
            request.thread_target_id,
            request.backend_thread_id,
            backend_runtime_instance_id=request.backend_runtime_instance_id,
        )
        self._thread_store.activate_thread(request.thread_target_id)
        return ThreadTargetResponse(
            thread=_thread_target_from_row(
                self._thread_store.get_thread(request.thread_target_id)
            )
        )

    def archive_thread_target(
        self, request: ThreadTargetArchiveRequest
    ) -> ThreadTargetResponse:
        if self._thread_store.get_thread(request.thread_target_id) is None:
            return ThreadTargetResponse(thread=None)
        self._thread_store.archive_thread(request.thread_target_id)
        return ThreadTargetResponse(
            thread=_thread_target_from_row(
                self._thread_store.get_thread(request.thread_target_id)
            )
        )

    def set_thread_backend_id(self, request: ThreadBackendIdUpdateRequest) -> None:
        self._thread_store.set_thread_backend_id(
            request.thread_target_id,
            request.backend_thread_id,
            backend_runtime_instance_id=request.backend_runtime_instance_id,
        )

    def record_thread_activity(self, request: ThreadActivityRecordRequest) -> None:
        self._thread_store.update_thread_after_turn(
            request.thread_target_id,
            last_turn_id=request.execution_id,
            last_message_preview=request.message_preview,
        )

    def update_thread_compact_seed(
        self, request: ThreadCompactSeedUpdateRequest
    ) -> ThreadTargetResponse:
        if self._thread_store.get_thread(request.thread_target_id) is None:
            return ThreadTargetResponse(thread=None)
        self._thread_store.set_thread_compact_seed(
            request.thread_target_id,
            request.compact_seed,
        )
        return ThreadTargetResponse(
            thread=_thread_target_from_row(
                self._thread_store.get_thread(request.thread_target_id)
            )
        )

    def create_thread_target(
        self, request: ThreadTargetCreateRequest
    ) -> ThreadTargetResponse:
        created = self._thread_store.create_thread(
            agent=request.agent_id,
            workspace_root=Path(request.workspace_root),
            repo_id=request.repo_id,
            resource_kind=request.resource_kind,
            resource_id=request.resource_id,
            name=request.display_name,
            backend_thread_id=request.backend_thread_id,
            metadata=request.metadata or None,
        )
        return ThreadTargetResponse(thread=_thread_target_from_row(created))

    def _execution_rejected(
        self, *, operation: str, exc: BaseException
    ) -> HubControlPlaneError:
        return HubControlPlaneError(
            "hub_rejected",
            _error_message(exc),
            details={"operation": operation},
        )

    def create_execution(self, request: ExecutionCreateRequest) -> ExecutionResponse:
        try:
            execution = self._execution_store.create_execution(
                request.thread_target_id,
                prompt=request.prompt,
                request_kind=request.request_kind,
                busy_policy=request.busy_policy,
                model=request.model,
                reasoning=request.reasoning,
                client_request_id=request.client_request_id,
                queue_payload=request.queue_payload or None,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise self._execution_rejected(
                operation="create_execution",
                exc=exc,
            ) from exc
        return ExecutionResponse(execution=_execution_from_record(execution))

    def get_execution(self, request: ExecutionLookupRequest) -> ExecutionResponse:
        execution = self._execution_store.get_execution(
            request.thread_target_id,
            request.execution_id,
        )
        return ExecutionResponse(execution=_execution_from_record(execution))

    def get_running_execution(
        self, request: RunningExecutionLookupRequest
    ) -> ExecutionResponse:
        execution = self._execution_store.get_running_execution(
            request.thread_target_id
        )
        return ExecutionResponse(execution=_execution_from_record(execution))

    def get_latest_execution(
        self, request: LatestExecutionLookupRequest
    ) -> ExecutionResponse:
        execution = self._execution_store.get_latest_execution(request.thread_target_id)
        return ExecutionResponse(execution=_execution_from_record(execution))

    def list_queued_executions(
        self, request: QueuedExecutionListRequest
    ) -> ExecutionListResponse:
        executions = self._execution_store.list_queued_executions(
            request.thread_target_id,
            limit=request.limit,
        )
        return ExecutionListResponse(executions=tuple(executions))

    def get_queue_depth(self, request: QueueDepthRequest) -> QueueDepthResponse:
        return QueueDepthResponse(
            thread_target_id=request.thread_target_id,
            queue_depth=self._execution_store.get_queue_depth(request.thread_target_id),
        )

    def cancel_queued_execution(
        self, request: ExecutionCancelRequest
    ) -> ExecutionCancelResponse:
        return ExecutionCancelResponse(
            thread_target_id=request.thread_target_id,
            execution_id=request.execution_id,
            cancelled=self._execution_store.cancel_queued_execution(
                request.thread_target_id,
                request.execution_id,
            ),
        )

    def promote_queued_execution(
        self, request: ExecutionPromoteRequest
    ) -> ExecutionPromoteResponse:
        return ExecutionPromoteResponse(
            thread_target_id=request.thread_target_id,
            execution_id=request.execution_id,
            promoted=self._execution_store.promote_queued_execution(
                request.thread_target_id,
                request.execution_id,
            ),
        )

    def record_execution_result(
        self, request: ExecutionResultRecordRequest
    ) -> ExecutionResponse:
        try:
            execution = self._execution_store.record_execution_result(
                request.thread_target_id,
                request.execution_id,
                status=request.status,
                assistant_text=request.assistant_text,
                error=request.error,
                backend_turn_id=request.backend_turn_id,
                transcript_turn_id=request.transcript_turn_id,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise self._execution_rejected(
                operation="record_execution_result",
                exc=exc,
            ) from exc
        return ExecutionResponse(execution=_execution_from_record(execution))

    def record_execution_interrupted(
        self, request: ExecutionInterruptRecordRequest
    ) -> ExecutionResponse:
        try:
            execution = self._execution_store.record_execution_interrupted(
                request.thread_target_id,
                request.execution_id,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise self._execution_rejected(
                operation="record_execution_interrupted",
                exc=exc,
            ) from exc
        return ExecutionResponse(execution=_execution_from_record(execution))

    def cancel_queued_executions(
        self, request: ExecutionCancelAllRequest
    ) -> ExecutionCancelAllResponse:
        return ExecutionCancelAllResponse(
            thread_target_id=request.thread_target_id,
            cancelled_count=self._execution_store.cancel_queued_executions(
                request.thread_target_id
            ),
        )

    def set_execution_backend_id(
        self, request: ExecutionBackendIdUpdateRequest
    ) -> None:
        self._execution_store.set_execution_backend_id(
            request.execution_id,
            request.backend_turn_id,
        )

    def claim_next_queued_execution(
        self, request: ExecutionClaimNextRequest
    ) -> ExecutionClaimNextResponse:
        claimed = self._execution_store.claim_next_queued_execution(
            request.thread_target_id
        )
        if claimed is None:
            return ExecutionClaimNextResponse(execution=None, queue_payload={})
        execution, queue_payload = claimed
        return ExecutionClaimNextResponse(
            execution=_execution_from_record(execution),
            queue_payload=dict(queue_payload),
        )

    def get_transcript_history(
        self, request: TranscriptHistoryRequest
    ) -> TranscriptHistoryResponse:
        from ..orchestration.transcript_mirror import TranscriptMirrorStore

        entries = TranscriptMirrorStore(self._hub_root).list_target_history(
            target_kind=request.target_kind,
            target_id=request.target_id,
            limit=request.limit,
        )
        return TranscriptHistoryResponse(entries=tuple(entries))

    def get_pma_snapshot(self) -> PmaSnapshotResponse:
        import asyncio

        from ..pma_context import build_hub_snapshot

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                build_hub_snapshot(self._supervisor, hub_root=self._hub_root), loop
            )
            snapshot = future.result(timeout=30)
        else:
            snapshot = asyncio.run(
                build_hub_snapshot(self._supervisor, hub_root=self._hub_root)
            )
        return PmaSnapshotResponse(snapshot=snapshot)

    def get_agent_workspace(
        self, request: AgentWorkspaceLookupRequest
    ) -> AgentWorkspaceResponse:
        try:
            snapshot = self._supervisor.get_agent_workspace_snapshot(
                request.workspace_id
            )
        except ValueError:
            return AgentWorkspaceResponse(workspace=None)
        return AgentWorkspaceResponse(
            workspace=_workspace_descriptor_from_snapshot(snapshot)
        )

    def list_agent_workspaces(
        self, request: AgentWorkspaceListRequest
    ) -> AgentWorkspaceListResponse:
        snapshots: Iterable[AgentWorkspaceSnapshot] = (
            self._supervisor.list_agent_workspaces(use_cache=False)
        )
        workspaces = tuple(
            descriptor
            for descriptor in (
                _workspace_descriptor_from_snapshot(snapshot) for snapshot in snapshots
            )
            if request.include_disabled or descriptor.enabled
        )
        return AgentWorkspaceListResponse(workspaces=workspaces)

    def run_workspace_setup_commands(
        self, request: WorkspaceSetupCommandRequest
    ) -> WorkspaceSetupCommandResult:
        workspace_root = Path(request.workspace_root).expanduser()
        count = self._supervisor.run_setup_commands_for_workspace(
            workspace_root,
            repo_id_hint=request.repo_id_hint,
        )
        return WorkspaceSetupCommandResult(
            workspace_root=str(workspace_root),
            repo_id_hint=request.repo_id_hint,
            setup_command_count=max(0, int(count or 0)),
        )

    def request_automation(self, request: AutomationRequest) -> AutomationResult:
        operation = request.operation.strip().lower()
        if operation != "process_now":
            raise HubControlPlaneError(
                "hub_rejected",
                f"Unsupported automation operation: {request.operation}",
                details={"operation": request.operation},
            )
        payload = dict(request.payload)
        include_timers = bool(payload.get("include_timers", True))
        limit = _coerce_positive_int(
            payload.get("limit"),
            field_name="limit",
            default=100,
        )
        result = self._supervisor.process_pma_automation_now(
            include_timers=include_timers,
            limit=limit,
        )
        if not isinstance(result, dict):
            result = {"result": result}
        return AutomationResult(
            operation=request.operation,
            accepted=True,
            payload=dict(result),
        )


__all__ = [
    "CONTROL_PLANE_API_VERSION",
    "CONTROL_PLANE_CAPABILITIES",
    "CONTROL_PLANE_MINIMUM_CLIENT_API_VERSION",
    "HubSharedStateService",
]
