from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .constants import (
    DISCORD_INTENT_GUILD_MESSAGES,
    DISCORD_INTENT_GUILDS,
    DISCORD_INTENT_MESSAGE_CONTENT,
    DISCORD_MAX_MESSAGE_LENGTH,
)
from .overflow import DEFAULT_MESSAGE_OVERFLOW, MESSAGE_OVERFLOW_OPTIONS

DEFAULT_BOT_TOKEN_ENV = "CAR_DISCORD_BOT_TOKEN"
DEFAULT_APP_ID_ENV = "CAR_DISCORD_APP_ID"
DEFAULT_STATE_FILE = ".codex-autorunner/discord_state.sqlite3"
DEFAULT_COMMAND_SCOPE = "guild"
DEFAULT_INTENTS = (
    DISCORD_INTENT_GUILDS
    | DISCORD_INTENT_GUILD_MESSAGES
    | DISCORD_INTENT_MESSAGE_CONTENT
)


class DiscordBotConfigError(Exception):
    """Raised when discord bot config is invalid."""


@dataclass(frozen=True)
class DiscordCommandRegistration:
    enabled: bool
    scope: str
    guild_ids: tuple[str, ...]


@dataclass(frozen=True)
class DiscordBotConfig:
    root: Path
    enabled: bool
    bot_token_env: str
    app_id_env: str
    bot_token: Optional[str]
    application_id: Optional[str]
    allowed_guild_ids: frozenset[str]
    allowed_channel_ids: frozenset[str]
    allowed_user_ids: frozenset[str]
    command_registration: DiscordCommandRegistration
    state_file: Path
    intents: int
    max_message_length: int
    message_overflow: str
    pma_enabled: bool

    @classmethod
    def from_raw(
        cls, *, root: Path, raw: dict[str, Any], pma_enabled: bool = True
    ) -> "DiscordBotConfig":
        cfg: dict[str, Any] = raw if isinstance(raw, dict) else {}
        enabled = bool(cfg.get("enabled", False))
        bot_token_env = str(cfg.get("bot_token_env", DEFAULT_BOT_TOKEN_ENV)).strip()
        app_id_env = str(cfg.get("app_id_env", DEFAULT_APP_ID_ENV)).strip()
        if not bot_token_env:
            raise DiscordBotConfigError("discord_bot.bot_token_env must be non-empty")
        if not app_id_env:
            raise DiscordBotConfigError("discord_bot.app_id_env must be non-empty")

        bot_token = os.environ.get(bot_token_env)
        application_id = os.environ.get(app_id_env)

        registration_raw = cfg.get("command_registration")
        registration_cfg = (
            registration_raw if isinstance(registration_raw, dict) else {}
        )
        scope_raw = (
            str(registration_cfg.get("scope", DEFAULT_COMMAND_SCOPE)).strip().lower()
        )
        if scope_raw not in {"global", "guild"}:
            raise DiscordBotConfigError(
                "discord_bot.command_registration.scope must be 'global' or 'guild'"
            )
        command_registration = DiscordCommandRegistration(
            enabled=bool(registration_cfg.get("enabled", True)),
            scope=scope_raw,
            guild_ids=tuple(_parse_string_ids(registration_cfg.get("guild_ids"))),
        )

        state_file_value = cfg.get("state_file", DEFAULT_STATE_FILE)
        if not isinstance(state_file_value, str) or not state_file_value.strip():
            raise DiscordBotConfigError("discord_bot.state_file must be a string path")

        intents_value = cfg.get("intents", DEFAULT_INTENTS)
        if not isinstance(intents_value, int):
            raise DiscordBotConfigError("discord_bot.intents must be an integer")
        if intents_value < 0:
            raise DiscordBotConfigError("discord_bot.intents must be >= 0")

        max_message_length_value = cfg.get(
            "max_message_length", DISCORD_MAX_MESSAGE_LENGTH
        )
        if not isinstance(max_message_length_value, int):
            raise DiscordBotConfigError(
                "discord_bot.max_message_length must be an integer"
            )
        if max_message_length_value <= 0:
            raise DiscordBotConfigError("discord_bot.max_message_length must be > 0")
        max_message_length = min(max_message_length_value, DISCORD_MAX_MESSAGE_LENGTH)

        message_overflow = str(
            cfg.get("message_overflow", DEFAULT_MESSAGE_OVERFLOW)
        ).strip()
        if message_overflow:
            message_overflow = message_overflow.lower()
        if message_overflow not in MESSAGE_OVERFLOW_OPTIONS:
            message_overflow = DEFAULT_MESSAGE_OVERFLOW

        if enabled:
            if not bot_token:
                raise DiscordBotConfigError(
                    f"Discord bot is enabled but env var {bot_token_env} is unset"
                )
            if not application_id:
                raise DiscordBotConfigError(
                    f"Discord bot is enabled but env var {app_id_env} is unset"
                )

        return cls(
            root=root,
            enabled=enabled,
            bot_token_env=bot_token_env,
            app_id_env=app_id_env,
            bot_token=bot_token,
            application_id=application_id,
            allowed_guild_ids=frozenset(
                _parse_string_ids(cfg.get("allowed_guild_ids"))
            ),
            allowed_channel_ids=frozenset(
                _parse_string_ids(cfg.get("allowed_channel_ids"))
            ),
            allowed_user_ids=frozenset(_parse_string_ids(cfg.get("allowed_user_ids"))),
            command_registration=command_registration,
            state_file=(root / state_file_value).resolve(),
            intents=intents_value,
            max_message_length=max_message_length,
            message_overflow=message_overflow,
            pma_enabled=pma_enabled,
        )


def _parse_string_ids(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    parsed: list[str] = []
    for item in items:
        token = str(item).strip()
        if token:
            parsed.append(token)
    return parsed
