from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.hub import AgentWorkspaceSnapshot
from codex_autorunner.core.hub_control_plane import (
    AutomationRequest,
    ExecutionBackendIdUpdateRequest,
    ExecutionClaimNextRequest,
    ExecutionColdTraceFinalizeRequest,
    ExecutionCreateRequest,
    ExecutionResultRecordRequest,
    ExecutionTimelinePersistRequest,
    HandshakeRequest,
    HttpHubControlPlaneClient,
    HubControlPlaneError,
    NotificationDeliveryMarkRequest,
    NotificationReplyTargetLookupRequest,
    QueueDepthRequest,
    QueuedExecutionListRequest,
    SurfaceBindingListRequest,
    SurfaceBindingUpsertRequest,
    ThreadBackendIdUpdateRequest,
    ThreadTargetListRequest,
    ThreadTargetLookupRequest,
    TranscriptWriteRequest,
    WorkspaceSetupCommandRequest,
    serialize_run_event,
)
from codex_autorunner.core.hub_control_plane.service import HubSharedStateService
from codex_autorunner.core.orchestration.sqlite import prepare_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    prepare_pma_thread_store,
)
from codex_autorunner.core.ports.run_event import Completed, Started
from codex_autorunner.surfaces.web.routes.hub_control_plane import (
    build_hub_control_plane_routes,
)


class _SupervisorStub:
    def __init__(self, workspace_snapshot: AgentWorkspaceSnapshot) -> None:
        self._workspace_snapshot = workspace_snapshot

    def list_agent_workspaces(
        self, *, use_cache: bool = True
    ) -> list[AgentWorkspaceSnapshot]:
        assert use_cache is False
        return [self._workspace_snapshot]

    def get_agent_workspace_snapshot(self, workspace_id: str) -> AgentWorkspaceSnapshot:
        if workspace_id != self._workspace_snapshot.id:
            raise ValueError(f"Unknown workspace id: {workspace_id}")
        return self._workspace_snapshot

    def run_setup_commands_for_workspace(
        self, workspace_root: Path, *, repo_id_hint: str | None = None
    ) -> int:
        return 3 if repo_id_hint == "repo-1" else 1

    def process_pma_automation_now(
        self, *, include_timers: bool = True, limit: int = 100
    ) -> dict[str, int]:
        return {
            "timers_processed": 2 if include_timers else 0,
            "wakeups_dispatched": limit,
        }


def _build_test_app(tmp_path: Path) -> tuple[FastAPI, str]:
    hub_root = tmp_path / "hub"
    workspace_root = hub_root / "agent-workspaces" / "zeroclaw" / "wksp-1"
    workspace_root.mkdir(parents=True, exist_ok=True)
    prepare_orchestration_sqlite(hub_root, durable=False)
    prepare_pma_thread_store(hub_root, durable=False)

    supervisor = _SupervisorStub(
        AgentWorkspaceSnapshot(
            id="wksp-1",
            runtime="zeroclaw",
            path=workspace_root,
            display_name="Workspace One",
            enabled=True,
            exists_on_disk=True,
        )
    )
    service = HubSharedStateService(
        hub_root=hub_root,
        supervisor=supervisor,
        hub_asset_version="asset-77",
        hub_build_version="build-77",
        durable_writes=False,
    )
    thread = PmaThreadStore(
        hub_root, durable=False, bootstrap_on_init=False
    ).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        resource_kind="agent_workspace",
        resource_id="wksp-1",
        name="Workspace Thread",
    )
    notification_store = PmaNotificationStore(hub_root)
    notification_store.record_notification(
        correlation_id="corr-1",
        source_kind="run",
        delivery_mode="chat",
        surface_kind="telegram",
        surface_key="chat:1",
        delivery_record_id="delivery-1",
        repo_id="repo-1",
        workspace_root=str(workspace_root),
        notification_id="notif-1",
    )

    app = FastAPI()
    app.state.hub_control_plane_service = service
    app.include_router(build_hub_control_plane_routes())
    return app, str(thread["managed_thread_id"])


