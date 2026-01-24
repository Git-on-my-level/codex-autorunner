from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Union


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Started:
    timestamp: str
    session_id: str


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
    Completed,
    Failed,
]


__all__ = [
    "RunEvent",
    "Started",
    "OutputDelta",
    "ToolCall",
    "ApprovalRequested",
    "Completed",
    "Failed",
    "now_iso",
]
