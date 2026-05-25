from __future__ import annotations

from types import SimpleNamespace
from typing import Literal, Optional, cast

from tests.acp_lifecycle_corpus import load_acp_lifecycle_corpus

from codex_autorunner.core.orchestration.runtime_state_events import (
    AssistantDelta,
    AssistantMessage,
    FailureSignal,
    ProgressSignal,
    TerminalSignal,
    TokenUsage,
    normalize_runtime_state_events,
)
from codex_autorunner.core.orchestration.runtime_turn_terminal_state import (
    RuntimeThreadTerminalSignal,
    RuntimeTurnTerminalStateMachine,
    TerminalEvidence,
    classify_runtime_status,
    reduce_terminal_evidence,
)


def _terminal_signal(
    status: str,
    *,
    source: str = "turn/completed",
) -> RuntimeThreadTerminalSignal:
    return RuntimeThreadTerminalSignal(
        source=source,
        status=cast(Literal["ok", "error", "interrupted"], status),
        timestamp="2026-01-01T00:00:00Z",
    )


def _evidence(**overrides: object) -> TerminalEvidence:
    values: dict[str, object] = {
        "transport_status": "",
        "transport_errors": (),
        "transport_returned": False,
        "assistant_text": "",
        "failure_cause": None,
        "terminal_signals": (),
        "execution_error_message": "Managed thread execution failed",
    }
    values.update(overrides)
    return TerminalEvidence(
        transport_status=cast(str, values["transport_status"]),
        transport_errors=cast(tuple[str, ...], values["transport_errors"]),
        transport_returned=cast(bool, values["transport_returned"]),
        assistant_text=cast(str, values["assistant_text"]),
        failure_cause=cast(Optional[str], values["failure_cause"]),
        terminal_signals=cast(
            tuple[RuntimeThreadTerminalSignal, ...], values["terminal_signals"]
        ),
        explicit_interrupt=cast(bool, values.get("explicit_interrupt", False)),
        explicit_timeout=cast(bool, values.get("explicit_timeout", False)),
        transport_exception=cast(Optional[str], values.get("transport_exception")),
        execution_error_message=cast(str, values["execution_error_message"]),
    )


def test_terminal_evidence_precedence_combinations() -> None:
    cases = [
        (
            "timeout drops partial text",
            _evidence(
                explicit_timeout=True,
                assistant_text="partial",
                failure_cause="Runtime thread timed out",
            ),
            ("error", "", "Runtime thread timed out", "timeout"),
        ),
        (
            "interrupt beats streamed success",
            _evidence(
                explicit_interrupt=True,
                assistant_text="done",
                terminal_signals=(_terminal_signal("ok"),),
                failure_cause="Runtime thread interrupted",
            ),
            ("interrupted", "", "Runtime thread interrupted", "interrupt"),
        ),
        (
            "durable transport success beats later interrupt request",
            _evidence(
                explicit_interrupt=True,
                transport_returned=True,
                transport_status="ok",
                assistant_text="done",
            ),
            ("ok", "done", None, "prompt_return"),
        ),
        (
            "failed transport without terminal success is error",
            _evidence(
                transport_returned=True,
                transport_status="failed",
                transport_errors=("permission denied",),
                assistant_text="partial",
            ),
            ("error", "", "permission denied", "prompt_return"),
        ),
        (
            "terminal success and text recover failed transport",
            _evidence(
                transport_returned=True,
                transport_status="failed",
                transport_errors=("connection reset",),
                assistant_text="final answer",
                terminal_signals=(_terminal_signal("ok"),),
            ),
            ("ok", "final answer", "connection reset", "reconciled_failure"),
        ),
        (
            "transport exception recovers from terminal success and text",
            _evidence(
                transport_exception="socket closed",
                assistant_text="final answer",
                terminal_signals=(_terminal_signal("ok"),),
            ),
            ("ok", "final answer", None, "reconciled_failure"),
        ),
        (
            "idle status alone cannot synthesize assistant text",
            _evidence(
                terminal_signals=(_terminal_signal("ok", source="session.status"),),
            ),
            ("ok", "", None, "stream_terminal_event"),
        ),
    ]

    for label, evidence, expected in cases:
        decision = reduce_terminal_evidence(evidence)

        assert (
            decision.status,
            decision.assistant_text,
            decision.error,
            decision.completion_source,
        ) == expected, label
        assert decision.evidence_fields(evidence)["terminal_evidence_reason"]


