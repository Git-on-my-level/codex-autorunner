from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Optional, Protocol, TypeVar

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ....core.hub_control_plane import (
    AgentWorkspaceListRequest,
    AgentWorkspaceLookupRequest,
    AutomationRequest,
    ExecutionBackendIdUpdateRequest,
    ExecutionCancelAllRequest,
    ExecutionCancelRequest,
    ExecutionClaimNextRequest,
    ExecutionColdTraceFinalizeRequest,
    ExecutionCreateRequest,
    ExecutionInterruptRecordRequest,
    ExecutionLookupRequest,
    ExecutionPromoteRequest,
    ExecutionResultRecordRequest,
    ExecutionTimelinePersistRequest,
    HandshakeRequest,
    HubControlPlaneError,
    HubSharedStateService,
    LatestExecutionLookupRequest,
    NotificationContinuationBindRequest,
    NotificationDeliveryMarkRequest,
    NotificationLookupRequest,
    NotificationReplyTargetLookupRequest,
    QueueDepthRequest,
    QueuedExecutionListRequest,
    RunningExecutionLookupRequest,
    RunningThreadTargetIdsRequest,
    SurfaceBindingListRequest,
    SurfaceBindingLookupRequest,
    SurfaceBindingUpsertRequest,
    ThreadActivityRecordRequest,
    ThreadBackendIdUpdateRequest,
    ThreadCompactSeedUpdateRequest,
    ThreadTargetArchiveRequest,
    ThreadTargetCreateRequest,
    ThreadTargetListRequest,
    ThreadTargetLookupRequest,
    ThreadTargetResumeRequest,
    TranscriptHistoryRequest,
    TranscriptWriteRequest,
    WorkspaceSetupCommandRequest,
)


