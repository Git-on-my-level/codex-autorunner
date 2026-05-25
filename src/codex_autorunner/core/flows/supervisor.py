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
    STALE_ALIVE = "stale_alive"
    DEAD = "dead"
    INVALID = "invalid"
    MISMATCH = "mismatch"

    @property
    def is_alive(self) -> bool:
        return self in {WorkerHealthStatus.ALIVE, WorkerHealthStatus.STALE_ALIVE}

    @property
    def is_deadish(self) -> bool:
        return self in {
            WorkerHealthStatus.ABSENT,
            WorkerHealthStatus.STALE_ALIVE,
            WorkerHealthStatus.DEAD,
            WorkerHealthStatus.INVALID,
            WorkerHealthStatus.MISMATCH,
        }


class RecoveryIntentKind(str, Enum):
    USER_STOP = "user_stop"
    WORKER_CRASH = "worker_crash"
    STALE_WORKER_REAPED = "stale_worker_reaped"
    STALE_ALIVE_WORKER = "stale_alive_worker"
    STALE_ALIVE_COMMIT_BARRIER_ACTIVE = "stale_alive_commit_barrier_active"
    STALE_ALIVE_COMMIT_BARRIER_EXHAUSTED = "stale_alive_commit_barrier_exhausted"
    STALE_ALIVE_UNKNOWN = "stale_alive_unknown"
    BACKEND_DISCONNECT = "backend_disconnect"
    COMMIT_BARRIER_REQUIRED = "commit_barrier_required"
    COMMIT_BARRIER_EXHAUSTED = "commit_barrier_exhausted"
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

    @property
    def recoverable_shutdown(self) -> bool:
        if self.exit_origin == "worker_watchdog":
            return True
        if self.exit_kind in {
            "max_wall_time",
            "opencode_stream_stalled_timeout",
            "app_server_stalled",
            "idle_stale",
            "missing_worker",
            "rotation_requested",
        }:
            return True
        # SIGTERM from the parent uses the same handler as an external signal, but
        # cooperative shutdown records shutdown_intent=True in worker.exit.json.
        # Only treat signal loss as recoverable when that cooperative bit is absent.
        if self.exit_origin == "worker_signal" and self.exit_kind == "external_signal":
            return not self.shutdown_intent
        return False


