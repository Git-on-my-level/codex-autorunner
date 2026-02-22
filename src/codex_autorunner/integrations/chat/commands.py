"""Platform-agnostic command models and lightweight parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_SLASH_COMMAND_RE = re.compile(r"^/([a-z0-9_]{1,32})(?:@([A-Za-z0-9_]{3,64}))?$")


@dataclass(frozen=True)
class ChatCommand:
    """Normalized command token parsed from chat text."""

    name: str
    args: str
    raw: str


def parse_chat_command(
    text: str, *, bot_username: Optional[str] = None
) -> Optional[ChatCommand]:
    """Parse a leading slash command from plain text.

    This parser is platform-agnostic and intentionally limited to plain-text
    command detection; adapters can provide stricter entity-aware parsing.

    TODO: For Discord/Slack readiness, this parser needs enhancement:
    - Discord: Commands come as application interactions (not plain text),
      but message content may contain /commands. Discord uses @botmention
      syntax differently - consider supporting "!/command" prefix used by
      some bots.
    - Slack: Commands come as slash command payloads from the API, but may
      also appear in messages. Slack doesn't use @bot suffix in the same way.
      Consider supporting "!/command" or platform-specific detection.
    - Consider adding a platform parameter to enable platform-specific parsing
      modes while maintaining backward compatibility.
    """

    raw = str(text or "").strip()
    if not raw or not raw.startswith("/"):
        return None
    parts = raw.split(None, 1)
    token = parts[0]
    remainder = parts[1] if len(parts) > 1 else ""
    match = _SLASH_COMMAND_RE.match(token)
    if match is None:
        return None
    name, mention = match.group(1), match.group(2)
    normalized_bot = (bot_username or "").strip().lstrip("@").lower()
    if mention and normalized_bot and mention.lower() != normalized_bot:
        return None
    return ChatCommand(name=name, args=remainder.strip(), raw=raw)
