from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows.lifecycle_reducer import (
    NO_CHANGE,
    EffectKind,
    FlowTrigger,
    InvalidTransition,
    TransitionResult,
    TriggerKind,
    reduce_flow_lifecycle,
    resolve_reconcile_trigger,
)
from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus

_NOW = "2024-01-15T12:00:00Z"


def _reduce(
    status: FlowRunStatus,
    trigger: FlowTrigger,
    *,
    state: dict | None = None,
    current_step: str | None = None,
    initial_step: str | None = "step_a",
) -> TransitionResult:
    return reduce_flow_lifecycle(
        status,
        state or {},
        trigger,
        now=_NOW,
        current_step=current_step,
        initial_step=initial_step,
    )


def _effect_kinds(result: TransitionResult) -> list[str]:
    return [e.kind.value for e in result.effects]


def _effect_event_names(result: TransitionResult) -> list[str]:
    return [e.event_type_name for e in result.effects if e.event_type_name]


class TestFlowStart:
    def test_pending_to_running(self):
        result = _reduce(
            FlowRunStatus.PENDING,
            FlowTrigger(kind=TriggerKind.FLOW_START),
            current_step=None,
            initial_step="step_a",
        )
        assert result.status == FlowRunStatus.RUNNING
        assert result.started_at == _NOW
        assert result.current_step == "step_a"
        assert result.finished_at is NO_CHANGE
        assert "emit_flow_event" in _effect_kinds(result)
        assert "emit_lifecycle_event" in _effect_kinds(result)

    def test_uses_current_step_over_initial(self):
        result = _reduce(
            FlowRunStatus.PENDING,
            FlowTrigger(kind=TriggerKind.FLOW_START),
            current_step="step_b",
            initial_step="step_a",
        )
        assert result.current_step == "step_b"

    def test_rejects_non_pending(self):
        for status in (
            FlowRunStatus.RUNNING,
            FlowRunStatus.COMPLETED,
            FlowRunStatus.FAILED,
            FlowRunStatus.STOPPED,
        ):
            with pytest.raises(InvalidTransition):
                _reduce(status, FlowTrigger(kind=TriggerKind.FLOW_START))

    def test_state_output_replaces_state(self):
        result = _reduce(
            FlowRunStatus.PENDING,
            FlowTrigger(kind=TriggerKind.FLOW_START, state_output={"k": "v"}),
            state={"old": True},
        )
        assert result.state == {"k": "v"}

    def test_missing_state_output_preserves_current(self):
        result = _reduce(
            FlowRunStatus.PENDING,
            FlowTrigger(kind=TriggerKind.FLOW_START),
            state={"existing": True},
        )
        assert result.state == {"existing": True}

    def test_explicit_empty_state_output_clears_state(self):
        result = _reduce(
            FlowRunStatus.PENDING,
            FlowTrigger(kind=TriggerKind.FLOW_START, state_output={}),
            state={"existing": True},
        )
        assert result.state == {}


class TestNoChangeSentinel:
    def test_no_change_is_falsy(self):
        assert not NO_CHANGE
        assert bool(NO_CHANGE) is False


class TestFlowResume:
    def test_stopped_to_running(self):
        result = _reduce(
            FlowRunStatus.STOPPED,
            FlowTrigger(kind=TriggerKind.FLOW_RESUME),
        )
        assert result.status == FlowRunStatus.RUNNING
        assert result.started_at is NO_CHANGE
        assert result.note == "flow-resumed"

    def test_failed_to_running(self):
        result = _reduce(
            FlowRunStatus.FAILED,
            FlowTrigger(kind=TriggerKind.FLOW_RESUME),
        )
        assert result.status == FlowRunStatus.RUNNING

    def test_paused_to_running(self):
        result = _reduce(
            FlowRunStatus.PAUSED,
            FlowTrigger(kind=TriggerKind.FLOW_RESUME),
        )
        assert result.status == FlowRunStatus.RUNNING

    def test_rejects_pending(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PENDING,
                FlowTrigger(kind=TriggerKind.FLOW_RESUME),
            )


