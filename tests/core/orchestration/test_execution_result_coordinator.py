from __future__ import annotations

from dataclasses import replace

from codex_autorunner.core.orchestration.execution_result_coordinator import (
    ExecutionResultCoordinator,
    build_terminal_transition,
)
from codex_autorunner.core.orchestration.models import ExecutionRecord, ThreadTarget


def test_record_execution_result_persists_and_notifies_terminal_transition() -> None:
    thread = ThreadTarget(
        thread_target_id="thread-1",
        agent_id="codex",
        repo_id="repo-1",
        resource_kind="ticket",
        resource_id="TICKET-005",
    )
    execution = ExecutionRecord(
        execution_id="exec-1",
        target_id="thread-1",
        target_kind="thread",
        status="running",
    )
    finished_calls: list[dict[str, object]] = []
    transition_payloads: list[dict[str, object]] = []

    def mark_turn_finished(execution_id: str, **kwargs: object) -> bool:
        nonlocal execution
        finished_calls.append({"execution_id": execution_id, **kwargs})
        execution = replace(execution, status=str(kwargs["status"]))
        return True

    result = ExecutionResultCoordinator(
        get_execution=lambda _thread_id, _execution_id: execution,
        get_thread_target=lambda _thread_id: thread,
        mark_turn_finished=mark_turn_finished,
        mark_turn_interrupted=lambda _execution_id: True,
        notify_transition=lambda payload: transition_payloads.append(payload)
        or {"created": 1},
    ).record_execution_result(
        "thread-1",
        "exec-1",
        status="ok",
        assistant_text="done",
        backend_turn_id="backend-turn-1",
    )

    assert result.status == "ok"
    assert finished_calls == [
        {
            "execution_id": "exec-1",
            "status": "ok",
            "assistant_text": "done",
            "error": None,
            "backend_turn_id": "backend-turn-1",
            "transcript_turn_id": None,
        }
    ]
    assert transition_payloads[0]["to_state"] == "completed"
    assert transition_payloads[0]["event_type"] == "managed_thread_completed"
    assert transition_payloads[0]["repo_id"] == "repo-1"
    assert transition_payloads[0]["resource_kind"] == "ticket"
    assert transition_payloads[0]["resource_id"] == "TICKET-005"
    assert transition_payloads[0]["agent"] == "codex"


def test_record_execution_interrupted_is_idempotent_without_duplicate_notify() -> None:
    execution = ExecutionRecord(
        execution_id="exec-1",
        target_id="thread-1",
        target_kind="thread",
        status="running",
    )
    interrupted_results = [True, False]
    transition_payloads: list[dict[str, object]] = []

    def mark_turn_interrupted(_execution_id: str) -> bool:
        nonlocal execution
        updated = interrupted_results.pop(0)
        execution = replace(execution, status="interrupted")
        return updated

    coordinator = ExecutionResultCoordinator(
        get_execution=lambda _thread_id, _execution_id: execution,
        get_thread_target=lambda _thread_id: None,
        mark_turn_finished=lambda _execution_id, **_kwargs: True,
        mark_turn_interrupted=mark_turn_interrupted,
        notify_transition=lambda payload: transition_payloads.append(payload)
        or {"created": 1},
    )

    first = coordinator.record_execution_interrupted("thread-1", "exec-1")
    second = coordinator.record_execution_interrupted("thread-1", "exec-1")

    assert first.status == "interrupted"
    assert second.status == "interrupted"
    assert len(transition_payloads) == 1
    assert transition_payloads[0]["to_state"] == "interrupted"
    assert transition_payloads[0]["event_type"] == "managed_thread_interrupted"


def test_terminal_transition_maps_unknown_status_to_failed() -> None:
    transition = build_terminal_transition(
        thread_target_id="thread-1",
        execution_id="exec-1",
        status="error",
        error="failed while running",
    )

    assert transition.to_state == "failed"
    assert transition.reason == "failed while running"
    assert transition.event_type == "managed_thread_failed"
