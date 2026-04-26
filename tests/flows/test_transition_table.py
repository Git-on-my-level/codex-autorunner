"""Reconciliation transition matrix — now exercised through the lifecycle reducer.

These tests validate that ``resolve_reconcile_trigger`` +
``reduce_flow_lifecycle`` produce the same transition semantics that the old
``transition.resolve_flow_transition`` helper provided.  The legacy module has
been removed; this is the single supported path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows.lifecycle_reducer import (
    NO_CHANGE,
    reduce_flow_lifecycle,
    resolve_reconcile_trigger,
)
from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus

_NOW = "2024-01-02T00:00:00Z"


def _rec(
    status: FlowRunStatus,
    state: dict | None = None,
    finished_at: str | None = None,
    error_message: str | None = None,
) -> FlowRunRecord:
    return FlowRunRecord(
        id="run-1",
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state=state or {},
        created_at="2024-01-01T00:00:00Z",
        finished_at=finished_at,
        error_message=error_message,
    )


def _health(alive: bool) -> SimpleNamespace:
    return SimpleNamespace(
        is_alive=alive,
        status="alive" if alive else "dead",
        artifact_path=None,
        pid=12345 if not alive else None,
        message="worker PID not running" if not alive else None,
    )


def _apply(record: FlowRunRecord, health: SimpleNamespace) -> SimpleNamespace:
    trigger = resolve_reconcile_trigger(record, health)
    if trigger is None:
        return SimpleNamespace(
            status=record.status,
            finished_at=record.finished_at,
            state=record.state,
            error_message=record.error_message,
            note="noop",
        )
    result = reduce_flow_lifecycle(
        record.status,
        record.state or {},
        trigger,
        now=_NOW,
        current_step=record.current_step,
    )
    error_msg = result.error_message if result.error_message is not NO_CHANGE else None
    finished = result.finished_at if result.finished_at is not NO_CHANGE else None
    state = result.state if result.state is not NO_CHANGE else record.state
    return SimpleNamespace(
        status=result.status,
        finished_at=finished,
        state=state,
        error_message=error_msg,
        note=result.note,
    )


@pytest.mark.parametrize(
    "status, inner_status, alive, expected",
    [
        (FlowRunStatus.RUNNING, "paused", True, FlowRunStatus.PAUSED),
        (FlowRunStatus.RUNNING, "paused", False, FlowRunStatus.FAILED),
        (FlowRunStatus.RUNNING, "completed", True, FlowRunStatus.COMPLETED),
        (FlowRunStatus.RUNNING, "completed", False, FlowRunStatus.COMPLETED),
        (FlowRunStatus.RUNNING, None, False, FlowRunStatus.FAILED),
        (FlowRunStatus.STOPPING, None, False, FlowRunStatus.STOPPED),
        (FlowRunStatus.PAUSED, "completed", True, FlowRunStatus.COMPLETED),
        (FlowRunStatus.PAUSED, "running", True, FlowRunStatus.RUNNING),
        (FlowRunStatus.PAUSED, None, True, FlowRunStatus.RUNNING),
        (FlowRunStatus.PAUSED, None, False, FlowRunStatus.PAUSED),
    ],
)
def test_transition_matrix(status, inner_status, alive, expected):
    state = {"ticket_engine": {"status": inner_status}}
    dec = _apply(_rec(status, state), _health(alive))
    assert dec.status == expected


def test_user_pause_is_sticky():
    state = {"ticket_engine": {"status": "running", "reason_code": "user_pause"}}
    dec = _apply(_rec(FlowRunStatus.PAUSED, state), _health(True))
    assert dec.status == FlowRunStatus.PAUSED


def test_finished_at_set_when_completed_from_paused():
    state = {"ticket_engine": {"status": "completed"}}
    dec = _apply(_rec(FlowRunStatus.PAUSED, state), _health(True))
    assert dec.status == FlowRunStatus.COMPLETED
    assert dec.finished_at == _NOW


def _health_with_shutdown(alive: bool, shutdown_intent: bool) -> SimpleNamespace:
    return SimpleNamespace(
        is_alive=alive,
        status="alive" if alive else "dead",
        artifact_path=None,
        pid=12345 if not alive else None,
        message="worker PID not running" if not alive else None,
        shutdown_intent=shutdown_intent,
    )


def test_shutdown_intent_transitions_to_stopped_not_failed():
    state = {"ticket_engine": {"status": "running"}}
    dec = _apply(
        _rec(FlowRunStatus.RUNNING, state),
        _health_with_shutdown(alive=False, shutdown_intent=True),
    )
    assert dec.status == FlowRunStatus.STOPPED
    assert dec.note == "worker-shutdown-intent"
    assert dec.finished_at == _NOW


def test_no_shutdown_intent_still_fails():
    state = {"ticket_engine": {"status": "running"}}
    dec = _apply(
        _rec(FlowRunStatus.RUNNING, state),
        _health_with_shutdown(alive=False, shutdown_intent=False),
    )
    assert dec.status == FlowRunStatus.FAILED
    assert dec.note == "worker-dead"
    assert "Worker died" in (dec.error_message or "")


def test_running_alive_with_no_change_returns_none():
    state = {"ticket_engine": {"status": "running"}}
    trigger = resolve_reconcile_trigger(
        _rec(FlowRunStatus.RUNNING, state), _health(True)
    )
    assert trigger is None


def test_paused_alive_with_user_pause_returns_none():
    state = {"ticket_engine": {"status": "running", "reason_code": "user_pause"}}
    trigger = resolve_reconcile_trigger(
        _rec(FlowRunStatus.PAUSED, state), _health(True)
    )
    assert trigger is None
