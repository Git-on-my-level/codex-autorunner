"""Adapter contract between chat-core orchestration and platform transports.

This module belongs to `integrations/chat` (adapter layer) and is intentionally
protocol-only so platform packages implement the behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence, runtime_checkable

from .capabilities import ChatCapabilities
from .models import (
    ChatAction,
    ChatEvent,
    ChatInteractionRef,
    ChatMessageRef,
    ChatThreadRef,
)
from .renderer import TextRenderer


@dataclass(frozen=True)
class SendTextRequest:
    """Normalized payload for outbound text delivery."""

    thread: ChatThreadRef
    text: str
    reply_to: Optional[ChatMessageRef] = None
    actions: tuple[ChatAction, ...] = field(default_factory=tuple)
    parse_mode: Optional[str] = None


@dataclass(frozen=True)
class SendAttachmentRequest:
    """Normalized payload for outbound attachment delivery."""

    thread: ChatThreadRef
    file_path: str
    caption: Optional[str] = None
    reply_to: Optional[ChatMessageRef] = None


@runtime_checkable
class ChatAdapter(Protocol):
    """Protocol implemented by each platform adapter (Telegram, Discord, etc.)."""

    @property
    def platform(self) -> str:
        """Stable platform id (for example: `telegram`)."""

    @property
    def capabilities(self) -> ChatCapabilities:
        """Advertised limits and feature flags for the active platform."""

    @property
    def renderer(self) -> TextRenderer:
        """Renderer used by transports/core for platform-specific formatting."""

    async def poll_events(
        self, *, timeout_seconds: float = 30.0
    ) -> Sequence[ChatEvent]:
        """Poll and normalize inbound events from the platform."""

    async def send_text(self, request: SendTextRequest) -> ChatMessageRef:
        """Deliver text to the platform and return a normalized message ref."""

    async def edit_text(
        self,
        message: ChatMessageRef,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
    ) -> None:
        """Edit an existing message in-place when supported by the platform."""

    async def delete_message(self, message: ChatMessageRef) -> None:
        """Delete a previously sent message when supported by the platform."""

    async def send_attachment(self, request: SendAttachmentRequest) -> ChatMessageRef:
        """Deliver a file/media attachment and return a normalized message ref."""

    async def ack_interaction(
        self,
        interaction: ChatInteractionRef,
        *,
        text: Optional[str] = None,
    ) -> None:
        """Acknowledge an interaction to clear platform-side loading states."""
