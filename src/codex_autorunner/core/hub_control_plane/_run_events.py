from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from ..ports.run_event import (
    ApprovalRequested,
    Completed,
    Failed,
    Interrupted,
    OutputDelta,
    ProviderRuntimeReported,
    RunEvent,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
    ToolResult,
    UserInputRequested,
)
from ..runtime_identity import RuntimeIdentityStage
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
    elif isinstance(event, UserInputRequested):
        event_type = "user_input_requested"
    elif isinstance(event, TokenUsage):
        event_type = "token_usage"
    elif isinstance(event, ProviderRuntimeReported):
        event_type = "provider_runtime_reported"
    elif isinstance(event, RunNotice):
        event_type = "run_notice"
    elif isinstance(event, Completed):
        event_type = "completed"
    elif isinstance(event, Failed):
        event_type = "failed"
    elif isinstance(event, Interrupted):
        event_type = "interrupted"
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
    if event_type == "user_input_requested":
        questions = event_payload.get("questions")
        if isinstance(questions, list):
            event_payload["questions"] = tuple(
                dict(question) if isinstance(question, Mapping) else question
                for question in questions
            )
        return UserInputRequested(**event_payload)
    if event_type == "token_usage":
        return TokenUsage(**event_payload)
    if event_type == "provider_runtime_reported":
        effective_runtime = event_payload.get("effective_runtime")
        if isinstance(effective_runtime, Mapping):
            event_payload["effective_runtime"] = RuntimeIdentityStage.from_mapping(
                effective_runtime,
                stage="effective",
                field_name="effective_runtime",
            )
        return ProviderRuntimeReported(**event_payload)
    if event_type == "run_notice":
        return RunNotice(**event_payload)
    if event_type == "completed":
        return Completed(**event_payload)
    if event_type == "failed":
        return Failed(**event_payload)
    if event_type == "interrupted":
        return Interrupted(**event_payload)
    raise ValueError(f"Unsupported run event type: {event_type}")


__all__ = [
    "deserialize_run_event",
    "serialize_run_event",
]
