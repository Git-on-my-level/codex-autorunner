from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from codex_autorunner.core.flows import FlowStore
from codex_autorunner.core.flows.models import (
    FlowEventType,
    FlowRunRecord,
    FlowRunStatus,
)
from codex_autorunner.core.flows.worker_process import FlowWorkerHealth
from codex_autorunner.integrations.telegram.handlers.commands import (
    flows as flows_module,
)
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands


def _health(tmp_path: Path, status: str = "alive") -> FlowWorkerHealth:
    return FlowWorkerHealth(
        status=status,
        pid=123,
        cmdline=[],
        artifact_path=tmp_path / "artifacts" / "worker.json",
        message=None,
    )


def _record(status: FlowRunStatus, *, state: dict | None = None) -> FlowRunRecord:
    return FlowRunRecord(
        id=str(uuid.uuid4()),
        flow_type="ticket_flow",
        status=status,
        input_data={},
        state=state or {},
        current_step=None,
        stop_requested=False,
        created_at="2026-01-30T00:00:00Z",
        started_at=None,
        finished_at=None,
        error_message=None,
        metadata={},
    )


def test_flow_status_includes_effective_current_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = str(uuid.uuid4())
    store.create_flow_run(run_id, "ticket_flow", {})
    store.update_flow_run_status(run_id, FlowRunStatus.RUNNING)
    store.create_event("e1", run_id, FlowEventType.STEP_STARTED, data={})
    store.create_event(
        "e2", run_id, FlowEventType.STEP_PROGRESS, data={"current_ticket": "TICKET-002"}
    )
    record = store.get_flow_run(run_id)
    assert record is not None

    monkeypatch.setattr(
        flows_module,
        "check_worker_health",
        lambda _root, _run_id: _health(tmp_path),
    )

    handler = FlowCommands()
    lines = handler._format_flow_status_lines(tmp_path, record, store)

    assert any(line == "Current: TICKET-002" for line in lines)
    store.close()


def test_flow_status_includes_reason_summary_and_error(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(
        FlowRunStatus.FAILED,
        state={
            "reason_summary": "agent error",
            "ticket_engine": {"reason": "failed to parse"},
        },
    )
    record.error_message = "Traceback"
    lines = handler._format_flow_status_lines(
        tmp_path, record, store=None, health=_health(tmp_path)
    )

    assert any(line == "Summary: agent error" for line in lines)
    assert any(line == "Reason: failed to parse" for line in lines)
    assert any(line == "Error: Traceback" for line in lines)


def test_flow_status_keyboard_paused(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(FlowRunStatus.PAUSED)
    keyboard = handler._build_flow_status_keyboard(record, health=_health(tmp_path))

    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    texts = [button["text"] for row in rows for button in row]
    assert texts == ["Resume", "Restart", "Archive"]


def test_flow_status_keyboard_dead_worker(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(FlowRunStatus.RUNNING)
    keyboard = handler._build_flow_status_keyboard(
        record, health=_health(tmp_path, status="dead")
    )

    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    texts = [button["text"] for row in rows for button in row]
    assert texts == ["Recover", "Refresh"]


def test_flow_status_keyboard_terminal(tmp_path: Path) -> None:
    handler = FlowCommands()
    record = _record(FlowRunStatus.COMPLETED)
    keyboard = handler._build_flow_status_keyboard(record, health=_health(tmp_path))

    assert keyboard is not None
    rows = keyboard["inline_keyboard"]
    texts = [button["text"] for row in rows for button in row]
    assert texts == ["Restart", "Archive", "Refresh"]