def test_hub_control_plane_routes_return_typed_validation_errors(
    tmp_path: Path,
) -> None:
    app, _thread_target_id = _build_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/hub/api/control-plane/handshake",
            json={"client_api_version": "1.0.0"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "hub_rejected"
    assert "client_name is required" in response.json()["error"]["message"]


def test_hub_control_plane_list_surface_bindings_route_validates_limit(
    tmp_path: Path,
) -> None:
    app, _thread_target_id = _build_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/hub/api/control-plane/surface-bindings/query",
            json={"limit": "oops"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "hub_rejected"
    assert "limit must be an integer" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            HubControlPlaneError("hub_unavailable", "Hub is not running"),
            503,
        ),
        (
            HubControlPlaneError("hub_incompatible", "schema generation mismatch"),
            409,
        ),
    ],
)
def test_hub_control_plane_routes_distinguish_unavailable_from_incompatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: HubControlPlaneError,
    expected_status: int,
) -> None:
    app, _thread_target_id = _build_test_app(tmp_path)
    service = app.state.hub_control_plane_service

    def _raise_handshake(_request: object) -> object:
        raise error

    monkeypatch.setattr(service, "handshake", _raise_handshake)

    with TestClient(app) as client:
        response = client.post(
            "/hub/api/control-plane/handshake",
            json={"client_name": "discord", "client_api_version": "1.0.0"},
        )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == error.code
    assert response.json()["error"]["retryable"] is error.retryable


