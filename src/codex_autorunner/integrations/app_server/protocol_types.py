from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union


@dataclass(frozen=True)
class OutputDeltaNotification:
    """Notification payload for output delta events."""

    method: str
    content: str
    item_id: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class ReasoningSummaryDeltaNotification:
    """Notification payload for reasoning summary delta events."""

    method: str
    delta: str
    item_id: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class ItemCompletedNotification:
    """Notification payload for item completed events."""

    method: str
    item: dict[str, Any]
    item_id: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class ToolCallNotification:
    """Notification payload for tool call events."""

    method: str
    tool_name: Optional[str] = None
    tool_input: Optional[dict[str, Any]] = None
    item_id: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class TokenUsageNotification:
    """Notification payload for token usage events."""

    method: str
    usage: dict[str, Any]
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class TurnCompletedNotification:
    """Notification payload for turn completed events."""

    method: str
    turn_id: Optional[str] = None
    result: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ErrorNotification:
    """Notification payload for error events."""

    method: str
    message: str
    code: Optional[int] = None
    turn_id: Optional[str] = None


@dataclass(frozen=True)
class ApprovalRequest:
    """Approval request payload."""

    method: str
    approval_type: str
    item_id: Optional[str] = None
    turn_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None


NotificationResult = Union[
    OutputDeltaNotification,
    ReasoningSummaryDeltaNotification,
    ItemCompletedNotification,
    ToolCallNotification,
    TokenUsageNotification,
    TurnCompletedNotification,
    ErrorNotification,
    ApprovalRequest,
    None,
]
