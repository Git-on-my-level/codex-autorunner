from __future__ import annotations

from codex_autorunner.core.orchestration.execution_history import (
    DEFAULT_EXECUTION_RETENTION_POLICY,
    build_hot_projection_envelope,
    provider_raw_trace_routing,
    route_run_event,
)
from codex_autorunner.core.ports.run_event import Completed, OutputDelta, ToolCall


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
