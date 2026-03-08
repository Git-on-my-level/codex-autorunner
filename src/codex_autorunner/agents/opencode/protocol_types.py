from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class TextPart:
    """Text content part from OpenCode message."""

    text: str
    message_id: Optional[str] = None
    role: Optional[str] = None


@dataclass(frozen=True)
class UsagePart:
    """Usage statistics part from OpenCode."""

    usage: dict[str, Any]
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    total_tokens: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    context_window: Optional[int] = None


@dataclass(frozen=True)
class PermissionRequest:
    """Permission request from OpenCode."""

    id: str
    permission: str
    reason: Optional[str] = None


@dataclass(frozen=True)
class QuestionRequest:
    """Question request from OpenCode."""

    id: str
    question: str
    context: Optional[list[list[str]]] = None


@dataclass(frozen=True)
class MessageEvent:
    """Parsed message event from OpenCode SSE stream."""

    event_type: str
    message_id: Optional[str] = None
    role: Optional[str] = None
    content: Optional[str] = None
    parts: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class UsageEvent:
    """Parsed usage event from OpenCode SSE stream."""

    event_type: str
    usage: dict[str, Any]
    provider_id: Optional[str] = None
    model_id: Optional[str] = None


@dataclass(frozen=True)
class PermissionEvent:
    """Parsed permission event from OpenCode SSE stream."""

    event_type: str
    permission_id: str
    permission: str
    reason: Optional[str] = None


@dataclass(frozen=True)
class QuestionEvent:
    """Parsed question event from OpenCode SSE stream."""

    event_type: str
    question_id: str
    question: str
    context: Optional[list[list[str]]] = None
