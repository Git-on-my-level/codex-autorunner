from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from ..ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from ._normalizers import normalize_required_text


def serialize_run_event(event: RunEvent) -> dict[str, Any]:
    if isinstance(event, Started):
        event_type = "started"
    elif isinstance(event, OutputDelta):
        event_type = "output_delta"
    elif isinstance(event, ToolCall):
        event_type = "tool_call"
    elif isinstance(event, ToolResult):
        event_type = "tool_result"
    elif isinstance(event, ApprovalRequested):
        event_type = "approval_requested"
    elif isinstance(event, TokenUsage):
        event_type = "token_usage"
    elif isinstance(event, RunNotice):
        event_type = "run_notice"
    elif isinstance(event, Completed):
        event_type = "completed"
    elif isinstance(event, Failed):
        event_type = "failed"
    else:
        raise ValueError(f"Unsupported run event payload: {type(event).__name__}")
    return {
        "event_type": event_type,
        "payload": asdict(event),
    }


def deserialize_run_event(data: Mapping[str, Any]) -> RunEvent:
    event_type = normalize_required_text(
        data.get("event_type"),
        field_name="event_type",
    )
    payload = data.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("payload is required")
    event_payload = dict(payload)
    if event_type == "started":
        return Started(**event_payload)
    if event_type == "output_delta":
        return OutputDelta(**event_payload)
    if event_type == "tool_call":
        return ToolCall(**event_payload)
    if event_type == "tool_result":
        return ToolResult(**event_payload)
    if event_type == "approval_requested":
        return ApprovalRequested(**event_payload)
    if event_type == "token_usage":
        return TokenUsage(**event_payload)
    if event_type == "run_notice":
        return RunNotice(**event_payload)
    if event_type == "completed":
        return Completed(**event_payload)
    if event_type == "failed":
        return Failed(**event_payload)
    raise ValueError(f"Unsupported run event type: {event_type}")


__all__ = [
    "deserialize_run_event",
    "serialize_run_event",
]
