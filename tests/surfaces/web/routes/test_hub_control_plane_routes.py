from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_autorunner.core.hub import AgentWorkspaceSnapshot
from codex_autorunner.core.hub_control_plane import (
    AutomationRequest,
    HandshakeRequest,
    HttpHubControlPlaneClient,
    HubControlPlaneError,
    NotificationDeliveryMarkRequest,
    NotificationReplyTargetLookupRequest,
    SurfaceBindingUpsertRequest,
    WorkspaceSetupCommandRequest,
)
from codex_autorunner.core.hub_control_plane.service import HubSharedStateService
from codex_autorunner.core.orchestration.sqlite import prepare_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    prepare_pma_thread_store,
)
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
    assert reply_target.record is not None
    assert reply_target.record.notification_id == "notif-1"
    assert setup_result.setup_command_count == 3


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
