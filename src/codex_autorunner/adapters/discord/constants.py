from __future__ import annotations

DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Discord hard limit for message content.
DISCORD_MAX_MESSAGE_LENGTH = 2000

# Common gateway intents bitflags (https://discord.com/developers/docs/topics/gateway#gateway-intents).
DISCORD_INTENT_GUILDS = 1 << 0
DISCORD_INTENT_GUILD_MESSAGES = 1 << 9
DISCORD_INTENT_MESSAGE_CONTENT = 1 << 15
