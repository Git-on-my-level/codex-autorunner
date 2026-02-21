"""Discord integration scaffold."""

from .constants import (
    DISCORD_API_BASE_URL,
    DISCORD_GATEWAY_URL,
    DISCORD_INTENT_GUILD_MESSAGES,
    DISCORD_INTENT_GUILDS,
    DISCORD_INTENT_MESSAGE_CONTENT,
    DISCORD_MAX_MESSAGE_LENGTH,
)
from .errors import DiscordAPIError, DiscordConfigError, DiscordError

__all__ = [
    "DISCORD_API_BASE_URL",
    "DISCORD_GATEWAY_URL",
    "DISCORD_INTENT_GUILDS",
    "DISCORD_INTENT_GUILD_MESSAGES",
    "DISCORD_INTENT_MESSAGE_CONTENT",
    "DISCORD_MAX_MESSAGE_LENGTH",
    "DiscordError",
    "DiscordConfigError",
    "DiscordAPIError",
]