class TestStopRequested:
    def test_running_to_stopped(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.STOP_REQUESTED),
        )
        assert result.status == FlowRunStatus.STOPPED
        assert result.finished_at == _NOW
        assert result.current_step is NO_CHANGE
        assert result.state.get("reason_summary") == "Stopped by user"
        assert "flow_stopped" in _effect_event_names(result)

    def test_stopping_to_stopped(self):
        result = _reduce(
            FlowRunStatus.STOPPING,
            FlowTrigger(kind=TriggerKind.STOP_REQUESTED),
        )
        assert result.status == FlowRunStatus.STOPPED

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PAUSED,
                FlowTrigger(kind=TriggerKind.STOP_REQUESTED),
            )


class TestStepContinue:
    def test_running_continues(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_CONTINUE,
                step_id="step_a",
                next_steps=frozenset({"step_b", "step_c"}),
                state_output={"progress": 1},
            ),
        )
        assert result.status == FlowRunStatus.RUNNING
        assert result.current_step == "step_b"
        assert result.state == {"progress": 1}
        assert result.finished_at is NO_CHANGE
        assert "step_completed" in _effect_event_names(result)

    def test_empty_next_steps(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_CONTINUE,
                step_id="step_a",
                next_steps=frozenset(),
            ),
        )
        assert result.current_step is None

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PAUSED,
                FlowTrigger(kind=TriggerKind.STEP_CONTINUE, step_id="s"),
            )


class TestStepComplete:
    def test_running_to_completed(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_COMPLETE,
                step_id="final_step",
                state_output={"result": "done"},
            ),
        )
        assert result.status == FlowRunStatus.COMPLETED
        assert result.finished_at == _NOW
        assert result.current_step is None
        assert result.state == {"result": "done"}
        assert "step_completed" in _effect_event_names(result)
        assert "flow_completed" in _effect_event_names(result)
        assert result.effects[-1].kind == EffectKind.EMIT_LIFECYCLE_EVENT

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PENDING,
                FlowTrigger(kind=TriggerKind.STEP_COMPLETE, step_id="s"),
            )


class TestStepFail:
    def test_running_to_failed(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_FAIL,
                step_id="bad_step",
                error_message="something broke",
            ),
        )
        assert result.status == FlowRunStatus.FAILED
        assert result.finished_at == _NOW
        assert result.error_message == "something broke"
        assert result.current_step is None
        assert result.state.get("reason_summary") == "something broke"
        assert "step_failed" in _effect_event_names(result)
        assert "flow_failed" in _effect_event_names(result)

        enrich = [
            e for e in result.effects if e.kind == EffectKind.ENRICH_FAILURE_PAYLOAD
        ]
        assert len(enrich) == 1
        assert enrich[0].step_id == "bad_step"

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.COMPLETED,
                FlowTrigger(kind=TriggerKind.STEP_FAIL, step_id="s", error_message="x"),
            )


class TestStepException:
    def test_running_to_failed_no_lifecycle(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_EXCEPTION,
                step_id="crash_step",
                error_message="unhandled error",
            ),
        )
        assert result.status == FlowRunStatus.FAILED
        assert result.error_message == "unhandled error"
        assert "step_failed" in _effect_event_names(result)
        assert "flow_failed" not in _effect_event_names(result)
        assert "emit_lifecycle_event" not in _effect_kinds(result)
        enrich = [
            e for e in result.effects if e.kind == EffectKind.ENRICH_FAILURE_PAYLOAD
        ]
        assert len(enrich) == 1

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PAUSED,
                FlowTrigger(
                    kind=TriggerKind.STEP_EXCEPTION, step_id="s", error_message="x"
                ),
            )


class TestStepStop:
    def test_running_to_stopped(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_STOP,
                step_id="step_a",
                state_output={"partial": True},
            ),
        )
        assert result.status == FlowRunStatus.STOPPED
        assert result.finished_at == _NOW
        assert result.current_step is None
        assert result.state.get("partial") is True
        assert "step_completed" in _effect_event_names(result)
        assert "flow_stopped" in _effect_event_names(result)

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.COMPLETED,
                FlowTrigger(kind=TriggerKind.STEP_STOP, step_id="s"),
            )


