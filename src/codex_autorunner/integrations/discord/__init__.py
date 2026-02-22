"""Discord integration scaffold."""

from .adapter import DiscordChatAdapter, DiscordTextRenderer
from .allowlist import DiscordAllowlist, allowlist_allows
from .chat_state_store import (
    DiscordChatStateStore,
    discord_conversation_key,
    parse_discord_conversation_key,
)
from .command_registry import sync_commands
from .commands import build_application_commands
from .config import (
    DEFAULT_STATE_FILE,
    DiscordBotConfig,
    DiscordBotConfigError,
    DiscordCommandRegistration,
)
from .constants import (
    DISCORD_API_BASE_URL,
    DISCORD_GATEWAY_URL,
    DISCORD_INTENT_GUILD_MESSAGES,
    DISCORD_INTENT_GUILDS,
    DISCORD_INTENT_MESSAGE_CONTENT,
    DISCORD_MAX_MESSAGE_LENGTH,
)
from .doctor import discord_doctor_checks
from .errors import DiscordAPIError, DiscordConfigError, DiscordError
from .gateway import (
    DiscordGatewayClient,
    GatewayFrame,
    build_identify_payload,
    calculate_reconnect_backoff,
    parse_gateway_frame,
)
from .interactions import (
    extract_channel_id,
    extract_command_path_and_options,
    extract_guild_id,
    extract_interaction_id,
    extract_interaction_token,
    extract_user_id,
)
from .outbox import DiscordOutboxManager
from .rest import DiscordRestClient
from .service import DiscordBotService, create_discord_bot_service
from .state import DiscordStateStore, OutboxRecord

__all__ = [
    "DEFAULT_STATE_FILE",
    "DISCORD_API_BASE_URL",
    "DISCORD_GATEWAY_URL",
    "DISCORD_INTENT_GUILDS",
    "DISCORD_INTENT_GUILD_MESSAGES",
    "DISCORD_INTENT_MESSAGE_CONTENT",
    "DISCORD_MAX_MESSAGE_LENGTH",
    "DiscordBotConfigError",
    "DiscordCommandRegistration",
    "DiscordBotConfig",
    "discord_doctor_checks",
    "DiscordAllowlist",
    "allowlist_allows",
    "extract_command_path_and_options",
    "extract_interaction_id",
    "extract_interaction_token",
    "extract_channel_id",
    "extract_guild_id",
    "extract_user_id",
    "build_application_commands",
    "sync_commands",
    "DiscordError",
    "DiscordConfigError",
    "DiscordAPIError",
    "GatewayFrame",
    "build_identify_payload",
    "parse_gateway_frame",
    "calculate_reconnect_backoff",
    "DiscordGatewayClient",
    "OutboxRecord",
    "DiscordStateStore",
    "DiscordChatStateStore",
    "discord_conversation_key",
    "parse_discord_conversation_key",
    "DiscordOutboxManager",
    "DiscordBotService",
    "create_discord_bot_service",
    "DiscordRestClient",
    "DiscordChatAdapter",
    "DiscordTextRenderer",
]
