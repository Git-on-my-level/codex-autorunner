"""Flow lifecycle reducer — the single authority for flow state transitions.

Both the runtime (live execution) and the reconciler (recovery / housekeeping)
feed typed triggers into :func:`reduce_flow_lifecycle` and receive a
:dataclass:`TransitionResult` with the next status, derived state, and explicit
effect intents.  Callers apply the effects; the reducer stays pure.

Architecture
------------
Runtime path
    ``FlowRuntime.run_flow`` → ``FlowTrigger`` → ``reduce_flow_lifecycle``
    → ``_apply_transition`` (persists + emits).

Reconciler path
    ``resolve_reconcile_trigger`` → ``FlowTrigger`` → ``reduce_flow_lifecycle``
    → reconciler applies the ``TransitionResult`` (persist + emit telemetry).

There is no other code path that may mutate flow lifecycle state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .models import FlowRunRecord, FlowRunStatus
from .reasons import ensure_reason_summary


class _NoChange:
    _instance = None

    def __new__(cls) -> _NoChange:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NO_CHANGE"


NO_CHANGE = _NoChange()


class TriggerKind(str, Enum):
    FLOW_START = "flow_start"
    FLOW_RESUME = "flow_resume"
    STOP_REQUESTED = "stop_requested"
    STEP_CONTINUE = "step_continue"
    STEP_COMPLETE = "step_complete"
    STEP_FAIL = "step_fail"
    STEP_STOP = "step_stop"
    STEP_PAUSE = "step_pause"
    STEP_EXCEPTION = "step_exception"
    FLOW_EXCEPTION = "flow_exception"
    RECONCILE_ENGINE_COMPLETED = "reconcile_engine_completed"
    RECONCILE_WORKER_DEAD = "reconcile_worker_dead"
    RECONCILE_WORKER_SHUTDOWN = "reconcile_worker_shutdown"
    RECONCILE_ENGINE_PAUSED = "reconcile_engine_paused"
    RECONCILE_STALE_PAUSE_RESUME = "reconcile_stale_pause_resume"
    RECONCILE_STOPPING_FINALIZE = "reconcile_stopping_finalize"
    RECONCILE_CLEAR_STALE_ERROR = "reconcile_clear_stale_error"


class EffectKind(str, Enum):
    EMIT_FLOW_EVENT = "emit_flow_event"
    EMIT_LIFECYCLE_EVENT = "emit_lifecycle_event"
    ENRICH_FAILURE_PAYLOAD = "enrich_failure_payload"


@dataclass
class FlowTrigger:
    kind: TriggerKind
    step_id: Optional[str] = None
    next_steps: frozenset[str] = frozenset()
    error_message: Optional[str] = None
    state_output: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EffectIntent:
    kind: EffectKind
    event_type_name: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    step_id: Optional[str] = None
    error_message: Optional[str] = None
    note: Optional[str] = None


@dataclass
class TransitionResult:
    status: FlowRunStatus
    state: Any = NO_CHANGE
    current_step: Any = NO_CHANGE
    started_at: Any = NO_CHANGE
    finished_at: Any = NO_CHANGE
    error_message: Any = NO_CHANGE
    effects: List[EffectIntent] = field(default_factory=list)
    note: Optional[str] = None


class InvalidTransition(Exception):
    pass


def reduce_flow_lifecycle(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
    current_step: Optional[str] = None,
    initial_step: Optional[str] = None,
) -> TransitionResult:
    if trigger.kind == TriggerKind.FLOW_START:
        return _reduce_flow_start(
            current_status,
            current_state,
            trigger,
            now=now,
            current_step=current_step,
            initial_step=initial_step,
        )
    if trigger.kind == TriggerKind.FLOW_RESUME:
        return _reduce_flow_resume(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.STOP_REQUESTED:
        return _reduce_stop_requested(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.STEP_CONTINUE:
        return _reduce_step_continue(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.STEP_COMPLETE:
        return _reduce_step_complete(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.STEP_FAIL:
        return _reduce_step_fail(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.STEP_EXCEPTION:
        return _reduce_step_exception(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.STEP_STOP:
        return _reduce_step_stop(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.STEP_PAUSE:
        return _reduce_step_pause(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.FLOW_EXCEPTION:
        return _reduce_flow_exception(current_status, current_state, trigger, now=now)
    if trigger.kind == TriggerKind.RECONCILE_ENGINE_COMPLETED:
        return _reduce_reconcile_engine_completed(
            current_status, current_state, trigger, now=now
        )
    if trigger.kind == TriggerKind.RECONCILE_WORKER_DEAD:
        return _reduce_reconcile_worker_dead(
            current_status, current_state, trigger, now=now
        )
    if trigger.kind == TriggerKind.RECONCILE_WORKER_SHUTDOWN:
        return _reduce_reconcile_worker_shutdown(
            current_status, current_state, trigger, now=now
        )
    if trigger.kind == TriggerKind.RECONCILE_ENGINE_PAUSED:
        return _reduce_reconcile_engine_paused(
            current_status, current_state, trigger, now=now
        )
    if trigger.kind == TriggerKind.RECONCILE_STALE_PAUSE_RESUME:
        return _reduce_reconcile_stale_pause_resume(
            current_status, current_state, trigger, now=now
        )
    if trigger.kind == TriggerKind.RECONCILE_STOPPING_FINALIZE:
        return _reduce_reconcile_stopping_finalize(
            current_status, current_state, trigger, now=now
        )
    if trigger.kind == TriggerKind.RECONCILE_CLEAR_STALE_ERROR:
        return _reduce_reconcile_clear_stale_error(
            current_status, current_state, trigger, now=now
        )
    raise InvalidTransition(f"Unknown trigger kind: {trigger.kind}")


def _require_status(
    current: FlowRunStatus, *allowed: FlowRunStatus, trigger: TriggerKind
) -> None:
    if current not in allowed:
        names = ", ".join(s.value for s in allowed)
        raise InvalidTransition(
            f"Trigger {trigger.value} requires status in ({names}), "
            f"got {current.value}"
        )


def _merge_state(current_state: Dict[str, Any], trigger: FlowTrigger) -> Dict[str, Any]:
    if trigger.kind in (TriggerKind.FLOW_START, TriggerKind.FLOW_RESUME):
        if trigger.state_output:
            return dict(trigger.state_output)
        return dict(current_state)
    state = dict(current_state)
    if trigger.state_output:
        state.update(trigger.state_output)
    return state


def _reduce_flow_start(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
    current_step: Optional[str],
    initial_step: Optional[str],
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.PENDING, trigger=trigger.kind)
    step = current_step or initial_step
    return TransitionResult(
        status=FlowRunStatus.RUNNING,
        state=_merge_state(current_state, trigger),
        current_step=step,
        started_at=now,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT, event_type_name="flow_started"
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT, event_type_name="flow_started"
            ),
        ],
        note="flow-started",
    )


def _reduce_flow_resume(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    if current_status == FlowRunStatus.PENDING:
        raise InvalidTransition("Use FLOW_START for PENDING flows, not FLOW_RESUME")
    return TransitionResult(
        status=FlowRunStatus.RUNNING,
        state=_merge_state(current_state, trigger),
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT, event_type_name="flow_resumed"
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT, event_type_name="flow_resumed"
            ),
        ],
        note="flow-resumed",
    )


def _reduce_stop_requested(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(
        current_status,
        FlowRunStatus.RUNNING,
        FlowRunStatus.STOPPING,
        trigger=trigger.kind,
    )
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(
        state, status=FlowRunStatus.STOPPED, default="Stopped by user"
    )
    return TransitionResult(
        status=FlowRunStatus.STOPPED,
        state=state,
        finished_at=now,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT, event_type_name="flow_stopped"
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT, event_type_name="flow_stopped"
            ),
        ],
        note="stop-requested",
    )


def _reduce_step_continue(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    next_step = min(trigger.next_steps) if trigger.next_steps else None
    return TransitionResult(
        status=FlowRunStatus.RUNNING,
        state=_merge_state(current_state, trigger),
        current_step=next_step,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT,
                event_type_name="step_completed",
                data={
                    "step_id": trigger.step_id,
                    "next_steps": sorted(trigger.next_steps),
                },
                step_id=trigger.step_id,
            ),
        ],
        note="step-continue",
    )


def _reduce_step_complete(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    return TransitionResult(
        status=FlowRunStatus.COMPLETED,
        state=_merge_state(current_state, trigger),
        finished_at=now,
        current_step=None,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT,
                event_type_name="step_completed",
                data={"step_id": trigger.step_id, "status": "completed"},
                step_id=trigger.step_id,
            ),
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT, event_type_name="flow_completed"
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT, event_type_name="flow_completed"
            ),
        ],
        note="step-complete",
    )


def _reduce_step_fail(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(
        state, status=FlowRunStatus.FAILED, error_message=trigger.error_message
    )
    return TransitionResult(
        status=FlowRunStatus.FAILED,
        state=state,
        finished_at=now,
        error_message=trigger.error_message,
        current_step=None,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT,
                event_type_name="step_failed",
                data={"step_id": trigger.step_id, "error": trigger.error_message},
                step_id=trigger.step_id,
            ),
            EffectIntent(
                kind=EffectKind.ENRICH_FAILURE_PAYLOAD,
                step_id=trigger.step_id,
                error_message=trigger.error_message,
                note="step_failed",
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT,
                event_type_name="flow_failed",
                data={"error": trigger.error_message or ""},
            ),
        ],
        note="step-failed",
    )


def _reduce_step_exception(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(
        state, status=FlowRunStatus.FAILED, error_message=trigger.error_message
    )
    return TransitionResult(
        status=FlowRunStatus.FAILED,
        state=state,
        finished_at=now,
        error_message=trigger.error_message,
        current_step=None,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT,
                event_type_name="step_failed",
                data={"step_id": trigger.step_id, "error": trigger.error_message},
                step_id=trigger.step_id,
            ),
            EffectIntent(
                kind=EffectKind.ENRICH_FAILURE_PAYLOAD,
                step_id=trigger.step_id,
                error_message=trigger.error_message,
                note="step_exception",
            ),
        ],
        note="step-exception",
    )


def _reduce_step_stop(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(state, status=FlowRunStatus.STOPPED)
    return TransitionResult(
        status=FlowRunStatus.STOPPED,
        state=state,
        finished_at=now,
        current_step=None,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT,
                event_type_name="step_completed",
                data={"step_id": trigger.step_id, "status": "stopped"},
                step_id=trigger.step_id,
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT, event_type_name="flow_stopped"
            ),
        ],
        note="step-stop",
    )


def _reduce_step_pause(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(state, status=FlowRunStatus.PAUSED)
    return TransitionResult(
        status=FlowRunStatus.PAUSED,
        state=state,
        current_step=trigger.step_id,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT,
                event_type_name="step_completed",
                data={"step_id": trigger.step_id, "status": "paused"},
                step_id=trigger.step_id,
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT, event_type_name="flow_paused"
            ),
        ],
        note="step-pause",
    )


def _reduce_flow_exception(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(
        state, status=FlowRunStatus.FAILED, error_message=trigger.error_message
    )
    return TransitionResult(
        status=FlowRunStatus.FAILED,
        state=state,
        finished_at=now,
        error_message=trigger.error_message,
        current_step=None,
        effects=[
            EffectIntent(
                kind=EffectKind.EMIT_FLOW_EVENT,
                event_type_name="flow_failed",
                data={"error": trigger.error_message or ""},
            ),
            EffectIntent(
                kind=EffectKind.ENRICH_FAILURE_PAYLOAD,
                step_id=trigger.step_id,
                error_message=trigger.error_message,
                note="flow_exception",
            ),
            EffectIntent(
                kind=EffectKind.EMIT_LIFECYCLE_EVENT,
                event_type_name="flow_failed",
                data={"error": trigger.error_message or ""},
            ),
        ],
        note="flow-exception",
    )


def _reduce_reconcile_engine_completed(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(
        current_status,
        FlowRunStatus.RUNNING,
        FlowRunStatus.PAUSED,
        trigger=trigger.kind,
    )
    state = _merge_state(current_state, trigger)
    return TransitionResult(
        status=FlowRunStatus.COMPLETED,
        state=state,
        finished_at=now,
        current_step=None,
        note="engine-completed",
    )


def _reduce_reconcile_worker_dead(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(
        state, status=FlowRunStatus.FAILED, error_message=trigger.error_message
    )
    return TransitionResult(
        status=FlowRunStatus.FAILED,
        state=state,
        finished_at=now,
        error_message=trigger.error_message,
        current_step=None,
        effects=[
            EffectIntent(
                kind=EffectKind.ENRICH_FAILURE_PAYLOAD,
                error_message=trigger.error_message,
                note="worker_dead",
            ),
        ],
        note="worker-dead",
    )


def _reduce_reconcile_worker_shutdown(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(
        state, status=FlowRunStatus.STOPPED, default="Worker stopped"
    )
    return TransitionResult(
        status=FlowRunStatus.STOPPED,
        state=state,
        finished_at=now,
        current_step=None,
        note="worker-shutdown-intent",
    )


def _reduce_reconcile_engine_paused(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(state, status=FlowRunStatus.PAUSED)
    return TransitionResult(
        status=FlowRunStatus.PAUSED,
        state=state,
        note="engine-paused",
    )


def _reduce_reconcile_stale_pause_resume(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.PAUSED, trigger=trigger.kind)
    state = dict(current_state)
    engine = state.get("ticket_engine")
    if isinstance(engine, dict):
        engine = dict(engine)
        engine.pop("reason", None)
        engine.pop("reason_details", None)
        engine.pop("reason_code", None)
        engine.pop("pause_context", None)
        engine["status"] = "running"
        state["ticket_engine"] = engine
    state.pop("reason_summary", None)
    if trigger.state_output:
        state.update(trigger.state_output)
    return TransitionResult(
        status=FlowRunStatus.RUNNING,
        state=state,
        note="stale-pause-resumed",
    )


def _reduce_reconcile_stopping_finalize(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.STOPPING, trigger=trigger.kind)
    state = _merge_state(current_state, trigger)
    state = ensure_reason_summary(
        state, status=FlowRunStatus.STOPPED, default="Worker stopped"
    )
    return TransitionResult(
        status=FlowRunStatus.STOPPED,
        state=state,
        finished_at=now,
        current_step=None,
        note="worker-dead",
    )


def _reduce_reconcile_clear_stale_error(
    current_status: FlowRunStatus,
    current_state: Dict[str, Any],
    trigger: FlowTrigger,
    *,
    now: str,
) -> TransitionResult:
    _require_status(current_status, FlowRunStatus.RUNNING, trigger=trigger.kind)
    return TransitionResult(
        status=FlowRunStatus.RUNNING,
        state=dict(current_state),
        error_message=None,
        note="clear-stale-error",
    )


def resolve_reconcile_trigger(
    record: FlowRunRecord,
    health: Any,
) -> Optional[FlowTrigger]:
    """Map reconciliation context to a lifecycle trigger.

    Returns ``None`` when no state transition is warranted (true no-ops).
    Callers (the reconciler) feed the returned trigger into
    :func:`reduce_flow_lifecycle` to obtain the authoritative
    :class:`TransitionResult`.
    """
    status = record.status
    state: dict[str, Any] = record.state if isinstance(record.state, dict) else {}
    engine_raw = state.get("ticket_engine") if isinstance(state, dict) else {}
    engine: dict[str, Any] = engine_raw if isinstance(engine_raw, dict) else {}
    inner_status = engine.get("status")
    reason_code = engine.get("reason_code")
    is_alive = getattr(health, "is_alive", False)

    if status == FlowRunStatus.RUNNING:
        if inner_status == "completed":
            return FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_COMPLETED)

        if not is_alive:
            shutdown_intent = getattr(health, "shutdown_intent", False)
            if shutdown_intent:
                return FlowTrigger(kind=TriggerKind.RECONCILE_WORKER_SHUTDOWN)

            error_msg = f"Worker died (status={getattr(health, 'status', 'unknown')}"
            pid = getattr(health, "pid", None)
            if pid:
                error_msg += f", pid={pid}"
            message = getattr(health, "message", None)
            if message:
                error_msg += f", reason: {message}"
            exit_code = getattr(health, "exit_code", None)
            if isinstance(exit_code, int):
                error_msg += f", exit_code={exit_code}"
            error_msg += ")"
            return FlowTrigger(
                kind=TriggerKind.RECONCILE_WORKER_DEAD,
                error_message=error_msg,
            )

        if inner_status == "paused":
            return FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_PAUSED)

        if record.error_message:
            return FlowTrigger(kind=TriggerKind.RECONCILE_CLEAR_STALE_ERROR)

        return None

    if status == FlowRunStatus.STOPPING:
        if not is_alive:
            return FlowTrigger(kind=TriggerKind.RECONCILE_STOPPING_FINALIZE)
        return None

    if status == FlowRunStatus.PAUSED:
        if inner_status == "completed":
            return FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_COMPLETED)

        if (
            inner_status in (None, "running")
            and reason_code != "user_pause"
            and is_alive
        ):
            return FlowTrigger(kind=TriggerKind.RECONCILE_STALE_PAUSE_RESUME)

        return None

    return None
