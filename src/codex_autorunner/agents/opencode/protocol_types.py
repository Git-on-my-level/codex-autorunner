from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from codex_autorunner.core.orchestration.runtime_payload_shapes import TokenUsageShape


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

    @property
    def typed_usage(self) -> TokenUsageShape:
        if not self.total_tokens and not self.input_tokens and not self.output_tokens:
            return TokenUsageShape.from_raw(self.usage)
        return TokenUsageShape(
            total_tokens=self.total_tokens,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
            reasoning_tokens=self.reasoning_tokens,
            context_window=self.context_window,
        )


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

    @property
    def typed_usage(self) -> TokenUsageShape:
        return TokenUsageShape.from_raw(self.usage)


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
