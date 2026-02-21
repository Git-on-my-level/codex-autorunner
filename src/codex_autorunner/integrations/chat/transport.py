"""Generic outbound chat transport contract (adapter layer).

This module belongs to `integrations/chat` and defines a platform-agnostic
delivery interface used by chat-core features.
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from .models import ChatAction, ChatInteractionRef, ChatMessageRef, ChatThreadRef


@runtime_checkable
class ChatTransport(Protocol):
    """Outbound delivery contract implemented by platform transports."""

    async def send_text(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        """Send text to a conversation and return a message reference."""

    async def edit_text(
        self,
        message: ChatMessageRef,
        text: str,
        *,
        actions: Sequence[ChatAction] = (),
    ) -> None:
        """Edit a previously-sent message."""

    async def delete_message(self, message: ChatMessageRef) -> None:
        """Delete a previously-sent message."""

    async def send_attachment(
        self,
        thread: ChatThreadRef,
        file_path: str,
        *,
        caption: Optional[str] = None,
        reply_to: Optional[ChatMessageRef] = None,
    ) -> ChatMessageRef:
        """Send an attachment and return a message reference."""

    async def present_actions(
        self,
        thread: ChatThreadRef,
        text: str,
        *,
        actions: Sequence[ChatAction],
        reply_to: Optional[ChatMessageRef] = None,
        parse_mode: Optional[str] = None,
    ) -> ChatMessageRef:
        """Present text plus interactive actions (buttons/menus)."""

    async def ack_interaction(
        self,
        interaction: ChatInteractionRef,
        *,
        text: Optional[str] = None,
    ) -> None:
        """Acknowledge a user interaction event."""
