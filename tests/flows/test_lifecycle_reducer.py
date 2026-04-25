from __future__ import annotations

import pytest

from codex_autorunner.core.flows.lifecycle_reducer import (
    NO_CHANGE,
    EffectKind,
    FlowTrigger,
    InvalidTransition,
    TransitionResult,
    TriggerKind,
    reduce_flow_lifecycle,
)
from codex_autorunner.core.flows.models import FlowRunStatus

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

    def test_empty_state_output_preserves_current(self):
        result = _reduce(
            FlowRunStatus.PENDING,
            FlowTrigger(kind=TriggerKind.FLOW_START),
            state={"existing": True},
        )
        assert result.state == {"existing": True}


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
