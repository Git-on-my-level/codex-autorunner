from __future__ import annotations

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
