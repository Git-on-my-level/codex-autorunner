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
    shutdown_intent: bool = False,
    signal: Optional[str] = None,
    exit_origin: Optional[str] = None,
    exit_kind: Optional[str] = None,
    reap_reason: Optional[str] = None,
    exit_code: Optional[int] = None,
    last_semantic_progress_at: Optional[str] = None,
    stale_reason: Optional[str] = None,
    semantic_stale_age_seconds: Optional[int] = None,
) -> WorkerObservation:
    return WorkerObservation(
        status=status,
        pid=pid,
        exit=WorkerExitObservation(
            shutdown_intent=shutdown_intent,
            signal=signal,
            exit_origin=exit_origin,
            exit_kind=exit_kind,
            reap_reason=reap_reason,
            exit_code=exit_code,
        ),
        last_semantic_progress_at=last_semantic_progress_at,
        stale_reason=stale_reason,
        semantic_stale_age_seconds=semantic_stale_age_seconds,
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


def test_policy_classifies_stale_alive_worker_as_unhealthy() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(
                WorkerHealthStatus.STALE_ALIVE,
                pid=123,
                last_semantic_progress_at="2026-05-12T00:00:00+00:00",
                stale_reason="semantic_progress_stale_without_active_tool",
                semantic_stale_age_seconds=2400,
            ),
        )
    )

    assert decision.note == "stale-alive-worker"
    assert _intent_kinds(decision) == [
        RecoveryIntentKind.STALE_ALIVE_WORKER,
        RecoveryIntentKind.STALE_ALIVE_UNKNOWN,
    ]
    assert SupervisorEffectKind.WRITE_CRASH_ARTIFACT in _effect_kinds(decision)
    trigger = decision.first_lifecycle_trigger()
    assert trigger is not None
    assert trigger.kind == TriggerKind.RECONCILE_WORKER_DEAD
    assert "Worker stalled while still alive" in (trigger.error_message or "")


def test_policy_restarts_stale_alive_worker_when_budget_remains() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.STALE_ALIVE, pid=123),
            restart=RestartPolicyObservation(enabled=True, attempts=0, max_attempts=2),
        )
    )

    assert _intent_kinds(decision) == [
        RecoveryIntentKind.STALE_ALIVE_WORKER,
        RecoveryIntentKind.STALE_ALIVE_UNKNOWN,
        RecoveryIntentKind.RESTART_ATTEMPTED,
    ]
    assert SupervisorEffectKind.SPAWN_WORKER in _effect_kinds(decision)


def test_policy_restarts_stale_alive_commit_barrier_when_budget_remains() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(
                WorkerHealthStatus.STALE_ALIVE,
                pid=123,
                last_semantic_progress_at="2026-05-12T00:00:00+00:00",
                stale_reason="semantic_progress_stale_without_active_tool",
                semantic_stale_age_seconds=2400,
            ),
            commit_barrier=CommitBarrierObservation(
                current_ticket=".codex-autorunner/tickets/TICKET-001.md",
                current_ticket_done=True,
                worktree_dirty=True,
                commit_pending=True,
                barrier_epoch="commit-barrier:abc",
                retries=1,
                max_retries=3,
            ),
            restart=RestartPolicyObservation(enabled=True, attempts=0, max_attempts=2),
        )
    )

    assert decision.note == "stale-alive-commit-barrier-active"
    assert _intent_kinds(decision) == [
        RecoveryIntentKind.STALE_ALIVE_WORKER,
        RecoveryIntentKind.STALE_ALIVE_COMMIT_BARRIER_ACTIVE,
        RecoveryIntentKind.COMMIT_BARRIER_REQUIRED,
        RecoveryIntentKind.RESTART_ATTEMPTED,
    ]
    assert SupervisorEffectKind.SPAWN_WORKER in _effect_kinds(decision)
    spawn_effects = [
        effect
        for effect in decision.effects
        if effect.kind == SupervisorEffectKind.SPAWN_WORKER
    ]
    assert spawn_effects[0].data["reason"] == "stale_alive_commit_barrier_active"


