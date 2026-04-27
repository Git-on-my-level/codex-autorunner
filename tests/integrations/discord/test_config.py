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
            "CAR_DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        },
        include_discord=True,
    )
    assert "CAR_DISCORD_BOT_TOKEN" in overrides
    assert "CAR_DISCORD_APP_ID" in overrides
    assert "CAR_DISCORD_WEBHOOK_URL" in overrides


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


def test_discord_bot_config_media_defaults(tmp_path) -> None:
    cfg = DiscordBotConfig.from_raw(root=tmp_path, raw={"enabled": False})
    assert cfg.media.enabled is True
    assert cfg.media.voice is True
    assert cfg.media.max_voice_bytes == 10 * 1024 * 1024


def test_discord_bot_config_media_overrides(tmp_path) -> None:
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={
            "enabled": False,
            "media": {
                "enabled": False,
                "voice": False,
                "max_voice_bytes": 1234,
            },
        },
    )
    assert cfg.media.enabled is False
    assert cfg.media.voice is False
    assert cfg.media.max_voice_bytes == 1234


def test_discord_bot_config_media_invalid_voice_raises(tmp_path) -> None:
    with pytest.raises(DiscordBotConfigError):
        DiscordBotConfig.from_raw(
            root=tmp_path,
            raw={
                "enabled": False,
                "media": {"voice": "false"},
            },
        )


def test_discord_bot_config_dispatch_defaults(tmp_path) -> None:
    cfg = DiscordBotConfig.from_raw(root=tmp_path, raw={"enabled": False})
    assert cfg.dispatch.handler_timeout_seconds is None
    assert cfg.dispatch.handler_stalled_warning_seconds == 60.0
    assert cfg.dispatch.ack_budget_ms == 2500
    assert cfg.dispatch.max_concurrent_interactions == 4


def test_discord_bot_config_dispatch_overrides(tmp_path) -> None:
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={
            "enabled": False,
            "dispatch": {
                "handler_timeout_seconds": 45,
                "handler_stalled_warning_seconds": 15,
                "ack_budget_ms": 1800,
                "max_concurrent_interactions": 3,
            },
        },
    )
    assert cfg.dispatch.handler_timeout_seconds == 45.0
    assert cfg.dispatch.handler_stalled_warning_seconds == 15.0
    assert cfg.dispatch.ack_budget_ms == 1800
    assert cfg.dispatch.max_concurrent_interactions == 3


def test_discord_bot_config_dispatch_explicit_null_disables_timeout_and_warning(
    tmp_path,
) -> None:
    cfg = DiscordBotConfig.from_raw(
        root=tmp_path,
        raw={
            "enabled": False,
            "dispatch": {
                "handler_timeout_seconds": None,
                "handler_stalled_warning_seconds": None,
                "ack_budget_ms": None,
                "max_concurrent_interactions": None,
            },
        },
    )
    assert cfg.dispatch.handler_timeout_seconds is None
    assert cfg.dispatch.handler_stalled_warning_seconds is None
    assert cfg.dispatch.ack_budget_ms == 2500
    assert cfg.dispatch.max_concurrent_interactions == 4


@pytest.mark.parametrize(
    ("dispatch_overrides", "expected_message"),
    [
        ({"ack_budget_ms": 0}, "discord_bot.dispatch.ack_budget_ms"),
        ({"ack_budget_ms": "2500"}, "discord_bot.dispatch.ack_budget_ms"),
        (
            {"max_concurrent_interactions": 0},
            "discord_bot.dispatch.max_concurrent_interactions",
        ),
        (
            {"max_concurrent_interactions": False},
            "discord_bot.dispatch.max_concurrent_interactions",
        ),
    ],
)
def test_discord_bot_config_dispatch_invalid_new_knobs_raise(
    tmp_path, dispatch_overrides: dict[str, object], expected_message: str
) -> None:
    with pytest.raises(DiscordBotConfigError, match=expected_message):
        DiscordBotConfig.from_raw(
            root=tmp_path,
            raw={
                "enabled": False,
                "dispatch": dispatch_overrides,
            },
        )


def test_discord_bot_config_builds_shared_collaboration_policy(
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
            "allowed_guild_ids": ["guild-1"],
        },
        collaboration_raw={
            "discord": {
                "default_plain_text_trigger": "mentions",
                "destinations": [
                    {
                        "guild_id": "guild-1",
                        "channel_id": "channel-1",
                        "mode": "silent",
                    }
                ],
            }
        },
    )
    assert cfg.collaboration_policy is not None
    assert cfg.collaboration_policy.default_plain_text_trigger == "mentions"
    assert cfg.collaboration_policy.destinations[0].mode == "silent"


def test_discord_bot_config_rejects_invalid_collaboration_policy(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DISCORD_TOKEN", "token")
    monkeypatch.setenv("TEST_DISCORD_APP_ID", "1234567890")
    with pytest.raises(DiscordBotConfigError):
        DiscordBotConfig.from_raw(
            root=tmp_path,
            raw={
                "enabled": True,
                "bot_token_env": "TEST_DISCORD_TOKEN",
                "app_id_env": "TEST_DISCORD_APP_ID",
                "allowed_guild_ids": ["guild-1"],
            },
            collaboration_raw={
                "discord": {
                    "destinations": [
                        {
                            "channel_id": "channel-1",
                            "plain_text_trigger": "sometimes",
                        }
                    ]
                }
            },
        )
