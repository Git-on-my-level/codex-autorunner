from __future__ import annotations

import pytest

from codex_autorunner.core.config import collect_env_overrides
from codex_autorunner.integrations.discord.config import (
    DEFAULT_INTENTS,
    DiscordBotConfig,
    DiscordBotConfigError,
)
from codex_autorunner.integrations.discord.constants import (
    DISCORD_INTENT_GUILD_MESSAGES,
    DISCORD_INTENT_GUILDS,
    DISCORD_INTENT_MESSAGE_CONTENT,
)


def test_discord_bot_config_disabled_allows_missing_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAR_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CAR_DISCORD_APP_ID", raising=False)
    cfg = DiscordBotConfig.from_raw(root=tmp_path, raw={"enabled": False})
    assert cfg.enabled is False
    assert cfg.bot_token is None
    assert cfg.application_id is None


def test_discord_bot_config_enabled_requires_env_vars(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("TEST_DISCORD_APP_ID", raising=False)
    with pytest.raises(DiscordBotConfigError):
        DiscordBotConfig.from_raw(
            root=tmp_path,
            raw={
                "enabled": True,
                "bot_token_env": "TEST_DISCORD_TOKEN",
                "app_id_env": "TEST_DISCORD_APP_ID",
            },
        )


def test_discord_bot_config_coerces_allowlists_to_string_sets(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "1234567890")
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "allowed_guild_ids": [123, "456"],
            "allowed_channel_ids": [987, "654"],
            "allowed_user_ids": [111, "222"],
            "command_registration": {
                "enabled": True,
                "scope": "guild",
                "guild_ids": [123, "456"],
            },
        },
    )
    assert cfg.allowed_guild_ids == frozenset({"123", "456"})
    assert cfg.allowed_channel_ids == frozenset({"987", "654"})
    assert cfg.allowed_user_ids == frozenset({"111", "222"})
    assert cfg.command_registration.guild_ids == ("123", "456")


def test_collect_env_overrides_includes_discord() -> None:
    overrides = collect_env_overrides(
        env={
            "CAR_DISCORD_BOT_TOKEN": "token",
            "CAR_DISCORD_APP_ID": "app-id",
        },
        include_discord=True,
    )
    assert "CAR_DISCORD_BOT_TOKEN" in overrides
    assert "CAR_DISCORD_APP_ID" in overrides


def test_discord_bot_config_upgrades_legacy_intents_without_message_content(
    tmp_path,
) -> None:
    legacy_intents = DISCORD_INTENT_GUILDS | DISCORD_INTENT_GUILD_MESSAGES
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={"enabled": False, "intents": legacy_intents},
    )
    assert cfg.intents == (
        DISCORD_INTENT_GUILDS
        | DISCORD_INTENT_GUILD_MESSAGES
        | DISCORD_INTENT_MESSAGE_CONTENT
    )


def test_discord_bot_config_preserves_non_legacy_intents_value(tmp_path) -> None:
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={"enabled": False, "intents": DISCORD_INTENT_GUILDS},
    )
    assert cfg.intents == DISCORD_INTENT_GUILDS


def test_discord_bot_config_migrates_legacy_intents_to_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "1234567890")
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={
            "enabled": True,
            "bot_token_env": "TEST_DISCORD_TOKEN",
            "app_id_env": "TEST_DISCORD_APP_ID",
            "intents": 513,
        },
    )
    assert cfg.intents == DEFAULT_INTENTS


def test_discord_bot_config_shell_defaults(tmp_path) -> None:
    cfg = DiscordBotConfig.from_raw(root=tmp_path, raw={"enabled": False})
    assert cfg.shell.enabled is True
    assert cfg.shell.timeout_ms == 120000
    assert cfg.shell.max_output_chars == 3800


def test_discord_bot_config_shell_overrides(tmp_path) -> None:
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={
            "enabled": False,
            "shell": {
                "enabled": False,
                "timeout_ms": 42000,
                "max_output_chars": 1234,
            },
        },
    )
    assert cfg.shell.enabled is False
    assert cfg.shell.timeout_ms == 42000
    assert cfg.shell.max_output_chars == 1234


def test_discord_bot_config_shell_invalid_timeout_raises(tmp_path) -> None:
    with pytest.raises(DiscordBotConfigError):
        DiscordBotConfig.from_raw(
            root=tmp_path,
            raw={
                "enabled": False,
                "shell": {"timeout_ms": "abc"},
            },
        )


def test_discord_bot_config_shell_invalid_enabled_raises(tmp_path) -> None:
    with pytest.raises(DiscordBotConfigError):
        DiscordBotConfig.from_raw(
            root=tmp_path,
            raw={
                "enabled": False,
                "shell": {"enabled": "false"},
            },
        )