def test_policy_does_not_restart_exhausted_stale_alive_commit_barrier() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.STALE_ALIVE, pid=123),
            commit_barrier=CommitBarrierObservation(
                current_ticket=".codex-autorunner/tickets/TICKET-001.md",
                current_ticket_done=True,
                worktree_dirty=True,
                commit_pending=True,
                barrier_epoch="commit-barrier:abc",
                retries=3,
                max_retries=3,
                exhausted=True,
            ),
            restart=RestartPolicyObservation(enabled=True, attempts=0, max_attempts=2),
        )
    )

    assert _intent_kinds(decision) == [
        RecoveryIntentKind.STALE_ALIVE_WORKER,
        RecoveryIntentKind.STALE_ALIVE_COMMIT_BARRIER_EXHAUSTED,
        RecoveryIntentKind.COMMIT_BARRIER_EXHAUSTED,
    ]
    assert SupervisorEffectKind.SPAWN_WORKER not in _effect_kinds(decision)


def test_policy_treats_signal_shutdown_as_recoverable_crash() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(status=FlowRunStatus.STOPPING, stop_requested=True),
            worker=_worker(
                WorkerHealthStatus.DEAD,
                pid=123,
                shutdown_intent=False,
                signal="SIGTERM",
                exit_origin="worker_signal",
                exit_kind="external_signal",
                exit_code=-15,
            ),
            restart=RestartPolicyObservation(enabled=True, attempts=0, max_attempts=2),
        )
    )

    assert _intent_kinds(decision) == [
        RecoveryIntentKind.WORKER_CRASH,
        RecoveryIntentKind.RESTART_ATTEMPTED,
    ]
    assert SupervisorEffectKind.SPAWN_WORKER in _effect_kinds(decision)
    trigger = decision.first_lifecycle_trigger()
    assert trigger is not None
    assert trigger.kind == TriggerKind.RECONCILE_WORKER_DEAD
    assert "exit_kind=external_signal" in (trigger.error_message or "")


def test_policy_treats_cooperative_sigterm_as_intentional_stop_not_recoverable() -> (
    None
):
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(status=FlowRunStatus.STOPPING, stop_requested=True),
            worker=_worker(
                WorkerHealthStatus.DEAD,
                pid=123,
                shutdown_intent=True,
                signal="SIGTERM",
                exit_origin="worker_signal",
                exit_kind="external_signal",
                exit_code=-15,
            ),
            restart=RestartPolicyObservation(enabled=True, attempts=0, max_attempts=2),
        )
    )

    assert _intent_kinds(decision) == []
    assert decision.note == "worker-stopped"
    assert SupervisorEffectKind.UPDATE_RUN_STATE in _effect_kinds(decision)
    trigger = decision.first_lifecycle_trigger()
    assert trigger is not None
    assert trigger.kind == TriggerKind.RECONCILE_STOPPING_FINALIZE


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


def test_policy_classifies_exhausted_commit_barrier() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.ALIVE, pid=123),
            commit_barrier=CommitBarrierObservation(
                current_ticket=".codex-autorunner/tickets/TICKET-001.md",
                current_ticket_done=True,
                worktree_dirty=True,
                commit_pending=True,
                barrier_epoch="commit-barrier:abc",
                retries=2,
                max_retries=2,
                exhausted=True,
            ),
        )
    )

    assert _intent_kinds(decision) == [RecoveryIntentKind.COMMIT_BARRIER_EXHAUSTED]
    assert decision.intents[0].data["barrier_epoch"] == "commit-barrier:abc"
    assert decision.intents[0].data["exhausted"] is True
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


def test_policy_treats_zero_restart_budget_as_disabled_not_exhausted() -> None:
    decision = supervise_flow_recovery(
        FlowSupervisorObservation(
            run=_run(),
            worker=_worker(WorkerHealthStatus.DEAD, pid=123),
            restart=RestartPolicyObservation(enabled=True, attempts=0, max_attempts=0),
        )
    )

    assert _intent_kinds(decision) == [RecoveryIntentKind.WORKER_CRASH]
    assert SupervisorEffectKind.SPAWN_WORKER not in _effect_kinds(decision)
    assert RecoveryIntentKind.RESTART_EXHAUSTED not in _intent_kinds(decision)


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
