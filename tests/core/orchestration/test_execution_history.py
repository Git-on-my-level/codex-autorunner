from __future__ import annotations

from codex_autorunner.core.orchestration.execution_history import (
    DEFAULT_EXECUTION_RETENTION_POLICY,
    build_hot_projection_envelope,
    provider_raw_trace_routing,
    route_run_event,
    timeline_hot_family_for_event_type,
    truncate_hot_event_payload,
)
from codex_autorunner.core.ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    ToolResult,
)


def test_output_delta_routing_is_delta_only_and_checkpointed() -> None:
    decision = route_run_event(
        OutputDelta(
            timestamp="2026-04-12T00:00:00Z",
            content="thinking chunk",
            delta_type="assistant_stream",
        )
    )

    assert decision.event_family == "output_delta"
    assert decision.persist_hot_projection is True
    assert decision.capture_cold_trace is True
    assert decision.update_checkpoint is True
    assert decision.hot_payload_contract == "delta_only"


def test_provider_raw_payloads_are_cold_trace_only() -> None:
    decision = provider_raw_trace_routing(policy=DEFAULT_EXECUTION_RETENTION_POLICY)

    assert decision.event_family == "provider_raw"
    assert decision.persist_hot_projection is False
    assert decision.capture_cold_trace is True
    assert decision.update_checkpoint is False
    assert decision.hot_payload_contract == "none"


def test_hot_projection_envelope_records_retention_metadata() -> None:
    envelope = build_hot_projection_envelope(
        event_index=3,
        event_type="tool_call",
        event=ToolCall(
            timestamp="2026-04-12T00:00:01Z",
            tool_name="shell",
            tool_input={"cmd": "pwd"},
        ),
        metadata={"agent": "codex"},
    ).to_payload()

    assert envelope["event_index"] == 3
    assert envelope["event_family"] == "tool_call"
    assert envelope["storage_layer"] == "hot_projection"
    assert envelope["captures_cold_trace"] is True
    assert envelope["updates_checkpoint"] is True
    assert envelope["event"]["tool_name"] == "shell"


def test_terminal_events_use_terminal_summary_contract() -> None:
    decision = route_run_event(
        Completed(
            timestamp="2026-04-12T00:00:02Z",
            final_message="done",
        )
    )

    assert decision.event_family == "terminal"
    assert decision.hot_payload_contract == "terminal_summary"


def test_tool_call_routing_is_structured_event_and_dual_write() -> None:
    decision = route_run_event(
        ToolCall(
            timestamp="2026-04-12T00:00:00Z",
            tool_name="shell",
            tool_input={"cmd": "ls -la"},
        )
    )

    assert decision.event_family == "tool_call"
    assert decision.persist_hot_projection is True
    assert decision.capture_cold_trace is True
    assert decision.update_checkpoint is True
    assert decision.hot_payload_contract == "structured_event"


def test_tool_result_routing_is_structured_event_and_dual_write() -> None:
    decision = route_run_event(
        ToolResult(
            timestamp="2026-04-12T00:00:01Z",
            tool_name="shell",
            status="completed",
            result={"stdout": "file.txt"},
        )
    )

    assert decision.event_family == "tool_result"
    assert decision.persist_hot_projection is True
    assert decision.capture_cold_trace is True
    assert decision.update_checkpoint is True
    assert decision.hot_payload_contract == "structured_event"


def test_run_notice_routing_is_structured_event_and_dual_write() -> None:
    decision = route_run_event(
        RunNotice(
            timestamp="2026-04-12T00:00:01Z",
            kind="thinking",
            message="planning",
        )
    )

    assert decision.event_family == "run_notice"
    assert decision.persist_hot_projection is True
    assert decision.capture_cold_trace is True
    assert decision.update_checkpoint is True
    assert decision.hot_payload_contract == "structured_event"


