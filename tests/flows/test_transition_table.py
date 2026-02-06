from __future__ import annotations

from types import SimpleNamespace

import pytest

from codex_autorunner.core.flows.models import FlowRunRecord, FlowRunStatus
from codex_autorunner.core.flows.transition import resolve_flow_transition


def _rec(
    status: FlowRunStatus, state: dict | None = None, finished_at: str | None = None
) -> FlowRunRecord:
    return FlowRunRecord(
        id="run-1",
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state=state or {},
        created_at="2024-01-01T00:00:00Z",
        finished_at=finished_at,
    )


def _health(alive: bool) -> SimpleNamespace:
    return SimpleNamespace(
        is_alive=alive,
        status="alive" if alive else "dead",
        artifact_path=None,
        pid=12345 if not alive else None,
        message="worker PID not running" if not alive else None,
    )


@pytest.mark.parametrize(
    "status, inner_status, alive, expected",
    [
        (FlowRunStatus.RUNNING, "paused", True, FlowRunStatus.PAUSED),
        (FlowRunStatus.RUNNING, "completed", True, FlowRunStatus.COMPLETED),
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
    dec = resolve_flow_transition(
        _rec(status, state), _health(alive), now="2024-01-02T00:00:00Z"
    )
    assert dec.status == expected


def test_user_pause_is_sticky():
    state = {"ticket_engine": {"status": "running", "reason_code": "user_pause"}}
    dec = resolve_flow_transition(
        _rec(FlowRunStatus.PAUSED, state), _health(True), now="2024-01-02T00:00:00Z"
    )
    assert dec.status == FlowRunStatus.PAUSED


def test_finished_at_set_when_completed_from_paused():
    state = {"ticket_engine": {"status": "completed"}}
    dec = resolve_flow_transition(
        _rec(FlowRunStatus.PAUSED, state), _health(True), now="2024-01-02T00:00:00Z"
    )
    assert dec.status == FlowRunStatus.COMPLETED
    assert dec.finished_at == "2024-01-02T00:00:00Z"
