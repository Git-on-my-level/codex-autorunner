from __future__ import annotations

from typing import Any, Dict, Optional

from codex_autorunner.core.flows.lifecycle_reducer import TriggerKind
from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.flows.supervisor import (
    CommitBarrierObservation,
    FlowSupervisorObservation,
    RecoveryIntentKind,
    RestartPolicyObservation,
    SupervisorEffectKind,
    WorkerExitObservation,
    WorkerHealthStatus,
    WorkerObservation,
    supervise_flow_recovery,
)


def _run(
    *,
    status: FlowRunStatus = FlowRunStatus.RUNNING,
    state: Optional[Dict[str, Any]] = None,
    stop_requested: bool = False,
    error_message: Optional[str] = None,
) -> FlowRunRecord:
    return FlowRunRecord(
        id="run-1",
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state=state or {},
        current_step="ticket_turn",
        stop_requested=stop_requested,
        created_at="2026-05-12T00:00:00+00:00",
        error_message=error_message,
    )


def _worker(
    status: WorkerHealthStatus,
    *,
    pid: Optional[int] = None,
    exit_origin: Optional[str] = None,
    exit_kind: Optional[str] = None,
    reap_reason: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> WorkerObservation:
    return WorkerObservation(
        status=status,
        pid=pid,
        exit=WorkerExitObservation(
            exit_origin=exit_origin,
            exit_kind=exit_kind,
            reap_reason=reap_reason,
            exit_code=exit_code,
        ),
    )


def _intent_kinds(decision: Any) -> list[RecoveryIntentKind]:
    return [intent.kind for intent in decision.intents]


def _effect_kinds(decision: Any) -> list[SupervisorEffectKind]:
    return [effect.kind for effect in decision.effects]


def test_policy_classifies_alive_worker_active_run_as_noop() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.ALIVE, pid=123),
        )
    )

    assert decision.note == "running-healthy"
    assert decision.intents == []
    assert decision.effects == []


def test_policy_classifies_dead_worker_active_run_as_crash() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.DEAD, pid=123, exit_code=1),
        )
    )

    assert _intent_kinds(decision) == [RecoveryIntentKind.WORKER_CRASH]
    assert SupervisorEffectKind.WRITE_CRASH_ARTIFACT in _effect_kinds(decision)
    assert SupervisorEffectKind.UPDATE_RUN_STATE in _effect_kinds(decision)
    trigger = decision.first_lifecycle_trigger()
    assert trigger is not None
    assert trigger.kind == TriggerKind.RECONCILE_WORKER_DEAD
    assert "pid=123" in (trigger.error_message or "")


def test_policy_classifies_stale_reaped_worker_active_run() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(
                WorkerHealthStatus.DEAD,
                pid=123,
                exit_origin="stale_reaper",
                exit_kind="reaped_stale",
                reap_reason="metadata_age_exceeded",
                exit_code=-15,
            ),
        )
    )

    assert _intent_kinds(decision) == [RecoveryIntentKind.STALE_WORKER_REAPED]
    trigger = decision.first_lifecycle_trigger()
    assert trigger is not None
    assert trigger.kind == TriggerKind.RECONCILE_WORKER_DEAD
    assert "reap_reason=metadata_age_exceeded" in (trigger.error_message or "")


def test_policy_classifies_user_stop_dead_worker() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(status=FlowRunStatus.PENDING, stop_requested=True),
            worker=_worker(WorkerHealthStatus.ABSENT),
        )
    )

    assert _intent_kinds(decision) == [RecoveryIntentKind.USER_STOP]
    trigger = decision.first_lifecycle_trigger()
    assert trigger is not None
    assert trigger.kind == TriggerKind.STOP_REQUESTED


def test_policy_classifies_done_current_ticket_dirty_worktree_as_commit_barrier() -> (
    None
):
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.ALIVE, pid=123),
            commit_barrier=CommitBarrierObservation(
                current_ticket=".codex-autorunner/tickets/TICKET-001.md",
                current_ticket_done=True,
                worktree_dirty=True,
            ),
        )
    )

    assert _intent_kinds(decision) == [RecoveryIntentKind.COMMIT_BARRIER_REQUIRED]
    assert _effect_kinds(decision) == [
        SupervisorEffectKind.NOTIFY_SURFACES,
        SupervisorEffectKind.EMIT_TELEMETRY,
    ]
    assert decision.first_lifecycle_trigger() is None


def test_policy_classifies_restart_attempts_exhausted() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.DEAD, pid=123),
            restart=RestartPolicyObservation(enabled=True, attempts=3, max_attempts=3),
        )
    )

    assert _intent_kinds(decision) == [
        RecoveryIntentKind.WORKER_CRASH,
        RecoveryIntentKind.RESTART_EXHAUSTED,
    ]
    assert SupervisorEffectKind.SPAWN_WORKER not in _effect_kinds(decision)
    assert _effect_kinds(decision).count(SupervisorEffectKind.NOTIFY_SURFACES) == 2


def test_issue1745_path_models_reaper_and_commit_barrier_without_surface_code() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(
                state={
                    "ticket_engine": {
                        "status": "running",
                        "current_ticket": ".codex-autorunner/tickets/TICKET-001.md",
                    }
                }
            ),
            worker=_worker(
                WorkerHealthStatus.DEAD,
                pid=99123,
                exit_origin="stale_reaper",
                exit_kind="reaped_stale",
                reap_reason="metadata_age_exceeded",
                exit_code=-15,
            ),
            commit_barrier=CommitBarrierObservation(
                current_ticket=".codex-autorunner/tickets/TICKET-001.md",
                current_ticket_done=True,
                worktree_dirty=True,
            ),
        )
    )

    assert _intent_kinds(decision) == [
        RecoveryIntentKind.STALE_WORKER_REAPED,
        RecoveryIntentKind.COMMIT_BARRIER_REQUIRED,
    ]
    assert SupervisorEffectKind.SPAWN_WORKER not in _effect_kinds(decision)
    assert SupervisorEffectKind.WRITE_CRASH_ARTIFACT in _effect_kinds(decision)
    trigger = decision.first_lifecycle_trigger()
    assert trigger is not None
    assert trigger.kind == TriggerKind.RECONCILE_WORKER_DEAD
    assert "pid=99123" in (trigger.error_message or "")
