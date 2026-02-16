from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ..time_utils import now_iso

# Canonical backend RunEvent contract:
# - A turn emits Started first.
# - A turn emits OutputDelta/ToolCall/ApprovalRequested/TokenUsage/RunNotice as progress.
# - A turn ends with exactly one terminal event: Completed or Failed.
# - Backend deltas should use the canonical delta_type set below.
RUN_EVENT_DELTA_TYPE_USER_MESSAGE = "user_message"
RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM = "assistant_stream"
RUN_EVENT_DELTA_TYPE_LOG_LINE = "log_line"
RUN_EVENT_DELTA_TYPES = frozenset(
    {
        RUN_EVENT_DELTA_TYPE_USER_MESSAGE,
        RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
        RUN_EVENT_DELTA_TYPE_LOG_LINE,
    }
)


@dataclass(frozen=True)
class Started:
    timestamp: str
    session_id: str
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class OutputDelta:
    timestamp: str
    content: str
    delta_type: str = "text"


@dataclass(frozen=True)
class ToolCall:
    timestamp: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ApprovalRequested:
    timestamp: str
    request_id: str
    description: str
    context: dict[str, Any]


@dataclass(frozen=True)
class TokenUsage:
    timestamp: str
    usage: dict[str, Any]


@dataclass(frozen=True)
class RunNotice:
    timestamp: str
    kind: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Completed:
    timestamp: str
    final_message: str = ""


@dataclass(frozen=True)
class Failed:
    timestamp: str
    error_message: str


RunEvent = Union[
    Started,
    OutputDelta,
    ToolCall,
    ApprovalRequested,
    TokenUsage,
    RunNotice,
    Completed,
    Failed,
]


def is_terminal_run_event(event: RunEvent) -> bool:
    return isinstance(event, (Completed, Failed))


__all__ = [
    "RUN_EVENT_DELTA_TYPE_USER_MESSAGE",
    "RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM",
    "RUN_EVENT_DELTA_TYPE_LOG_LINE",
    "RUN_EVENT_DELTA_TYPES",
    "RunEvent",
    "Started",
    "OutputDelta",
    "ToolCall",
    "ApprovalRequested",
    "TokenUsage",
    "RunNotice",
    "Completed",
    "Failed",
    "is_terminal_run_event",
    "now_iso",
]