def test_token_usage_routing_is_structured_event_and_dual_write() -> None:
    decision = route_run_event(
        TokenUsage(
            timestamp="2026-04-12T00:00:01Z",
            usage={"input_tokens": 100, "output_tokens": 50},
        )
    )

    assert decision.event_family == "token_usage"
    assert decision.persist_hot_projection is True
    assert decision.capture_cold_trace is True
    assert decision.update_checkpoint is True
    assert decision.hot_payload_contract == "structured_event"


def test_started_routing_is_run_notice_family() -> None:
    decision = route_run_event(
        Started(
            timestamp="2026-04-12T00:00:00Z",
            session_id="sess-1",
            thread_id="thread-1",
            turn_id="turn-1",
        )
    )

    assert decision.event_family == "run_notice"
    assert decision.hot_payload_contract == "structured_event"


def test_approval_requested_routing_is_run_notice_family() -> None:
    decision = route_run_event(
        ApprovalRequested(
            timestamp="2026-04-12T00:00:00Z",
            request_id="req-1",
            description="allow file write",
            context={"path": "/tmp/out.txt"},
        )
    )

    assert decision.event_family == "run_notice"
    assert decision.hot_payload_contract == "structured_event"


def test_failed_routing_is_terminal_family() -> None:
    decision = route_run_event(
        Failed(
            timestamp="2026-04-12T00:00:05Z",
            error_message="connection refused",
        )
    )

    assert decision.event_family == "terminal"
    assert decision.hot_payload_contract == "terminal_summary"
    assert decision.persist_hot_projection is True
    assert decision.capture_cold_trace is True


def test_timeline_hot_family_for_event_type_maps_turn_events() -> None:
    assert timeline_hot_family_for_event_type("turn_started") == "run_notice"
    assert timeline_hot_family_for_event_type("tool_call") == "tool_call"
    assert timeline_hot_family_for_event_type("turn_completed") == "terminal"
    assert timeline_hot_family_for_event_type("unknown") is None


def test_truncate_hot_event_small_tool_call_keeps_full_payload() -> None:
    event = ToolCall(
        timestamp="2026-04-12T00:00:00Z",
        tool_name="shell",
        tool_input={"cmd": "pwd"},
    )
    bounded = truncate_hot_event_payload(event, "structured_event")

    assert bounded["tool_name"] == "shell"
    assert bounded["tool_input"] == {"cmd": "pwd"}
    assert "tool_input_truncated" not in bounded


def test_truncate_hot_event_large_tool_input_is_replaced_with_preview() -> None:
    large_input = {"cmd": "x" * 5000}
    event = ToolCall(
        timestamp="2026-04-12T00:00:00Z",
        tool_name="shell",
        tool_input=large_input,
    )
    bounded = truncate_hot_event_payload(event, "structured_event")

    assert bounded["tool_name"] == "shell"
    assert "tool_input" not in bounded
    assert "tool_input_preview" in bounded
    assert bounded["tool_input_truncated"] is True
    assert bounded["details_in_cold_trace"] is True


def test_truncate_hot_event_large_tool_result_is_replaced_with_preview() -> None:
    large_result = {"stdout": "x" * 5000}
    event = ToolResult(
        timestamp="2026-04-12T00:00:00Z",
        tool_name="shell",
        status="completed",
        result=large_result,
    )
    bounded = truncate_hot_event_payload(event, "structured_event")

    assert bounded["tool_name"] == "shell"
    assert bounded["status"] == "completed"
    assert "result" not in bounded
    assert "result_preview" in bounded
    assert bounded["result_truncated"] is True
    assert bounded["details_in_cold_trace"] is True


def test_truncate_hot_event_small_tool_result_keeps_full_payload() -> None:
    event = ToolResult(
        timestamp="2026-04-12T00:00:00Z",
        tool_name="shell",
        status="completed",
        result={"stdout": "/home/user"},
    )
    bounded = truncate_hot_event_payload(event, "structured_event")

    assert bounded["result"] == {"stdout": "/home/user"}
    assert "result_truncated" not in bounded