@pytest.mark.anyio
async def test_hub_control_plane_http_client_round_trip(tmp_path: Path) -> None:
    app, thread_target_id = _build_test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http_client:
        client = HttpHubControlPlaneClient(
            base_url="http://testserver",
            http_client=http_client,
        )
        handshake = await client.handshake(
            HandshakeRequest.from_mapping(
                {"client_name": "telegram", "client_api_version": "1.0.0"}
            )
        )
        await client.upsert_surface_binding(
            SurfaceBindingUpsertRequest.from_mapping(
                {
                    "surface_kind": "telegram",
                    "surface_key": "chat:1",
                    "thread_target_id": thread_target_id,
                    "agent_id": "codex",
                    "repo_id": "repo-1",
                    "resource_kind": "agent_workspace",
                    "resource_id": "wksp-1",
                }
            )
        )
        discord_binding = await client.upsert_surface_binding(
            SurfaceBindingUpsertRequest.from_mapping(
                {
                    "surface_kind": "discord",
                    "surface_key": "channel:2",
                    "thread_target_id": thread_target_id,
                    "agent_id": "codex",
                    "repo_id": "repo-1",
                    "resource_kind": "agent_workspace",
                    "resource_id": "wksp-1",
                    "mode": "switch",
                }
            )
        )
        fetched_thread = await client.get_thread_target(
            ThreadTargetLookupRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            )
        )
        listed_threads = await client.list_thread_targets(
            ThreadTargetListRequest.from_mapping(
                {
                    "agent_id": "codex",
                    "resource_kind": "agent_workspace",
                    "resource_id": "wksp-1",
                    "limit": 10,
                }
            )
        )
        listed_bindings = await client.list_surface_bindings(
            SurfaceBindingListRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "repo_id": "repo-1",
                    "resource_kind": "agent_workspace",
                    "resource_id": "wksp-1",
                    "agent_id": "codex",
                    "surface_kind": "discord",
                    "limit": 10,
                }
            )
        )
        first_delivery = await client.mark_notification_delivered(
            NotificationDeliveryMarkRequest.from_mapping(
                {
                    "delivery_record_id": "delivery-1",
                    "delivered_message_id": 88,
                }
            )
        )
        second_delivery = await client.mark_notification_delivered(
            NotificationDeliveryMarkRequest.from_mapping(
                {
                    "delivery_record_id": "delivery-1",
                    "delivered_message_id": 88,
                }
            )
        )
        reply_target = await client.get_notification_reply_target(
            NotificationReplyTargetLookupRequest.from_mapping(
                {
                    "surface_kind": "telegram",
                    "surface_key": "chat:1",
                    "delivered_message_id": 88,
                }
            )
        )
        running_execution = await client.create_execution(
            ExecutionCreateRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "prompt": "First remote turn",
                }
            )
        )
        queued_execution = await client.create_execution(
            ExecutionCreateRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "prompt": "Queued remote turn",
                    "busy_policy": "queue",
                    "queue_payload": {"source": "test"},
                }
            )
        )
        queued_list = await client.list_queued_executions(
            QueuedExecutionListRequest.from_mapping(
                {"thread_target_id": thread_target_id, "limit": 5}
            )
        )
        queue_depth = await client.get_queue_depth(
            QueueDepthRequest.from_mapping({"thread_target_id": thread_target_id})
        )
        timeline_events = [
            serialize_run_event(
                Started(
                    timestamp="2026-04-13T01:02:03Z",
                    session_id="session-1",
                    thread_id="backend-thread-1",
                    turn_id="backend-turn-1",
                )
            ),
            serialize_run_event(
                Completed(
                    timestamp="2026-04-13T01:02:04Z",
                    final_message="done",
                )
            ),
        ]
        timeline_result = await client.persist_execution_timeline(
            ExecutionTimelinePersistRequest.from_mapping(
                {
                    "execution_id": running_execution.execution.execution_id,
                    "target_kind": "thread_target",
                    "target_id": thread_target_id,
                    "repo_id": "repo-1",
                    "resource_kind": "agent_workspace",
                    "resource_id": "wksp-1",
                    "metadata": {"status": "ok", "surface_kind": "telegram"},
                    "events": timeline_events,
                    "start_index": 1,
                }
            )
        )
        trace_result = await client.finalize_execution_cold_trace(
            ExecutionColdTraceFinalizeRequest.from_mapping(
                {
                    "execution_id": running_execution.execution.execution_id,
                    "events": timeline_events,
                    "backend_thread_id": "backend-thread-1",
                    "backend_turn_id": "backend-turn-1",
                }
            )
        )
        transcript_result = await client.write_transcript(
            TranscriptWriteRequest.from_mapping(
                {
                    "turn_id": running_execution.execution.execution_id,
                    "metadata": {"managed_thread_id": thread_target_id},
                    "assistant_text": "done",
                }
            )
        )
        await client.set_execution_backend_id(
            ExecutionBackendIdUpdateRequest.from_mapping(
                {
                    "execution_id": running_execution.execution.execution_id,
                    "backend_turn_id": "backend-77",
                }
            )
        )
        finalized = await client.record_execution_result(
            ExecutionResultRecordRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "execution_id": running_execution.execution.execution_id,
                    "status": "ok",
                    "backend_turn_id": "backend-77",
                }
            )
        )
        claimed = await client.claim_next_queued_execution(
            ExecutionClaimNextRequest.from_mapping(
                {"thread_target_id": thread_target_id}
            )
        )
        setup_result = await client.run_workspace_setup_commands(
            WorkspaceSetupCommandRequest.from_mapping(
                {
                    "workspace_root": str(
                        tmp_path / "hub" / "agent-workspaces" / "zeroclaw" / "wksp-1"
                    ),
                    "repo_id_hint": "repo-1",
                }
            )
        )

    assert handshake.hub_asset_version == "asset-77"
    assert handshake.hub_build_version == "build-77"
    assert "notification_reply_targets" in handshake.capabilities
    assert first_delivery.record is not None
    assert second_delivery.record is not None
    assert first_delivery.record.delivered_message_id == "88"
    assert second_delivery.record.delivered_message_id == "88"
    assert fetched_thread.thread is not None
    assert fetched_thread.thread.agent_id == "codex"
    assert listed_threads.threads
    assert listed_threads.threads[0].agent_id == "codex"
    assert discord_binding.binding is not None
    assert [binding.binding_id for binding in listed_bindings.bindings] == [
        discord_binding.binding.binding_id
    ]
    assert reply_target.record is not None
    assert reply_target.record.notification_id == "notif-1"
    assert running_execution.execution is not None
    assert queued_execution.execution is not None
    assert (
        queued_list.executions[0].execution_id
        == queued_execution.execution.execution_id
    )
    assert queue_depth.queue_depth == 1
    assert timeline_result.persisted_event_count == 2
    assert trace_result.trace_manifest_id
    assert transcript_result.turn_id == running_execution.execution.execution_id
    assert finalized.execution is not None
    assert finalized.execution.backend_id == "backend-77"
    assert claimed.execution is not None
    assert claimed.execution.execution_id == queued_execution.execution.execution_id
    assert claimed.queue_payload == {"source": "test"}
    assert setup_result.setup_command_count == 3