class TestStepPause:
    def test_running_to_paused(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_PAUSE,
                step_id="wait_step",
                state_output={"waiting_for": "approval"},
            ),
        )
        assert result.status == FlowRunStatus.PAUSED
        assert result.current_step == "wait_step"
        assert result.finished_at is NO_CHANGE
        assert result.state.get("waiting_for") == "approval"
        assert "step_completed" in _effect_event_names(result)
        assert "flow_paused" in _effect_event_names(result)

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.STOPPED,
                FlowTrigger(kind=TriggerKind.STEP_PAUSE, step_id="s"),
            )


class TestFlowException:
    def test_any_status_to_failed(self):
        for status in FlowRunStatus:
            result = _reduce(
                status,
                FlowTrigger(
                    kind=TriggerKind.FLOW_EXCEPTION,
                    error_message="boom",
                ),
            )
            assert result.status == FlowRunStatus.FAILED
            assert result.error_message == "boom"
            assert result.finished_at == _NOW
            assert result.current_step is None

    def test_includes_all_effects(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.FLOW_EXCEPTION,
                step_id="s",
                error_message="err",
            ),
        )
        assert "flow_failed" in _effect_event_names(result)
        assert "emit_lifecycle_event" in _effect_kinds(result)
        enrich = [
            e for e in result.effects if e.kind == EffectKind.ENRICH_FAILURE_PAYLOAD
        ]
        assert len(enrich) == 1


class TestReasonSummary:
    def test_failure_adds_reason_from_error(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_FAIL,
                step_id="s",
                error_message="timeout exceeded",
            ),
        )
        assert result.state["reason_summary"] == "timeout exceeded"

    def test_stopped_adds_default_reason(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.STOP_REQUESTED),
        )
        assert result.state["reason_summary"] == "Stopped by user"

    def test_preserves_existing_reason(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.STOP_REQUESTED),
            state={"reason_summary": "User clicked stop"},
        )
        assert result.state["reason_summary"] == "User clicked stop"


class TestStateOutput:
    def test_step_triggers_merge_output(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_CONTINUE,
                step_id="s",
                next_steps=frozenset({"s2"}),
                state_output={"key": "val"},
            ),
            state={"existing": True},
        )
        assert result.state == {"existing": True, "key": "val"}

    def test_start_trigger_replaces_state(self):
        result = _reduce(
            FlowRunStatus.PENDING,
            FlowTrigger(
                kind=TriggerKind.FLOW_START,
                state_output={"fresh": True},
            ),
            state={"old": True},
        )
        assert result.state == {"fresh": True}

    def test_resume_trigger_replaces_state(self):
        result = _reduce(
            FlowRunStatus.STOPPED,
            FlowTrigger(
                kind=TriggerKind.FLOW_RESUME,
                state_output={"fresh": True},
            ),
            state={"old": True},
        )
        assert result.state == {"fresh": True}


class TestEffectOrdering:
    def test_step_fail_events_before_lifecycle(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.STEP_FAIL,
                step_id="s",
                error_message="err",
            ),
        )
        flow_event_idxs = [
            i
            for i, e in enumerate(result.effects)
            if e.kind == EffectKind.EMIT_FLOW_EVENT
        ]
        lifecycle_idxs = [
            i
            for i, e in enumerate(result.effects)
            if e.kind == EffectKind.EMIT_LIFECYCLE_EVENT
        ]
        assert flow_event_idxs
        assert lifecycle_idxs
        assert max(flow_event_idxs) < min(lifecycle_idxs)

    def test_step_complete_events_before_lifecycle(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.STEP_COMPLETE, step_id="s"),
        )
        flow_event_idxs = [
            i
            for i, e in enumerate(result.effects)
            if e.kind == EffectKind.EMIT_FLOW_EVENT
        ]
        lifecycle_idxs = [
            i
            for i, e in enumerate(result.effects)
            if e.kind == EffectKind.EMIT_LIFECYCLE_EVENT
        ]
        assert flow_event_idxs
        assert lifecycle_idxs
        assert max(flow_event_idxs) < min(lifecycle_idxs)


