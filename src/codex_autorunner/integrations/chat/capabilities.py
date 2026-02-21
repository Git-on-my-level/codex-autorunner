"""Platform capability declarations for the adapter layer.

This module belongs to `integrations/chat` and defines a stable, typed shape
for describing platform limits and optional feature support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ChatCapabilities:
    """Capabilities surfaced by a concrete chat-platform adapter."""

    max_text_length: Optional[int] = None
    max_caption_length: Optional[int] = None
    max_callback_payload_bytes: Optional[int] = None
    supports_threads: bool = False
    supports_message_edits: bool = True
    supports_message_delete: bool = True
    supports_attachments: bool = True
    supports_interactions: bool = True
    supported_parse_modes: tuple[str, ...] = field(default_factory=tuple)