@pytest.mark.anyio
async def test_hub_control_plane_http_client_preserves_thread_backend_ids(
    tmp_path: Path,
) -> None:
    app, thread_target_id = _build_test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http_client:
        client = HttpHubControlPlaneClient(
            base_url="http://testserver",
            http_client=http_client,
        )
        await client.set_thread_backend_id(
            ThreadBackendIdUpdateRequest.from_mapping(
                {
                    "thread_target_id": thread_target_id,
                    "backend_thread_id": "conversation-1",
                    "backend_runtime_instance_id": "runtime-1",
                }
            )
        )
        fetched = await client.get_thread_target(
            ThreadTargetLookupRequest(thread_target_id=thread_target_id)
        )

    assert fetched.thread is not None
    assert fetched.thread.agent_id == "codex"
    assert fetched.thread.backend_thread_id == "conversation-1"
    assert fetched.thread.backend_runtime_instance_id == "runtime-1"


@pytest.mark.anyio
async def test_hub_control_plane_http_client_maps_typed_errors(
    tmp_path: Path,
) -> None:
    app, _thread_target_id = _build_test_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as http_client:
        client = HttpHubControlPlaneClient(
            base_url="http://testserver",
            http_client=http_client,
        )
        with pytest.raises(HubControlPlaneError) as exc_info:
            await client.request_automation(
                AutomationRequest.from_mapping(
                    {
                        "operation": "unsupported-op",
                        "payload": {},
                    }
                )
            )

    assert exc_info.value.code == "hub_rejected"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_retryable"),
    [
        (
            HubControlPlaneError("hub_unavailable", "Hub service unreachable"),
            503,
            True,
        ),
        (
            HubControlPlaneError("hub_incompatible", "API version mismatch"),
            409,
            False,
        ),
        (
            HubControlPlaneError("hub_rejected", "Invalid request payload"),
            400,
            False,
        ),
    ],
)
def test_hub_control_plane_routes_map_error_codes_to_http_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: HubControlPlaneError,
    expected_status: int,
    expected_retryable: bool,
) -> None:
    app, _thread_target_id = _build_test_app(tmp_path)
    service = app.state.hub_control_plane_service

    def _raise_handshake(_request: object) -> object:
        raise error

    monkeypatch.setattr(service, "handshake", _raise_handshake)

    with TestClient(app) as client:
        response = client.post(
            "/hub/api/control-plane/handshake",
            json={"client_name": "telegram", "client_api_version": "1.0.0"},
        )

    assert response.status_code == expected_status
    body = response.json()
    assert body["error"]["code"] == error.code
    assert body["error"]["retryable"] is expected_retryable


def test_hub_control_plane_unknown_thread_target_returns_not_found(
    tmp_path: Path,
) -> None:
    app, _thread_target_id = _build_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/hub/api/control-plane/thread-targets/query",
            json={"thread_target_id": "nonexistent-thread"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body.get("thread") is None


def test_hub_control_plane_get_execution_returns_error_for_unknown(
    tmp_path: Path,
) -> None:
    app, thread_target_id = _build_test_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/hub/api/control-plane/executions/query",
            json={
                "thread_target_id": thread_target_id,
                "execution_id": "nonexistent-execution",
            },
        )

    assert response.status_code == 404