class TestReconcileEngineCompleted:
    def test_running_to_completed(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_COMPLETED),
        )
        assert result.status == FlowRunStatus.COMPLETED
        assert result.finished_at == _NOW
        assert result.current_step is None
        assert result.note == "engine-completed"

    def test_paused_to_completed(self):
        result = _reduce(
            FlowRunStatus.PAUSED,
            FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_COMPLETED),
        )
        assert result.status == FlowRunStatus.COMPLETED
        assert result.finished_at == _NOW

    def test_rejects_non_running_paused(self):
        for status in (
            FlowRunStatus.PENDING,
            FlowRunStatus.FAILED,
            FlowRunStatus.STOPPED,
        ):
            with pytest.raises(InvalidTransition):
                _reduce(
                    status, FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_COMPLETED)
                )


class TestReconcileWorkerDead:
    def test_running_to_failed(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.RECONCILE_WORKER_DEAD,
                error_message="Worker died (status=dead, pid=12345)",
            ),
        )
        assert result.status == FlowRunStatus.FAILED
        assert result.finished_at == _NOW
        assert result.error_message == "Worker died (status=dead, pid=12345)"
        assert result.current_step is None
        assert result.note == "worker-dead"
        assert (
            result.state.get("reason_summary") == "Worker died (status=dead, pid=12345)"
        )

    def test_includes_failure_enrichment_effect(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(
                kind=TriggerKind.RECONCILE_WORKER_DEAD,
                error_message="worker crashed",
            ),
        )
        enrich = [
            e for e in result.effects if e.kind == EffectKind.ENRICH_FAILURE_PAYLOAD
        ]
        assert len(enrich) == 1
        assert enrich[0].note == "worker_dead"

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PAUSED,
                FlowTrigger(kind=TriggerKind.RECONCILE_WORKER_DEAD, error_message="x"),
            )


class TestReconcileWorkerShutdown:
    def test_running_to_stopped(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.RECONCILE_WORKER_SHUTDOWN),
        )
        assert result.status == FlowRunStatus.STOPPED
        assert result.finished_at == _NOW
        assert result.current_step is None
        assert result.note == "worker-shutdown-intent"
        assert result.state.get("reason_summary") == "Worker stopped"

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PAUSED,
                FlowTrigger(kind=TriggerKind.RECONCILE_WORKER_SHUTDOWN),
            )


class TestReconcileEnginePaused:
    def test_running_to_paused(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_PAUSED),
        )
        assert result.status == FlowRunStatus.PAUSED
        assert result.finished_at is NO_CHANGE
        assert result.note == "engine-paused"

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.COMPLETED,
                FlowTrigger(kind=TriggerKind.RECONCILE_ENGINE_PAUSED),
            )


class TestReconcileStalePauseResume:
    def test_paused_to_running_clears_pause_metadata(self):
        result = _reduce(
            FlowRunStatus.PAUSED,
            FlowTrigger(kind=TriggerKind.RECONCILE_STALE_PAUSE_RESUME),
            state={
                "ticket_engine": {
                    "status": "paused",
                    "reason": "old reason",
                    "reason_details": "details",
                    "reason_code": "stale",
                    "pause_context": {"waiting": True},
                },
                "reason_summary": "Paused",
            },
        )
        assert result.status == FlowRunStatus.RUNNING
        assert result.note == "stale-pause-resumed"
        engine = result.state.get("ticket_engine", {})
        assert engine.get("status") == "running"
        assert "reason" not in engine
        assert "reason_details" not in engine
        assert "reason_code" not in engine
        assert "pause_context" not in engine
        assert "reason_summary" not in result.state

    def test_rejects_non_paused(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.RUNNING,
                FlowTrigger(kind=TriggerKind.RECONCILE_STALE_PAUSE_RESUME),
            )


class TestReconcileStoppingFinalize:
    def test_stopping_to_stopped(self):
        result = _reduce(
            FlowRunStatus.STOPPING,
            FlowTrigger(kind=TriggerKind.RECONCILE_STOPPING_FINALIZE),
        )
        assert result.status == FlowRunStatus.STOPPED
        assert result.finished_at == _NOW
        assert result.current_step is None
        assert result.note == "worker-dead"
        assert result.state.get("reason_summary") == "Worker stopped"

    def test_rejects_non_stopping(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.RUNNING,
                FlowTrigger(kind=TriggerKind.RECONCILE_STOPPING_FINALIZE),
            )


