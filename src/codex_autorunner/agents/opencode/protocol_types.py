from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from codex_autorunner.core.orchestration.runtime_payload_shapes import TokenUsageShape


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
