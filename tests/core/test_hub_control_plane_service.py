from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.hub import AgentWorkspaceSnapshot
from codex_autorunner.core.hub_control_plane import (
    AgentWorkspaceListRequest,
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
    HubSharedStateService,
    LatestExecutionLookupRequest,
    NotificationReplyTargetLookupRequest,
    QueueDepthRequest,
    QueuedExecutionListRequest,
    RunningExecutionLookupRequest,
    SurfaceBindingListRequest,
    SurfaceBindingUpsertRequest,
    ThreadTargetListRequest,
    TranscriptWriteRequest,
    WorkspaceSetupCommandRequest,
    serialize_run_event,
)
from codex_autorunner.core.orchestration.cold_trace_store import ColdTraceStore
from codex_autorunner.core.orchestration.sqlite import prepare_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.pma_thread_store import (
    PmaThreadStore,
    prepare_pma_thread_store,
)
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore
from codex_autorunner.core.ports.run_event import Completed, Started


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


def test_shared_state_service_thread_listing_accepts_status_alias(
    tmp_path: Path,
) -> None:
    service, thread_target_id = _build_service(tmp_path)

    active = service.list_thread_targets(
        ThreadTargetListRequest.from_mapping({"status": "active"})
    )
    idle = service.list_thread_targets(
        ThreadTargetListRequest.from_mapping({"status": "idle"})
    )

    assert [thread.thread_target_id for thread in active.threads] == [thread_target_id]
    assert [thread.thread_target_id for thread in idle.threads] == [thread_target_id]


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