def test_runtime_status_classification_shared_aliases() -> None:
    cases = [
        ("completed", "ok", True, True, "successful_status"),
        ("done", "ok", True, True, "successful_status"),
        ("success", "ok", True, True, "successful_status"),
        ("interrupted", "interrupted", True, False, "interrupted_status"),
        ("cancelled", "interrupted", True, False, "interrupted_status"),
        ("aborted", "interrupted", True, False, "interrupted_status"),
        ("failed", "error", True, False, "failed_status"),
        ("error", "error", True, False, "failed_status"),
        ("running", None, False, False, "active_or_unknown_status"),
    ]

    for (
        source_status,
        normalized_outcome,
        terminal,
        prefers_settle,
        reason,
    ) in cases:
        classification = classify_runtime_status(source_status)

        assert classification.source_status == source_status
        assert classification.normalized_outcome == normalized_outcome
        assert classification.terminal is terminal
        assert classification.prefers_completion_settle is prefers_settle
        assert classification.reason == reason


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
        if expected["output_delta"] and expected.get("message_phase") != "commentary":
            assert state.last_assistant_text == expected["output_delta"]
        if expected.get("message_phase") == "commentary":
            assert state.last_assistant_text == ""
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
    assert checkpoint.token_usage == {
        "totalTokens": 19,
        "inputTokens": 12,
        "outputTokens": 7,
    }
    assert checkpoint.assistant_text_preview == "hello world"
    assert checkpoint.assistant_char_count == len("hello world")
    assert checkpoint.raw_event_count == 2
    assert checkpoint.projection_event_cursor == 4
    assert checkpoint.terminal_signals[0].status == "ok"


def test_runtime_turn_terminal_state_machine_ignores_commentary_stream_for_fallback() -> (
    None
):
    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )

    state.note_raw_event(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "phase": "commentary",
                    "content": [{"type": "text", "text": "draft plan"}],
                }
            },
        }
    )

    assert state.last_assistant_text == ""


def test_runtime_turn_terminal_state_machine_ignores_prompt_output_commentary_for_fallback() -> (
    None
):
    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )

    state.note_raw_event(
        {
            "method": "prompt/output",
            "params": {
                "phase": "commentary",
                "delta": "draft plan",
            },
        }
    )

    assert state.last_assistant_text == ""


def test_runtime_turn_terminal_state_machine_reduces_semantic_events() -> None:
    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )

    state.note_state_event(AssistantDelta(text="hel", source="semantic"))
    state.note_state_event(AssistantDelta(text="hello", source="semantic"))
    state.note_state_event(
        AssistantMessage(text="hello final", source="semantic", message_id="msg-1")
    )
    state.note_state_event(
        TokenUsage(usage={"input": 3, "output": 5}, source="semantic")
    )
    state.note_state_event(FailureSignal(error="late warning", source="semantic"))
    state.note_state_event(
        TerminalSignal(status="ok", source="semantic", final_text="hello final")
    )

    assert state.last_assistant_text == "hello final"
    assert state.token_usage == {"input": 3, "output": 5}
    assert state.failure_cause == "late warning"
    assert len(state.terminal_signals) == 1
    assert state.terminal_signals[0].source == "semantic"
    assert state.terminal_signals[0].status == "ok"


def test_unknown_raw_event_remains_observable_without_terminal_mutation() -> None:
    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )
    raw = {"method": "vendor/unknown", "params": {"text": "ignore me"}}

    assert normalize_runtime_state_events(raw) == []

    state.note_raw_event(raw, timestamp="2026-01-01T00:00:00Z")

    assert state.raw_events == [raw]
    assert state.last_progress_timestamp == "2026-01-01T00:00:00Z"
    assert state.last_runtime_method is None
    assert state.last_assistant_text == ""
    assert state.failure_cause is None
    assert state.token_usage is None
    assert state.terminal_signals == []


def test_retrying_turn_error_is_progress_not_terminal_failure() -> None:
    raw = {
        "method": "turn/error",
        "params": {"message": "Transport lost; retrying", "willRetry": True},
    }

    events = normalize_runtime_state_events(raw)

    assert events
    assert any(
        isinstance(event, ProgressSignal) and event.kind == "runtime_retry"
        for event in events
    )
    assert not any(isinstance(event, FailureSignal) for event in events)
    assert not any(isinstance(event, TerminalSignal) for event in events)

    state = RuntimeTurnTerminalStateMachine(
        backend_thread_id="thread-1",
        backend_turn_id="turn-1",
    )
    state.note_raw_event(raw, timestamp="2026-01-01T00:00:00Z")

    assert state.raw_events == [raw]
    assert state.failure_cause is None
    assert state.terminal_signals == []
    assert not state.terminal_signal_waiter().is_set()


def test_reconnecting_turn_error_without_retry_flag_is_terminal_failure() -> None:
    raw = {
        "method": "turn/error",
        "params": {"message": "Reconnecting... 5/5", "willRetry": False},
    }

    events = normalize_runtime_state_events(raw)

    assert any(isinstance(event, FailureSignal) for event in events)
    assert any(isinstance(event, TerminalSignal) for event in events)
