"""Pure flow recovery supervision policy.

The supervisor observes run, worker, backend, and worktree health, then returns
typed intents for adapters to apply.  It does not mutate files, spawn workers,
or decide lifecycle status directly; lifecycle changes are represented as
``UPDATE_RUN_STATE`` effects carrying a :class:`FlowTrigger` for the lifecycle
reducer to apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .lifecycle_reducer import FlowTrigger, TriggerKind
from .models import FlowRunRecord, FlowRunStatus


class WorkerHealthStatus(str, Enum):
    ABSENT = "absent"
    ALIVE = "alive"
    DEAD = "dead"
    INVALID = "invalid"
    MISMATCH = "mismatch"

    @property
    def is_alive(self) -> bool:
        return self == WorkerHealthStatus.ALIVE

    @property
    def is_deadish(self) -> bool:
        return self in {
            WorkerHealthStatus.ABSENT,
            WorkerHealthStatus.DEAD,
            WorkerHealthStatus.INVALID,
            WorkerHealthStatus.MISMATCH,
        }


class RecoveryIntentKind(str, Enum):
    USER_STOP = "user_stop"
    WORKER_CRASH = "worker_crash"
    STALE_WORKER_REAPED = "stale_worker_reaped"
    BACKEND_DISCONNECT = "backend_disconnect"
    COMMIT_BARRIER_REQUIRED = "commit_barrier_required"
    RESTART_ATTEMPTED = "restart_attempted"
    RESTART_EXHAUSTED = "restart_exhausted"


class SupervisorEffectKind(str, Enum):
    UPDATE_RUN_STATE = "update_run_state"
    WRITE_CRASH_ARTIFACT = "write_crash_artifact"
    CLEAR_WORKER_METADATA = "clear_worker_metadata"
    SPAWN_WORKER = "spawn_worker"
    EMIT_LIFECYCLE_EVENT = "emit_lifecycle_event"
    NOTIFY_SURFACES = "notify_surfaces"
    EMIT_TELEMETRY = "emit_telemetry"


@dataclass(frozen=True)
class WorkerExitObservation:
    exit_code: Optional[int] = None
    signal: Optional[str] = None
    shutdown_intent: bool = False
    exit_origin: Optional[str] = None
    exit_kind: Optional[str] = None
    reap_reason: Optional[str] = None
    stderr_tail: Optional[str] = None

    @property
    def stale_reaper_exit(self) -> bool:
        return self.exit_origin == "stale_reaper" or self.exit_kind == "reaped_stale"


@dataclass(frozen=True)
class WorkerObservation:
    status: WorkerHealthStatus
    pid: Optional[int] = None
    message: Optional[str] = None
    artifact_path: Optional[Path] = None
    exit: WorkerExitObservation = field(default_factory=WorkerExitObservation)
    crash_info: Optional[Dict[str, Any]] = None

    @property
    def is_alive(self) -> bool:
        return self.status.is_alive

    @property
    def is_deadish(self) -> bool:
        return self.status.is_deadish


@dataclass(frozen=True)
class CommitBarrierObservation:
    current_ticket: Optional[str] = None
    current_ticket_done: bool = False
    worktree_dirty: bool = False
    commit_pending: bool = False

    @property
    def required(self) -> bool:
        return self.commit_pending or (self.current_ticket_done and self.worktree_dirty)


@dataclass(frozen=True)
class RestartPolicyObservation:
    enabled: bool = False
    attempts: int = 0
    max_attempts: int = 0
    backoff_ready: bool = True

    @property
    def exhausted(self) -> bool:
        return (
            self.enabled
            and self.max_attempts > 0
            and self.attempts >= self.max_attempts
        )

    @property
    def can_attempt(self) -> bool:
        return (
            self.enabled
            and self.backoff_ready
            and self.max_attempts > 0
            and self.attempts < self.max_attempts
        )


@dataclass(frozen=True)
class FlowSupervisorObservation:
    run: FlowRunRecord
    worker: WorkerObservation
    commit_barrier: CommitBarrierObservation = field(
        default_factory=CommitBarrierObservation
    )
    restart: RestartPolicyObservation = field(default_factory=RestartPolicyObservation)
    backend_connected: bool = True


@dataclass(frozen=True)
class RecoveryIntent:
    kind: RecoveryIntentKind
    reason: str
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SupervisorEffectIntent:
    kind: SupervisorEffectKind
    data: Dict[str, Any] = field(default_factory=dict)
    lifecycle_trigger: Optional[FlowTrigger] = None


@dataclass(frozen=True)
class FlowSupervisorDecision:
    intents: List[RecoveryIntent] = field(default_factory=list)
    effects: List[SupervisorEffectIntent] = field(default_factory=list)
    note: str = "noop"

    def first_lifecycle_trigger(self) -> Optional[FlowTrigger]:
        for effect in self.effects:
            if (
                effect.kind == SupervisorEffectKind.UPDATE_RUN_STATE
                and effect.lifecycle_trigger is not None
            ):
                return effect.lifecycle_trigger
        return None


def supervise_flow_recovery(
    observation: FlowSupervisorObservation,
) -> FlowSupervisorDecision:
    """Classify flow health and return typed effects for adapters to apply."""

    record = observation.run
    worker = observation.worker
    engine = _ticket_engine(record)
    inner_status = engine.get("status")
    reason_code = engine.get("reason_code")
    intents: List[RecoveryIntent] = []
    effects: List[SupervisorEffectIntent] = []

    if not observation.backend_connected and record.status in {
        FlowRunStatus.PENDING,
        FlowRunStatus.RUNNING,
        FlowRunStatus.PAUSED,
        FlowRunStatus.STOPPING,
    }:
        intents.append(
            RecoveryIntent(
                RecoveryIntentKind.BACKEND_DISCONNECT,
                "backend-disconnected",
                {"run_id": record.id},
            )
        )
        effects.extend(
            [
                _effect(
                    SupervisorEffectKind.EMIT_LIFECYCLE_EVENT, "backend_disconnect"
                ),
                _effect(SupervisorEffectKind.NOTIFY_SURFACES, "backend_disconnect"),
                _effect(SupervisorEffectKind.EMIT_TELEMETRY, "backend_disconnect"),
            ]
        )

    if record.status == FlowRunStatus.PENDING:
        if record.stop_requested and worker.is_deadish:
            intents.append(
                RecoveryIntent(
                    RecoveryIntentKind.USER_STOP,
                    "pending-run-stop-requested-without-worker",
                    {"run_id": record.id},
                )
            )
            effects.append(
                SupervisorEffectIntent(
                    SupervisorEffectKind.UPDATE_RUN_STATE,
                    {"reason": "user_stop"},
                    FlowTrigger(kind=TriggerKind.STOP_REQUESTED),
                )
            )
            effects.append(
                _effect(SupervisorEffectKind.EMIT_LIFECYCLE_EVENT, "user_stop")
            )
            return FlowSupervisorDecision(intents, effects, note="user-stop")
        return FlowSupervisorDecision(intents, effects, note="pending-noop")

    if record.status == FlowRunStatus.RUNNING:
        if inner_status == "completed":
            effects.append(
                SupervisorEffectIntent(
                    SupervisorEffectKind.UPDATE_RUN_STATE,
                    {"reason": "engine_completed"},
                    FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_COMPLETED),
                )
            )
            return FlowSupervisorDecision(intents, effects, note="engine-completed")

        if worker.is_deadish:
            if worker.exit.shutdown_intent and not worker.exit.stale_reaper_exit:
                effects.append(
                    SupervisorEffectIntent(
                        SupervisorEffectKind.UPDATE_RUN_STATE,
                        {"reason": "worker_shutdown_intent"},
                        FlowTrigger(kind=TriggerKind.RECONCILE_WORKER_SHUTDOWN),
                    )
                )
                effects.append(
                    _effect(
                        SupervisorEffectKind.EMIT_LIFECYCLE_EVENT,
                        "worker_shutdown_intent",
                    )
                )
                return FlowSupervisorDecision(
                    intents, effects, note="worker-shutdown-intent"
                )
            _append_worker_dead_decision(observation, intents, effects)
            return FlowSupervisorDecision(
                intents,
                effects,
                note=(
                    "stale-worker-reaped"
                    if worker.exit.stale_reaper_exit
                    else "worker-crash"
                ),
            )

        if inner_status == "paused":
            effects.append(
                SupervisorEffectIntent(
                    SupervisorEffectKind.UPDATE_RUN_STATE,
                    {"reason": "engine_paused"},
                    FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_PAUSED),
                )
            )
            return FlowSupervisorDecision(intents, effects, note="engine-paused")

        if observation.commit_barrier.required:
            _append_commit_barrier_intent(observation, intents, effects)
            return FlowSupervisorDecision(
                intents, effects, note="commit-barrier-required"
            )

        if record.error_message:
            effects.append(
                SupervisorEffectIntent(
                    SupervisorEffectKind.UPDATE_RUN_STATE,
                    {"reason": "clear_stale_error"},
                    FlowTrigger(kind=TriggerKind.RECONCILE_CLEAR_STALE_ERROR),
                )
            )
            return FlowSupervisorDecision(intents, effects, note="clear-stale-error")

        return FlowSupervisorDecision(intents, effects, note="running-healthy")

    if record.status == FlowRunStatus.STOPPING:
        if worker.is_deadish:
            if worker.exit.stale_reaper_exit:
                _append_worker_dead_decision(observation, intents, effects)
                return FlowSupervisorDecision(
                    intents, effects, note="stale-worker-reaped"
                )
            effects.append(
                SupervisorEffectIntent(
                    SupervisorEffectKind.UPDATE_RUN_STATE,
                    {"reason": "worker_stopped"},
                    FlowTrigger(kind=TriggerKind.RECONCILE_STOPPING_FINALIZE),
                )
            )
            effects.append(
                _effect(SupervisorEffectKind.EMIT_LIFECYCLE_EVENT, "worker_stopped")
            )
            return FlowSupervisorDecision(intents, effects, note="worker-stopped")
        return FlowSupervisorDecision(intents, effects, note="stopping-noop")

    if record.status == FlowRunStatus.PAUSED:
        if inner_status == "completed":
            effects.append(
                SupervisorEffectIntent(
                    SupervisorEffectKind.UPDATE_RUN_STATE,
                    {"reason": "engine_completed"},
                    FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_COMPLETED),
                )
            )
            return FlowSupervisorDecision(intents, effects, note="engine-completed")

        if (
            inner_status in (None, "running")
            and reason_code != "user_pause"
            and worker.is_alive
        ):
            effects.append(
                SupervisorEffectIntent(
                    SupervisorEffectKind.UPDATE_RUN_STATE,
                    {"reason": "stale_pause_resume"},
                    FlowTrigger(kind=TriggerKind.RECONCILE_STALE_PAUSE_RESUME),
                )
            )
            return FlowSupervisorDecision(intents, effects, note="stale-pause-resume")

        if worker.is_deadish:
            effects.extend(
                [
                    _effect(SupervisorEffectKind.WRITE_CRASH_ARTIFACT, "worker_dead"),
                    _effect(SupervisorEffectKind.NOTIFY_SURFACES, "worker_dead"),
                    _effect(SupervisorEffectKind.EMIT_TELEMETRY, "worker_dead"),
                ]
            )
            return FlowSupervisorDecision(intents, effects, note="paused-worker-dead")

        return FlowSupervisorDecision(intents, effects, note="paused-noop")

    return FlowSupervisorDecision(intents, effects, note="terminal-noop")


def worker_observation_from_health(health: Any) -> WorkerObservation:
    status = _worker_status(getattr(health, "status", "absent"))
    return WorkerObservation(
        status=status,
        pid=getattr(health, "pid", None),
        message=getattr(health, "message", None),
        artifact_path=getattr(health, "artifact_path", None),
        exit=WorkerExitObservation(
            exit_code=getattr(health, "exit_code", None),
            shutdown_intent=bool(getattr(health, "shutdown_intent", False)),
            exit_origin=getattr(health, "exit_origin", None),
            exit_kind=getattr(health, "exit_kind", None),
            reap_reason=getattr(health, "reap_reason", None),
            stderr_tail=getattr(health, "stderr_tail", None),
        ),
        crash_info=getattr(health, "crash_info", None),
    )


def supervise_reconcile_flow(
    record: FlowRunRecord,
    health: Any,
    *,
    commit_barrier: Optional[CommitBarrierObservation] = None,
    restart: Optional[RestartPolicyObservation] = None,
    backend_connected: bool = True,
) -> FlowSupervisorDecision:
    """Adapter shim for reconcilers that already have a run record and health."""

    return supervise_flow_recovery(
        FlowSupervisorObservation(
            run=record,
            worker=worker_observation_from_health(health),
            commit_barrier=commit_barrier or CommitBarrierObservation(),
            restart=restart or RestartPolicyObservation(),
            backend_connected=backend_connected,
        )
    )


def _append_worker_dead_decision(
    observation: FlowSupervisorObservation,
    intents: List[RecoveryIntent],
    effects: List[SupervisorEffectIntent],
) -> None:
    worker = observation.worker
    stale_reaped = worker.exit.stale_reaper_exit
    if stale_reaped:
        intents.append(
            RecoveryIntent(
                RecoveryIntentKind.STALE_WORKER_REAPED,
                "worker-reaped-by-stale-reaper",
                _worker_data(worker),
            )
        )
    else:
        intents.append(
            RecoveryIntent(
                RecoveryIntentKind.WORKER_CRASH,
                "worker-dead-while-run-active",
                _worker_data(worker),
            )
        )

    if observation.commit_barrier.required:
        _append_commit_barrier_intent(observation, intents, effects)

    effects.extend(
        [
            _effect(SupervisorEffectKind.WRITE_CRASH_ARTIFACT, "worker_dead"),
            SupervisorEffectIntent(
                SupervisorEffectKind.UPDATE_RUN_STATE,
                {"reason": "worker_dead"},
                FlowTrigger(
                    kind=TriggerKind.RECONCILE_WORKER_DEAD,
                    error_message=_worker_dead_error_message(worker),
                ),
            ),
            _effect(SupervisorEffectKind.CLEAR_WORKER_METADATA, "worker_dead"),
            _effect(SupervisorEffectKind.EMIT_LIFECYCLE_EVENT, "worker_dead"),
            _effect(SupervisorEffectKind.NOTIFY_SURFACES, "worker_dead"),
            _effect(SupervisorEffectKind.EMIT_TELEMETRY, "worker_dead"),
        ]
    )

    if observation.restart.exhausted:
        intents.append(
            RecoveryIntent(
                RecoveryIntentKind.RESTART_EXHAUSTED,
                "restart-attempts-exhausted",
                {
                    "attempts": observation.restart.attempts,
                    "max_attempts": observation.restart.max_attempts,
                },
            )
        )
        effects.append(
            _effect(SupervisorEffectKind.NOTIFY_SURFACES, "restart_exhausted")
        )
    elif observation.restart.can_attempt and not observation.commit_barrier.required:
        intents.append(
            RecoveryIntent(
                RecoveryIntentKind.RESTART_ATTEMPTED,
                "restart-attempted",
                {
                    "attempts": observation.restart.attempts,
                    "max_attempts": observation.restart.max_attempts,
                },
            )
        )
        effects.append(_effect(SupervisorEffectKind.SPAWN_WORKER, "restart_attempted"))


def _append_commit_barrier_intent(
    observation: FlowSupervisorObservation,
    intents: List[RecoveryIntent],
    effects: List[SupervisorEffectIntent],
) -> None:
    intents.append(
        RecoveryIntent(
            RecoveryIntentKind.COMMIT_BARRIER_REQUIRED,
            "done-current-ticket-has-uncommitted-worktree-changes",
            {
                "current_ticket": observation.commit_barrier.current_ticket,
                "current_ticket_done": observation.commit_barrier.current_ticket_done,
                "worktree_dirty": observation.commit_barrier.worktree_dirty,
                "commit_pending": observation.commit_barrier.commit_pending,
            },
        )
    )
    effects.extend(
        [
            _effect(SupervisorEffectKind.NOTIFY_SURFACES, "commit_barrier_required"),
            _effect(SupervisorEffectKind.EMIT_TELEMETRY, "commit_barrier_required"),
        ]
    )


def _ticket_engine(record: FlowRunRecord) -> Dict[str, Any]:
    state = record.state if isinstance(record.state, dict) else {}
    engine = state.get("ticket_engine") if isinstance(state, dict) else {}
    return engine if isinstance(engine, dict) else {}


def _worker_status(value: Any) -> WorkerHealthStatus:
    try:
        return WorkerHealthStatus(str(value))
    except ValueError:
        return WorkerHealthStatus.INVALID


def _worker_dead_error_message(worker: WorkerObservation) -> str:
    error_msg = f"Worker died (status={worker.status.value}"
    if worker.pid:
        error_msg += f", pid={worker.pid}"
    if worker.message:
        error_msg += f", reason: {worker.message}"
    if worker.exit.reap_reason:
        error_msg += f", reap_reason={worker.exit.reap_reason}"
    if isinstance(worker.exit.exit_code, int):
        error_msg += f", exit_code={worker.exit.exit_code}"
    error_msg += ")"
    return error_msg


def _worker_data(worker: WorkerObservation) -> Dict[str, Any]:
    return {
        "status": worker.status.value,
        "pid": worker.pid,
        "exit_code": worker.exit.exit_code,
        "exit_origin": worker.exit.exit_origin,
        "exit_kind": worker.exit.exit_kind,
        "reap_reason": worker.exit.reap_reason,
    }


def _effect(kind: SupervisorEffectKind, reason: str) -> SupervisorEffectIntent:
    return SupervisorEffectIntent(kind=kind, data={"reason": reason})
