from __future__ import annotations

from pathlib import Path

from codex_autorunner.core.hub_control_plane import (
    AutomationEventListRequest,
    AutomationJobActionRequest,
    AutomationJobListRequest,
    AutomationJobLookupRequest,
    AutomationRequest,
    AutomationRuleEnabledRequest,
    AutomationRuleListRequest,
    AutomationRuleRunRequest,
    AutomationRuleUpsertRequest,
    AutomationScheduleListRequest,
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
    PreviousCompletedExecutionLookupRequest,
    QueueDepthRequest,
    QueuedExecutionListRequest,
    RunningExecutionLookupRequest,
    SurfaceBindingListRequest,
    SurfaceBindingUpsertRequest,
    ThreadTargetListRequest,
    TranscriptHistoryRequest,
    TranscriptWriteRequest,
    WorkspaceSetupCommandRequest,
    serialize_run_event,
)
from codex_autorunner.core.managed_thread_store import (
    ManagedThreadStore,
    prepare_managed_thread_store,
)
from codex_autorunner.core.orchestration import SQLiteChatSurfaceEventJournal
from codex_autorunner.core.orchestration.cold_trace_store import ColdTraceStore
from codex_autorunner.core.orchestration.sqlite import prepare_orchestration_sqlite
from codex_autorunner.core.pma_notification_store import PmaNotificationStore
from codex_autorunner.core.pma_transcripts import PmaTranscriptStore
from codex_autorunner.core.ports.run_event import Completed, Started


class _SupervisorStub:
    def __init__(self) -> None:
        self.setup_calls: list[tuple[Path, str | None]] = []
        self.automation_calls: list[tuple[bool, int]] = []

    def run_setup_commands_for_workspace(
        self, workspace_root: Path, *, repo_id_hint: str | None = None
    ) -> int:
        self.setup_calls.append((workspace_root, repo_id_hint))
        return 2

    def process_automation_now(
        self, *, include_timers: bool = True, limit: int = 100
    ) -> dict[str, int]:
        self.automation_calls.append((include_timers, limit))
        return {
            "timers_processed": 1 if include_timers else 0,
            "wakeups_dispatched": limit,
        }


