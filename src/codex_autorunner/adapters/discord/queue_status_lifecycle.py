"""Discord queue-status message lifecycle helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class QueueStatusCleanupPolicy(Enum):
    DELETE_STALE = "delete_stale"
    PRESERVE_COMPONENT_SOURCE = "preserve_component_source"


@dataclass(frozen=True)
class DiscordQueueComponentAction:
    conversation_id: str
    channel_id: str
    queued_user_message_id: str
    component_source_message_id: Optional[str]

    @classmethod
    def build(
        cls,
        *,
        conversation_id: str,
        channel_id: str,
        queued_user_message_id: str,
        component_source_message_id: Optional[str],
    ) -> "DiscordQueueComponentAction":
        return cls(
            conversation_id=str(conversation_id or "").strip(),
            channel_id=str(channel_id or "").strip(),
            queued_user_message_id=str(queued_user_message_id or "").strip(),
            component_source_message_id=(
                str(component_source_message_id or "").strip() or None
            ),
        )