class _ControlPlaneResponse(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


ResponseT = TypeVar("ResponseT", bound=_ControlPlaneResponse)


def _require_control_plane_service(request: Request) -> HubSharedStateService:
    service = getattr(request.app.state, "hub_control_plane_service", None)
    if isinstance(service, HubSharedStateService):
        return service
    raise HTTPException(status_code=503, detail="Hub control-plane service unavailable")


def _status_code_for_error(exc: HubControlPlaneError) -> int:
    if exc.code == "hub_unavailable":
        return 503
    if exc.code == "hub_incompatible":
        return 409
    if exc.code == "hub_rejected":
        return 400
    if exc.code == "transport_failure":
        return 502
    return 500


def _error_response(exc: HubControlPlaneError) -> JSONResponse:
    return JSONResponse(
        status_code=_status_code_for_error(exc),
        content={"error": exc.info.to_dict()},
    )


async def _run_control_plane_call(
    *,
    request_factory: Callable[[], Any],
    operation: Callable[[Any], ResponseT],
) -> JSONResponse:
    try:
        request_model = request_factory()
    except ValueError as exc:
        return _error_response(HubControlPlaneError("hub_rejected", str(exc)))
    try:
        response_model = await asyncio.to_thread(operation, request_model)
    except HubControlPlaneError as exc:
        return _error_response(exc)
    except ValueError as exc:
        return _error_response(HubControlPlaneError("hub_rejected", str(exc)))
    return JSONResponse(content=response_model.to_dict())


async def _run_control_plane_command(
    *,
    request_factory: Callable[[], Any],
    operation: Callable[[Any], None],
) -> Response:
    try:
        request_model = request_factory()
    except ValueError as exc:
        return _error_response(HubControlPlaneError("hub_rejected", str(exc)))
    try:
        await asyncio.to_thread(operation, request_model)
    except HubControlPlaneError as exc:
        return _error_response(exc)
    except ValueError as exc:
        return _error_response(HubControlPlaneError("hub_rejected", str(exc)))
    return Response(status_code=204)


def build_hub_control_plane_routes() -> APIRouter:
    router = APIRouter(prefix="/hub/api/control-plane", tags=["hub-control-plane"])

    @router.post("/handshake")
    async def handshake(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: HandshakeRequest.from_mapping(payload),
            operation=service.handshake,
        )

    @router.get("/notifications/{notification_id}")
    async def get_notification_record(request: Request, notification_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: NotificationLookupRequest.from_mapping(
                {"notification_id": notification_id}
            ),
            operation=service.get_notification_record,
        )

    @router.get("/notification-reply-target")
    async def get_notification_reply_target(
        request: Request,
        surface_kind: str,
        surface_key: str,
        delivered_message_id: str,
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: NotificationReplyTargetLookupRequest.from_mapping(
                {
                    "surface_kind": surface_kind,
                    "surface_key": surface_key,
                    "delivered_message_id": delivered_message_id,
                }
            ),
            operation=service.get_notification_reply_target,
        )

    @router.post("/notifications/continuation")
    async def bind_notification_continuation(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: NotificationContinuationBindRequest.from_mapping(
                payload
            ),
            operation=service.bind_notification_continuation,
        )

    @router.post("/notifications/delivery")
    async def mark_notification_delivered(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: NotificationDeliveryMarkRequest.from_mapping(
                payload
            ),
            operation=service.mark_notification_delivered,
        )

    @router.get("/surface-bindings")
    async def get_surface_binding(
        request: Request,
        surface_kind: str,
        surface_key: str,
        include_disabled: bool = False,
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: SurfaceBindingLookupRequest.from_mapping(
                {
                    "surface_kind": surface_kind,
                    "surface_key": surface_key,
                    "include_disabled": include_disabled,
                }
            ),
            operation=service.get_surface_binding,
        )

    @router.put("/surface-bindings")
    async def upsert_surface_binding(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: SurfaceBindingUpsertRequest.from_mapping(payload),
            operation=service.upsert_surface_binding,
        )

    @router.post("/surface-bindings/query")
    async def list_surface_bindings(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: SurfaceBindingListRequest.from_mapping(payload),
            operation=service.list_surface_bindings,
        )

    @router.get("/thread-targets/{thread_target_id}")
    async def get_thread_target(request: Request, thread_target_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ThreadTargetLookupRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            ),
            operation=service.get_thread_target,
        )

    @router.post("/thread-targets/query")
    async def list_thread_targets(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ThreadTargetListRequest.from_mapping(payload),
            operation=service.list_thread_targets,
        )

    @router.post("/thread-targets/{thread_target_id}/resume")
    async def resume_thread_target(
        request: Request, thread_target_id: str, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("thread_target_id", thread_target_id)
        return await _run_control_plane_call(
            request_factory=lambda: ThreadTargetResumeRequest.from_mapping(
                request_payload
            ),
            operation=service.resume_thread_target,
        )

    @router.post("/thread-targets/{thread_target_id}/archive")
    async def archive_thread_target(
        request: Request,
        thread_target_id: str,
        payload: Optional[dict[str, Any]] = None,
    ):
        service = _require_control_plane_service(request)
        request_payload: Mapping[str, Any] = {
            "thread_target_id": thread_target_id,
            **dict(payload or {}),
        }
        return await _run_control_plane_call(
            request_factory=lambda: ThreadTargetArchiveRequest.from_mapping(
                request_payload
            ),
            operation=service.archive_thread_target,
        )

    @router.post("/thread-targets/{thread_target_id}/backend-id")
    async def set_thread_backend_id(
        request: Request, thread_target_id: str, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("thread_target_id", thread_target_id)
        return await _run_control_plane_command(
            request_factory=lambda: ThreadBackendIdUpdateRequest.from_mapping(
                request_payload
            ),
            operation=service.set_thread_backend_id,
        )

    @router.post("/thread-targets/{thread_target_id}/activity")
    async def record_thread_activity(
        request: Request, thread_target_id: str, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("thread_target_id", thread_target_id)
        return await _run_control_plane_command(
            request_factory=lambda: ThreadActivityRecordRequest.from_mapping(
                request_payload
            ),
            operation=service.record_thread_activity,
        )

    @router.post("/thread-targets/{thread_target_id}/compact-seed")
    async def update_thread_compact_seed(
        request: Request, thread_target_id: str, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("thread_target_id", thread_target_id)
        return await _run_control_plane_call(
            request_factory=lambda: ThreadCompactSeedUpdateRequest.from_mapping(
                request_payload
            ),
            operation=service.update_thread_compact_seed,
        )

    @router.post("/thread-targets")
    async def create_thread_target(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ThreadTargetCreateRequest.from_mapping(payload),
            operation=service.create_thread_target,
        )

    @router.get("/thread-targets/{thread_target_id}/executions/running")
    async def get_running_execution(request: Request, thread_target_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: RunningExecutionLookupRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            ),
            operation=service.get_running_execution,
        )

    @router.post("/thread-targets/executions/running/query")
    async def list_thread_target_ids_with_running_executions(
        request: Request, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: RunningThreadTargetIdsRequest.from_mapping(payload),
            operation=service.list_thread_target_ids_with_running_executions,
        )

    @router.get("/thread-targets/{thread_target_id}/executions/latest")
    async def get_latest_execution(request: Request, thread_target_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: LatestExecutionLookupRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            ),
            operation=service.get_latest_execution,
        )

    @router.get("/thread-targets/{thread_target_id}/executions/queued")
    async def list_queued_executions(
        request: Request, thread_target_id: str, limit: int = 200
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: QueuedExecutionListRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "limit": limit,
                }
            ),
            operation=service.list_queued_executions,
        )

    @router.get("/thread-targets/{thread_target_id}/executions/queue-depth")
    async def get_queue_depth(request: Request, thread_target_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: QueueDepthRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            ),
            operation=service.get_queue_depth,
        )

    @router.post("/thread-targets/{thread_target_id}/executions/claim-next")
    async def claim_next_queued_execution(request: Request, thread_target_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionClaimNextRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            ),
            operation=service.claim_next_queued_execution,
        )

    @router.post("/thread-targets/{thread_target_id}/executions/cancel-all")
    async def cancel_queued_executions(request: Request, thread_target_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionCancelAllRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            ),
            operation=service.cancel_queued_executions,
        )

    @router.get("/thread-targets/{thread_target_id}/executions/{execution_id}")
    async def get_execution(request: Request, thread_target_id: str, execution_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionLookupRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "execution_id": execution_id,
                }
            ),
            operation=service.get_execution,
        )

    @router.post("/thread-targets/{thread_target_id}/executions")
    async def create_execution(
        request: Request, thread_target_id: str, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("thread_target_id", thread_target_id)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionCreateRequest.from_mapping(
                request_payload
            ),
            operation=service.create_execution,
        )

    @router.post("/thread-targets/{thread_target_id}/executions/{execution_id}/cancel")
    async def cancel_queued_execution(
        request: Request, thread_target_id: str, execution_id: str
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionCancelRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "execution_id": execution_id,
                }
            ),
            operation=service.cancel_queued_execution,
        )

    @router.post("/thread-targets/{thread_target_id}/executions/{execution_id}/promote")
    async def promote_queued_execution(
        request: Request, thread_target_id: str, execution_id: str
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionPromoteRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "execution_id": execution_id,
                }
            ),
            operation=service.promote_queued_execution,
        )

    @router.post("/thread-targets/{thread_target_id}/executions/{execution_id}/result")
    async def record_execution_result(
        request: Request,
        thread_target_id: str,
        execution_id: str,
        payload: dict[str, Any],
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("thread_target_id", thread_target_id)
        request_payload.setdefault("execution_id", execution_id)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionResultRecordRequest.from_mapping(
                request_payload
            ),
            operation=service.record_execution_result,
        )

    @router.post(
        "/thread-targets/{thread_target_id}/executions/{execution_id}/interrupt"
    )
    async def record_execution_interrupted(
        request: Request, thread_target_id: str, execution_id: str
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionInterruptRecordRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "execution_id": execution_id,
                }
            ),
            operation=service.record_execution_interrupted,
        )

    @router.put("/thread-executions/{execution_id}/backend-id", status_code=204)
    async def set_execution_backend_id(
        request: Request, execution_id: str, payload: Optional[dict[str, Any]] = None
    ):
        service = _require_control_plane_service(request)
        request_payload: Mapping[str, Any] = {
            "execution_id": execution_id,
            **dict(payload or {}),
        }
        return await _run_control_plane_command(
            request_factory=lambda: ExecutionBackendIdUpdateRequest.from_mapping(
                request_payload
            ),
            operation=service.set_execution_backend_id,
        )

    @router.post("/thread-executions/{execution_id}/timeline")
    async def persist_execution_timeline(
        request: Request, execution_id: str, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("execution_id", execution_id)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionTimelinePersistRequest.from_mapping(
                request_payload
            ),
            operation=service.persist_execution_timeline,
        )

    @router.post("/thread-executions/{execution_id}/cold-trace/finalize")
    async def finalize_execution_cold_trace(
        request: Request, execution_id: str, payload: dict[str, Any]
    ):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("execution_id", execution_id)
        return await _run_control_plane_call(
            request_factory=lambda: ExecutionColdTraceFinalizeRequest.from_mapping(
                request_payload
            ),
            operation=service.finalize_execution_cold_trace,
        )

    @router.get("/transcript-history")
    async def get_transcript_history(
        request: Request,
        target_kind: str,
        target_id: str,
        limit: int = 10,
    ):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: TranscriptHistoryRequest.from_mapping(
                {
                    "target_kind": target_kind,
                    "target_id": target_id,
                    "limit": limit,
                }
            ),
            operation=service.get_transcript_history,
        )

    @router.post("/transcripts/{turn_id}")
    async def write_transcript(request: Request, turn_id: str, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        request_payload = dict(payload)
        request_payload.setdefault("turn_id", turn_id)
        return await _run_control_plane_call(
            request_factory=lambda: TranscriptWriteRequest.from_mapping(
                request_payload
            ),
            operation=service.write_transcript,
        )

    @router.get("/pma-snapshot")
    async def get_pma_snapshot(request: Request):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: None,
            operation=lambda _: service.get_pma_snapshot(),
        )

    @router.get("/agent-workspaces")
    async def list_agent_workspaces(request: Request, include_disabled: bool = True):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: AgentWorkspaceListRequest.from_mapping(
                {"include_disabled": include_disabled}
            ),
            operation=service.list_agent_workspaces,
        )

    @router.get("/agent-workspaces/{workspace_id}")
    async def get_agent_workspace(request: Request, workspace_id: str):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: AgentWorkspaceLookupRequest.from_mapping(
                {"workspace_id": workspace_id}
            ),
            operation=service.get_agent_workspace,
        )

    @router.post("/workspace-setup-commands")
    async def run_workspace_setup_commands(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: WorkspaceSetupCommandRequest.from_mapping(payload),
            operation=service.run_workspace_setup_commands,
        )

    @router.post("/automation")
    async def request_automation(request: Request, payload: dict[str, Any]):
        service = _require_control_plane_service(request)
        return await _run_control_plane_call(
            request_factory=lambda: AutomationRequest.from_mapping(payload),
            operation=service.request_automation,
        )

    return router


__all__ = ["build_hub_control_plane_routes"]
