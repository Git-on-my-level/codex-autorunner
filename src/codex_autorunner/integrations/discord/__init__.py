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
from .gateway import (
    DiscordGatewayClient,
    GatewayFrame,
    build_identify_payload,
    calculate_reconnect_backoff,
    parse_gateway_frame,
)
from .rest import DiscordRestClient

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
    "GatewayFrame",
    "build_identify_payload",
    "parse_gateway_frame",
    "calculate_reconnect_backoff",
    "DiscordGatewayClient",
    "DiscordRestClient",
]
