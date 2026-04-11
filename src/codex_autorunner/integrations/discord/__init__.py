"""Discord integration: gateway, ingress, command runner, and turn execution."""

from __future__ import annotations

from importlib import import_module

_LAZY_EXPORTS = {
    "DEFAULT_STATE_FILE": (".config", "DEFAULT_STATE_FILE"),
    "DISCORD_API_BASE_URL": (".constants", "DISCORD_API_BASE_URL"),
    "DISCORD_GATEWAY_URL": (".constants", "DISCORD_GATEWAY_URL"),
    "DISCORD_INTENT_GUILDS": (".constants", "DISCORD_INTENT_GUILDS"),
    "DISCORD_INTENT_GUILD_MESSAGES": (".constants", "DISCORD_INTENT_GUILD_MESSAGES"),
    "DISCORD_INTENT_MESSAGE_CONTENT": (
        ".constants",
        "DISCORD_INTENT_MESSAGE_CONTENT",
    ),
    "DISCORD_MAX_MESSAGE_LENGTH": (".constants", "DISCORD_MAX_MESSAGE_LENGTH"),
    "DiscordAPIError": (".errors", "DiscordAPIError"),
    "DiscordAllowlist": (".allowlist", "DiscordAllowlist"),
    "DiscordBotConfig": (".config", "DiscordBotConfig"),
    "DiscordBotConfigError": (".config", "DiscordBotConfigError"),
    "DiscordBotMediaConfig": (".config", "DiscordBotMediaConfig"),
    "DiscordBotService": (".service", "DiscordBotService"),
    "DiscordChatAdapter": (".adapter", "DiscordChatAdapter"),
    "DiscordCommandRegistration": (".config", "DiscordCommandRegistration"),
    "DiscordError": (".errors", "DiscordError"),
    "DiscordGatewayClient": (".gateway", "DiscordGatewayClient"),
    "DiscordOutboxManager": (".outbox", "DiscordOutboxManager"),
    "DiscordRestClient": (".rest", "DiscordRestClient"),
    "DiscordStateStore": (".state", "DiscordStateStore"),
    "DiscordTextRenderer": (".adapter", "DiscordTextRenderer"),
    "GatewayFrame": (".gateway", "GatewayFrame"),
    "OutboxRecord": (".state", "OutboxRecord"),
    "allowlist_allows": (".allowlist", "allowlist_allows"),
    "build_application_commands": (".commands", "build_application_commands"),
    "build_identify_payload": (".gateway", "build_identify_payload"),
    "calculate_reconnect_backoff": (".gateway", "calculate_reconnect_backoff"),
    "create_discord_bot_service": (".service", "create_discord_bot_service"),
    "discord_doctor_checks": (".doctor", "discord_doctor_checks"),
    "extract_channel_id": (".interactions", "extract_channel_id"),
    "extract_command_path_and_options": (
        ".interactions",
        "extract_command_path_and_options",
    ),
    "extract_guild_id": (".interactions", "extract_guild_id"),
    "extract_interaction_id": (".interactions", "extract_interaction_id"),
    "extract_interaction_token": (".interactions", "extract_interaction_token"),
    "extract_user_id": (".interactions", "extract_user_id"),
    "parse_gateway_frame": (".gateway", "parse_gateway_frame"),
    "sync_commands": (".command_registry", "sync_commands"),
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    module = import_module(module_name, __name__)
    return getattr(module, attr_name)