def _build_service(tmp_path: Path) -> tuple[HubSharedStateService, str]:
    hub_root = tmp_path / "hub"
    workspace_root = hub_root / "repos" / "repo-1"
    workspace_root.mkdir(parents=True, exist_ok=True)
    prepare_orchestration_sqlite(hub_root, durable=False)
    prepare_managed_thread_store(hub_root, durable=False)
    supervisor = _SupervisorStub()
    service = HubSharedStateService(
        hub_root=hub_root,
        supervisor=supervisor,
        hub_asset_version="web-asset-1",
        hub_build_version="build-1",
        durable_writes=False,
    )
    thread = ManagedThreadStore(
        hub_root, durable=False, bootstrap_on_init=False
    ).create_thread(
        "codex",
        workspace_root,
        repo_id="repo-1",
        resource_kind="repo",
        resource_id="repo-1",
        name="Repo Thread",
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

    assert handshake.api_version == "1.0.0"
    assert handshake.minimum_client_api_version == "1.0.0"
    assert handshake.hub_build_version == "build-1"
    assert handshake.hub_asset_version == "web-asset-1"
    assert "notification_reply_targets" in handshake.capabilities


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
        workspace_root=str(tmp_path / "hub" / "repos" / "repo-1"),
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
            "resource_kind": "repo",
            "resource_id": "repo-1",
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

    events = SQLiteChatSurfaceEventJournal(tmp_path / "hub").read_history()
    notification_events = [
        event
        for event in events
        if event.event_type == "notification.reply_context_changed"
    ]
    assert [event.status for event in notification_events] == [
        "recorded",
        "delivered",
    ]


def test_shared_state_service_lists_surface_bindings_with_filters(
    tmp_path: Path,
) -> None:
    service, thread_target_id = _build_service(tmp_path)
    workspace_root = tmp_path / "hub" / "repos" / "repo-1"
    second_thread = ManagedThreadStore(
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
                "resource_kind": "repo",
                "resource_id": "repo-1",
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
                "resource_kind": "repo",
                "resource_id": "repo-1",
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
            {"workspace_root": str(tmp_path / "hub" / "repos" / "repo-1")}
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
                "resource_kind": "repo",
                "resource_id": "repo-1",
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
    service.write_transcript(
        TranscriptWriteRequest.from_mapping(
            {
                "turn_id": "exec-2",
                "metadata": {"managed_thread_id": thread_target_id},
                "assistant_text": "done again",
            }
        )
    )
    transcript_history = service.get_transcript_history(
        TranscriptHistoryRequest.from_mapping(
            {
                "target_kind": "thread_target",
                "target_id": thread_target_id,
                "limit": 0,
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
    assert len(transcript_history.entries) == 2


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
    previous_completed = service.get_previous_completed_execution(
        PreviousCompletedExecutionLookupRequest.from_mapping(
            {
                "thread_target_id": thread_target_id,
                "exclude_execution_id": second.execution.execution_id,
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
    assert previous_completed.execution is not None
    assert previous_completed.execution.execution_id == first.execution.execution_id
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


def test_shared_state_service_automation_rule_crud_and_manual_run(
    tmp_path: Path,
) -> None:
    service, _thread_target_id = _build_service(tmp_path)

    created = service.upsert_automation_rule(
        AutomationRuleUpsertRequest.from_mapping(
            {
                "rule": {
                    "rule_id": "rule-1",
                    "name": "Daily PMA check",
                    "trigger_kind": "schedule",
                    "trigger": {"schedule_kind": "daily"},
                    "target_policy": "hub",
                    "target": {"repo_id": "repo-1"},
                    "executor_kind": "managed_thread_turn",
                    "executor": {
                        "lane_id": "pma:default",
                        "api_token": "secret-value",
                        "message": "Manual {{ event.payload.prompt }}",
                    },
                    "policy": {
                        "approval_mode": "pause_and_request_user",
                        "dedupe_key": "{{ metadata.manual_dedupe_key }}",
                    },
                },
                "schedule": {
                    "schedule_id": "schedule-1",
                    "schedule_kind": "daily",
                    "next_fire_at": "2026-04-12T01:02:03Z",
                },
            }
        )
    )
    disabled = service.set_automation_rule_enabled(
        AutomationRuleEnabledRequest.from_mapping(
            {"rule_id": "rule-1", "enabled": False}
        )
    )
    enabled = service.set_automation_rule_enabled(
        AutomationRuleEnabledRequest.from_mapping(
            {"rule_id": "rule-1", "enabled": True}
        )
    )
    rules = service.list_automation_rules(
        AutomationRuleListRequest.from_mapping({"enabled": True})
    )
    schedules = service.list_automation_schedules(
        AutomationScheduleListRequest.from_mapping({"rule_id": "rule-1"})
    )
    run = service.run_automation_rule(
        AutomationRuleRunRequest.from_mapping(
            {
                "rule_id": "rule-1",
                "payload": {"prompt": "check now", "secret": "hide me"},
                "dedupe_key": "manual-run-1",
            }
        )
    )
    jobs = service.list_automation_jobs(
        AutomationJobListRequest.from_mapping({"rule_id": "rule-1"})
    )
    events = service.list_automation_events(
        AutomationEventListRequest.from_mapping({"event_type": "manual.run"})
    )

    assert created.rule is not None
    assert created.rule["executor"]["api_token"] == "[redacted]"
    assert created.schedule is not None
    assert created.schedule["schedule_id"] == "schedule-1"
    assert disabled.rule is not None and disabled.rule["enabled"] is False
    assert enabled.rule is not None and enabled.rule["enabled"] is True
    assert [rule["rule_id"] for rule in rules.rules] == ["rule-1"]
    assert [schedule["schedule_id"] for schedule in schedules.schedules] == [
        "schedule-1"
    ]
    assert run.job is not None
    assert run.job["dedupe_key"] == "manual-run-1"
    assert run.job["executor"]["message"] == "Manual check now"
    assert run.job["payload"]["request"]["secret"] == "[redacted]"
    assert run.job["payload"]["event"]["event_type"] == "manual.run"
    assert [job["job_id"] for job in jobs.jobs] == [run.job["job_id"]]
    assert len(events.events) == 1
    assert events.events[0]["raw_payload"]["secret"] == "[redacted]"


def test_shared_state_service_automation_job_cancel_retry_and_detail(
    tmp_path: Path,
) -> None:
    service, _thread_target_id = _build_service(tmp_path)
    service.upsert_automation_rule(
        AutomationRuleUpsertRequest.from_mapping(
            {
                "rule_id": "rule-1",
                "name": "PR event",
                "trigger_kind": "event",
                "trigger": {
                    "event_types": ["scm.github.pull_request.opened"],
                },
                "target_policy": "hub",
                "executor_kind": "managed_thread_turn",
            }
        )
    )
    cancelled = service.run_automation_rule(
        AutomationRuleRunRequest.from_mapping(
            {"rule_id": "rule-1", "dedupe_key": "cancel-job"}
        )
    )
    assert cancelled.job is not None
    cancelled_job = service.cancel_automation_job(
        AutomationJobActionRequest.from_mapping({"job_id": cancelled.job["job_id"]})
    )

    failed = service.run_automation_rule(
        AutomationRuleRunRequest.from_mapping(
            {"rule_id": "rule-1", "dedupe_key": "retry-job"}
        )
    )
    assert failed.job is not None
    service._automation_store.start_job(failed.job["job_id"])
    service._automation_store.fail_job(
        failed.job["job_id"],
        error_text="temporary failure",
    )
    retried = service.retry_automation_job(
        AutomationJobActionRequest.from_mapping({"job_id": failed.job["job_id"]})
    )
    detail = service.get_automation_job(
        AutomationJobLookupRequest.from_mapping({"job_id": failed.job["job_id"]})
    )

    assert cancelled_job.job is not None
    assert cancelled_job.job["state"] == "cancelled"
    assert retried.job is not None
    assert retried.job["state"] == "pending"
    assert detail.job is not None
    assert detail.job["job_id"] == failed.job["job_id"]