@dataclass(frozen=True)
class WorkerObservation:
    status: WorkerHealthStatus
    pid: Optional[int] = None
    message: Optional[str] = None
    artifact_path: Optional[Path] = None
    exit: WorkerExitObservation = field(default_factory=WorkerExitObservation)
    crash_info: Optional[Dict[str, Any]] = None
    last_semantic_progress_at: Optional[str] = None
    last_tool_activity_at: Optional[str] = None
    current_phase: Optional[str] = None
    stale_reason: Optional[str] = None
    stale_threshold_seconds: Optional[int] = None
    semantic_stale_age_seconds: Optional[int] = None

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
    barrier_epoch: Optional[str] = None
    retries: int = 0
    max_retries: Optional[int] = None
    exhausted: bool = False

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
            if (
                record.stop_requested
                and worker.exit.shutdown_intent
                and not worker.exit.stale_reaper_exit
                and not worker.exit.recoverable_shutdown
            ):
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
                    (
                        "stale-alive-commit-barrier-active"
                        if (
                            worker.status == WorkerHealthStatus.STALE_ALIVE
                            and observation.commit_barrier.required
                            and not observation.commit_barrier.exhausted
                            and observation.restart.can_attempt
                        )
                        else "stale-alive-worker"
                    )
                    if worker.status == WorkerHealthStatus.STALE_ALIVE
                    else (
                        "stale-worker-reaped"
                        if worker.exit.stale_reaper_exit
                        else "worker-crash"
                    )
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
            if worker.exit.recoverable_shutdown:
                _append_worker_dead_decision(observation, intents, effects)
                return FlowSupervisorDecision(
                    intents, effects, note="recoverable-worker-shutdown"
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
            signal=getattr(health, "signal", None),
            shutdown_intent=bool(getattr(health, "shutdown_intent", False)),
            exit_origin=getattr(health, "exit_origin", None),
            exit_kind=getattr(health, "exit_kind", None),
            reap_reason=getattr(health, "reap_reason", None),
            stderr_tail=getattr(health, "stderr_tail", None),
        ),
        crash_info=getattr(health, "crash_info", None),
        last_semantic_progress_at=getattr(health, "last_semantic_progress_at", None),
        last_tool_activity_at=getattr(health, "last_tool_activity_at", None),
        current_phase=getattr(health, "current_phase", None),
        stale_reason=getattr(health, "stale_reason", None),
        stale_threshold_seconds=getattr(health, "stale_threshold_seconds", None),
        semantic_stale_age_seconds=getattr(health, "semantic_stale_age_seconds", None),
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
    stale_alive = worker.status == WorkerHealthStatus.STALE_ALIVE
    if stale_alive:
        intents.append(
            RecoveryIntent(
                RecoveryIntentKind.STALE_ALIVE_WORKER,
                "alive-worker-stale-semantic-progress",
                _worker_data(worker),
            )
        )
    elif stale_reaped:
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

    if stale_alive:
        if observation.commit_barrier.required:
            intents.append(
                RecoveryIntent(
                    (
                        RecoveryIntentKind.STALE_ALIVE_COMMIT_BARRIER_EXHAUSTED
                        if observation.commit_barrier.exhausted
                        else RecoveryIntentKind.STALE_ALIVE_COMMIT_BARRIER_ACTIVE
                    ),
                    (
                        "stale-alive-commit-barrier-exhausted"
                        if observation.commit_barrier.exhausted
                        else "stale-alive-commit-barrier-active"
                    ),
                    {
                        **_worker_data(worker),
                        "current_ticket": observation.commit_barrier.current_ticket,
                        "barrier_epoch": observation.commit_barrier.barrier_epoch,
                        "commit_retries": observation.commit_barrier.retries,
                        "commit_max_retries": observation.commit_barrier.max_retries,
                    },
                )
            )
        else:
            intents.append(
                RecoveryIntent(
                    RecoveryIntentKind.STALE_ALIVE_UNKNOWN,
                    "stale-alive-without-classified-blocker",
                    _worker_data(worker),
                )
            )

    if observation.commit_barrier.required:
        _append_commit_barrier_intent(observation, intents, effects)

    effects.extend(
        [
            _effect(
                SupervisorEffectKind.WRITE_CRASH_ARTIFACT,
                "stale_alive_worker" if stale_alive else "worker_dead",
            ),
            SupervisorEffectIntent(
                SupervisorEffectKind.UPDATE_RUN_STATE,
                {"reason": "stale_alive_worker" if stale_alive else "worker_dead"},
                FlowTrigger(
                    kind=TriggerKind.RECONCILE_WORKER_DEAD,
                    error_message=_worker_dead_error_message(worker),
                ),
            ),
            _effect(
                SupervisorEffectKind.CLEAR_WORKER_METADATA,
                "stale_alive_worker" if stale_alive else "worker_dead",
            ),
            _effect(
                SupervisorEffectKind.EMIT_LIFECYCLE_EVENT,
                "stale_alive_worker" if stale_alive else "worker_dead",
            ),
            _effect(
                SupervisorEffectKind.NOTIFY_SURFACES,
                "stale_alive_worker" if stale_alive else "worker_dead",
            ),
            _effect(
                SupervisorEffectKind.EMIT_TELEMETRY,
                "stale_alive_worker" if stale_alive else "worker_dead",
            ),
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
    elif observation.restart.can_attempt and (
        not observation.commit_barrier.required
        or (
            stale_alive
            and observation.commit_barrier.required
            and not observation.commit_barrier.exhausted
        )
    ):
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
        effects.append(
            _effect(
                SupervisorEffectKind.SPAWN_WORKER,
                (
                    "stale_alive_commit_barrier_active"
                    if stale_alive and observation.commit_barrier.required
                    else "restart_attempted"
                ),
            )
        )


def _append_commit_barrier_intent(
    observation: FlowSupervisorObservation,
    intents: List[RecoveryIntent],
    effects: List[SupervisorEffectIntent],
) -> None:
    exhausted = bool(observation.commit_barrier.exhausted)
    intents.append(
        RecoveryIntent(
            (
                RecoveryIntentKind.COMMIT_BARRIER_EXHAUSTED
                if exhausted
                else RecoveryIntentKind.COMMIT_BARRIER_REQUIRED
            ),
            (
                "commit-barrier-retry-budget-exhausted"
                if exhausted
                else "done-current-ticket-has-uncommitted-worktree-changes"
            ),
            {
                "current_ticket": observation.commit_barrier.current_ticket,
                "current_ticket_done": observation.commit_barrier.current_ticket_done,
                "worktree_dirty": observation.commit_barrier.worktree_dirty,
                "commit_pending": observation.commit_barrier.commit_pending,
                "barrier_epoch": observation.commit_barrier.barrier_epoch,
                "retries": observation.commit_barrier.retries,
                "max_retries": observation.commit_barrier.max_retries,
                "exhausted": exhausted,
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
    if worker.status == WorkerHealthStatus.STALE_ALIVE:
        error_msg = "Worker stalled while still alive"
        if worker.pid:
            error_msg += f" (status={worker.status.value}, pid={worker.pid}"
        else:
            error_msg += f" (status={worker.status.value}"
        if worker.stale_reason:
            error_msg += f", reason: {worker.stale_reason}"
        if worker.last_semantic_progress_at:
            error_msg += (
                f", last_semantic_progress_at={worker.last_semantic_progress_at}"
            )
        if isinstance(worker.semantic_stale_age_seconds, int):
            error_msg += (
                f", semantic_stale_age_seconds={worker.semantic_stale_age_seconds}"
            )
        if isinstance(worker.stale_threshold_seconds, int):
            error_msg += f", stale_threshold_seconds={worker.stale_threshold_seconds}"
        error_msg += ")"
        return error_msg
    error_msg = f"Worker died (status={worker.status.value}"
    if worker.pid:
        error_msg += f", pid={worker.pid}"
    if worker.message:
        error_msg += f", reason: {worker.message}"
    if worker.exit.reap_reason:
        error_msg += f", reap_reason={worker.exit.reap_reason}"
    if worker.exit.signal:
        error_msg += f", signal={worker.exit.signal}"
    if worker.exit.exit_origin:
        error_msg += f", exit_origin={worker.exit.exit_origin}"
    if worker.exit.exit_kind:
        error_msg += f", exit_kind={worker.exit.exit_kind}"
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
        "last_semantic_progress_at": worker.last_semantic_progress_at,
        "last_tool_activity_at": worker.last_tool_activity_at,
        "current_phase": worker.current_phase,
        "stale_reason": worker.stale_reason,
        "stale_threshold_seconds": worker.stale_threshold_seconds,
        "semantic_stale_age_seconds": worker.semantic_stale_age_seconds,
    }


def _effect(kind: SupervisorEffectKind, reason: str) -> SupervisorEffectIntent:
    return SupervisorEffectIntent(kind=kind, data={"reason": reason})
