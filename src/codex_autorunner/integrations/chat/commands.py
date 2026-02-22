"""Platform-agnostic command models and lightweight parsing helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

MIN_COMMAND_NAME_LENGTH = 1
MAX_COMMAND_NAME_LENGTH = 32
MIN_COMMAND_MENTION_LENGTH = 3
MAX_COMMAND_MENTION_LENGTH = 64
_COMMAND_NAME_CHARCLASS = "a-z0-9_"
_COMMAND_MENTION_CHARCLASS = "A-Za-z0-9_"
_SLASH_COMMAND_PATTERN = (
    rf"^/([{_COMMAND_NAME_CHARCLASS}]"
    rf"{{{MIN_COMMAND_NAME_LENGTH},{MAX_COMMAND_NAME_LENGTH}}})"
    rf"(?:@([{_COMMAND_MENTION_CHARCLASS}]"
    rf"{{{MIN_COMMAND_MENTION_LENGTH},{MAX_COMMAND_MENTION_LENGTH}}}))?$"
)
_SLASH_COMMAND_RE = re.compile(_SLASH_COMMAND_PATTERN)


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
    Current non-goals (intentional rejections) include:
    - uppercase command names (for example `/Status`)
    - non-slash-prefixed forms (for example `!/status`)

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
