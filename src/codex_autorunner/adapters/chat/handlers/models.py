"""Normalized context types for chat-core handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..models import ChatThreadRef


@dataclass(frozen=True)
class ChatContext:
    """Conversation/user context passed to chat-core handlers."""

    thread: ChatThreadRef
    topic_key: str
    user_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
