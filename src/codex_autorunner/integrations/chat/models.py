"""Normalized chat-domain models used by adapter-layer components.

This module lives in the adapter layer (`integrations/chat`) and intentionally
contains platform-agnostic event and reference types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass(frozen=True)
class ChatThreadRef:
    """Normalized conversation identity for any chat platform."""

    platform: str
    chat_id: str
    thread_id: Optional[str] = None


@dataclass(frozen=True)
class ChatMessageRef:
    """Reference to a concrete message inside a conversation."""

    thread: ChatThreadRef
    message_id: str


@dataclass(frozen=True)
class ChatInteractionRef:
    """Reference to a user interaction (button, menu click, etc.)."""

    thread: ChatThreadRef
    interaction_id: str


@dataclass(frozen=True)
class ChatAttachment:
    """Normalized attachment metadata attached to an inbound message."""

    kind: str
    file_id: str
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    source_url: Optional[str] = None


@dataclass(frozen=True)
class ChatAction:
    """Adapter-neutral action button descriptor for outbound messages."""

    label: str
    action_id: str
    payload: Optional[str] = None


@dataclass(frozen=True)
class ChatMessageEvent:
    """Inbound message event normalized by a chat adapter."""

    update_id: str
    thread: ChatThreadRef
    message: ChatMessageRef
    from_user_id: Optional[str]
    text: Optional[str]
    is_edited: bool = False
    reply_to: Optional[ChatMessageRef] = None
    attachments: tuple[ChatAttachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ChatInteractionEvent:
    """Inbound interaction event normalized by a chat adapter."""

    update_id: str
    thread: ChatThreadRef
    interaction: ChatInteractionRef
    from_user_id: Optional[str]
    payload: Optional[str] = None
    message: Optional[ChatMessageRef] = None


ChatEvent = Union[ChatMessageEvent, ChatInteractionEvent]
