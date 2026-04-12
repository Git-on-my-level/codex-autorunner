from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.hub import AgentWorkspaceSnapshot
from codex_autorunner.core.hub_control_plane import (
    AgentWorkspaceListRequest,
    AutomationRequest,
    HandshakeRequest,
    HubSharedStateService,
    NotificationReplyTargetLookupRequest,
    SurfaceBindingUpsertRequest,
    WorkspaceSetupCommandRequest,
)
from codex_autorunner.core.orchestration.sqlite import prepare_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    prepare_pma_thread_store,
)


class _SupervisorStub:
    def __init__(self, workspace_snapshot: AgentWorkspaceSnapshot) -> None:
        self._workspace_snapshot = workspace_snapshot
        self.setup_calls: list[tuple[Path, str | None]] = []
        self.automation_calls: list[tuple[bool, int]] = []

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
        self.setup_calls.append((workspace_root, repo_id_hint))
        return 2

    def process_pma_automation_now(
        self, *, include_timers: bool = True, limit: int = 100
    ) -> dict[str, int]:
        self.automation_calls.append((include_timers, limit))
        return {
            "timers_processed": 1 if include_timers else 0,
            "wakeups_dispatched": limit,
        }


def _build_service(tmp_path: Path) -> tuple[HubSharedStateService, str]:
    hub_root = tmp_path / "hub"
    workspace_root = hub_root / "agent-workspaces" / "zeroclaw" / "wksp-1"
    workspace_root.mkdir(parents=True, exist_ok=True)
    prepare_orchestration_sqlite(hub_root, durable=False)
    prepare_pma_thread_store(hub_root, durable=False)
    workspace_snapshot = AgentWorkspaceSnapshot(
        id="wksp-1",
        runtime="zeroclaw",
        path=workspace_root,
        display_name="Workspace One",
        enabled=True,
        exists_on_disk=True,
    )
    supervisor = _SupervisorStub(workspace_snapshot)
    service = HubSharedStateService(
        hub_root=hub_root,
        supervisor=supervisor,
        hub_asset_version="web-asset-1",
        hub_build_version="build-1",
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
    thread_target_id = str(thread["managed_thread_id"])
    return service, thread_target_id


def test_shared_state_service_handshake_and_listing(tmp_path: Path) -> None:
    service, _thread_target_id = _build_service(tmp_path)

    handshake = service.handshake(
        HandshakeRequest.from_mapping(
            {
                "client_name": "discord",
                "client_api_version": "1.0.0",
            }
        )
    )
    workspaces = service.list_agent_workspaces(
        AgentWorkspaceListRequest.from_mapping({"include_disabled": True})
    )

    assert handshake.api_version == "1.0.0"
    assert handshake.minimum_client_api_version == "1.0.0"
    assert handshake.hub_build_version == "build-1"
    assert handshake.hub_asset_version == "web-asset-1"
    assert "notification_reply_targets" in handshake.capabilities
    assert workspaces.workspaces[0].workspace_id == "wksp-1"


def test_shared_state_service_reply_lookup_and_binding_idempotency(
    tmp_path: Path,
) -> None:
    service, thread_target_id = _build_service(tmp_path)
    notification_store = PmaNotificationStore(tmp_path / "hub")
    recorded = notification_store.record_notification(
        correlation_id="corr-1",
        source_kind="run",
        delivery_mode="chat",
        surface_kind="telegram",
        surface_key="chat:1",
        delivery_record_id="delivery-1",
        repo_id="repo-1",
        workspace_root=str(
            tmp_path / "hub" / "agent-workspaces" / "zeroclaw" / "wksp-1"
        ),
        notification_id="notif-1",
    )
    notification_store.mark_delivered(
        delivery_record_id="delivery-1",
        delivered_message_id=99,
    )

    binding_request = SurfaceBindingUpsertRequest.from_mapping(
        {
            "surface_kind": "telegram",
            "surface_key": "chat:1",
            "thread_target_id": thread_target_id,
            "agent_id": "codex",
            "repo_id": "repo-1",
            "resource_kind": "agent_workspace",
            "resource_id": "wksp-1",
            "mode": "reuse",
        }
    )
    first_binding = service.upsert_surface_binding(binding_request)
    second_binding = service.upsert_surface_binding(binding_request)
    reply_target = service.get_notification_reply_target(
        NotificationReplyTargetLookupRequest.from_mapping(
            {
                "surface_kind": "telegram",
                "surface_key": "chat:1",
                "delivered_message_id": 99,
            }
        )
    )

    assert first_binding.binding is not None
    assert second_binding.binding is not None
    assert first_binding.binding.binding_id == second_binding.binding.binding_id
    assert reply_target.record is not None
    assert reply_target.record.notification_id == recorded.notification_id
    assert reply_target.record.delivered_message_id == "99"


def test_shared_state_service_workspace_setup_and_automation(tmp_path: Path) -> None:
    service, _thread_target_id = _build_service(tmp_path)

    setup_result = service.run_workspace_setup_commands(
        WorkspaceSetupCommandRequest.from_mapping(
            {
                "workspace_root": str(
                    tmp_path / "hub" / "agent-workspaces" / "zeroclaw" / "wksp-1"
                )
            }
        )
    )
    automation_result = service.request_automation(
        AutomationRequest.from_mapping(
            {
                "operation": "process_now",
                "payload": {"include_timers": False, "limit": 7},
            }
        )
    )

    assert setup_result.executed is True
    assert setup_result.setup_command_count == 2
    assert automation_result.accepted is True
    assert automation_result.payload == {
        "timers_processed": 0,
        "wakeups_dispatched": 7,
    }