class TestReconcileClearStaleError:
    def test_clears_error_message(self):
        result = _reduce(
            FlowRunStatus.RUNNING,
            FlowTrigger(kind=TriggerKind.RECONCILE_CLEAR_STALE_ERROR),
        )
        assert result.status == FlowRunStatus.RUNNING
        assert result.error_message is None
        assert result.finished_at is NO_CHANGE
        assert result.note == "clear-stale-error"

    def test_rejects_non_running(self):
        with pytest.raises(InvalidTransition):
            _reduce(
                FlowRunStatus.PAUSED,
                FlowTrigger(kind=TriggerKind.RECONCILE_CLEAR_STALE_ERROR),
            )


def _rec(
    status: FlowRunStatus,
    state: dict | None = None,
    error_message: str | None = None,
) -> FlowRunRecord:
    return FlowRunRecord(
        id="run-1",
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state=state or {},
        created_at="2024-01-01T00:00:00Z",
        error_message=error_message,
    )


def _health(alive: bool, **kwargs) -> SimpleNamespace:
    defaults = {
        "is_alive": alive,
        "status": "alive" if alive else "dead",
        "artifact_path": None,
        "pid": 12345 if not alive else None,
        "message": "worker PID not running" if not alive else None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestResolveReconcileTrigger:
    def test_running_engine_completed(self):
        rec = _rec(FlowRunStatus.RUNNING, {"ticket_engine": {"status": "completed"}})
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_ENGINE_COMPLETED

    def test_running_dead_worker(self):
        rec = _rec(FlowRunStatus.RUNNING, {"ticket_engine": {"status": "running"}})
        trigger = resolve_reconcile_trigger(rec, _health(False))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_WORKER_DEAD
        assert "Worker died" in (trigger.error_message or "")

    def test_running_dead_shutdown_intent(self):
        rec = _rec(FlowRunStatus.RUNNING, {"ticket_engine": {"status": "running"}})
        trigger = resolve_reconcile_trigger(rec, _health(False, shutdown_intent=True))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_WORKER_SHUTDOWN

    def test_running_engine_paused(self):
        rec = _rec(FlowRunStatus.RUNNING, {"ticket_engine": {"status": "paused"}})
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_ENGINE_PAUSED

    def test_running_alive_noop(self):
        rec = _rec(FlowRunStatus.RUNNING, {"ticket_engine": {"status": "running"}})
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is None

    def test_running_alive_stale_error(self):
        rec = _rec(
            FlowRunStatus.RUNNING,
            {"ticket_engine": {"status": "running"}},
            error_message="old error",
        )
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_CLEAR_STALE_ERROR

    def test_stopping_dead_worker(self):
        rec = _rec(FlowRunStatus.STOPPING, {"ticket_engine": {"status": "running"}})
        trigger = resolve_reconcile_trigger(rec, _health(False))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_STOPPING_FINALIZE

    def test_paused_engine_completed(self):
        rec = _rec(FlowRunStatus.PAUSED, {"ticket_engine": {"status": "completed"}})
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_ENGINE_COMPLETED

    def test_paused_stale_resume(self):
        rec = _rec(FlowRunStatus.PAUSED, {"ticket_engine": {"status": "running"}})
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_STALE_PAUSE_RESUME

    def test_paused_stale_resume_none_engine(self):
        rec = _rec(FlowRunStatus.PAUSED, {"ticket_engine": {}})
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is not None
        assert trigger.kind == TriggerKind.RECONCILE_STALE_PAUSE_RESUME

    def test_paused_user_pause_sticky(self):
        rec = _rec(
            FlowRunStatus.PAUSED,
            {"ticket_engine": {"status": "running", "reason_code": "user_pause"}},
        )
        trigger = resolve_reconcile_trigger(rec, _health(True))
        assert trigger is None

    def test_paused_dead_worker_noop(self):
        rec = _rec(
            FlowRunStatus.PAUSED,
            {"ticket_engine": {"status": "paused", "reason_code": "user_pause"}},
        )
        trigger = resolve_reconcile_trigger(rec, _health(False))
        assert trigger is None

    def test_terminal_returns_none(self):
        for status in (
            FlowRunStatus.COMPLETED,
            FlowRunStatus.FAILED,
            FlowRunStatus.STOPPED,
        ):
            rec = _rec(status, {"ticket_engine": {"status": "running"}})
            trigger = resolve_reconcile_trigger(rec, _health(True))
            assert trigger is None
