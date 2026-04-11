from __future__ import annotations

from types import SimpleNamespace

from tests.acp_lifecycle_corpus import load_acp_lifecycle_corpus

from codex_autorunner.core.orchestration.runtime_turn_terminal_state import (
    RuntimeTurnTerminalStateMachine,
)


def test_runtime_turn_terminal_state_machine_shared_lifecycle_corpus() -> None:
    for case in load_acp_lifecycle_corpus():
        state = RuntimeTurnTerminalStateMachine(
            backend_thread_id="thread-1",
            backend_turn_id="turn-1",
        )
        raw = dict(case["raw"])
        expected = dict(case["expected"])

        state.note_raw_event(raw)

        if expected["assistant_text"]:
            assert state.last_assistant_text == expected["assistant_text"]
        if expected["output_delta"]:
            assert state.last_assistant_text == expected["output_delta"]
        if expected["error_message"]:
            assert state.failure_cause == expected["error_message"]
        if expected["runtime_terminal_status"] is None:
            assert state.terminal_signals == []
            continue
        assert state.terminal_signals
        assert state.terminal_signals[0].source == raw["method"]
        assert state.terminal_signals[0].status == expected["runtime_terminal_status"]


def test_runtime_turn_terminal_state_machine_transport_failed_with_output_stays_error() -> (
    None
):
    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )
    state.note_raw_event({"method": "prompt/delta", "params": {"delta": "partial"}})
    state.note_transport_result(
        SimpleNamespace(
            status="failed",
            assistant_text="partial",
            errors=["permission denied"],
            raw_events=[],
        )
    )

    outcome = state.build_outcome("Managed thread execution failed")

    assert outcome.status == "error"
    assert outcome.assistant_text == ""
    assert outcome.error == "permission denied"
    assert outcome.completion_source == "prompt_return"


def test_runtime_turn_terminal_state_machine_transport_cancelled_with_output_stays_interrupted() -> (
    None
):
    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )
    state.note_raw_event({"method": "prompt/delta", "params": {"delta": "partial"}})
    state.note_transport_result(
        SimpleNamespace(
            status="cancelled",
            assistant_text="partial",
            errors=["request cancelled"],
            raw_events=[],
        )
    )

    outcome = state.build_outcome("Managed thread execution failed")

    assert outcome.status == "interrupted"
    assert outcome.assistant_text == ""
    assert outcome.error == "request cancelled"
    assert outcome.completion_source == "prompt_return"


def test_runtime_turn_terminal_state_machine_builds_compact_checkpoint() -> None:
    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )
    state.note_raw_event(
        {
            "method": "session/update",
            "params": {
                "usage": {"input": 12, "output": 7},
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": [{"type": "text", "text": "hello world"}],
                },
            },
        }
    )
    state.note_raw_event(
        {"method": "turn/completed", "params": {"status": "completed"}}
    )

    checkpoint = state.build_checkpoint(
        execution_id="exec-1",
        thread_target_id="thread-target-1",
        status="running",
        projection_event_cursor=4,
    )

    assert checkpoint.execution_id == "exec-1"
    assert checkpoint.thread_target_id == "thread-target-1"
    assert checkpoint.backend_thread_id == "thread-1"
    assert checkpoint.backend_turn_id == "turn-1"
    assert checkpoint.status == "running"
    assert checkpoint.last_runtime_method == "turn/completed"
    assert checkpoint.token_usage == {"input": 12, "output": 7}
    assert checkpoint.assistant_text_preview == "hello world"
    assert checkpoint.assistant_char_count == len("hello world")
    assert checkpoint.raw_event_count == 2
    assert checkpoint.projection_event_cursor == 4
    assert checkpoint.terminal_signals[0].status == "ok"