def test_shared_state_service_lists_surface_bindings_with_filters(
    tmp_path: Path,
) -> None:
    service, thread_target_id = _build_service(tmp_path)
    workspace_root = tmp_path / "hub" / "agent-workspaces" / "zeroclaw" / "wksp-1"
    second_thread = PmaThreadStore(
        tmp_path / "hub", durable=False, bootstrap_on_init=False
    ).create_thread(
        "opencode",
        workspace_root,
        repo_id="repo-2",
        resource_kind="repo",
        resource_id="repo-2",
        name="Repo Two Thread",
    )

    first_binding = service.upsert_surface_binding(
        SurfaceBindingUpsertRequest.from_mapping(
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
    )
    second_binding = service.upsert_surface_binding(
        SurfaceBindingUpsertRequest.from_mapping(
            {
                "surface_kind": "discord",
                "surface_key": "channel:2",
                "thread_target_id": str(second_thread["managed_thread_id"]),
                "agent_id": "opencode",
                "repo_id": "repo-2",
                "resource_kind": "repo",
                "resource_id": "repo-2",
                "mode": "switch",
            }
        )
    )
    assert second_binding.binding is not None
    service._binding_store.disable_binding(binding_id=second_binding.binding.binding_id)

    visible = service.list_surface_bindings(
        SurfaceBindingListRequest.from_mapping(
            {
                "repo_id": "repo-1",
                "resource_kind": "agent_workspace",
                "resource_id": "wksp-1",
                "agent_id": "codex",
                "surface_kind": "telegram",
                "thread_target_id": thread_target_id,
                "limit": 5,
            }
        )
    )
    include_disabled = service.list_surface_bindings(
        SurfaceBindingListRequest.from_mapping(
            {
                "repo_id": "repo-2",
                "resource_kind": "repo",
                "resource_id": "repo-2",
                "agent_id": "opencode",
                "surface_kind": "discord",
                "include_disabled": True,
                "limit": 5,
            }
        )
    )

    assert first_binding.binding is not None
    assert [binding.binding_id for binding in visible.bindings] == [
        first_binding.binding.binding_id
    ]
    assert [binding.binding_id for binding in include_disabled.bindings] == [
        second_binding.binding.binding_id
    ]
    assert include_disabled.bindings[0].disabled_at is not None


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


def test_shared_state_service_persists_timeline_transcript_and_cold_trace(
    tmp_path: Path,
) -> None:
    service, thread_target_id = _build_service(tmp_path)
    events = (
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
    )

    timeline_result = service.persist_execution_timeline(
        ExecutionTimelinePersistRequest.from_mapping(
            {
                "execution_id": "exec-1",
                "target_kind": "thread_target",
                "target_id": thread_target_id,
                "repo_id": "repo-1",
                "resource_kind": "agent_workspace",
                "resource_id": "wksp-1",
                "metadata": {"status": "ok", "surface_kind": "discord"},
                "events": list(events),
                "start_index": 1,
            }
        )
    )
    trace_result = service.finalize_execution_cold_trace(
        ExecutionColdTraceFinalizeRequest.from_mapping(
            {
                "execution_id": "exec-1",
                "events": list(events),
                "backend_thread_id": "backend-thread-1",
                "backend_turn_id": "backend-turn-1",
            }
        )
    )
    transcript_result = service.write_transcript(
        TranscriptWriteRequest.from_mapping(
            {
                "turn_id": "exec-1",
                "metadata": {"managed_thread_id": thread_target_id},
                "assistant_text": "done",
            }
        )
    )

    checkpoint = ColdTraceStore(tmp_path / "hub").load_checkpoint("exec-1")
    manifest = ColdTraceStore(tmp_path / "hub").get_manifest("exec-1")
    transcript = PmaTranscriptStore(tmp_path / "hub").read_transcript("exec-1")

    assert timeline_result.persisted_event_count == 2
    assert checkpoint is not None
    assert checkpoint.execution_id == "exec-1"
    assert trace_result.trace_manifest_id
    assert manifest is not None
    assert manifest.trace_id == trace_result.trace_manifest_id
    assert transcript_result.turn_id == "exec-1"
    assert transcript is not None
    assert transcript["content"] == "done"


def test_shared_state_service_execution_lifecycle_delegates_to_thread_store(
    tmp_path: Path,
) -> None:
    service, thread_target_id = _build_service(tmp_path)

    first = service.create_execution(
        ExecutionCreateRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "prompt": "First turn",
                "client_request_id": "client-1",
            }
        )
    )
    second = service.create_execution(
        ExecutionCreateRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "prompt": "Second turn",
                "busy_policy": "queue",
                "client_request_id": "client-2",
                "queue_payload": {"priority": "normal"},
            }
        )
    )

    assert first.execution is not None
    assert first.execution.status == "running"
    assert second.execution is not None
    assert second.execution.status == "queued"

    running = service.get_running_execution(
        RunningExecutionLookupRequest.from_mapping(
            {"thread_target_id": thread_target_id}
        )
    )
    latest = service.get_latest_execution(
        LatestExecutionLookupRequest.from_mapping(
            {"thread_target_id": thread_target_id}
        )
    )
    queued = service.list_queued_executions(
        QueuedExecutionListRequest.from_mapping(
            {"thread_target_id": thread_target_id, "limit": 5}
        )
    )
    queue_depth = service.get_queue_depth(
        QueueDepthRequest.from_mapping({"thread_target_id": thread_target_id})
    )

    assert running.execution is not None
    assert running.execution.execution_id == first.execution.execution_id
    assert latest.execution is not None
    assert latest.execution.execution_id == first.execution.execution_id
    assert [execution.execution_id for execution in queued.executions] == [
        second.execution.execution_id
    ]
    assert queue_depth.queue_depth == 1

    service.set_execution_backend_id(
        ExecutionBackendIdUpdateRequest.from_mapping(
            {
                "execution_id": first.execution.execution_id,
                "backend_turn_id": "backend-1",
            }
        )
    )
    finished = service.record_execution_result(
        ExecutionResultRecordRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "execution_id": first.execution.execution_id,
                "status": "ok",
                "assistant_text": "done",
                "backend_turn_id": "backend-1",
                "transcript_turn_id": "tx-1",
            }
        )
    )
    claimed = service.claim_next_queued_execution(
        ExecutionClaimNextRequest.from_mapping({"thread_target_id": thread_target_id})
    )
    interrupted = service.record_execution_interrupted(
        ExecutionInterruptRecordRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "execution_id": second.execution.execution_id,
            }
        )
    )
    lookup = service.get_execution(
        ExecutionLookupRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "execution_id": first.execution.execution_id,
            }
        )
    )

    assert finished.execution is not None
    assert finished.execution.status == "ok"
    assert lookup.execution is not None
    assert lookup.execution.backend_id == "backend-1"
    assert claimed.execution is not None
    assert claimed.execution.execution_id == second.execution.execution_id
    assert claimed.queue_payload == {"priority": "normal"}
    assert interrupted.execution is not None
    assert interrupted.execution.status == "interrupted"


def test_shared_state_service_execution_queue_controls_delegate_to_thread_store(
    tmp_path: Path,
) -> None:
    service, thread_target_id = _build_service(tmp_path)

    running = service.create_execution(
        ExecutionCreateRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "prompt": "Running turn",
            }
        )
    )
    queued_one = service.create_execution(
        ExecutionCreateRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "prompt": "Queued one",
                "busy_policy": "queue",
            }
        )
    )
    queued_two = service.create_execution(
        ExecutionCreateRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "prompt": "Queued two",
                "busy_policy": "queue",
            }
        )
    )

    promote = service.promote_queued_execution(
        ExecutionPromoteRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "execution_id": queued_two.execution.execution_id,
            }
        )
    )
    cancel = service.cancel_queued_execution(
        ExecutionCancelRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "execution_id": queued_one.execution.execution_id,
            }
        )
    )
    cancel_all = service.cancel_queued_executions(
        ExecutionCancelAllRequest.from_mapping({"thread_target_id": thread_target_id})
    )
    queue_depth = service.get_queue_depth(
        QueueDepthRequest.from_mapping({"thread_target_id": thread_target_id})
    )

    assert running.execution is not None
    assert queued_one.execution is not None
    assert queued_two.execution is not None
    assert promote.promoted is True
    assert cancel.cancelled is True
    assert cancel_all.cancelled_count == 1
    assert queue_depth.queue_depth == 0