def test_truncate_hot_event_terminal_summary_bounded_preview() -> None:
    long_message = "x" * 500
    event = Completed(
        timestamp="2026-04-12T00:00:05Z",
        final_message=long_message,
    )
    bounded = truncate_hot_event_payload(event, "terminal_summary")

    assert len(bounded["final_message"]) <= 240
    assert bounded["final_message_truncated"] is True
    assert bounded["final_message_chars"] == 500
    assert "timestamp" in bounded


def test_truncate_hot_event_terminal_summary_short_message_kept_intact() -> None:
    event = Completed(
        timestamp="2026-04-12T00:00:05Z",
        final_message="done",
    )
    bounded = truncate_hot_event_payload(event, "terminal_summary")

    assert bounded["final_message"] == "done"
    assert "final_message_truncated" not in bounded


def test_truncate_hot_event_failed_terminal_summary() -> None:
    long_error = "e" * 500
    event = Failed(
        timestamp="2026-04-12T00:00:05Z",
        error_message=long_error,
    )
    bounded = truncate_hot_event_payload(event, "terminal_summary")

    assert len(bounded["error_message"]) <= 240
    assert bounded["error_message_truncated"] is True
    assert bounded["error_message_chars"] == 500


def test_truncate_hot_event_delta_only_includes_char_count() -> None:
    event = OutputDelta(
        timestamp="2026-04-12T00:00:00Z",
        content="hello world",
        delta_type="assistant_stream",
    )
    bounded = truncate_hot_event_payload(event, "delta_only")

    assert bounded["content"] == "hello world"
    assert bounded["content_chars"] == 11
    assert bounded["delta_type"] == "assistant_stream"


def test_truncate_hot_event_large_delta_only_marks_cold_trace_detail() -> None:
    event = OutputDelta(
        timestamp="2026-04-12T00:00:00Z",
        content="x" * 800,
        delta_type="assistant_stream",
    )
    bounded = truncate_hot_event_payload(event, "delta_only")

    assert len(bounded["content"]) == 512
    assert bounded["content_chars"] == 800
    assert bounded["content_truncated"] is True
    assert bounded["details_in_cold_trace"] is True


def test_truncate_hot_event_none_contract_returns_empty() -> None:
    event = ToolCall(
        timestamp="2026-04-12T00:00:00Z",
        tool_name="shell",
        tool_input={"cmd": "pwd"},
    )
    bounded = truncate_hot_event_payload(event, "none")

    assert bounded == {}


def test_truncate_hot_event_large_run_notice_data_is_truncated() -> None:
    large_data = {f"key_{i}": f"val_{i}" for i in range(200)}
    event = RunNotice(
        timestamp="2026-04-12T00:00:00Z",
        kind="progress",
        message="working",
        data=large_data,
    )
    bounded = truncate_hot_event_payload(event, "structured_event")

    assert bounded["kind"] == "progress"
    assert "data" not in bounded
    assert "data_preview" in bounded
    assert bounded["data_truncated"] is True
    assert bounded["details_in_cold_trace"] is True


def test_truncate_hot_event_large_run_notice_message_is_truncated() -> None:
    event = RunNotice(
        timestamp="2026-04-12T00:00:00Z",
        kind="progress",
        message="x" * 900,
    )
    bounded = truncate_hot_event_payload(event, "structured_event")

    assert len(bounded["message"]) == 512
    assert bounded["message_chars"] == 900
    assert bounded["message_truncated"] is True
    assert bounded["details_in_cold_trace"] is True


def test_truncate_hot_event_envelope_uses_bounded_payload_for_large_tool_input() -> (
    None
):
    envelope = build_hot_projection_envelope(
        event_index=1,
        event_type="tool_call",
        event=ToolCall(
            timestamp="2026-04-12T00:00:00Z",
            tool_name="shell",
            tool_input={"cmd": "x" * 5000},
        ),
    ).to_payload()

    assert envelope["event"]["tool_name"] == "shell"
    assert "tool_input" not in envelope["event"]
    assert "tool_input_preview" in envelope["event"]
    assert envelope["hot_payload_contract"] == "structured_event"
